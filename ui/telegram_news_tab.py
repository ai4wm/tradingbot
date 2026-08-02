# -*- coding: utf-8 -*-
"""분석창 텔레그램 뉴스 탭.

AnalysisWindow가 상속해 쓰는 화면 조각이다. 상단 전광판과 관심종목
상태를 분석창과 공유하기 때문에 독립 위젯 대신 믹스인으로 분리했다.
"""
from __future__ import annotations

import asyncio
import logging
import re

from PySide6.QtCore import Qt, QTimer, QUrl
from PySide6.QtGui import (
    QColor, QDesktopServices, QKeySequence, QShortcut)
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QDialog, QHBoxLayout, QHeaderView,
    QInputDialog, QLabel, QLineEdit, QMessageBox, QPushButton, QTableWidget,
    QTableWidgetItem, QTextBrowser, QVBoxLayout, QWidget,
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


_LINK_RE = re.compile(r"https?://[^\s]+")


class TelegramPostDialog(QDialog):
    """텔레그램 원문 상세창. 웹뷰 하나를 재사용하고 닫으면 비운다."""

    def __init__(self, owner):
        super().__init__(owner)
        self._owner = owner
        self._webview = None
        self._url = ""
        self._article_url = ""
        self.setWindowTitle("텔레그램 원문")
        self.setModal(False)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(6, 6, 6, 6)
        self._header = QLabel()
        self._header.setWordWrap(True)
        self._header.setStyleSheet("QLabel { font-weight: 700; }")
        layout.addWidget(self._header)
        self._text = QTextBrowser()
        self._text.setOpenExternalLinks(True)
        self._text.hide()
        layout.addWidget(self._text, 1)
        self._host = QWidget()
        self._host_layout = QVBoxLayout(self._host)
        self._host_layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self._host, 1)

        try:
            self._zoom = float(self._owner._settings.value(
                "telegram_detail_zoom", 1.0))
        except (TypeError, ValueError):
            self._zoom = 1.0
        self._zoom = min(3.0, max(0.5, self._zoom))

        buttons = QHBoxLayout()
        zoom_out = QPushButton("가－")
        zoom_out.setFixedWidth(46)
        zoom_out.setToolTip("글자 작게 (Ctrl+-)")
        zoom_out.clicked.connect(lambda: self._change_zoom(-0.1))
        zoom_in = QPushButton("가＋")
        zoom_in.setFixedWidth(46)
        zoom_in.setToolTip("글자 크게 (Ctrl++)")
        zoom_in.clicked.connect(lambda: self._change_zoom(0.1))
        self._zoom_label = QLabel()
        self._zoom_label.setFixedWidth(48)
        self._zoom_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._zoom_label.setToolTip("클릭하면 100%로 되돌립니다.")
        buttons.addWidget(zoom_out)
        buttons.addWidget(self._zoom_label)
        buttons.addWidget(zoom_in)
        for sequence, delta in (("Ctrl++", 0.1), ("Ctrl+=", 0.1),
                                ("Ctrl+-", -0.1)):
            QShortcut(QKeySequence(sequence), self,
                      activated=lambda d=delta: self._change_zoom(d))
        QShortcut(QKeySequence("Ctrl+0"), self,
                  activated=lambda: self._change_zoom(0, reset=True))

        self._article_button = QPushButton("본문 기사 링크")
        self._article_button.clicked.connect(self._open_article)
        app_button = QPushButton("Telegram 앱으로 열기")
        app_button.clicked.connect(
            lambda: self._owner.open_telegram_post(self._url))
        close_button = QPushButton("닫기")
        close_button.clicked.connect(self.close)
        buttons.addWidget(self._article_button)
        buttons.addStretch(1)
        buttons.addWidget(app_button)
        buttons.addWidget(close_button)
        layout.addLayout(buttons)

        self._apply_zoom()
        geo = self._owner._settings.value("telegram_detail_geo")
        if geo is None or not self.restoreGeometry(geo):
            self.resize(900, 900)

    def _change_zoom(self, delta: float, reset: bool = False):
        self._zoom = 1.0 if reset else min(3.0, max(0.5, self._zoom + delta))
        self._owner._settings.setValue(
            "telegram_detail_zoom", round(self._zoom, 2))
        self._apply_zoom()

    def _apply_zoom(self):
        self._zoom_label.setText(f"{self._zoom * 100:.0f}%")
        if self._webview is not None:
            self._webview.setZoomFactor(self._zoom)
        font = self._text.font()
        font.setPointSizeF(max(6.0, 10.0 * self._zoom))
        self._text.setFont(font)

    def _ensure_webview(self):
        """처음 열 때만 웹뷰를 만든다. 안 쓰면 렌더러도 뜨지 않는다."""
        if self._webview is not None:
            return self._webview
        try:
            from PySide6.QtWebEngineCore import QWebEnginePage
            from PySide6.QtWebEngineWidgets import QWebEngineView
        except ImportError:
            return None
        self._webview = QWebEngineView(self._host)
        # 종토방 웹뷰 프로파일을 같이 써서 캐시 디렉터리를 늘리지 않는다.
        profile = getattr(self._owner, "_news_web_profile", None)
        if profile is not None:
            self._webview.setPage(QWebEnginePage(profile, self._webview))
        self._host_layout.addWidget(self._webview)
        self._webview.setZoomFactor(self._zoom)
        self._webview.loadFinished.connect(self._embed_loaded)
        return self._webview

    def show_post(self, context: dict):
        self._url = str(context.get("url") or "")
        body = str(context.get("body") or "")
        links = _LINK_RE.findall(body)
        self._article_url = links[0] if links else ""
        self._article_button.setEnabled(bool(self._article_url))
        self._article_button.setToolTip(self._article_url or "본문에 링크가 없습니다.")
        channel = str(context.get("channel") or "")
        title = str(context.get("title") or "")
        self._header.setText(f"{channel} · {title}" if channel else title)

        # 임베드는 네트워크를 타므로 저장된 본문을 먼저 띄워 대기 시간을 없앤다.
        self._text.setPlainText(body)
        self._text.show()
        webview = self._ensure_webview() if self._url else None
        if webview is not None:
            self._host.show()
            webview.hide()
            self._header.setText(self._header.text() + "  · 원문 불러오는 중…")
            # 공식 임베드 주소만 로그인 없이 본문과 이미지를 그대로 보여준다.
            webview.setUrl(QUrl(f"{self._url}?embed=1&userpic=true"))
        else:
            self._host.hide()
        self.show()
        self.raise_()
        self.activateWindow()

    def _embed_loaded(self, succeeded: bool):
        """임베드가 다 뜨면 미리 보여주던 텍스트와 바꾼다."""
        self._header.setText(self._header.text().replace(
            "  · 원문 불러오는 중…", ""))
        if not succeeded or self._webview is None:
            return
        if self._webview.url().toString() in ("", "about:blank"):
            return
        self._text.hide()
        self._webview.show()

    def _open_article(self):
        if self._article_url:
            QDesktopServices.openUrl(QUrl(self._article_url))

    def _save_geo(self):
        self._owner._settings.setValue(
            "telegram_detail_geo", self.saveGeometry())

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._save_geo()

    def moveEvent(self, event):
        super().moveEvent(event)
        self._save_geo()

    def closeEvent(self, event):
        self._save_geo()
        if self._webview is not None:
            # 웹뷰를 없애 렌더러 프로세스까지 정리한다. 창을 닫아둔 동안에는
            # 이 기능이 메모리를 전혀 차지하지 않는다.
            self._webview.setUrl(QUrl("about:blank"))
            self._host_layout.removeWidget(self._webview)
            self._webview.deleteLater()
            self._webview = None
        super().closeEvent(event)


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
                str(row.get("stock_names") or ""), " ".join(codes),
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
            f"{row.get('body') or ''}\n\n클릭: 앱 안에서 원문 상세 보기")
        body_item.setData(Qt.ItemDataRole.UserRole, str(row.get("url") or ""))
        body_item.setData(Qt.ItemDataRole.UserRole + 1, str(row.get("body") or ""))
        body_item.setData(
            Qt.ItemDataRole.UserRole + 2,
            str(row.get("channel_title") or row.get("channel") or ""))
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
        channel = str(row.get("channel_title") or row.get("channel") or "")
        self._latest_ls_news_context = {
            "provider": "TELEGRAM",
            "title": title,
            "stock_code": codes[0] if codes else "",
            "url": str(row.get("url") or ""),
            "body": str(row.get("body") or ""),
            "channel": channel,
        }
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
        if item is None:
            return
        self.open_telegram_detail({
            "url": str(item.data(Qt.ItemDataRole.UserRole) or ""),
            "body": str(item.data(Qt.ItemDataRole.UserRole + 1) or ""),
            "channel": str(item.data(Qt.ItemDataRole.UserRole + 2) or ""),
            "title": item.text(),
        })

    def open_telegram_detail(self, context: dict):
        """상세창 하나를 재사용해 원문을 앱 안에서 보여준다."""
        if not (context.get("url") or context.get("body")):
            return
        window = getattr(self, "_telegram_detail_window", None)
        if window is None:
            window = TelegramPostDialog(self)
            self._telegram_detail_window = window
        window.show_post(context)

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
