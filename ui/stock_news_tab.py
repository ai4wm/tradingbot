# -*- coding: utf-8 -*-
"""분석창 종목뉴스·종토방 탭.

AnalysisWindow가 상속해 쓰는 화면 조각이다. 관심종목 표와 네이버 웹뷰가
분석창의 상단 전광판·상태바를 함께 쓰기 때문에 믹스인으로 분리했다.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from datetime import datetime, timedelta

from PySide6.QtCore import QPoint, Qt, QTimer, QUrl
from PySide6.QtGui import QColor, QDesktopServices, QPalette
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QHBoxLayout, QHeaderView, QLabel,
    QLineEdit, QMenu, QMessageBox, QPushButton, QSplitter, QTableWidget,
    QTableWidgetItem, QVBoxLayout, QWidget,
)

import config
from analysis_db import (
    DB_PATH, log_content_request, news_request_count_today, news_rows,
    realtime_watch_codes, realtime_watch_rows,
    reconcile_news_search_results,
    resolve_analysis_stock, save_news_items, set_realtime_watch,
)
from gui import NumericTableWidgetItem, PURPLE
from naver_news_api import NaverNewsClient
from rank import _beep


log = logging.getLogger("stock_news_tab")


class StockNewsTabMixin:
    """관심종목 뉴스 수집과 네이버 웹뷰 표시를 담당한다."""

    def _show_latest_naver_news(self, row: dict):
        """네이버 API 신규 뉴스를 상단 공용 전광판에 표시한다."""
        title = " ".join(str(row.get("current_title") or "").split())
        if not title:
            return
        stock_code = str(row.get("stock_code") or "").strip()
        stock_name = str(row.get("stock_name") or stock_code).strip()
        self._latest_ls_news_context = {
            "provider": "NAVER",
            "title": title,
            "stock_code": stock_code,
            "stock_name": stock_name,
            "url": str(
                row.get("naver_url") or row.get("original_url") or ""
            ).strip(),
        }
        headline = f"[네이버] {stock_name} · {title}" if stock_name else title
        self._latest_ls_news_label.set_headline(headline)
        self._set_latest_ls_news_highlight(True)
        self._latest_ls_news_highlight_timer.start(3500)

    def _build_realtime_news_page(self, layout: QVBoxLayout):
        controls = QHBoxLayout()
        self._news_watch_search = QLineEdit()
        self._news_watch_search.setPlaceholderText("종목코드·종목명")
        self._news_watch_search.setClearButtonEnabled(True)
        self._news_watch_search.returnPressed.connect(self._add_news_watch)
        add_btn = QPushButton("감시 추가")
        add_btn.clicked.connect(self._add_news_watch)
        remove_btn = QPushButton("선택 감시 해제")
        remove_btn.clicked.connect(self._remove_selected_news_watch)
        fetch_btn = QPushButton("선택 종목 뉴스 조회")
        fetch_btn.clicked.connect(
            lambda: self._start_realtime_news_collection(True, True))
        self._news_auto = QCheckBox("자동수집")
        self._news_auto.setChecked(
            self._settings.value(
                "analysis_news_auto", "false") == "true")
        self._news_auto_interval = QComboBox()
        for minutes in (1, 3, 5, 10, 15, 30):
            self._news_auto_interval.addItem(f"{minutes}분", minutes)
        saved_news_interval = int(
            self._settings.value("analysis_news_interval", 5))
        saved_news_index = self._news_auto_interval.findData(
            saved_news_interval)
        self._news_auto_interval.setCurrentIndex(
            saved_news_index if saved_news_index >= 0 else 2)
        self._news_auto.toggled.connect(self._news_auto_toggled)
        self._news_auto_interval.currentIndexChanged.connect(
            self._news_auto_interval_changed)
        controls.addWidget(QLabel("감시종목"))
        controls.addWidget(self._news_watch_search, 1)
        controls.addWidget(add_btn)
        controls.addWidget(remove_btn)
        controls.addWidget(fetch_btn)
        controls.addWidget(self._news_auto)
        controls.addWidget(self._news_auto_interval)
        layout.addLayout(controls)

        self._news_status = QLabel(
            "감시 종목을 직접 추가해 주세요. 종목뉴스·종토방은 웹뷰 열람 전용입니다.")
        self._news_status.setWordWrap(True)
        layout.addWidget(self._news_status)

        watch_columns = (
            "번호", "종목명", "감시", "종목코드", "최근 뉴스", "마지막 조회",
        )
        self._news_watch_table = QTableWidget(0, len(watch_columns))
        self._news_watch_table.setHorizontalHeaderLabels(watch_columns)
        self._news_watch_table.setSortingEnabled(True)
        self._news_watch_table.setAlternatingRowColors(True)
        self._news_watch_table.setEditTriggers(
            QTableWidget.EditTrigger.NoEditTriggers)
        self._news_watch_table.verticalHeader().setVisible(False)
        watch_header = self._news_watch_table.horizontalHeader()
        watch_header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        watch_header.setSectionsMovable(False)
        watch_header.setStretchLastSection(False)
        saved_watch_header = self._settings.value(
            "analysis_news_watch_header_v4")
        self._news_watch_header_restored = bool(
            saved_watch_header is not None
            and watch_header.restoreState(saved_watch_header))
        if not self._news_watch_header_restored:
            for column, width in enumerate((42, 115, 38, 72, 145, 145)):
                self._news_watch_table.setColumnWidth(column, width)
        # 기본은 등록 순서(번호 오름차순)이며, 이후 모든 열 제목으로 정렬 가능하다.
        self._news_watch_table.sortItems(0, Qt.SortOrder.AscendingOrder)
        self._news_watch_header_initialized = False
        self._news_watch_header_timer = QTimer(self)
        self._news_watch_header_timer.setSingleShot(True)
        self._news_watch_header_timer.timeout.connect(
            self._save_news_watch_header)
        watch_header.sectionResized.connect(
            lambda *_: self._news_watch_header_timer.start(400))
        self._news_watch_table.cellClicked.connect(
            self._news_watch_table_clicked)
        self._news_watch_table.setContextMenuPolicy(
            Qt.ContextMenuPolicy.CustomContextMenu)
        self._news_watch_table.customContextMenuRequested.connect(
            self._news_watch_context_menu)

        news_columns = (
            "게시시각", "종목", "제목", "재료", "상태", "언론사",
        )
        self._realtime_news_table = QTableWidget(0, len(news_columns))
        self._realtime_news_table.setHorizontalHeaderLabels(news_columns)
        self._realtime_news_table.setSortingEnabled(True)
        self._realtime_news_table.setAlternatingRowColors(True)
        self._realtime_news_table.setStyleSheet(
            "QTableWidget::item:selected {"
            " background-color: #5B4FC4;"
            " color: #FFFFFF;"
            "}")
        self._realtime_news_table.setEditTriggers(
            QTableWidget.EditTrigger.NoEditTriggers)
        self._realtime_news_table.verticalHeader().setVisible(False)
        self._realtime_news_table.cellClicked.connect(
            self._realtime_news_table_clicked)

        watch_pane = QWidget()
        watch_layout = QVBoxLayout(watch_pane)
        watch_layout.setContentsMargins(0, 0, 0, 0)
        watch_layout.addWidget(QLabel("직접 등록한 감시 종목"))
        watch_layout.addWidget(self._news_watch_table)

        news_pane = QWidget()
        news_layout = QVBoxLayout(news_pane)
        news_layout.setContentsMargins(0, 0, 0, 0)
        news_header = QHBoxLayout()
        news_header.addWidget(QLabel("네이버 공식 검색 API 뉴스"))
        self._news_scope = QComboBox()
        self._news_scope.addItem("관심종목 전체뉴스", "all")
        self._news_scope.addItem("선택한 종목뉴스만", "selected")
        saved_news_scope = str(self._settings.value(
            "analysis_news_scope", "selected") or "selected")
        saved_scope_index = self._news_scope.findData(saved_news_scope)
        self._news_scope.setCurrentIndex(
            saved_scope_index if saved_scope_index >= 0 else 1)
        self._news_scope.currentIndexChanged.connect(
            self._news_scope_changed)
        news_header.addWidget(self._news_scope)
        news_header.addStretch(1)
        news_layout.addLayout(news_header)
        news_layout.addWidget(self._realtime_news_table)

        web_pane = QWidget()
        web_layout = QVBoxLayout(web_pane)
        web_layout.setContentsMargins(0, 0, 0, 0)
        web_controls = QHBoxLayout()
        self._news_web_back = QPushButton("←")
        self._news_web_forward = QPushButton("→")
        self._news_web_reload = QPushButton("새로고침")
        self._news_web_auto = QCheckBox("자동 새로고침")
        self._news_web_auto.setToolTip(
            "종목토론·뉴스·공시 목록 화면에서만 선택한 간격으로 새로고침")
        self._news_web_auto.setChecked(
            self._settings.value("analysis_web_auto", "false") == "true")
        self._news_web_auto_interval = QComboBox()
        for minutes in (1, 3, 5, 10, 15, 30):
            self._news_web_auto_interval.addItem(f"{minutes}분", minutes)
        saved_web_interval = int(
            self._settings.value("analysis_web_auto_interval", 5))
        saved_web_interval_index = self._news_web_auto_interval.findData(
            saved_web_interval)
        self._news_web_auto_interval.setCurrentIndex(
            saved_web_interval_index
            if saved_web_interval_index >= 0 else 2)
        self._news_web_auto_interval.setFixedWidth(68)
        self._news_web_auto_interval.setToolTip(
            "웹뷰 자동 새로고침 간격")
        external_btn = QPushButton("외부 브라우저")
        self._news_web_back.clicked.connect(
            lambda: self._news_web_action("back"))
        self._news_web_forward.clicked.connect(
            lambda: self._news_web_action("forward"))
        self._news_web_reload.clicked.connect(
            lambda: self._news_web_action("reload"))
        external_btn.clicked.connect(self._open_news_web_external)
        self._news_web_auto.toggled.connect(self._news_web_auto_toggled)
        self._news_web_auto_interval.currentIndexChanged.connect(
            self._news_web_auto_interval_changed)
        web_controls.addWidget(self._news_web_back)
        web_controls.addWidget(self._news_web_forward)
        web_controls.addWidget(self._news_web_reload)
        web_controls.addWidget(self._news_web_auto)
        web_controls.addWidget(self._news_web_auto_interval)
        web_controls.addStretch(1)
        web_controls.addWidget(external_btn)
        web_layout.addLayout(web_controls)

        item_menu = QHBoxLayout()
        item_menu.setSpacing(2)
        naver_item_pages = (
            ("종합정보", "main.naver"),
            ("시세", "sise.naver"),
            ("차트", "fchart.naver"),
            ("투자자별 매매동향", "frgn.naver"),
            ("뉴스·공시", "news.naver"),
            ("종목분석", "coinfo.naver"),
            ("종목토론", "board.naver"),
            ("전자공시", "dart.naver"),
            ("공매도현황", "short_trade.naver"),
        )
        self._news_item_menu_buttons = []
        for title, path in naver_item_pages:
            button = QPushButton(title)
            button.setToolTip(
                f"선택한 감시 종목의 네이버 금융 {title}를 엽니다.")
            button.clicked.connect(
                lambda _checked=False, page=path:
                self._open_selected_watch_page(page))
            item_menu.addWidget(button)
            self._news_item_menu_buttons.append(button)
        web_layout.addLayout(item_menu)

        self._news_web_host = QWidget()
        self._news_web_host_layout = QVBoxLayout(self._news_web_host)
        self._news_web_host_layout.setContentsMargins(0, 0, 0, 0)
        self._news_web_placeholder = QLabel(
            "뉴스 제목이나 종토방을 누르면 이곳에 웹페이지가 열립니다.\n"
            "웹페이지 내용은 자동 추출하거나 DB에 저장하지 않습니다.")
        self._news_web_placeholder.setAlignment(
            Qt.AlignmentFlag.AlignCenter)
        self._news_web_placeholder.setWordWrap(True)
        self._news_web_host_layout.addWidget(self._news_web_placeholder)
        # 첫 링크 클릭 때 Chromium 프로세스 생성과 레이아웃 삽입이 겹치면
        # 분석창이 잠깐 사라졌다 다시 나타나는 것처럼 보일 수 있다. 창이
        # 표시되기 전에 숨김 웹뷰를 준비하고 첫 클릭에는 URL만 교체한다.
        self._news_webview = None
        self._news_web_profile = None
        try:
            from PySide6.QtWebEngineCore import (
                QWebEnginePage, QWebEngineProfile,
            )
            from PySide6.QtWebEngineWidgets import QWebEngineView

            analysis_window = self

            class AnalysisWebPage(QWebEnginePage):
                """새 창 주소를 현재 웹뷰의 방문기록에 포함해 연다."""

                def createWindow(page_self, _window_type):
                    popup_page = QWebEnginePage(
                        page_self.profile(), page_self)

                    def open_in_current(url):
                        if (
                            analysis_window._news_webview is not None
                            and url.isValid()
                            and url.toString() not in ("", "about:blank")
                        ):
                            # 현재 페이지의 setUrl 이동이어야 뒤로가기 기록이
                            # 기존 종목 페이지 뒤에 정상적으로 쌓인다.
                            analysis_window._news_webview.setUrl(url)
                            QTimer.singleShot(0, popup_page.deleteLater)

                    popup_page.urlChanged.connect(open_in_current)
                    return popup_page

            profile_root = DB_PATH.parent / "web_profile" / "naver"
            storage_path = profile_root / "storage"
            cache_path = profile_root / "cache"
            storage_path.mkdir(parents=True, exist_ok=True)
            cache_path.mkdir(parents=True, exist_ok=True)
            self._news_web_profile = QWebEngineProfile(
                "analysis_naver", self)
            self._news_web_profile.setPersistentStoragePath(
                os.fspath(storage_path))
            self._news_web_profile.setCachePath(
                os.fspath(cache_path))
            self._news_web_profile.setHttpCacheType(
                QWebEngineProfile.HttpCacheType.DiskHttpCache)
            self._news_web_profile.setPersistentCookiesPolicy(
                QWebEngineProfile.PersistentCookiesPolicy
                .ForcePersistentCookies)
            self._news_webview = QWebEngineView(self._news_web_host)
            web_page = AnalysisWebPage(
                self._news_web_profile, self._news_webview)
            # 비활성 창의 Chromium 표면이 다시 합성되는 순간 기본 검정색이
            # 드러나지 않도록 앱의 웹뷰 바탕색을 명시한다.
            web_page.setBackgroundColor(QColor("#202124"))
            self._news_webview.setPage(web_page)
            self._news_webview.setVisible(False)
            self._news_web_host_layout.addWidget(self._news_webview)
            self._news_webview.urlChanged.connect(
                lambda current: setattr(
                    self, "_news_current_url", current.toString()))
            self._news_webview.loadFinished.connect(
                self._news_web_load_finished)
            self._news_webview.setUrl(QUrl("about:blank"))
        except ImportError:
            pass
        web_layout.addWidget(self._news_web_host, 1)
        if self._news_web_auto.isChecked():
            self._news_web_auto_timer.start(
                int(self._news_web_auto_interval.currentData())
                * 60 * 1000)

        self._news_right_splitter = QSplitter(Qt.Orientation.Vertical)
        self._news_right_splitter.addWidget(news_pane)
        self._news_right_splitter.addWidget(web_pane)
        self._news_right_splitter.setStretchFactor(0, 2)
        self._news_right_splitter.setStretchFactor(1, 3)
        self._news_main_splitter = QSplitter(Qt.Orientation.Horizontal)
        self._news_main_splitter.addWidget(watch_pane)
        self._news_main_splitter.addWidget(self._news_right_splitter)
        self._news_main_splitter.setStretchFactor(0, 1)
        self._news_main_splitter.setStretchFactor(1, 3)
        layout.addWidget(self._news_main_splitter, 1)

        for key, splitter in (
            ("analysis_news_main_splitter", self._news_main_splitter),
            ("analysis_news_right_splitter", self._news_right_splitter),
        ):
            state = self._settings.value(key)
            if state is not None:
                splitter.restoreState(state)
            splitter.splitterMoved.connect(self._save_news_splitters)

        self._selected_watch_code = ""
        self._news_current_url = ""
        self._news_scroll_mode = ""
        self._news_new_ids_by_code: dict[str, set[int]] = {}
        self._news_task = None
        self._news_pending_codes: set[str] = set()
        self._news_flash_serial = 0

    def _save_news_splitters(self, *_args):
        if not hasattr(self, "_news_main_splitter"):
            return
        self._settings.setValue(
            "analysis_news_main_splitter",
            self._news_main_splitter.saveState())
        self._settings.setValue(
            "analysis_news_right_splitter",
            self._news_right_splitter.saveState())
        self._settings.sync()

    def _save_news_watch_header(self):
        if not hasattr(self, "_news_watch_table"):
            return
        self._settings.setValue(
            "analysis_news_watch_header_v4",
            self._news_watch_table.horizontalHeader().saveState())
        self._settings.sync()

    @staticmethod
    def _display_timestamp(value: str) -> str:
        value = str(value or "")
        return value[:19].replace("T", " ") if value else "-"

    @staticmethod
    def _is_today_news(published: str) -> bool:
        """원본 게시시각을 로컬 시간대로 바꿔 오늘 기사인지 판별한다."""
        try:
            value = datetime.fromisoformat(str(published or ""))
            if value.tzinfo is not None:
                value = value.astimezone()
            return value.date() == datetime.now().astimezone().date()
        except (TypeError, ValueError):
            return False

    def _refresh_realtime_watch_table(self):
        if not hasattr(self, "_news_watch_table"):
            return
        rows = realtime_watch_rows()
        table = self._news_watch_table
        selected_code = self._selected_watch_code
        table.setSortingEnabled(False)
        table.setRowCount(len(rows))
        selected_row = -1
        for row_index, row in enumerate(rows):
            values = (
                row_index + 1, row["stock_name"], "★", row["stock_code"],
                self._display_timestamp(row["latest_news_at"]),
                self._display_timestamp(row["last_news_checked_at"]),
            )
            for column, value in enumerate(values):
                item = (
                    NumericTableWidgetItem(str(value), int(value))
                    if column == 0 else QTableWidgetItem(str(value or ""))
                )
                item.setData(
                    Qt.ItemDataRole.UserRole + 2, row["stock_code"])
                item.setData(
                    Qt.ItemDataRole.UserRole + 3, row["stock_name"])
                if column == 0:
                    item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                elif column == 2:
                    item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                    item.setForeground(QColor("#f4b400"))
                elif column == 1 and row["market"] == "KOSDAQ":
                    item.setForeground(PURPLE)
                    item.setToolTip("KOSDAQ 종목")
                table.setItem(row_index, column, item)
            if row["stock_code"] == selected_code:
                selected_row = row_index
        table.setSortingEnabled(True)
        if selected_code:
            for row_index in range(table.rowCount()):
                item = table.item(row_index, 0)
                if item and str(
                        item.data(Qt.ItemDataRole.UserRole + 2) or "") == selected_code:
                    selected_row = row_index
                    break
        if not self._news_watch_header_initialized:
            if not self._news_watch_header_restored:
                for column, width in enumerate((42, 115, 38, 72, 145, 145)):
                    table.setColumnWidth(column, width)
            self._news_watch_header_initialized = True
        if rows and selected_row < 0:
            selected_row = 0
            self._selected_watch_code = str(rows[0]["stock_code"])
        if selected_row >= 0:
            table.setCurrentCell(selected_row, 0)
        used = news_request_count_today()
        key_state = (
            "API 키 준비" if config.NAVER_CLIENT_ID
            and config.NAVER_CLIENT_SECRET else "API 키 없음")
        self._news_status.setText(
            f"감시 {len(rows):,}/80종목 · 오늘 뉴스 API {used:,}/25,000회 "
            f"· {key_state} · 종목뉴스·종토방은 웹뷰 열람 전용")

    def _refresh_realtime_news_table(self):
        if not hasattr(self, "_realtime_news_table"):
            return
        show_all_watched = (
            hasattr(self, "_news_scope")
            and self._news_scope.currentData() == "all"
        )
        if show_all_watched:
            rows = news_rows("", 300, watched_only=True)
            watched_codes = realtime_watch_codes()
            new_ids = set().union(*(
                self._news_new_ids_by_code.get(code, set())
                for code in watched_codes
            )) if watched_codes else set()
        else:
            rows = (
                news_rows(self._selected_watch_code, 300)
                if self._selected_watch_code else []
            )
            new_ids = self._news_new_ids_by_code.get(
                self._selected_watch_code, set())
        table = self._realtime_news_table
        is_dark = (
            table.palette().color(QPalette.ColorRole.Base).lightness() < 128)
        link_color = QColor("#64B5F6" if is_dark else "#1565C0")
        table.setSortingEnabled(False)
        table.setRowCount(len(rows))
        for row_index, row in enumerate(rows):
            is_today = self._is_today_news(row["published_at_source"])
            removal_status = str(row["removal_status"] or "ACTIVE").upper()
            is_removed = removal_status in ("REMOVED", "DELETED")
            is_missing = removal_status == "MISSING"
            is_new = int(row["news_id"]) in new_ids
            if is_removed:
                status = "삭제"
            elif is_missing:
                status = "검색에서 사라짐"
            elif row["modified_count"]:
                status = f"수정 {row['modified_count']}회"
            else:
                status = "정상"
            values = (
                self._display_timestamp(row["published_at_source"]),
                row["stock_name"], row["current_title"],
                row["material_type"], status, row["publisher"],
            )
            url = row["naver_url"] or row["original_url"]
            for column, value in enumerate(values):
                item = QTableWidgetItem(str(value or ""))
                item.setData(Qt.ItemDataRole.UserRole + 20, url)
                item.setData(
                    Qt.ItemDataRole.UserRole + 2, row["stock_code"])
                if is_removed:
                    item.setBackground(QColor("#E0E0E0"))
                    item.setForeground(QColor("#777777"))
                elif is_missing:
                    item.setBackground(QColor("#FFE0B2"))
                    item.setForeground(QColor("#7A3E00"))
                elif is_new:
                    item.setBackground(QColor("#C8F7D4"))
                    item.setForeground(QColor("#14532D"))
                elif is_today:
                    item.setBackground(QColor("#FFF0A6"))
                    item.setForeground(QColor("#5D3A00"))
                if column == 2:
                    item.setToolTip(row["current_summary"] or "")
                    if is_new and not is_removed and not is_missing:
                        title_font = item.font()
                        title_font.setBold(True)
                        item.setFont(title_font)
                    elif not is_today and not is_removed and not is_missing:
                        item.setForeground(link_color)
                    elif is_today and not is_removed and not is_missing:
                        title_font = item.font()
                        title_font.setBold(True)
                        item.setFont(title_font)
                elif column == 4:
                    item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                    if is_removed:
                        item.setForeground(QColor("#C62828"))
                        status_font = item.font()
                        status_font.setBold(True)
                        item.setFont(status_font)
                    elif is_missing:
                        item.setForeground(QColor("#E65100"))
                        status_font = item.font()
                        status_font.setBold(True)
                        item.setFont(status_font)
                    elif row["modified_count"]:
                        item.setForeground(QColor("#E65100"))
                table.setItem(row_index, column, item)
        table.setSortingEnabled(True)
        table.resizeColumnsToContents()
        if table.columnCount() > 2:
            table.setColumnWidth(
                2, min(620, max(300, table.columnWidth(2))))

    def _news_scope_changed(self, _index: int):
        """네이버 공식 뉴스 표의 관심종목 전체/선택종목 범위를 전환한다."""
        scope = str(self._news_scope.currentData() or "selected")
        self._settings.setValue("analysis_news_scope", scope)
        self._settings.sync()
        self._refresh_realtime_news_table()

    def _add_news_watch(self):
        query = self._news_watch_search.text().strip()
        stock = resolve_analysis_stock(query)
        if stock is None:
            QMessageBox.information(
                self, "실시간 감시", "종목코드 또는 종목명을 확인해 주세요.")
            return
        try:
            set_realtime_watch(
                stock["stock_code"], True, "MANUAL_NEWS_TAB")
        except ValueError as error:
            QMessageBox.warning(self, "실시간 감시", str(error))
            return
        self._selected_watch_code = stock["stock_code"]
        self._news_watch_search.clear()
        self._refresh_realtime_watch_table()
        self._refresh_realtime_news_table()
        self._refresh_limit_up_table()
        self.watchlist_changed.emit()
        if self._news_auto.isChecked():
            self._start_realtime_news_collection(True, False)

    def _remove_selected_news_watch(self):
        code = self._selected_watch_code
        if not code:
            return
        set_realtime_watch(code, False)
        self._selected_watch_code = ""
        self._refresh_realtime_watch_table()
        self._refresh_realtime_news_table()
        self._refresh_limit_up_table()
        self.watchlist_changed.emit()

    def _news_watch_table_clicked(self, row: int, column: int):
        item = self._news_watch_table.item(row, 0)
        if item is None:
            return
        code = str(
            item.data(Qt.ItemDataRole.UserRole + 2) or "")
        if column == 2:
            set_realtime_watch(code, False)
            if self._selected_watch_code == code:
                self._selected_watch_code = ""
            self._refresh_realtime_watch_table()
            self._refresh_limit_up_table()
            self.watchlist_changed.emit()
        else:
            self._selected_watch_code = code
            self._open_selected_watch_board()
        self._refresh_realtime_news_table()

    def _news_watch_context_menu(self, position):
        """감시목록 종목명 우클릭은 종목코드만 복사한다."""
        item = self._news_watch_table.itemAt(position)
        if item is None or item.column() != 1:
            return
        code = str(
            item.data(Qt.ItemDataRole.UserRole + 2) or "")
        if not code:
            return
        QApplication.clipboard().setText(code)
        self.statusBar().showMessage(f"{code} 복사됨", 2000)

    def _realtime_news_table_clicked(self, row: int, column: int):
        if column != 2:
            return
        item = self._realtime_news_table.item(row, 0)
        url = (
            item.data(Qt.ItemDataRole.UserRole + 20) if item else "")
        if url:
            self._show_news_web_url(str(url), "article")

    def _news_auto_toggled(self, enabled: bool):
        self._settings.setValue(
            "analysis_news_auto", "true" if enabled else "false")
        self._settings.sync()
        self.news_auto_changed.emit(enabled)
        if enabled:
            self._start_realtime_news_collection(False, False)

    def _news_auto_interval_changed(self, _index: int):
        minutes = int(self._news_auto_interval.currentData())
        self._settings.setValue("analysis_news_interval", minutes)
        self._settings.sync()
        self.news_auto_interval_changed.emit(minutes)

    def _start_realtime_news_collection(
            self, selected_only: bool, show_warning: bool):
        if not config.NAVER_CLIENT_ID or not config.NAVER_CLIENT_SECRET:
            self._news_status.setText(
                ".env에 NAVER_CLIENT_ID와 NAVER_CLIENT_SECRET이 필요합니다. "
                "감시목록과 웹뷰는 API 키 없이도 사용할 수 있습니다.")
            if show_warning:
                QMessageBox.information(
                    self, "네이버 뉴스 API",
                    ".env에 NAVER_CLIENT_ID와 "
                    "NAVER_CLIENT_SECRET을 입력해 주세요.")
            return
        if news_request_count_today() >= 20000:
            self._news_status.setText(
                "오늘 뉴스 API 안전 한도 20,000회에 도달해 자동수집을 멈췄습니다.")
            self._news_auto.setChecked(False)
            return
        codes = (
            [self._selected_watch_code]
            if selected_only and self._selected_watch_code
            else [row["stock_code"] for row in realtime_watch_rows()]
        )
        if not codes:
            if show_warning:
                QMessageBox.information(
                    self, "네이버 뉴스 API", "감시 종목을 먼저 추가해 주세요.")
            return
        self._news_pending_codes.update(code for code in codes if code)
        if self._news_task and not self._news_task.done():
            self._news_status.setText(
                f"뉴스 조회 중 · 추가 대기 "
                f"{len(self._news_pending_codes):,}종목")
            return
        self._news_task = asyncio.ensure_future(
            self._drain_realtime_news_queue())

    async def _drain_realtime_news_queue(self):
        try:
            while self._news_pending_codes:
                codes = sorted(self._news_pending_codes)
                self._news_pending_codes.clear()
                await self._collect_realtime_news(codes)
        finally:
            self._news_task = None

    async def _collect_realtime_news(self, codes: list[str]):
        client = NaverNewsClient(
            config.NAVER_CLIENT_ID, config.NAVER_CLIENT_SECRET)
        total_new = total_updated = errors = 0
        latest_new_news = None
        try:
            for index, code in enumerate(codes, 1):
                stock = resolve_analysis_stock(code)
                if stock is None:
                    continue
                stock_name = stock["stock_name"]
                query = f'"{stock_name}"'
                started = time.monotonic()
                try:
                    items = await client.search(query)
                    matched_items = [
                        item for item in items
                        if stock_name.lower() in (
                            f"{item.get('title') or ''} "
                            f"{item.get('summary') or ''}"
                        ).lower()
                    ]
                    saved = save_news_items(
                        code, stock_name, matched_items)
                    code_new_ids = {
                        int(news_id) for news_id in saved["new_ids"]
                    }
                    self._news_new_ids_by_code[code] = code_new_ids
                    if code_new_ids:
                        candidate = next((
                            row for row in news_rows(code, 300)
                            if int(row["news_id"]) in code_new_ids
                        ), None)
                        if candidate is not None and (
                            latest_new_news is None
                            or (
                                str(candidate["published_at_source"] or ""),
                                int(candidate["news_id"]),
                            ) > (
                                str(latest_new_news["published_at_source"] or ""),
                                int(latest_new_news["news_id"]),
                            )
                        ):
                            latest_new_news = candidate
                    reconcile_news_search_results(code, items)
                    elapsed = int((time.monotonic() - started) * 1000)
                    log_content_request(
                        code, query, 200, len(items),
                        saved["new"], elapsed)
                    total_new += saved["new"]
                    total_updated += saved["updated"]
                except Exception as error:  # noqa: BLE001
                    elapsed = int((time.monotonic() - started) * 1000)
                    log_content_request(
                        code, query, 0, 0, 0, elapsed, str(error))
                    errors += 1
                    log.warning(
                        "naver news collection failed: %s %s", code, error)
                self._news_status.setText(
                    f"뉴스 조회 {index:,}/{len(codes):,} · 신규 "
                    f"{total_new:,} · 수정 {total_updated:,} · 오류 {errors:,}")
                await asyncio.sleep(0)
        finally:
            self._refresh_realtime_watch_table()
            self._refresh_realtime_news_table()
            self._news_status.setText(
                f"뉴스 조회 완료 · 신규 {total_new:,} · 수정 "
                f"{total_updated:,} · 오류 {errors:,} · 오늘 API "
                f"{news_request_count_today():,}/25,000회")
            if total_new:
                if latest_new_news is not None:
                    self._show_latest_naver_news(latest_new_news)
                if (
                    hasattr(self, "_ls_news_sound")
                    and self._ls_news_sound.isChecked()
                ):
                    _beep("naver_news")
                self.new_news_found.emit(total_new)

    def flash_realtime_news_tab(self):
        """분석창이 보일 때 실시간 뉴스 탭을 짧게 점멸한다."""
        index = next((
            i for i in range(self._tabs.count())
            if self._tabs.tabText(i) == "종목뉴스·종토방"
        ), -1)
        if index < 0:
            return
        self._news_flash_serial += 1
        serial = self._news_flash_serial
        tab_bar = self._tabs.tabBar()

        def set_color(color: QColor):
            if serial == self._news_flash_serial:
                tab_bar.setTabTextColor(index, color)

        for delay, color in (
            (0, QColor("#FFB300")),
            (300, QColor()),
            (600, QColor("#FFB300")),
            (900, QColor()),
            (1200, QColor("#FFB300")),
            (1800, QColor()),
        ):
            QTimer.singleShot(delay, lambda c=color: set_color(c))

    def open_realtime_watch(self, code: str, fetch_news: bool = True):
        """뉴스 탭에서 종목을 선택하고 저장 뉴스와 종토방을 함께 연다."""
        self._selected_watch_code = str(code or "")
        for index in range(self._tabs.count()):
            if self._tabs.tabText(index) == "종목뉴스·종토방":
                self._tabs.setCurrentIndex(index)
                break
        self._refresh_realtime_watch_table()
        self._refresh_realtime_news_table()
        self._refresh_limit_up_table()
        self._open_selected_watch_board()
        self.show()
        self.raise_()
        self.activateWindow()
        if fetch_news:
            self._start_realtime_news_collection(True, False)

    def _open_selected_watch_page(self, page: str):
        if not self._selected_watch_code:
            self._news_status.setText(
                "왼쪽 감시목록에서 종목을 먼저 선택해 주세요.")
            return
        self._show_news_web_url(
            f"https://finance.naver.com/item/{page}?code="
            f"{self._selected_watch_code}",
            "item",
        )

    def _open_selected_watch_board(self):
        self._open_selected_watch_page("board.naver")

    def _show_news_web_url(self, url: str, scroll_mode: str = ""):
        self._news_current_url = str(url or "")
        self._news_scroll_mode = str(scroll_mode or "")
        if not self._news_current_url:
            return
        if self._news_webview is None:
            QDesktopServices.openUrl(QUrl(self._news_current_url))
            self._news_status.setText(
                "Qt 웹뷰를 사용할 수 없어 외부 브라우저로 열었습니다.")
            return
        self._news_web_placeholder.hide()
        self._news_webview.show()
        self._news_webview.setUrl(QUrl(self._news_current_url))

    def _news_web_load_finished(self, succeeded: bool):
        """종목 페이지는 중간 메뉴, 뉴스 기사는 실제 제목부터 보이게 한다."""
        if not succeeded or self._news_webview is None:
            return
        current_url = self._news_webview.url()
        loaded_url = current_url.toString()
        item_menu_paths = {
            "/item/main.naver", "/item/sise.naver", "/item/fchart.naver",
            "/item/frgn.naver", "/item/news.naver", "/item/coinfo.naver",
            "/item/board.naver", "/item/dart.naver",
            "/item/short_trade.naver",
        }
        is_item_page = (
            current_url.host().lower() == "finance.naver.com"
            and (
                current_url.path() in item_menu_paths
                or current_url.path() == "/item/board_read.naver"
            )
        )
        if is_item_page:
            today_highlight = ""
            today_page_labels = {
                "/item/board.naver": "게시글",
                "/item/news.naver": "뉴스",
                "/item/dart.naver": "공시",
            }
            today_page_label = today_page_labels.get(current_url.path())
            if today_page_label:
                today_short = datetime.now().strftime("%y.%m.%d")
                today_long_dot = datetime.now().strftime("%Y.%m.%d")
                today_long_dash = datetime.now().strftime("%Y-%m-%d")
                today_month_day = datetime.now().strftime("%m.%d")
                today_highlight = f"""
                    const todayLabels = [
                        {today_short!r}, {today_long_dot!r},
                        {today_long_dash!r}, {today_month_day!r}
                    ];
                    const applyTodayHighlight = () => {{
                    let todayCount = 0;
                    // 뉴스·공시 목록은 본문 iframe에 들어가므로 함께 검사한다.
                    const documents = [document];
                    for (const frame of document.querySelectorAll('iframe')) {{
                        try {{
                            if (frame.contentDocument) documents.push(frame.contentDocument);
                        }} catch (_error) {{}}
                    }}
                    for (const source of documents) {{
                        for (const row of source.querySelectorAll('table tr')) {{
                            const text = (row.innerText || '').replace(/\\s+/g, ' ');
                            const isToday = todayLabels.some(label => text.includes(label));
                            if (!isToday) continue;
                            todayCount += 1;
                            row.style.setProperty('background-color', '#FFF3B0', 'important');
                            row.style.setProperty('box-shadow', 'inset 4px 0 #F57C00', 'important');
                            row.style.setProperty('font-weight', '700', 'important');
                            for (const cell of row.querySelectorAll('td, th')) {{
                                cell.style.setProperty('background-color', '#FFF3B0', 'important');
                                cell.style.setProperty('font-weight', '700', 'important');
                            }}
                            row.title = '오늘 {today_page_label}';
                        }}
                    }}
                    const board = document.querySelector('#content, #container, body');
                    if (board) {{
                        let badge = document.getElementById('codex-today-item-badge');
                        if (!badge) {{
                            badge = document.createElement('div');
                            badge.id = 'codex-today-item-badge';
                            badge.style.cssText = [
                                'position:sticky', 'top:0', 'z-index:9999',
                                'display:inline-block', 'margin:4px 0',
                                'padding:3px 8px', 'border-radius:3px',
                                'background:#FFF3B0', 'color:#6D4300',
                                'font:700 12px sans-serif'
                            ].join(';');
                            board.prepend(badge);
                        }}
                        badge.textContent = `오늘 {today_page_label} 강조: ${{todayCount}}건`;
                    }}
                    }};
                    applyTodayHighlight();
                    // 상위 문서는 먼저 끝나고 목록 iframe이 뒤늦게 채워질 수 있다.
                    for (const frame of document.querySelectorAll('iframe')) {{
                        frame.addEventListener('load', applyTodayHighlight);
                    }}
                    window.setTimeout(applyTodayHighlight, 500);
                    window.setTimeout(applyTodayHighlight, 1500);
                """
            script = f"""
                (() => {{
                    const itemMenuPaths = new Set([
                        '/item/main.naver', '/item/sise.naver',
                        '/item/fchart.naver', '/item/frgn.naver',
                        '/item/news.naver', '/item/coinfo.naver',
                        '/item/board.naver', '/item/dart.naver',
                        '/item/short_trade.naver'
                    ]);
                    const findItemMenu = () => {{
                        // 목록과 본문에 공통으로 있는 정확한 종목 중간 메뉴를
                        // 먼저 사용한다. inner_sub는 게시글 본문 영역이므로
                        // 본문보기에서 스크롤 기준으로 사용하면 위치가 달라진다.
                        const exactMenu = document.querySelector(
                            'ul.tabs_submenu.tab_total_submenu'
                        );
                        if (exactMenu) return exactMenu;

                        const candidates = Array.from(
                            document.querySelectorAll(
                                'ul.tabs_submenu, [class*="tabs_submenu"]'
                            )
                        );
                        let bestTarget = null;
                        let bestScore = 0;
                        for (const candidate of candidates) {{
                            const matchedPaths = new Set();
                            for (const link of candidate.querySelectorAll(
                                'a[href]'
                            )) {{
                                try {{
                                    const path = new URL(
                                        link.getAttribute('href'), location.href
                                    ).pathname.replace(/\/$/, '');
                                    if (itemMenuPaths.has(path)) {{
                                        matchedPaths.add(path);
                                    }}
                                }} catch (_error) {{}}
                            }}
                            if (matchedPaths.size > bestScore) {{
                                bestScore = matchedPaths.size;
                                bestTarget = candidate;
                            }}
                        }}
                        return bestScore >= 3 ? bestTarget : null;
                    }};

                    const alignItemMenu = () => {{
                        const target = findItemMenu();
                        if (!target) return false;
                        const top = Math.max(
                            0,
                            target.getBoundingClientRect().top
                                + window.scrollY - 4
                        );
                        window.scrollTo(0, top);
                        return true;
                    }};

                    try {{
                        history.scrollRestoration = 'manual';
                    }} catch (_error) {{}}
                    const aligned = alignItemMenu();

                    if (location.pathname.replace(/\/$/, '') ===
                        '/item/board_read.naver') {{
                        // Chromium이 내용보기에서 목록의 이전 스크롤 위치를
                        // 뒤늦게 복원하거나 광고 영역 높이가 변해도 공통 메뉴가
                        // 목록과 같은 자리에 있도록 잠깐만 보정한다.
                        if (window.__analysisBoardMenuAlignTimer) {{
                            window.clearInterval(
                                window.__analysisBoardMenuAlignTimer
                            );
                        }}
                        let remainingAlignments = 24;
                        let userMovedPage = false;
                        const cancelAlignment = () => {{
                            userMovedPage = true;
                            if (window.__analysisBoardMenuAlignTimer) {{
                                window.clearInterval(
                                    window.__analysisBoardMenuAlignTimer
                                );
                                window.__analysisBoardMenuAlignTimer = null;
                            }}
                        }};
                        for (const eventName of [
                            'wheel', 'touchstart', 'pointerdown', 'keydown'
                        ]) {{
                            window.addEventListener(
                                eventName, cancelAlignment,
                                {{ once: true, capture: true }}
                            );
                        }}
                        window.__analysisBoardMenuAlignTimer =
                            window.setInterval(() => {{
                                if (userMovedPage ||
                                    --remainingAlignments <= 0) {{
                                    cancelAlignment();
                                    return;
                                }}
                                alignItemMenu();
                            }}, 100);
                    }}
                    {today_highlight}
                    return aligned;
                }})();
            """
        elif self._news_scroll_mode == "article":
            script = """
                (() => {
                    const selectors = [
                        '#title_area', '.media_end_head_headline',
                        'article h1', 'main h1', '.article_title',
                        '.news_title', '.headline', 'h1',
                        '[class*="article"] h2'
                    ];
                    for (const selector of selectors) {
                        const candidates = document.querySelectorAll(selector);
                        for (const target of candidates) {
                            const style = window.getComputedStyle(target);
                            const rect = target.getBoundingClientRect();
                            if (style.display === 'none' ||
                                style.visibility === 'hidden' ||
                                rect.width === 0 || rect.height === 0 ||
                                !target.textContent.trim()) continue;
                            const top = rect.top + window.scrollY;
                            window.scrollTo(0, Math.max(0, top - 6));
                            return true;
                        }
                    }
                    return false;
                })();
            """
        else:
            return

        def scroll_loaded_page():
            if self._news_webview is None:
                return
            if self._news_webview.url().toString() == loaded_url:
                self._news_webview.page().runJavaScript(script)

        # 종목토론 내용보기는 자바스크립트가 약 2.4초 동안 레이아웃 변화를
        # 추적한다. 바깥 타이머를 다시 걸면 사용자의 직접 스크롤 이후에도
        # 위치를 되돌릴 수 있으므로 내용보기에는 한 번만 실행한다.
        delays = (
            (0,)
            if current_url.path() == "/item/board_read.naver"
            else (0, 500)
        )
        for delay in delays:
            QTimer.singleShot(delay, scroll_loaded_page)

    def _news_web_action(self, action: str):
        if self._news_webview is None:
            return
        getattr(self._news_webview, action)()

    def _open_news_web_external(self):
        if self._news_current_url:
            QDesktopServices.openUrl(QUrl(self._news_current_url))

    def _news_web_auto_toggled(self, enabled: bool):
        self._settings.setValue("analysis_web_auto", "true" if enabled else "false")
        self._settings.sync()
        if enabled:
            self._news_web_auto_timer.start(
                int(self._news_web_auto_interval.currentData())
                * 60 * 1000)
        else:
            self._news_web_auto_timer.stop()

    def _news_web_auto_interval_changed(self, _index: int):
        minutes = int(self._news_web_auto_interval.currentData())
        self._settings.setValue("analysis_web_auto_interval", minutes)
        self._settings.sync()
        if self._news_web_auto.isChecked():
            self._news_web_auto_timer.start(minutes * 60 * 1000)

    def _auto_reload_news_web(self):
        if (self._news_webview is not None and self._news_webview.isVisible()
                and _is_news_web_auto_reload_url(self._news_webview.url())):
            self._news_webview.reload()
