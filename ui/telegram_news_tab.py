# -*- coding: utf-8 -*-
"""분석창 텔레그램 뉴스 탭.

AnalysisWindow가 상속해 쓰는 화면 조각이다. 상단 전광판과 관심종목
상태를 분석창과 공유하기 때문에 독립 위젯 대신 믹스인으로 분리했다.
"""
from __future__ import annotations

import asyncio
import logging

from PySide6.QtCore import Qt, QTimer, QUrl
from PySide6.QtGui import QColor, QDesktopServices
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QHBoxLayout, QHeaderView, QInputDialog, QLabel,
    QLineEdit, QMessageBox, QPushButton, QTableWidget, QTableWidgetItem,
    QVBoxLayout,
)

from analysis_db import (
    realtime_watch_codes, save_telegram_news, set_realtime_watch,
    telegram_news_rows,
)
from gui import NumericTableWidgetItem
from rank import _beep
from telegram_news import TelegramNewsStream, telegram_app_url
from ui import (
    NEWS_NEW_ROLE, NEWS_NEW_TIME_BACKGROUND, NEWS_NEW_TIME_FOREGROUND,
    NEWS_NEW_TITLE_BACKGROUND, NEWS_NEW_TITLE_FOREGROUND,
)


log = logging.getLogger("telegram_news_tab")
VISIBLE_LIMIT = 500


class TelegramNewsTabMixin:
    """텔레그램 채널 글 표시와 검색, 알림음을 담당한다."""

    def _build_telegram_news_page(self, layout: QVBoxLayout):
        """텔레그램 채널 글을 소급 수집분과 실시간분 모두 최신순으로 보여준다."""
        status_row = QHBoxLayout()
        self._telegram_status = QLabel("● 연결 준비")
        self._telegram_status.setStyleSheet(
            "QLabel { color: #8A94A6; font-weight: 700; }")
        self._telegram_count = QLabel("0건")
        self._telegram_search = QLineEdit()
        self._telegram_search.setPlaceholderText(
            "검색 · 공백으로 나눈 모든 낱말 포함 (채널·종목·본문)")
        self._telegram_search.setClearButtonEnabled(True)
        self._telegram_search.setMinimumWidth(240)
        self._telegram_search_timer = QTimer(self)
        self._telegram_search_timer.setSingleShot(True)
        self._telegram_search_timer.timeout.connect(
            self._reload_telegram_news)
        self._telegram_search.textChanged.connect(
            lambda *_: self._telegram_search_timer.start(400))
        self._telegram_search.returnPressed.connect(
            self._reload_telegram_news)
        self._telegram_sound = QCheckBox("소리")
        self._telegram_sound.setToolTip(
            "체크하면 새 텔레그램 글이 도착할 때 알림음을 재생합니다.\n"
            "종목코드 있음: "
            r"C:\KiwoomHero4\sound\sound10.wav"
            "\n종목코드 없음: "
            r"C:\KiwoomHero4\sound\sound9.wav")
        self._telegram_sound.setChecked(
            str(self._settings.value(
                "analysis_telegram_sound", "false"
            )).strip().lower() in {"1", "true", "yes"}
        )
        self._telegram_sound.toggled.connect(
            lambda checked: self._settings.setValue(
                "analysis_telegram_sound", "true" if checked else "false"))
        self._telegram_stock_only = QCheckBox("종목")
        self._telegram_stock_only.setToolTip(
            "종목코드가 연결된 글만 표시합니다.")
        self._telegram_stock_only.setChecked(
            str(self._settings.value(
                "analysis_telegram_stock_only", "false"
            )).strip().lower() in {"1", "true", "yes"}
        )
        self._telegram_stock_only.toggled.connect(
            self._set_telegram_stock_filter)
        self._telegram_watched_only = QCheckBox("관심종목")
        self._telegram_watched_only.setToolTip(
            "글에 연결된 종목 중 하나라도 관심종목이면 표시합니다.")
        self._telegram_watched_only.setChecked(
            str(self._settings.value(
                "analysis_telegram_watched_only", "false"
            )).strip().lower() in {"1", "true", "yes"}
        )
        self._telegram_watched_only.toggled.connect(
            self._set_telegram_watched_filter)
        status_row.addWidget(self._telegram_status)
        status_row.addWidget(self._telegram_count)
        status_row.addWidget(self._telegram_search, 1)
        status_row.addWidget(self._telegram_sound)
        self._telegram_clear_new_button = QPushButton("신규해제")
        self._telegram_clear_new_button.setEnabled(False)
        self._telegram_clear_new_button.setToolTip(
            "새 텔레그램 글의 강조색만 해제합니다.\n"
            "목록과 DB 저장 데이터는 그대로 유지합니다.")
        self._telegram_clear_new_button.clicked.connect(
            self._clear_telegram_new_markers)
        status_row.addWidget(self._telegram_stock_only)
        status_row.addWidget(self._telegram_watched_only)
        status_row.addWidget(self._telegram_clear_new_button)
        layout.addLayout(status_row)

        self._telegram_table = QTableWidget(0, 5)
        self._telegram_table.setHorizontalHeaderLabels(
            ("번호", "게시 시각", "채널", "종목", "내용"))
        self._telegram_table.setSortingEnabled(False)
        self._telegram_table.setAlternatingRowColors(True)
        self._telegram_table.setWordWrap(False)
        self._telegram_table.setEditTriggers(
            QTableWidget.EditTrigger.NoEditTriggers)
        self._telegram_table.setSelectionBehavior(
            QTableWidget.SelectionBehavior.SelectRows)
        self._telegram_table.setSelectionMode(
            QTableWidget.SelectionMode.SingleSelection)
        self._telegram_table.verticalHeader().setVisible(False)
        self._telegram_table.verticalHeader().setDefaultSectionSize(24)
        header = self._telegram_table.horizontalHeader()
        header.setSectionsMovable(False)
        header.setStretchLastSection(False)
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        saved_header = self._settings.value("analysis_telegram_header_v2")
        if saved_header is None or not header.restoreState(saved_header):
            for column, width in enumerate((48, 130, 130, 130, 660)):
                self._telegram_table.setColumnWidth(column, width)
        self._telegram_header_timer = QTimer(self)
        self._telegram_header_timer.setSingleShot(True)
        self._telegram_header_timer.timeout.connect(
            lambda: self._settings.setValue(
                "analysis_telegram_header_v2", header.saveState()))
        header.sectionResized.connect(
            lambda *_: self._telegram_header_timer.start(400))
        self._telegram_table.cellClicked.connect(
            self._telegram_table_clicked)
        self._telegram_table.setContextMenuPolicy(
            Qt.ContextMenuPolicy.CustomContextMenu)
        self._telegram_table.customContextMenuRequested.connect(
            self._telegram_table_right_clicked)
        layout.addWidget(self._telegram_table, 1)
        self._telegram_seen: set[tuple[str, int]] = set()
        self._telegram_new_count = 0

    def _set_telegram_stock_filter(self, checked: bool):
        self._settings.setValue(
            "analysis_telegram_stock_only", "true" if checked else "false")
        self._reload_telegram_news()

    def _set_telegram_watched_filter(self, checked: bool):
        self._settings.setValue(
            "analysis_telegram_watched_only", "true" if checked else "false")
        self._reload_telegram_news()

    def _set_telegram_status(self, state: str, message: str):
        if not hasattr(self, "_telegram_status"):
            return
        colors = {
            "live": "#4CD07D", "connecting": "#E8C46A",
            "error": "#FF7A7A", "off": "#8A94A6",
        }
        self._telegram_status.setStyleSheet(
            "QLabel { color: %s; font-weight: 700; }"
            % colors.get(state, "#8A94A6"))
        self._telegram_status.setText(f"● {message}")

    async def _telegram_prompt(self, title: str, label: str,
                               password: bool = False) -> str:
        mode = (QLineEdit.EchoMode.Password if password
                else QLineEdit.EchoMode.Normal)
        text, accepted = QInputDialog.getText(
            self, title, label, mode)
        if not accepted:
            raise RuntimeError("텔레그램 로그인이 취소되었습니다.")
        return text.strip()

    def _start_telegram_stream(self):
        if self._telegram_task and not self._telegram_task.done():
            return
        self._telegram_stream = TelegramNewsStream(
            code_callback=lambda: self._telegram_prompt(
                "텔레그램 로그인", "받은 인증번호"),
            password_callback=lambda: self._telegram_prompt(
                "텔레그램 로그인", "2단계 인증 비밀번호", password=True),
            phone_callback=lambda: self._telegram_prompt(
                "텔레그램 로그인", "전화번호 (+8210...)"),
        )
        self._telegram_task = asyncio.ensure_future(
            self._telegram_stream.run(
                self._append_telegram_news, self._set_telegram_status))

    def _load_saved_telegram_news(self):
        self._reload_telegram_news()

    def _reload_telegram_news(self):
        if not hasattr(self, "_telegram_table"):
            return
        try:
            rows = telegram_news_rows(
                VISIBLE_LIMIT,
                stock_only=self._telegram_stock_only.isChecked(),
                watched_only=self._telegram_watched_only.isChecked(),
                query=self._telegram_search.text(),
            )
        except Exception as error:  # noqa: BLE001 - 실시간 수신은 계속한다.
            log.exception("telegram news load failed")
            self._set_telegram_status("error", f"DB 오류: {error}")
            return
        self._telegram_table.setRowCount(0)
        self._telegram_new_count = 0
        for row in reversed(rows):
            self._insert_telegram_row(row, highlight=False)
        self._renumber_telegram_table()
        self._update_telegram_new_button()

    def _append_telegram_news(self, row: dict, is_new: bool):
        """수집 콜백. 저장에 성공한 새 글만 화면과 전광판에 올린다."""
        if not hasattr(self, "_telegram_table"):
            return
        key = (str(row.get("channel") or ""), int(row.get("message_id") or 0))
        if key in self._telegram_seen:
            return
        self._telegram_seen.add(key)
        try:
            inserted = save_telegram_news(row, ensure_schema=False)
        except Exception:  # noqa: BLE001 - 화면 표시는 유지한다.
            log.exception("telegram news DB save failed")
            inserted = True
        if not inserted:
            return
        codes = tuple(row.get("stock_codes") or ())
        if self._telegram_stock_only.isChecked() and not codes:
            return
        if self._telegram_watched_only.isChecked() and not any(
                code in self._ls_news_watched_codes
                for code in row.get("stock_codes") or ()):
            return
        query_words = self._telegram_search.text().split()
        if query_words:
            haystack = " ".join((
                str(row.get("title") or ""), str(row.get("body") or ""),
                str(row.get("stock_names") or ""),
                str(row.get("channel") or ""),
                str(row.get("channel_title") or ""),
            ))
            if not all(word in haystack for word in query_words):
                return
        self._insert_telegram_row(row, highlight=is_new)
        self._renumber_telegram_table()
        if is_new:
            self._show_latest_telegram_news(row)
            if self._telegram_sound.isChecked():
                _beep("telegram_news_with_code" if codes
                      else "telegram_news_without_code")

    def _insert_telegram_row(self, row: dict, highlight: bool):
        table = self._telegram_table
        was_at_top = table.verticalScrollBar().value() <= 1
        table.insertRow(0)
        number_item = NumericTableWidgetItem("1", 1)
        number_item.setTextAlignment(
            Qt.AlignmentFlag.AlignCenter | Qt.AlignmentFlag.AlignVCenter)
        published = str(row.get("published_at") or "")
        time_item = QTableWidgetItem(published[5:16].replace("T", " "))
        time_item.setTextAlignment(
            Qt.AlignmentFlag.AlignCenter | Qt.AlignmentFlag.AlignVCenter)
        time_item.setToolTip(published)
        channel_item = QTableWidgetItem(
            str(row.get("channel_title") or row.get("channel") or ""))
        channel_item.setToolTip(str(row.get("channel") or ""))
        codes = tuple(row.get("stock_codes") or ())
        stock_item = QTableWidgetItem(str(row.get("stock_names") or ""))
        stock_item.setData(Qt.ItemDataRole.UserRole, list(codes))
        stock_item.setToolTip(
            ("종목코드: " + ", ".join(codes)
             + "\n\n좌클릭: 대표 종목코드 복사"
             "\n우클릭: 대표 종목 관심종목 추가 · 종토방 열기")
            if codes else "연결된 종목이 없습니다.")
        body_item = QTableWidgetItem(str(row.get("title") or ""))
        body_item.setToolTip(
            f"{row.get('body') or ''}\n\n클릭: Telegram 앱으로 원문 열기")
        body_item.setData(Qt.ItemDataRole.UserRole, str(row.get("url") or ""))
        for column, item in enumerate((
                number_item, time_item, channel_item, stock_item, body_item)):
            table.setItem(0, column, item)
        if highlight:
            time_item.setData(NEWS_NEW_ROLE, True)
            time_item.setBackground(QColor(NEWS_NEW_TIME_BACKGROUND))
            time_item.setForeground(QColor(NEWS_NEW_TIME_FOREGROUND))
            body_item.setBackground(QColor(NEWS_NEW_TITLE_BACKGROUND))
            body_item.setForeground(QColor(NEWS_NEW_TITLE_FOREGROUND))
            font = body_item.font()
            font.setBold(True)
            body_item.setFont(font)
            self._telegram_new_count += 1
        while table.rowCount() > VISIBLE_LIMIT:
            last_row = table.rowCount() - 1
            if self._telegram_row_is_new(last_row):
                self._telegram_new_count = max(0, self._telegram_new_count - 1)
            table.removeRow(last_row)
        self._update_telegram_new_button()
        if was_at_top:
            table.scrollToTop()

    def _telegram_row_is_new(self, row: int) -> bool:
        time_item = self._telegram_table.item(row, 1)
        return bool(time_item is not None and time_item.data(NEWS_NEW_ROLE))

    def _update_telegram_new_button(self):
        if not hasattr(self, "_telegram_clear_new_button"):
            return
        count = max(0, int(self._telegram_new_count))
        self._telegram_clear_new_button.setText(
            f"신규해제 ({count:,})" if count else "신규해제")
        self._telegram_clear_new_button.setEnabled(count > 0)

    def _clear_telegram_new_markers(self):
        """목록은 유지하고 새 텔레그램 글의 강조 표시만 해제한다."""
        if not hasattr(self, "_telegram_table"):
            return
        cleared = 0
        for row in range(self._telegram_table.rowCount()):
            if not self._telegram_row_is_new(row):
                continue
            cleared += 1
            for column in (1, 4):
                item = self._telegram_table.item(row, column)
                if item is None:
                    continue
                item.setData(Qt.ItemDataRole.BackgroundRole, None)
                item.setData(Qt.ItemDataRole.ForegroundRole, None)
            time_item = self._telegram_table.item(row, 1)
            if time_item is not None:
                time_item.setData(NEWS_NEW_ROLE, None)
            body_item = self._telegram_table.item(row, 4)
            if body_item is not None:
                font = body_item.font()
                font.setBold(False)
                body_item.setFont(font)
        self._telegram_new_count = 0
        self._update_telegram_new_button()
        if cleared:
            self.statusBar().showMessage(
                f"신규 텔레그램 글 강조 {cleared:,}건을 해제했습니다. "
                "목록과 DB는 유지됩니다.",
                3000,
            )

    def _renumber_telegram_table(self):
        table = self._telegram_table
        total = table.rowCount()
        for row in range(total):
            item = table.item(row, 0)
            if item is not None:
                item.setText(str(total - row))
        self._telegram_count.setText(f"{total}건")

    def _show_latest_telegram_news(self, row: dict):
        title = " ".join(str(row.get("title") or "").split())
        if not title:
            return
        codes = tuple(row.get("stock_codes") or ())
        self._latest_ls_news_context = {
            "provider": "TELEGRAM",
            "title": title,
            "stock_code": codes[0] if codes else "",
            "url": str(row.get("url") or ""),
        }
        channel = str(row.get("channel_title") or row.get("channel") or "")
        self._latest_ls_news_label.set_headline(f"[TG] {channel} · {title}")
        self._set_latest_ls_news_highlight(True)
        self._latest_ls_news_highlight_timer.start(3500)

    def _telegram_row_stock_code(self, row: int) -> str:
        item = self._telegram_table.item(row, 3)
        codes = list(item.data(Qt.ItemDataRole.UserRole) or []) if item else []
        return str(codes[0]) if codes else ""

    def _telegram_table_clicked(self, row: int, column: int):
        if column == 3:
            stock_code = self._telegram_row_stock_code(row)
            if stock_code:
                QApplication.clipboard().setText(stock_code)
                self.statusBar().showMessage(
                    f"종목코드 {stock_code}를 복사했습니다.", 3000)
            return
        if column != 4:
            return
        item = self._telegram_table.item(row, 4)
        url = str(item.data(Qt.ItemDataRole.UserRole) or "") if item else ""
        self.open_telegram_post(url)

    def open_telegram_post(self, url: str):
        """Telegram 앱으로 먼저 열고, 앱이 없을 때만 웹으로 넘긴다."""
        url = str(url or "").strip()
        if not url:
            return
        app_url = telegram_app_url(url)
        if app_url and QDesktopServices.openUrl(QUrl(app_url)):
            return
        QDesktopServices.openUrl(QUrl(url))

    def _telegram_table_right_clicked(self, position):
        """종목 열 우클릭 시 대표 종목을 관심종목에 넣고 종토방을 연다."""
        item = self._telegram_table.itemAt(position)
        if item is None or item.column() != 3:
            return
        row = item.row()
        stock_code = self._telegram_row_stock_code(row)
        if not stock_code:
            return
        name_item = self._telegram_table.item(row, 3)
        stock_name = str(name_item.text() if name_item else "").split(",")[0]
        stock_name = stock_name.strip() or stock_code
        try:
            already_registered = stock_code in realtime_watch_codes()
            if not already_registered:
                set_realtime_watch(
                    stock_code, True, "TELEGRAM_NEWS",
                    stock_name="" if stock_name == stock_code else stock_name,
                )
        except ValueError as error:
            QMessageBox.warning(self, "관심종목 추가", str(error))
            return

        if not already_registered:
            self.watchlist_changed.emit()
        self.open_realtime_watch(stock_code, fetch_news=False)
        action = "선택" if already_registered else "추가"
        self.statusBar().showMessage(
            f"{stock_name}({stock_code}) 관심종목 {action} · 종토방 이동", 3000)
