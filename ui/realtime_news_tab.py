# -*- coding: utf-8 -*-
"""분석창 LS 실시간 뉴스 탭.

AnalysisWindow가 상속해 쓰는 화면 조각이다. 상단 전광판과 관심종목
상태를 분석창과 공유하기 때문에 독립 위젯 대신 믹스인으로 분리했다.
"""
from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import re
import time
from collections import deque
from datetime import datetime, timedelta
from urllib.parse import quote, urlsplit

from PySide6.QtCore import (
    QPoint, QSettings, Qt, QTimer, QUrl, QUrlQuery, Signal)
from PySide6.QtGui import (
    QColor, QDesktopServices, QFont, QKeySequence, QShortcut, QTextCursor,
)
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QDialog, QHBoxLayout, QHeaderView, QLabel,
    QLineEdit, QMenu, QMessageBox, QPushButton, QTableWidget,
    QTableWidgetItem, QTextBrowser, QVBoxLayout, QWidget,
)

import config
from analysis_db import (
    DB_PATH, log_content_request, news_request_count_today,
    resolve_analysis_stock, set_realtime_watch, ls_realtime_news_detail, ls_realtime_news_rows,
    parse_ls_news_search_query, realtime_watch_codes, save_ls_realtime_news,
    search_ls_realtime_news, split_ls_news_stock_codes,
    update_ls_realtime_news_detail, update_ls_realtime_news_original_url,
    update_ls_realtime_news_source,
)
from gui import NumericTableWidgetItem
from naver_news_api import NaverNewsClient
from ls_news_server_sync import LSNewsServerSync
from ls_news_ws import (
    LSNewsItem, LSNewsStream, NEWS_SOURCE_DOMAINS, NEWS_SOURCE_NAMES,
    extract_news_links, format_news_body, format_news_time,
    infer_news_original_url, infer_news_source, news_source_from_url,
    normalize_news_title, source_label,
)
from rank import _beep
from ui import (
    NEWS_NEW_TIME_BACKGROUND, NEWS_NEW_TIME_FOREGROUND,
    NEWS_NEW_TITLE_BACKGROUND, NEWS_NEW_TITLE_FOREGROUND,
)


log = logging.getLogger("realtime_news_tab")

LS_NEWS_VISIBLE_LIMIT = 500
LS_NEWS_NEW_ROLE = Qt.ItemDataRole.UserRole + 40
LS_NEWS_NEW_TIME_BACKGROUND = NEWS_NEW_TIME_BACKGROUND
LS_NEWS_NEW_TITLE_BACKGROUND = NEWS_NEW_TITLE_BACKGROUND
LS_NEWS_NEW_TIME_FOREGROUND = NEWS_NEW_TIME_FOREGROUND
LS_NEWS_NEW_TITLE_FOREGROUND = NEWS_NEW_TITLE_FOREGROUND
LS_NEWS_REUTERS_ORIGINAL_LABELS = frozenset({
    "원문바로가기",
    "원문보기",
})
LS_NEWS_SOURCE_SEARCH_ENDPOINTS = {
    "연합뉴스": ("https://www.yna.co.kr/search/index", "query"),
    "한국거래소": (
        "https://open.krx.co.kr/contents/COM/SearchMain.jsp"
        "?headerSearchType=all",
        "headerSearchWord",
    ),
    "이데일리": ("https://www.edaily.co.kr/search/news/", "keyword"),
    "머니투데이": ("https://www.mt.co.kr/search", "keyword"),
    "아시아경제": ("https://www.asiae.co.kr/search/index.htm", "kwd"),
    "뉴스핌": ("https://www.newspim.com/search", "searchword"),
    "매일경제": ("https://www.mk.co.kr/search", "word"),
    "한국경제": ("https://search.hankyung.com/search/news", "query"),
    "데이터투자": ("https://www.datatooza.com/search.php", "sn"),
    "인포스탁": (
        "https://www.infostockdaily.co.kr/news/searchForm.html", "sc_word"),
    "팜뉴스": (
        "https://www.pharmnews.com/news/articleList.html", "sc_word"),
    "연합인포맥스": (
        "https://news.einfomax.co.kr/news/articleList.html"
        "?sc_area=A&view_type=sm",
        "sc_word",
    ),
    "코리아헤럴드": ("https://www.koreaherald.com/search", "q"),
    "뉴스웨이": ("https://www.newsway.co.kr/search", "q"),
    "헤럴드경제": ("https://biz.heraldcorp.com/search", "q"),
    "파이낸셜뉴스": ("https://www.fnnews.com/search", "search_txt"),
    "이투데이": ("https://www.etoday.co.kr/search/", "keyword"),
    "조선비즈": (
        "https://biz.chosun.com/nsearch/"
        "?siteid=chosunbiz&website=chosunbiz&opt_chk=true",
        "query",
    ),
    "서울경제": (
        "https://www.sedaily.com/search?v1=20260722", "word"),
}
LS_NEWS_SOURCE_SEARCH_HOST_NAMES = {
    "www.yna.co.kr": "연합뉴스",
    "open.krx.co.kr": "한국거래소",
    "www.edaily.co.kr": "이데일리",
    "www.mt.co.kr": "머니투데이",
    "www.asiae.co.kr": "아시아경제",
    "www.newspim.com": "뉴스핌",
    "www.mk.co.kr": "매일경제",
    "search.hankyung.com": "한국경제",
    "www.datatooza.com": "데이터투자",
    "www.infostockdaily.co.kr": "인포스탁",
    "www.pharmnews.com": "팜뉴스",
    "news.einfomax.co.kr": "연합인포맥스",
    "www.koreaherald.com": "코리아헤럴드",
    "www.newsway.co.kr": "뉴스웨이",
    "biz.heraldcorp.com": "헤럴드경제",
    "www.reuters.com": "로이터",
    "www.fnnews.com": "파이낸셜뉴스",
    "www.etoday.co.kr": "이투데이",
    "biz.chosun.com": "조선비즈",
    "www.sedaily.com": "서울경제",
}


class LatestLSNewsLabel(QLabel):
    """분석창 상단에 최신 실시간 뉴스 제목 한 줄을 표시한다."""

    clicked = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._headline = ""
        self.setTextFormat(Qt.TextFormat.PlainText)
        self.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.setWordWrap(False)
        self.setCursor(Qt.CursorShape.PointingHandCursor)

    def set_headline(self, headline: str):
        self._headline = " ".join(str(headline or "").split())
        self.setToolTip(
            f"{self._headline}\n\n클릭: 해당 뉴스 열기")
        self._refresh_text()

    def _refresh_text(self):
        available_width = max(0, self.contentsRect().width() - 8)
        visible_text = self.fontMetrics().elidedText(
            self._headline,
            Qt.TextElideMode.ElideRight,
            available_width,
        )
        super().setText(visible_text)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._refresh_text()

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            self.clicked.emit()
            event.accept()
            return
        super().mousePressEvent(event)


class LSNewsDetailDialog(QDialog):
    """LS 뉴스 제목에서 여는 비모달 본문 상세창."""

    refresh_requested = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._original_url = ""
        self._original_search_url = ""
        self._body_plain = ""
        self._body_links = ()
        self._settings = QSettings("layout.ini", QSettings.IniFormat)
        try:
            saved_font_size = float(self._settings.value(
                "analysis_ls_news_body_font_size", 11.0))
        except (TypeError, ValueError):
            saved_font_size = 11.0
        self._body_font_size = min(18.0, max(8.0, saved_font_size))
        self.setWindowTitle("LS 뉴스 본문")
        self.setWindowModality(Qt.WindowModality.NonModal)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        self.resize(980, 720)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 12, 14, 12)
        layout.setSpacing(8)
        self._title_label = QLabel()
        self._title_label.setTextFormat(Qt.TextFormat.PlainText)
        self._title_label.setWordWrap(True)
        self._title_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        self._title_label.setStyleSheet(
            "QLabel { font-family: '맑은 고딕'; font-size: 20px; "
            "font-weight: 800; line-height: 140%; }")
        layout.addWidget(self._title_label)

        self._meta_label = QLabel()
        self._meta_label.setTextFormat(Qt.TextFormat.PlainText)
        self._meta_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        self._meta_label.setStyleSheet("QLabel { color: #AEB8C4; }")
        layout.addWidget(self._meta_label)

        self._stocks_label = QLabel()
        self._stocks_label.setTextFormat(Qt.TextFormat.PlainText)
        self._stocks_label.setWordWrap(True)
        self._stocks_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        self._stocks_label.setStyleSheet("QLabel { color: #8ED6FF; }")
        layout.addWidget(self._stocks_label)

        self._status_label = QLabel("본문 확인 중…")
        self._status_label.setTextFormat(Qt.TextFormat.PlainText)
        self._status_label.setStyleSheet(
            "QLabel { color: #D5A33D; font-weight: 700; }")
        layout.addWidget(self._status_label)

        self._body_edit = QTextBrowser()
        self._body_edit.setOpenLinks(False)
        self._body_edit.setOpenExternalLinks(False)
        self._body_edit.anchorClicked.connect(self._open_news_link)
        self._body_edit.highlighted.connect(
            lambda url: self._body_edit.setToolTip(url.toString()))
        self._body_edit.setPlaceholderText("본문 내용이 없습니다.")
        self._body_edit.document().setDefaultFont(QFont("맑은 고딕", 11))
        self._body_edit.setStyleSheet(
            "QTextBrowser { background-color: #242424; color: #ECEFF4; "
            "border: 1px solid #484848; border-radius: 6px; }"
            "QTextBrowser:focus { border-color: #B75AD8; }"
        )
        layout.addWidget(self._body_edit, 1)

        buttons = QHBoxLayout()
        self._refresh_button = QPushButton("본문 다시 불러오기")
        self._refresh_button.clicked.connect(self.refresh_requested.emit)
        self._smaller_button = QPushButton("A−")
        self._smaller_button.setFixedWidth(42)
        self._smaller_button.clicked.connect(
            lambda: self._adjust_body_font_size(-1))
        self._larger_button = QPushButton("A+")
        self._larger_button.setFixedWidth(42)
        self._larger_button.clicked.connect(
            lambda: self._adjust_body_font_size(1))
        self._update_body_font_buttons()
        self._original_button = QPushButton("원문 검색")
        self._original_button.setEnabled(False)
        self._original_button.clicked.connect(
            self._open_original_or_search)
        copy_button = QPushButton("본문 복사")
        copy_button.clicked.connect(
            lambda: QApplication.clipboard().setText(
                self._body_edit.toPlainText()))
        close_button = QPushButton("닫기")
        close_button.clicked.connect(self.close)
        buttons.addWidget(self._refresh_button)
        buttons.addWidget(self._smaller_button)
        buttons.addWidget(self._larger_button)
        buttons.addWidget(self._original_button)
        buttons.addStretch(1)
        buttons.addWidget(copy_button)
        buttons.addWidget(close_button)
        layout.addLayout(buttons)

    def set_metadata(self, title: str, metadata: str, stocks: str):
        self._title_label.setText(str(title or "제목 없음"))
        self._meta_label.setText(str(metadata or ""))
        self._stocks_label.setText(str(stocks or "관련 종목 없음"))

    def set_loading(self, message: str = "LS에서 본문 불러오는 중…"):
        self._status_label.setText(message)
        self._status_label.setStyleSheet(
            "QLabel { color: #D5A33D; font-weight: 700; }")
        self._refresh_button.setEnabled(False)

    def set_body(self, body: str, status: str):
        plain_body = format_news_body(body)
        links = extract_news_links(body)
        self._body_plain = plain_body
        self._body_links = links
        self._render_body()
        self._body_edit.moveCursor(QTextCursor.MoveOperation.Start)
        status_parts = [status]
        if plain_body:
            status_parts.append(f"본문 {len(plain_body):,}자")
        if links:
            status_parts.append(f"링크 {len(links):,}개")
        self._status_label.setText(" · ".join(status_parts))
        self._status_label.setStyleSheet(
            "QLabel { color: #55C981; font-weight: 700; }")
        self._refresh_button.setEnabled(True)

    def _adjust_body_font_size(self, change: int):
        next_size = min(18.0, max(8.0, self._body_font_size + change))
        if next_size == self._body_font_size:
            return
        self._body_font_size = next_size
        self._settings.setValue(
            "analysis_ls_news_body_font_size", self._body_font_size)
        self._settings.sync()
        self._render_body(preserve_scroll=True)
        self._update_body_font_buttons()

    def _render_body(self, preserve_scroll: bool = False):
        scroll_bar = self._body_edit.verticalScrollBar()
        old_maximum = scroll_bar.maximum()
        old_ratio = (
            scroll_bar.value() / old_maximum if old_maximum > 0 else 0.0)
        self._body_edit.setHtml(self._reader_html(
            self._body_plain,
            self._body_links,
            self._body_font_size,
        ))
        if preserve_scroll:
            QTimer.singleShot(
                0,
                lambda: scroll_bar.setValue(round(
                    old_ratio * scroll_bar.maximum())),
            )

    def _update_body_font_buttons(self):
        size_label = f"{self._body_font_size:g}pt"
        self._smaller_button.setEnabled(self._body_font_size > 8.0)
        self._larger_button.setEnabled(self._body_font_size < 18.0)
        self._smaller_button.setToolTip(
            f"본문 글자를 작게 표시합니다. (현재 {size_label})")
        self._larger_button.setToolTip(
            f"본문 글자를 크게 표시합니다. (현재 {size_label})")

    @staticmethod
    def _linked_text_html(
            block: str, links: tuple[tuple[str, str], ...]) -> str:
        matches = []
        for priority, (label, url) in enumerate(links):
            start = 0
            while label:
                index = block.find(label, start)
                if index < 0:
                    break
                matches.append((
                    index, index + len(label), -len(label),
                    priority, label, url,
                ))
                start = index + len(label)
        matches.sort(key=lambda match: (
            match[0], match[2], match[3]))

        def escaped(value: str) -> str:
            return html.escape(value).replace("\n", "<br>")

        rendered = []
        cursor = 0
        for start, end, _length, _priority, label, url in matches:
            if start < cursor:
                continue
            rendered.append(escaped(block[cursor:start]))
            rendered.append(
                f'<a href="{html.escape(url, quote=True)}">'
                f"{escaped(label)}</a>")
            cursor = end
        rendered.append(escaped(block[cursor:]))
        return "".join(rendered)

    @classmethod
    def _reader_html(
            cls, plain_body: str,
            links: tuple[tuple[str, str], ...] = (),
            font_size: float = 11.0) -> str:
        blocks = [
            block.strip() for block in str(plain_body or "").split("\n\n")
            if block.strip()
        ]
        rendered = []
        for index, block in enumerate(blocks):
            class_name = "article"
            if index == 0 and (block.startswith("[") or len(block) <= 240):
                class_name = "lead"
            if block.startswith("[관련기사]"):
                class_name = "related"
            lowered = block.lower()
            if index >= max(0, len(blocks) - 2) and (
                    "copyright" in lowered or "저작권자" in block
                    or "@" in block):
                class_name = "footer"
            content = cls._linked_text_html(block, links)
            rendered.append(
                f'<p class="{class_name}">{content}</p>')
        secondary_font_size = max(8.0, font_size - 1.5)
        return (
            "<!DOCTYPE html><html><head><meta charset='utf-8'><style>"
            "body { font-family: '맑은 고딕', 'Segoe UI', sans-serif; "
            f"font-size: {font_size:g}pt; line-height: 165%; color: #ECEFF4; "
            "margin: 20px 24px; }"
            "p { margin: 0 0 15px 0; }"
            ".lead { color: #FFFFFF; font-weight: 600; "
            "background-color: #30343A; padding: 12px 14px; }"
            f".related {{ color: #AEB8C4; font-size: {secondary_font_size:g}pt; "
            "background-color: #20242A; padding: 12px 14px; }"
            f".footer {{ color: #9099A6; font-size: {secondary_font_size:g}pt; }}"
            "a { color: #72B7FF; text-decoration: underline; }"
            "</style></head><body>"
            + "".join(rendered)
            + "</body></html>"
        )

    def _open_news_link(self, url: QUrl):
        if url.scheme().lower() not in ("http", "https", "mailto"):
            return
        if self._open_infostock_search(url):
            return
        QDesktopServices.openUrl(url)

    @staticmethod
    def _open_infostock_search(url: QUrl) -> bool:
        """GET 검색을 지원하지 않는 인포스탁 검색 폼을 POST로 연다."""
        if (
            url.host().casefold() != "www.infostockdaily.co.kr"
            or url.path() != "/news/searchForm.html"
        ):
            return False
        search_title = QUrlQuery(url).queryItemValue(
            "sc_word", QUrl.ComponentFormattingOption.FullyDecoded).strip()
        if not search_title:
            return False
        bridge_path = DB_PATH.parent / "ls_news_infostock_search.html"
        escaped_title = html.escape(search_title, quote=True)
        bridge_html = (
            "<!doctype html><html lang='ko'><head><meta charset='utf-8'>"
            "<title>인포스탁 기사 검색</title></head>"
            "<body onload=\"document.getElementById('search').submit()\">"
            "<form id='search' method='post' "
            "action='https://www.infostockdaily.co.kr/news/articleList.html'>"
            "<input type='hidden' name='sc_area' value='A'>"
            "<input type='hidden' name='view_type' value='sm'>"
            f"<input type='hidden' name='sc_word' value='{escaped_title}'>"
            "<noscript><button type='submit'>인포스탁에서 검색</button>"
            "</noscript></form></body></html>"
        )
        try:
            bridge_path.parent.mkdir(parents=True, exist_ok=True)
            bridge_path.write_text(bridge_html, encoding="utf-8")
        except OSError:
            log.exception("Infostock search bridge save failed")
            return False
        return QDesktopServices.openUrl(
            QUrl.fromLocalFile(os.fspath(bridge_path)))

    def set_original_link(
            self, original_url: str = "", search_url: str = "",
            resolving: bool = False):
        original = QUrl(str(original_url or "").strip())
        search = QUrl(str(search_url or "").strip())
        self._original_url = (
            original.toString()
            if original.isValid()
            and original.scheme().lower() in ("http", "https") else "")
        self._original_search_url = (
            search.toString()
            if search.isValid()
            and search.scheme().lower() in ("http", "https") else "")
        if self._original_url:
            self._original_button.setText("원문 열기 ↗")
            self._original_button.setToolTip(
                f"확인된 언론사 원문을 기본 브라우저로 엽니다.\n"
                f"{self._original_url}")
            self._original_button.setEnabled(True)
        elif resolving:
            self._original_button.setText("원문 확인 중…")
            self._original_button.setToolTip(
                "제목과 언론사를 대조해 정확한 원문 주소를 확인하고 있습니다.")
            self._original_button.setEnabled(False)
        else:
            search_name = LS_NEWS_SOURCE_SEARCH_HOST_NAMES.get(
                search.host().casefold(), "")
            self._original_button.setText(
                f"{search_name} 검색 ↗" if search_name else "원문 검색")
            self._original_button.setToolTip(
                (
                    f"정확한 원문 주소가 없어 {search_name}에서 "
                    "제목을 검색합니다.\n"
                    f"{self._original_search_url}"
                )
                if search_name else
                "정확한 원문 주소가 없어 제목으로 뉴스 검색 결과를 엽니다."
            )
            self._original_button.setEnabled(bool(self._original_search_url))

    def _open_original_or_search(self):
        target = self._original_url or self._original_search_url
        if target:
            self._open_news_link(QUrl(target))

    def set_error(self, message: str):
        self._status_label.setText(str(message or "본문을 불러오지 못했습니다."))
        self._status_label.setStyleSheet(
            "QLabel { color: #F06A6A; font-weight: 700; }")
        self._refresh_button.setEnabled(True)

class RealtimeNewsTabMixin:
    """LS 실시간 뉴스 수신, 표시, 검색과 본문 상세를 담당한다."""

    def _build_ls_realtime_news_page(self, layout: QVBoxLayout):
        """LS NWS 전체 제목을 수신 즉시 최신순으로 보여주는 최소 화면."""
        status_row = QHBoxLayout()
        self._ls_news_connection = QLabel("● 연결 준비")
        self._ls_news_connection.setStyleSheet(
            "QLabel { color: #8A94A6; font-weight: 700; }")
        self._ls_news_count = QLabel("수신 0건")
        self._ls_news_count.setToolTip(f"저장 위치: {DB_PATH}")
        self._ls_news_server_sync_status = QLabel("서버 ↻")
        self._ls_news_server_sync_status.setMinimumWidth(105)
        self._ls_news_server_sync_status.setAlignment(
            Qt.AlignmentFlag.AlignCenter)
        self._ls_news_server_sync_status.setStyleSheet(
            "QLabel { color: #8A94A6; background-color: #292D33; "
            "border: 1px solid #4B525C; border-radius: 3px; "
            "padding: 2px 6px; font-weight: 700; }")
        self._ls_news_server_sync_status.setToolTip(
            "앱이 꺼져 있던 동안 우분투 DB에 쌓인 뉴스의 확인을 기다립니다.")
        self.statusBar().addPermanentWidget(
            self._ls_news_server_sync_status)
        self._ls_news_search = QLineEdit()
        self._ls_news_search.setPlaceholderText(
            "검색 · 공백=모두 · |=하나라도 · -=제외")
        self._ls_news_search.setClearButtonEnabled(True)
        self._ls_news_search.setMinimumWidth(240)
        self._ls_news_search.setToolTip(
            "현재 화면의 제목·종목명·종목코드·뉴스출처를 검색합니다.\n"
            "\n검색 방법\n"
            "• 반도체 실적 → 모두 포함\n"
            "• 반도체 | 배터리 → 하나라도 포함\n"
            "• 반도체 -미국 → 미국 제외\n"
            "• \"유상증자 결정\" → 정확한 문구 포함\n"
            "\n제외어 앞은 공백으로 구분합니다.\n"
            "Enter 또는 DB 검색 버튼: 전체 DB 검색 · Esc: 검색 해제")
        self._ls_news_search.textChanged.connect(
            self._ls_news_search_text_changed)
        self._ls_news_search.returnPressed.connect(
            lambda: QTimer.singleShot(0, self._start_ls_news_db_search))
        self._ls_news_db_search_button = QPushButton("검색")
        self._ls_news_db_search_button.setFixedWidth(72)
        self._ls_news_db_search_button.setToolTip(
            "입력한 검색어로 저장된 LS 뉴스 전체를 검색합니다.\n"
            "최신순으로 최대 500건을 표시합니다.")
        self._ls_news_db_search_button.clicked.connect(
            self._start_ls_news_db_search)
        self._ls_news_search_clear_shortcut = QShortcut(
            QKeySequence(Qt.Key.Key_Escape), self._ls_news_search)
        self._ls_news_search_clear_shortcut.setContext(
            Qt.ShortcutContext.WidgetShortcut)
        self._ls_news_search_clear_shortcut.activated.connect(
            self._ls_news_search.clear)
        self._ls_news_sound = QCheckBox("소리")
        self._ls_news_sound.setToolTip(
            "체크하면 새 실시간 뉴스가 화면에 추가될 때 알림음을 재생합니다.\n"
            "종목코드 있음: "
            r"C:\KiwoomHero4\sound\sound8.wav"
            "\n종목코드 없음: "
            r"C:\KiwoomHero4\sound\sound11.wav"
            "\n네이버 API 관심종목 새 뉴스: "
            r"C:\KiwoomHero4\sound\sound12.wav")
        self._ls_news_sound.setChecked(
            str(self._settings.value(
                "analysis_ls_news_sound", "false"
            )).strip().lower() in {"1", "true", "yes"}
        )
        self._ls_news_sound.toggled.connect(self._set_ls_news_sound)
        self._ls_news_stock_only = QCheckBox("종목")
        self._ls_news_stock_only.setToolTip(
            "종목코드가 연결된 뉴스만 표시합니다.")
        self._ls_news_stock_only.setChecked(
            str(self._settings.value(
                "analysis_ls_news_stock_only", "false"
            )).strip().lower() in {"1", "true", "yes"}
        )
        self._ls_news_stock_only.toggled.connect(
            self._set_ls_news_stock_filter)
        self._ls_news_clear_new_button = QPushButton("신규해제")
        self._ls_news_clear_new_button.setEnabled(False)
        self._ls_news_clear_new_button.setToolTip(
            "새 실시간 뉴스의 강조색만 해제합니다.\n"
            "뉴스 목록과 DB 저장 데이터는 그대로 유지합니다.")
        self._ls_news_clear_new_button.clicked.connect(
            self._clear_ls_news_new_markers)
        self._ls_news_watched_only = QCheckBox("관심종목")
        self._ls_news_watched_only.setToolTip(
            "체크하면 뉴스에 연결된 전체 종목코드 중 하나라도\n"
            "관심종목에 포함된 뉴스만 표시합니다.\n"
            "대표종목이 아닌 관련 종목도 필터에 포함됩니다.")
        self._ls_news_watched_only.setChecked(
            str(self._settings.value(
                "analysis_ls_news_watched_only", "false"
            )).strip().lower() in {"1", "true", "yes"}
        )
        self._ls_news_watched_only.toggled.connect(
            self._set_ls_news_watched_filter)
        status_row.addWidget(self._ls_news_connection)
        status_row.addWidget(self._ls_news_count)
        status_row.addWidget(self._ls_news_search, 1)
        status_row.addWidget(self._ls_news_db_search_button)
        status_row.addWidget(self._ls_news_sound)
        status_row.addWidget(self._ls_news_stock_only)
        status_row.addWidget(self._ls_news_watched_only)
        status_row.addWidget(self._ls_news_clear_new_button)
        layout.addLayout(status_row)

        self._ls_news_table = QTableWidget(0, 5)
        self._ls_news_table.setHorizontalHeaderLabels(
            ("번호", "시간", "종목명", "제목", "뉴스출처"))
        self._ls_news_table.horizontalHeaderItem(2).setToolTip(
            "좌클릭: 대표 종목코드 복사\n"
            "우클릭: 대표 종목을 관심종목에 추가하고 종토방 열기")
        self._ls_news_table.horizontalHeaderItem(3).setToolTip(
            "뉴스 제목을 클릭하면 본문 상세창을 엽니다.")
        self._ls_news_table.setSortingEnabled(False)
        self._ls_news_table.setAlternatingRowColors(True)
        self._ls_news_table.setWordWrap(False)
        self._ls_news_table.setEditTriggers(
            QTableWidget.EditTrigger.NoEditTriggers)
        self._ls_news_table.setSelectionBehavior(
            QTableWidget.SelectionBehavior.SelectRows)
        self._ls_news_table.setSelectionMode(
            QTableWidget.SelectionMode.SingleSelection)
        self._ls_news_table.verticalHeader().setVisible(False)
        self._ls_news_table.verticalHeader().setDefaultSectionSize(24)
        header = self._ls_news_table.horizontalHeader()
        header.setSectionsMovable(False)
        header.setStretchLastSection(False)
        header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        saved_header = self._settings.value("analysis_ls_news_header_v2")
        if saved_header is None or not header.restoreState(saved_header):
            for column, width in enumerate((48, 105, 120, 650, 120)):
                self._ls_news_table.setColumnWidth(column, width)
        self._ls_news_header_timer = QTimer(self)
        self._ls_news_header_timer.setSingleShot(True)
        self._ls_news_header_timer.timeout.connect(
            self._save_ls_news_header)
        header.sectionResized.connect(
            lambda *_: self._ls_news_header_timer.start(400))
        self._ls_news_table.cellClicked.connect(
            self._ls_news_table_clicked)
        self._ls_news_table.cellDoubleClicked.connect(
            self._ls_news_table_double_clicked)
        self._ls_news_table.setContextMenuPolicy(
            Qt.ContextMenuPolicy.CustomContextMenu)
        self._ls_news_table.customContextMenuRequested.connect(
            self._ls_news_table_context_menu)
        layout.addWidget(self._ls_news_table, 1)

        self._ls_news_received = 0
        self._ls_news_new_count = 0
        self._ls_news_db_error = ""
        self._ls_news_loading_saved = False
        self._ls_news_stock_names: dict[str, str] = {}
        try:
            self._ls_news_watched_codes = realtime_watch_codes()
        except Exception as error:  # noqa: BLE001 - 뉴스 수신 화면은 계속 연다.
            self._ls_news_watched_codes = set()
            log.warning("LS news watched codes load failed: %s", error)
        self._ls_news_seen_keys: set[str] = set()
        self._ls_news_seen_order: deque[str] = deque()
        self._ls_news_source_names = dict(NEWS_SOURCE_NAMES)
        try:
            saved_sources = json.loads(str(self._settings.value(
                "analysis_ls_news_sources", "{}") or "{}"))
        except (TypeError, ValueError, json.JSONDecodeError):
            saved_sources = {}
        if isinstance(saved_sources, dict):
            self._ls_news_source_names.update({
                str(source_id).strip(): str(name).strip()
                for source_id, name in saved_sources.items()
                if (
                    str(source_id).strip()
                    and str(name).strip()
                    and str(source_id).strip() not in NEWS_SOURCE_NAMES
                )
            })
        self._ls_news_source_pending: set[str] = set()
        self._ls_news_source_attempts: dict[str, int] = {}
        self._ls_news_source_tasks: set[asyncio.Task] = set()

    def _start_ls_news_stream(self):
        if self._ls_news_task and not self._ls_news_task.done():
            return
        self._ls_news_stream = LSNewsStream()
        self._ls_news_task = asyncio.ensure_future(
            self._ls_news_stream.run(
                self._append_ls_news, self._set_ls_news_status))

    def _schedule_ls_news_server_sync(self):
        """LS 연결 뒤 서버 커서부터 누락 제목을 한 번 보완한다."""
        if (
            self._ls_news_server_sync_task is not None
            and not self._ls_news_server_sync_task.done()
        ):
            self._ls_news_server_sync_pending = True
            return
        self._ls_news_server_sync_pending = False
        self._ls_news_server_sync_task = asyncio.ensure_future(
            self._sync_ls_news_from_server())

    def _set_ls_news_server_sync_status(
            self, text: str, state: str, tooltip: str = ""):
        if not hasattr(self, "_ls_news_server_sync_status"):
            return
        color = {
            "waiting": "#8A94A6",
            "running": "#D5A33D",
            "completed": "#55C981",
            "failed": "#F06A6A",
            "disabled": "#8A94A6",
        }.get(state, "#8A94A6")
        background = {
            "running": "#3A321D",
            "completed": "#1E3828",
            "failed": "#422327",
        }.get(state, "#292D33")
        self._ls_news_server_sync_status.setText(text)
        self._ls_news_server_sync_status.setStyleSheet(
            f"QLabel {{ color: {color}; background-color: {background}; "
            "border: 1px solid #4B525C; border-radius: 3px; "
            "padding: 2px 6px; font-weight: 700; }")
        self._ls_news_server_sync_status.setToolTip(tooltip)

    def _show_ls_news_server_sync_progress(self, result: dict):
        upper = int(result.get("upper_id") or 0)
        cursor = int(result.get("cursor") or 0)
        remaining = max(0, upper - cursor)
        processed = int(result.get("processed") or 0)
        inserted = int(result.get("inserted") or 0)
        updated = int(result.get("updated") or 0)
        self._set_ls_news_server_sync_status(
            f"서버 … {processed:,}",
            "running",
            f"서버 커서 {cursor:,} / 시작 시 상한 {upper:,}\n"
            f"확인 {processed:,}건 · 누락 저장 {inserted:,}건 "
            f"· 중복 {updated:,}건\n남은 ID 약 {remaining:,}",
        )
        self.statusBar().showMessage(
            f"서버 누락 뉴스 동기화 · 확인 {processed:,}건"
            f" · 신규 저장 {inserted:,}건"
            f" · 남은 ID 약 {remaining:,}")

    async def _sync_ls_news_from_server(self):
        try:
            self._set_ls_news_server_sync_status(
                "서버 …", "running",
                "우분투 서버의 마지막 뉴스 ID를 확인하고 있습니다.")
            result = await LSNewsServerSync().sync(
                self._show_ls_news_server_sync_progress)
            if result.get("status") == "disabled":
                self._set_ls_news_server_sync_status(
                    "서버 -", "disabled",
                    "LS_NEWS_SYNC_ENABLED 설정이 꺼져 있습니다.")
                return
            processed = int(result.get("processed") or 0)
            inserted = int(result.get("inserted") or 0)
            updated = int(result.get("updated") or 0)
            upper = int(result.get("upper_id") or 0)
            cursor = int(result.get("cursor") or 0)
            if processed:
                # 수만 건을 표에 직접 추가하지 않고 저장 완료 뒤 최신 500건만 갱신한다.
                self._reload_ls_news_current_rows(preserve_new=True)
            completed_now = datetime.now()
            label = f"서버 ✓ {completed_now:%H:%M}"
            self._set_ls_news_server_sync_status(
                label,
                "completed",
                f"완료 시각 {completed_now:%Y-%m-%d %H:%M:%S}\n"
                f"확인 {processed:,}건 · 누락 저장 {inserted:,}건 "
                f"· 중복 {updated:,}건\n"
                f"서버 커서 {cursor:,} / 시작 시 상한 {upper:,}",
            )
            self.statusBar().showMessage(
                f"서버 누락 뉴스 동기화 완료 · 확인 {processed:,}건"
                f" · 누락 저장 {inserted:,}건 · 중복 {updated:,}건",
                30000,
            )
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001 - LS 실시간 수신은 계속한다.
            log.warning("LS server gap sync failed: %s", error)
            self._set_ls_news_server_sync_status(
                "서버 !", "failed",
                f"{datetime.now():%Y-%m-%d %H:%M:%S}\n{error}",
            )
            self.statusBar().showMessage(
                f"서버 누락 뉴스 동기화 실패: {error}", 30000)
        finally:
            self._ls_news_server_sync_task = None
            if self._ls_news_server_sync_pending:
                self._ls_news_server_sync_pending = False
                QTimer.singleShot(0, self._schedule_ls_news_server_sync)

    def _set_ls_news_status(self, state: str, message: str):
        if not hasattr(self, "_ls_news_connection"):
            return
        color = {
            "connected": "#39B86B",
            "connecting": "#D59B2D",
            "retrying": "#E05D5D",
            "missing": "#E05D5D",
            "stopped": "#8A94A6",
        }.get(state, "#8A94A6")
        self._ls_news_connection.setText(f"● {message}")
        self._ls_news_connection.setStyleSheet(
            f"QLabel {{ color: {color}; font-weight: 700; }}")
        if state in {"connected", "missing"}:
            # 연결을 먼저 확보해 동기화 도중 도착하는 새 뉴스도 놓치지 않는다.
            self._schedule_ls_news_server_sync()

    def _set_latest_ls_news_highlight(self, highlighted: bool):
        if not hasattr(self, "_latest_ls_news_label"):
            return
        if highlighted:
            background = "#315C72"
            border = "#79D2FF"
        else:
            background = "#20303A"
            border = "#52788B"
        self._latest_ls_news_label.setStyleSheet(
            "QLabel {"
            " color: #FFFFFF;"
            f" background-color: {background};"
            f" border: 3px solid {border};"
            " border-radius: 7px;"
            " padding: 8px 14px;"
            " font-size: 24px;"
            " font-weight: 900;"
            "}")

    def _clear_latest_ls_news_highlight(self):
        self._set_latest_ls_news_highlight(False)

    def _show_latest_ls_news(self, context: dict):
        title = " ".join(str(context.get("title") or "").split())
        if not title:
            return
        self._latest_ls_news_context = {
            **dict(context), "provider": "LS",
        }
        self._latest_ls_news_label.set_headline(title)
        self._set_latest_ls_news_highlight(True)
        self._latest_ls_news_highlight_timer.start(3500)

    def _open_latest_ls_news(self):
        if not self._latest_ls_news_context:
            return
        context = dict(self._latest_ls_news_context)
        if context.get("provider") == "TELEGRAM":
            # 앱 우선으로 열도록 텔레그램 탭의 공용 경로를 쓴다.
            self.open_telegram_post(str(context.get("url") or ""))
            return
        if context.get("provider") == "NAVER":
            stock_code = str(context.get("stock_code") or "").strip()
            if stock_code:
                self.open_realtime_watch(stock_code, fetch_news=False)
            url = str(context.get("url") or "").strip()
            if url:
                self._show_news_web_url(url, "article")
            return
        self.open_ls_realtime_news()
        self._open_ls_news_detail(context)

    def _append_ls_news(self, item: LSNewsItem, persist: bool = True,
                        news_key: str = "", original_url: str = ""):
        if not hasattr(self, "_ls_news_table"):
            return
        dedupe_key = item.realkey or (
            f"{item.date}|{item.time}|{item.source_id}|{item.title}")
        if dedupe_key in self._ls_news_seen_keys:
            return
        self._ls_news_seen_keys.add(dedupe_key)
        self._ls_news_seen_order.append(dedupe_key)
        while len(self._ls_news_seen_order) > 5000:
            self._ls_news_seen_keys.discard(
                self._ls_news_seen_order.popleft())

        source_name = self._ls_news_source_names.get(
            item.source_id, source_label(item.source_id))
        if persist:
            try:
                saved = save_ls_realtime_news(
                    {
                        "date": item.date,
                        "time": item.time,
                        "title": item.title,
                        "source_id": item.source_id,
                        "source_name": source_name,
                        "realkey": item.realkey,
                        "code": item.code,
                        "body_size": item.body_size,
                    },
                    ensure_schema=False,
                )
                news_key = str(saved.get("news_key") or news_key)
                self._ls_news_db_error = ""
            except Exception as error:  # noqa: BLE001 - 화면 수신은 유지한다.
                self._ls_news_db_error = str(error)
                log.exception("LS realtime news DB save failed")

        valid_stock_codes = tuple(
            code for code in split_ls_news_stock_codes(item.code)
            if len(code) == 6 and code.isdigit()
        )
        if persist:
            self._ls_news_received += 1
            self._schedule_ls_news_source_resolution(item)
        if self._ls_news_stock_only.isChecked() and not valid_stock_codes:
            if not self._ls_news_loading_saved:
                self._update_ls_news_count()
            return
        if (
            self._ls_news_watched_only.isChecked()
            and not any(
                code in self._ls_news_watched_codes
                for code in valid_stock_codes
            )
        ):
            if not self._ls_news_loading_saved:
                self._update_ls_news_count()
            return

        table = self._ls_news_table
        was_at_top = table.verticalScrollBar().value() <= 1
        table.insertRow(0)
        number_item = NumericTableWidgetItem("1", 1)
        number_item.setTextAlignment(
            Qt.AlignmentFlag.AlignCenter | Qt.AlignmentFlag.AlignVCenter)
        time_item = QTableWidgetItem(format_news_time(item.date, item.time))
        time_item.setTextAlignment(
            Qt.AlignmentFlag.AlignCenter | Qt.AlignmentFlag.AlignVCenter)
        stock_label, stock_codes, stock_tooltip = (
            self._ls_news_stock_display(
                valid_stock_codes
                if self._ls_news_stock_only.isChecked() else item.code))
        stock_item = QTableWidgetItem(stock_label)
        stock_item.setTextAlignment(
            Qt.AlignmentFlag.AlignCenter | Qt.AlignmentFlag.AlignVCenter)
        stock_item.setData(Qt.ItemDataRole.UserRole, list(stock_codes))
        stock_item.setData(Qt.ItemDataRole.UserRole + 1, stock_tooltip)
        if stock_codes:
            stock_tooltip += (
                "\n\n좌클릭: 대표 종목코드 복사"
                "\n우클릭: 대표 종목 관심종목 추가 · 종토방 열기")
        stock_item.setToolTip(stock_tooltip)
        title_item = QTableWidgetItem(item.title)
        title_item.setToolTip(
            f"{item.title}\n\n클릭: 앱 본문 열기\n"
            "더블클릭·Ctrl+클릭: 확인된 원문을 기본 브라우저로 열기")
        title_item.setData(Qt.ItemDataRole.UserRole, item.realkey)
        title_item.setData(Qt.ItemDataRole.UserRole + 1,
                           news_key or item.realkey)
        title_item.setData(
            Qt.ItemDataRole.UserRole + 2, str(original_url or "").strip())
        source_item = QTableWidgetItem(source_name)
        source_item.setTextAlignment(
            Qt.AlignmentFlag.AlignCenter | Qt.AlignmentFlag.AlignVCenter)
        source_item.setData(Qt.ItemDataRole.UserRole, item.source_id)
        source_item.setToolTip(
            f"LS 뉴스 매체 식별자: {item.source_id or '-'}")
        table.setItem(0, 0, number_item)
        table.setItem(0, 1, time_item)
        table.setItem(0, 2, stock_item)
        table.setItem(0, 3, title_item)
        table.setItem(0, 4, source_item)
        matches_search = self._ls_news_row_matches_search(0)
        table.setRowHidden(0, not matches_search)
        if self._ls_news_db_search_active and not matches_search:
            table.removeRow(0)
            if not self._ls_news_loading_saved:
                self._update_ls_news_count()
            return
        if persist and matches_search:
            self._mark_ls_news_row_new(0)
            context = self._ls_news_row_context(0)
            if context is not None:
                self._show_latest_ls_news(context)

        while table.rowCount() > LS_NEWS_VISIBLE_LIMIT:
            last_row = table.rowCount() - 1
            if self._ls_news_row_is_new(last_row):
                self._ls_news_new_count = max(
                    0, self._ls_news_new_count - 1)
            table.removeRow(last_row)
        if not self._ls_news_loading_saved:
            self._renumber_ls_news_table()
        self._update_ls_news_new_button()
        if not self._ls_news_loading_saved:
            self._update_ls_news_count()
        if was_at_top:
            table.scrollToTop()
        if (
            persist and matches_search and self._ls_news_sound.isChecked()
        ):
            _beep(
                "ls_news_with_code"
                if valid_stock_codes else "ls_news_without_code")

    def _load_saved_ls_news(self):
        """저장된 최근 LS 뉴스를 현재 목록에 최신순으로 복원한다."""
        try:
            rows = ls_realtime_news_rows(
                LS_NEWS_VISIBLE_LIMIT,
                stock_only=self._ls_news_stock_only.isChecked(),
                watched_only=self._ls_news_watched_only.isChecked(),
            )
            self._ls_news_db_error = ""
        except Exception as error:  # noqa: BLE001 - 실시간 연결은 계속한다.
            self._ls_news_db_error = str(error)
            log.exception("saved LS realtime news load failed")
            self._update_ls_news_count()
            return
        self._append_ls_news_rows(rows)
        self._update_ls_news_count()

    def _append_ls_news_rows(self, rows: list[dict]):
        """DB 조회 행을 종목·출처 캐시와 함께 현재 표에 추가한다."""
        for row in rows:
            source_id = str(row.get("source_id") or "").strip()
            source_name = str(row.get("source_name") or "").strip()
            stock_code = str(row.get("stock_code") or "").strip()
            stock_name = str(row.get("stock_name") or "").strip()
            if stock_code and stock_name:
                self._ls_news_stock_names[stock_code] = stock_name
            if (
                source_id and source_name
                and source_id not in NEWS_SOURCE_NAMES
                and source_name != source_label(source_id)
            ):
                self._ls_news_source_names[source_id] = source_name
        self._ls_news_loading_saved = True
        try:
            for row in reversed(rows):
                related_codes = str(
                    row.get("related_stock_codes") or "").strip()
                code_value = (
                    related_codes
                    if split_ls_news_stock_codes(related_codes)
                    else str(row.get("stock_code") or "")
                )
                self._append_ls_news(
                    LSNewsItem(
                        date=str(row.get("news_date") or ""),
                        time=str(row.get("news_time") or ""),
                        title=str(row.get("title") or ""),
                        source_id=str(row.get("source_id") or ""),
                        realkey=str(row.get("realkey") or ""),
                        code=code_value,
                        body_size=int(row.get("body_size") or 0),
                    ),
                    persist=False,
                    news_key=str(row.get("news_key") or ""),
                    original_url=str(row.get("original_url") or ""),
                )
        finally:
            self._ls_news_loading_saved = False
            self._renumber_ls_news_table()

    def _renumber_ls_news_table(self):
        """현재 표시 순서대로 실시간 뉴스 행 번호를 다시 매긴다."""
        if not hasattr(self, "_ls_news_table"):
            return
        table = self._ls_news_table
        visible_number = 0
        for row in range(table.rowCount()):
            item = table.item(row, 0)
            if item is None:
                item = NumericTableWidgetItem("", 0)
                table.setItem(row, 0, item)
            if table.isRowHidden(row):
                item.setText("")
                item.setData(Qt.ItemDataRole.UserRole, 0)
            else:
                visible_number += 1
                item.setText(str(visible_number))
                item.setData(Qt.ItemDataRole.UserRole, visible_number)
            item.setTextAlignment(
                Qt.AlignmentFlag.AlignCenter | Qt.AlignmentFlag.AlignVCenter)

    def _set_ls_news_stock_filter(self, checked: bool):
        """전체·종목 뉴스 표시를 전환하고 DB의 최근 목록을 다시 채운다."""
        self._settings.setValue(
            "analysis_ls_news_stock_only", "true" if checked else "false")
        self._settings.sync()
        self._reload_ls_news_after_filter_change()

    def _set_ls_news_watched_filter(self, checked: bool):
        """연결된 전체 종목 중 관심종목이 있는 뉴스만 표시한다."""
        self._settings.setValue(
            "analysis_ls_news_watched_only", "true" if checked else "false")
        self._settings.sync()
        self._sync_ls_news_watched_codes(reload=False)
        self._reload_ls_news_after_filter_change()

    def _sync_ls_news_watched_codes(
            self, watched_codes=None, *, reload: bool = True):
        """관심종목 캐시를 갱신하고 활성 필터 목록을 즉시 다시 채운다."""
        try:
            current = (
                realtime_watch_codes()
                if watched_codes is None else set(watched_codes)
            )
        except Exception as error:  # noqa: BLE001 - 기존 캐시를 유지한다.
            log.warning("LS news watched codes refresh failed: %s", error)
            return
        normalized = {
            str(code).strip() for code in current
            if len(str(code).strip()) == 6 and str(code).strip().isdigit()
        }
        changed = normalized != self._ls_news_watched_codes
        self._ls_news_watched_codes = normalized
        if (
            changed and reload
            and hasattr(self, "_ls_news_watched_only")
            and self._ls_news_watched_only.isChecked()
        ):
            self._reload_ls_news_after_filter_change()

    def _reload_ls_news_after_filter_change(self):
        """종목·관심종목 필터 변경을 최근 목록과 DB 검색에 함께 반영한다."""
        if not hasattr(self, "_ls_news_table"):
            return
        search_query = self._ls_news_search.text().strip()
        rerun_db_search = bool(search_query) and (
            self._ls_news_db_search_active
            or (
                self._ls_news_search_task is not None
                and not self._ls_news_search_task.done()
            )
        )
        self._cancel_ls_news_db_search()
        self._ls_news_db_search_active = False
        self._ls_news_db_search_query = ""
        self._reload_ls_news_current_rows()
        if rerun_db_search:
            QTimer.singleShot(0, self._start_ls_news_db_search)

    def _set_ls_news_sound(self, checked: bool):
        """LS 실시간 뉴스 알림음 사용 여부를 저장한다."""
        self._settings.setValue(
            "analysis_ls_news_sound", "true" if checked else "false")
        self._settings.sync()

    def _reload_ls_news_current_rows(self, preserve_new: bool = False):
        """현재 필터의 최신 500건을 복원하며 필요하면 신규 강조를 보존한다."""
        if not hasattr(self, "_ls_news_table"):
            return
        new_keys = set()
        if preserve_new:
            for row in range(self._ls_news_table.rowCount()):
                if not self._ls_news_row_is_new(row):
                    continue
                title_item = self._ls_news_table.item(row, 3)
                if title_item is not None:
                    key = str(
                        title_item.data(Qt.ItemDataRole.UserRole + 1)
                        or title_item.data(Qt.ItemDataRole.UserRole)
                        or ""
                    ).strip()
                    if key:
                        new_keys.add(key)
        self._ls_news_table.setUpdatesEnabled(False)
        try:
            self._ls_news_table.setRowCount(0)
            self._reset_ls_news_new_markers_state()
            self._ls_news_seen_keys.clear()
            self._ls_news_seen_order.clear()
            self._load_saved_ls_news()
            if new_keys:
                for row in range(self._ls_news_table.rowCount()):
                    title_item = self._ls_news_table.item(row, 3)
                    if title_item is None:
                        continue
                    key = str(
                        title_item.data(Qt.ItemDataRole.UserRole + 1)
                        or title_item.data(Qt.ItemDataRole.UserRole)
                        or ""
                    ).strip()
                    if key in new_keys:
                        self._mark_ls_news_row_new(row)
        finally:
            self._ls_news_table.setUpdatesEnabled(True)
        self._ls_news_table.scrollToTop()

    def _cancel_ls_news_db_search(self):
        """진행 중인 전체 DB 검색 결과가 화면에 반영되지 않게 취소한다."""
        self._ls_news_search_serial += 1
        task = self._ls_news_search_task
        self._ls_news_search_task = None
        if task is not None and not task.done():
            task.cancel()
        if hasattr(self, "_ls_news_db_search_button"):
            self._ls_news_db_search_button.setEnabled(True)
            self._ls_news_db_search_button.setText("검색")

    def _ls_news_search_text_changed(self, text: str):
        """검색어 수정 시 DB 검색 모드를 끝내고 최신 목록 필터로 돌아간다."""
        if self._ls_news_db_search_active:
            self._cancel_ls_news_db_search()
            self._ls_news_db_search_active = False
            self._ls_news_db_search_query = ""
            self._reload_ls_news_current_rows()
            return
        if self._ls_news_search_task is not None:
            self._cancel_ls_news_db_search()
        self._apply_ls_news_search_filter(text)

    def _start_ls_news_db_search(self):
        """Enter 입력 시 전체 LS 뉴스 DB 검색을 백그라운드에서 시작한다."""
        query = " ".join(self._ls_news_search.text().split())
        if not query:
            if self._ls_news_db_search_active:
                self._ls_news_db_search_active = False
                self._ls_news_db_search_query = ""
                self._reload_ls_news_current_rows()
            return
        self._cancel_ls_news_db_search()
        serial = self._ls_news_search_serial
        stock_only = self._ls_news_stock_only.isChecked()
        watched_only = self._ls_news_watched_only.isChecked()
        self._ls_news_count.setText(f"전체 DB에서 ‘{query}’ 검색 중…")
        self._ls_news_count.setToolTip(
            f"저장된 LS 뉴스 전체 검색 중\n검색어: {query}")
        task = asyncio.ensure_future(self._run_ls_news_db_search(
            serial, query, stock_only, watched_only))
        self._ls_news_search_task = task
        self._ls_news_db_search_button.setEnabled(False)
        self._ls_news_db_search_button.setText("검색 중…")

    async def _run_ls_news_db_search(
            self, serial: int, query: str, stock_only: bool,
            watched_only: bool):
        """SQLite 전체 검색을 UI 이벤트 루프 밖에서 실행하고 결과를 표시한다."""
        current_task = asyncio.current_task()
        try:
            rows = await asyncio.to_thread(
                search_ls_realtime_news,
                query,
                LS_NEWS_VISIBLE_LIMIT,
                stock_only=stock_only,
                watched_only=watched_only,
            )
            if (
                serial != self._ls_news_search_serial
                or " ".join(self._ls_news_search.text().split()) != query
                or self._ls_news_stock_only.isChecked() != stock_only
                or self._ls_news_watched_only.isChecked() != watched_only
            ):
                return
            self._ls_news_db_search_active = True
            self._ls_news_db_search_query = query
            self._ls_news_table.setUpdatesEnabled(False)
            try:
                self._ls_news_table.setRowCount(0)
                self._reset_ls_news_new_markers_state()
                self._ls_news_seen_keys.clear()
                self._ls_news_seen_order.clear()
                self._append_ls_news_rows(rows)
            finally:
                self._ls_news_table.setUpdatesEnabled(True)
            self._ls_news_table.scrollToTop()
            self._update_ls_news_count()
            suffix = " (최대 500건)" if len(rows) >= 500 else ""
            self.statusBar().showMessage(
                f"전체 DB 검색 완료 · {len(rows):,}건{suffix} · "
                "검색어를 지우면 실시간 목록으로 복귀합니다.",
                5000,
            )
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001 - 실시간 목록은 유지한다.
            if serial == self._ls_news_search_serial:
                log.exception("LS news full DB search failed")
                self.statusBar().showMessage(
                    f"전체 DB 검색 실패: {error}", 5000)
                self._update_ls_news_count()
        finally:
            if self._ls_news_search_task is current_task:
                self._ls_news_search_task = None
                self._ls_news_db_search_button.setEnabled(True)
                self._ls_news_db_search_button.setText("검색")

    def _ls_news_row_matches_search(self, row: int) -> bool:
        """현재 행이 포함·OR·제외 검색식과 일치하는지 확인한다."""
        include_groups, exclude_terms = parse_ls_news_search_query(
            self._ls_news_search.text())
        if not include_groups and not exclude_terms:
            return True
        stock_item = self._ls_news_table.item(row, 2)
        title_item = self._ls_news_table.item(row, 3)
        source_item = self._ls_news_table.item(row, 4)
        searchable = " ".join((
            str(stock_item.data(Qt.ItemDataRole.UserRole + 1) or "")
            if stock_item else "",
            title_item.text() if title_item else "",
            source_item.text() if source_item else "",
        )).casefold()
        included = all(
            any(term.casefold() in searchable for term in group)
            for group in include_groups
        )
        excluded = any(
            term.casefold() in searchable for term in exclude_terms)
        return included and not excluded

    def _apply_ls_news_search_filter(self, _text: str = ""):
        """검색어 변경 즉시 현재 500개 행의 표시 여부를 갱신한다."""
        if not hasattr(self, "_ls_news_table"):
            return
        for row in range(self._ls_news_table.rowCount()):
            self._ls_news_table.setRowHidden(
                row, not self._ls_news_row_matches_search(row))
        self._renumber_ls_news_table()
        self._update_ls_news_count()

    def _update_ls_news_count(self):
        if not hasattr(self, "_ls_news_count"):
            return
        error_status = " · DB 저장 오류" if self._ls_news_db_error else ""
        if self._ls_news_db_search_active:
            self._ls_news_count.setText(
                f"수신 {self._ls_news_received:,}건 · 전체 DB 검색 "
                f"{self._ls_news_table.rowCount():,}건{error_status}")
            tooltip = (
                f"저장 위치: {DB_PATH}\n"
                f"전체 DB 검색어: {self._ls_news_db_search_query}\n"
                "최대 500건 · 검색어 수정/삭제 시 실시간 목록 복귀"
            )
            if self._ls_news_db_error:
                tooltip += f"\n최근 저장 오류: {self._ls_news_db_error}"
            self._ls_news_count.setToolTip(tooltip)
            return
        search_status = ""
        if self._ls_news_search.text().strip():
            visible_rows = sum(
                not self._ls_news_table.isRowHidden(row)
                for row in range(self._ls_news_table.rowCount())
            )
            search_status = f" · 검색 {visible_rows:,}건"
        self._ls_news_count.setText(
            f"수신 {self._ls_news_received:,}건 · 화면 최근 "
            f"{self._ls_news_table.rowCount():,}건{search_status}"
            f"{error_status}")
        tooltip = f"저장 위치: {DB_PATH}"
        if self._ls_news_db_error:
            tooltip += f"\n최근 저장 오류: {self._ls_news_db_error}"
        self._ls_news_count.setToolTip(tooltip)

    def _ls_news_row_context(self, row: int) -> dict | None:
        title_item = self._ls_news_table.item(row, 3)
        if title_item is None:
            return None
        realkey = str(
            title_item.data(Qt.ItemDataRole.UserRole) or "").strip()
        news_key = str(
            title_item.data(Qt.ItemDataRole.UserRole + 1) or "").strip()
        time_item = self._ls_news_table.item(row, 1)
        stock_item = self._ls_news_table.item(row, 2)
        source_item = self._ls_news_table.item(row, 4)
        context = {
            "title": title_item.text(),
            "time": time_item.text() if time_item else "-",
            "stocks": stock_item.toolTip() if stock_item else "연결 종목 없음",
            "stock_codes": (
                stock_item.data(Qt.ItemDataRole.UserRole)
                if stock_item else []),
            "source_name": source_item.text() if source_item else "출처 미상",
            "source_id": (
                str(source_item.data(Qt.ItemDataRole.UserRole) or "")
                if source_item else ""),
            "realkey": realkey,
            "news_key": news_key,
            "original_url": str(
                title_item.data(Qt.ItemDataRole.UserRole + 2)
                or "").strip(),
        }
        search_source = (
            "로이터" if context["source_id"] == "28"
            else context["source_name"]
        )
        context["search_url"] = self._ls_news_search_url(
            context["title"], search_source)
        return context

    @staticmethod
    def _ls_news_search_url(
            title: str, source_name: str = "") -> str:
        def encoded_search_url(
                endpoint: str,
                items: tuple[tuple[str, str], ...]) -> str:
            url = QUrl(endpoint)
            encoded = bytes(url.toEncoded()).decode("ascii")
            separator = "&" if url.hasQuery() else "?"
            encoded_items = "&".join(
                f"{bytes(QUrl.toPercentEncoding(name)).decode('ascii')}="
                f"{bytes(QUrl.toPercentEncoding(value)).decode('ascii')}"
                for name, value in items
            )
            return f"{encoded}{separator}{encoded_items}"

        title = str(title or "").strip()
        source_name = str(source_name or "").strip()
        if source_name == "로이터":
            # 로이터는 번역된 한글 송고 제목으로 검색하지 않는다. 본문에
            # 포함된 '원문 바로가기' 주소가 확인될 때만 원문 버튼을 켠다.
            return ""
        source_search = LS_NEWS_SOURCE_SEARCH_ENDPOINTS.get(source_name)
        if title and source_search:
            endpoint, query_name = source_search
            return encoded_search_url(endpoint, ((query_name, title),))

        terms = [title]
        if (
            source_name and source_name != "출처 미상"
            and not source_name.startswith("매체 ")
        ):
            terms.append(source_name)
        query_text = " ".join(term for term in terms if term)
        if not query_text:
            return ""
        return encoded_search_url(
            "https://search.naver.com/search.naver",
            (("where", "news"), ("query", query_text)),
        )

    def _open_ls_news_original_from_context(self, context: dict) -> bool:
        original_url = str(context.get("original_url") or "").strip()
        if not original_url:
            try:
                saved = ls_realtime_news_detail(
                    realkey=str(context.get("realkey") or ""),
                    news_key=str(context.get("news_key") or ""),
                )
            except Exception:  # noqa: BLE001 - 일반 본문 열기로 계속한다.
                saved = None
            original_url = str(
                (saved or {}).get("original_url") or "").strip()
        url = QUrl(original_url)
        if (
            not original_url or not url.isValid()
            or url.scheme().lower() not in ("http", "https")
        ):
            return False
        context["original_url"] = original_url
        QDesktopServices.openUrl(url)
        return True

    def _ls_news_table_clicked(self, row: int, column: int):
        if column == 2:
            representative = self._ls_news_representative_stock(row)
            if representative is None:
                return
            code, stock_name = representative
            QApplication.clipboard().setText(code)
            self.statusBar().showMessage(
                f"{stock_name} 대표 종목코드 {code} 복사됨", 2500)
            return
        if column != 3:
            return
        context = self._ls_news_row_context(row)
        if context is None:
            return
        if (
            QApplication.keyboardModifiers()
            & Qt.KeyboardModifier.ControlModifier
            and self._open_ls_news_original_from_context(context)
        ):
            return
        self._open_ls_news_detail(context)

    def _ls_news_table_double_clicked(self, row: int, column: int):
        if column != 3:
            return
        context = self._ls_news_row_context(row)
        if context is not None:
            self._open_ls_news_original_from_context(context)

    def _ls_news_representative_stock(
            self, row: int) -> tuple[str, str] | None:
        """뉴스 행에 연결된 첫 번째 유효 종목코드와 종목명을 반환한다."""
        stock_item = self._ls_news_table.item(row, 2)
        if stock_item is None:
            return None
        stock_codes = tuple(
            code for code in split_ls_news_stock_codes(
                stock_item.data(Qt.ItemDataRole.UserRole))
            if len(code) == 6 and code.isdigit()
        )
        if not stock_codes:
            return None
        code = stock_codes[0]
        stock_name = str(self._ls_news_stock_names.get(code) or "").strip()
        if not stock_name or stock_name == code:
            stock = resolve_analysis_stock(code)
            resolved_name = str(
                (stock or {}).get("stock_name") or "").strip()
            if resolved_name:
                stock_name = resolved_name
                self._ls_news_stock_names[code] = resolved_name
        return code, stock_name or code

    def _ls_news_table_context_menu(self, position):
        """종목명 우클릭 시 대표 종목을 감시에 넣고 종토방으로 이동한다."""
        item = self._ls_news_table.itemAt(position)
        if item is None or item.column() != 2:
            return
        representative = self._ls_news_representative_stock(item.row())
        if representative is None:
            return
        code, stock_name = representative
        try:
            already_registered = code in realtime_watch_codes()
            if not already_registered:
                set_realtime_watch(
                    code, True, "LS_REALTIME_NEWS",
                    stock_name="" if stock_name == code else stock_name,
                )
        except ValueError as error:
            QMessageBox.warning(self, "관심종목 추가", str(error))
            return

        if not already_registered:
            self.watchlist_changed.emit()
        self.open_realtime_watch(code, fetch_news=False)
        action = "선택" if already_registered else "추가"
        self._news_status.setText(
            f"{stock_name}({code}) 관심종목 {action} · 종목토론을 열었습니다.")
        self.statusBar().showMessage(
            f"{stock_name} 관심종목 {action} · 종토방 이동", 3000)

    def _open_ls_news_detail(self, context: dict):
        realkey = str(context.get("realkey") or "").strip()
        news_key = str(context.get("news_key") or "").strip()
        window_key = news_key or realkey or (
            f"{context['time']}|{context['title']}")
        dialog = self._ls_news_detail_windows.get(window_key)
        if dialog is not None:
            dialog.show()
            dialog.raise_()
            dialog.activateWindow()
            return

        dialog = LSNewsDetailDialog(self)
        dialog.set_metadata(
            context["title"],
            f"{context['time']} · {context['source_name']}",
            context["stocks"],
        )
        dialog.set_original_link(
            str(context.get("original_url") or ""),
            str(context.get("search_url") or ""),
            resolving=not bool(context.get("original_url")),
        )
        dialog.refresh_requested.connect(
            lambda key=window_key, target=dialog, values=context:
            self._start_ls_news_detail_load(
                key, target, values, force=True))
        dialog.finished.connect(
            lambda _result, key=window_key, target=dialog:
            self._forget_ls_news_detail_window(key, target))
        self._ls_news_detail_windows[window_key] = dialog
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()
        self._start_ls_news_detail_load(window_key, dialog, context)
        self._start_ls_news_original_url_resolution(
            window_key, dialog, context)

    def _forget_ls_news_detail_window(
            self, window_key: str, dialog: LSNewsDetailDialog):
        if self._ls_news_detail_windows.get(window_key) is dialog:
            self._ls_news_detail_windows.pop(window_key, None)
        task = self._ls_news_detail_tasks.pop(window_key, None)
        if task is not None and not task.done():
            task.cancel()
        url_task = self._ls_news_url_tasks.pop(window_key, None)
        if url_task is not None and not url_task.done():
            url_task.cancel()

    def _start_ls_news_detail_load(
            self, window_key: str, dialog: LSNewsDetailDialog,
            context: dict, force: bool = False):
        existing = self._ls_news_detail_tasks.get(window_key)
        if existing is not None and not existing.done():
            return
        dialog.set_loading(
            "LS에서 본문 다시 불러오는 중…" if force
            else "본문 확인 중…")
        task = asyncio.ensure_future(
            self._load_ls_news_detail(
                window_key, dialog, context, force=force))
        self._ls_news_detail_tasks[window_key] = task
        task.add_done_callback(
            lambda completed, key=window_key:
            self._ls_news_detail_task_finished(key, completed))

    def _ls_news_detail_task_finished(
            self, window_key: str, task: asyncio.Task):
        if self._ls_news_detail_tasks.get(window_key) is task:
            self._ls_news_detail_tasks.pop(window_key, None)

    def _start_ls_news_original_url_resolution(
            self, window_key: str, dialog: LSNewsDetailDialog,
            context: dict):
        original_url = str(context.get("original_url") or "").strip()
        search_url = str(context.get("search_url") or "")
        if original_url:
            dialog.set_original_link(original_url, search_url)
            return
        existing = self._ls_news_url_tasks.get(window_key)
        if existing is not None and not existing.done():
            return
        dialog.set_original_link("", search_url, resolving=True)
        task = asyncio.ensure_future(
            self._resolve_ls_news_original_url(
                window_key, dialog, context))
        self._ls_news_url_tasks[window_key] = task
        task.add_done_callback(
            lambda completed, key=window_key:
            self._ls_news_url_task_finished(key, completed))

    def _ls_news_url_task_finished(
            self, window_key: str, task: asyncio.Task):
        if self._ls_news_url_tasks.get(window_key) is task:
            self._ls_news_url_tasks.pop(window_key, None)

    @staticmethod
    def _ls_news_source_names_equal(left: str, right: str) -> bool:
        normalize = lambda value: "".join(  # noqa: E731 - 짧은 비교 전용
            character.lower() for character in str(value or "")
            if character.isalnum())
        return bool(normalize(left)) and normalize(left) == normalize(right)

    @staticmethod
    def _ls_news_url_checked_recently(value: str) -> bool:
        try:
            checked_at = datetime.fromisoformat(str(value or ""))
            if checked_at.tzinfo is None:
                checked_at = checked_at.astimezone()
            return (
                datetime.now().astimezone() - checked_at
                < timedelta(minutes=10)
            )
        except (TypeError, ValueError):
            return False

    @staticmethod
    def _ls_news_candidate_time_matches(
            expected: str, candidate: str) -> bool:
        if not expected or not candidate:
            return True
        try:
            expected_at = datetime.fromisoformat(str(expected))
            candidate_at = datetime.fromisoformat(str(candidate))
            if expected_at.tzinfo is None:
                expected_at = expected_at.astimezone()
            if candidate_at.tzinfo is None:
                candidate_at = candidate_at.astimezone()
            return abs(
                (expected_at - candidate_at).total_seconds()) <= 72 * 3600
        except (TypeError, ValueError):
            return True

    def _remember_ls_news_original_url(
            self, realkey: str, news_key: str, original_url: str):
        realkey = str(realkey or "").strip()
        news_key = str(news_key or "").strip()
        original_url = str(original_url or "").strip()
        if not original_url:
            return
        for row in range(self._ls_news_table.rowCount()):
            title_item = self._ls_news_table.item(row, 3)
            if title_item is None:
                continue
            item_realkey = str(
                title_item.data(Qt.ItemDataRole.UserRole) or "").strip()
            item_news_key = str(
                title_item.data(Qt.ItemDataRole.UserRole + 1) or "").strip()
            if (
                (realkey and item_realkey == realkey)
                or (news_key and item_news_key == news_key)
            ):
                title_item.setData(
                    Qt.ItemDataRole.UserRole + 2, original_url)
                title_item.setToolTip(
                    f"{title_item.text()}\n\n클릭: 앱 본문 열기\n"
                    "더블클릭·Ctrl+클릭: 원문을 기본 브라우저로 열기\n"
                    f"{original_url}")

    def _set_ls_news_original_result(
            self, window_key: str, dialog: LSNewsDetailDialog,
            context: dict, original_url: str = ""):
        if self._ls_news_detail_windows.get(window_key) is not dialog:
            return
        search_url = str(context.get("search_url") or "")
        original_url = str(original_url or "").strip()
        if original_url:
            context["original_url"] = original_url
            self._remember_ls_news_original_url(
                str(context.get("realkey") or ""),
                str(context.get("news_key") or ""),
                original_url,
            )
        dialog.set_original_link(original_url, search_url, resolving=False)

    async def _resolve_ls_news_original_url(
            self, window_key: str, dialog: LSNewsDetailDialog,
            context: dict):
        try:
            saved = ls_realtime_news_detail(
                realkey=str(context.get("realkey") or ""),
                news_key=str(context.get("news_key") or ""),
            ) or {}
            original_url = str(saved.get("original_url") or "").strip()
            if original_url:
                self._set_ls_news_original_result(
                    window_key, dialog, context, original_url)
                return

            title = str(
                saved.get("title") or context.get("title") or "").strip()
            body = str(saved.get("body") or "")
            source_name = str(
                saved.get("source_name")
                or context.get("source_name") or "").strip()
            source_id = str(
                saved.get("source_id")
                or context.get("source_id") or "").strip()
            configured_source = NEWS_SOURCE_NAMES.get(source_id, "")
            if configured_source:
                source_name = configured_source
            else:
                inferred_source = infer_news_source(body)
                if inferred_source:
                    source_name = inferred_source
            is_reuters = (
                source_id == "28"
                or self._ls_news_source_names_equal(source_name, "로이터")
            )
            if is_reuters:
                source_name = "로이터"
            context["search_url"] = self._ls_news_search_url(
                title, source_name)

            body_links = extract_news_links(body)
            if is_reuters:
                for label, candidate_url in body_links:
                    candidate = QUrl(str(candidate_url or "").strip())
                    is_fnguide_original = (
                        candidate.host().casefold() == "trnews.fnguide.com"
                        and candidate.path().casefold()
                        == "/home/originalarticle"
                        and QUrlQuery(candidate).hasQueryItem("id")
                    )
                    if (
                        not candidate.isValid()
                        or candidate.scheme().lower() not in ("http", "https")
                        or (
                            normalize_news_title(label)
                            not in LS_NEWS_REUTERS_ORIGINAL_LABELS
                            and not is_fnguide_original
                        )
                    ):
                        continue
                    direct_url = candidate.toString()
                    try:
                        update_ls_realtime_news_original_url(
                            direct_url, "BODY_ORIGINAL_LINK", 1.0,
                            realkey=str(context.get("realkey") or ""),
                            news_key=str(context.get("news_key") or ""),
                        )
                    except Exception:  # noqa: BLE001 - 화면 연결은 유지한다.
                        log.exception(
                            "LS Reuters original URL save failed")
                    self._set_ls_news_original_result(
                        window_key, dialog, context, direct_url)
                    return
                try:
                    update_ls_realtime_news_original_url(
                        "", "BODY_ORIGINAL_LINK_NOT_FOUND", 0,
                        realkey=str(context.get("realkey") or ""),
                        news_key=str(context.get("news_key") or ""),
                    )
                except Exception:  # noqa: BLE001 - 버튼 비활성화는 유지한다.
                    log.exception(
                        "LS Reuters missing original link state save failed")
                self._set_ls_news_original_result(
                    window_key, dialog, context)
                return

            inferred_original_url = infer_news_original_url(body)
            if inferred_original_url:
                try:
                    update_ls_realtime_news_original_url(
                        inferred_original_url, "BODY_MEDIA_PATH", 0.99,
                        realkey=str(context.get("realkey") or ""),
                        news_key=str(context.get("news_key") or ""),
                    )
                except Exception:  # noqa: BLE001 - 화면 연결은 유지한다.
                    log.exception(
                        "LS original URL media path save failed")
                self._set_ls_news_original_result(
                    window_key, dialog, context, inferred_original_url)
                return

            target_title = normalize_news_title(title)
            if target_title and source_name:
                for label, candidate_url in body_links:
                    if (
                        normalize_news_title(label) == target_title
                        and self._ls_news_source_names_equal(
                            news_source_from_url(candidate_url), source_name)
                    ):
                        try:
                            update_ls_realtime_news_original_url(
                                candidate_url, "BODY_EXACT", 1.0,
                                realkey=str(context.get("realkey") or ""),
                                news_key=str(context.get("news_key") or ""),
                            )
                        except Exception:  # noqa: BLE001 - 화면 연결은 유지한다.
                            log.exception(
                                "LS original URL body match save failed")
                        self._set_ls_news_original_result(
                            window_key, dialog, context, candidate_url)
                        return

            if self._ls_news_url_checked_recently(
                    str(saved.get("original_url_checked_at") or "")):
                self._set_ls_news_original_result(
                    window_key, dialog, context)
                return

            known_sources = set(NEWS_SOURCE_DOMAINS.values())
            if (
                not target_title
                or not any(self._ls_news_source_names_equal(
                    source_name, known) for known in known_sources)
                or not config.NAVER_CLIENT_ID
                or not config.NAVER_CLIENT_SECRET
                or news_request_count_today() >= 20000
            ):
                self._set_ls_news_original_result(
                    window_key, dialog, context)
                return

            started = time.monotonic()
            items = []
            try:
                items = await NaverNewsClient(
                    config.NAVER_CLIENT_ID,
                    config.NAVER_CLIENT_SECRET,
                ).search(title, display=20)
                elapsed = int((time.monotonic() - started) * 1000)
                log_content_request(
                    None, f"LS 원문: {title}", 200, len(items), 0, elapsed)
            except asyncio.CancelledError:
                raise
            except Exception as error:  # noqa: BLE001 - 검색 버튼으로 대체한다.
                elapsed = int((time.monotonic() - started) * 1000)
                try:
                    log_content_request(
                        None, f"LS 원문: {title}", 0, 0, 0,
                        elapsed, str(error))
                except Exception:  # noqa: BLE001 - 원래 오류를 유지한다.
                    log.exception("LS original URL request log failed")
                try:
                    update_ls_realtime_news_original_url(
                        "", "NAVER_ERROR", 0,
                        realkey=str(context.get("realkey") or ""),
                        news_key=str(context.get("news_key") or ""),
                    )
                except Exception:  # noqa: BLE001
                    log.exception("LS original URL error state save failed")
                self._set_ls_news_original_result(
                    window_key, dialog, context)
                return

            matched_url = ""
            for item in items:
                candidate_url = str(
                    item.get("original_url") or "").strip()
                if (
                    candidate_url
                    and normalize_news_title(item.get("title")) == target_title
                    and self._ls_news_source_names_equal(
                        news_source_from_url(candidate_url), source_name)
                    and self._ls_news_candidate_time_matches(
                        str(saved.get("published_at") or ""),
                        str(item.get("published_at_source") or ""))
                ):
                    matched_url = candidate_url
                    break
            try:
                update_ls_realtime_news_original_url(
                    matched_url,
                    "NAVER_EXACT" if matched_url else "NAVER_NOT_FOUND",
                    1.0 if matched_url else 0,
                    realkey=str(context.get("realkey") or ""),
                    news_key=str(context.get("news_key") or ""),
                )
            except Exception:  # noqa: BLE001 - 화면 결과는 유지한다.
                log.exception("LS original URL match save failed")
            self._set_ls_news_original_result(
                window_key, dialog, context, matched_url)
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001 - 본문 기능은 유지한다.
            log.info(
                "LS original URL resolution failed key=%s type=%s",
                str(context.get("realkey") or ""), type(error).__name__)
            self._set_ls_news_original_result(
                window_key, dialog, context)

    def _set_ls_news_detail_metadata(
            self, dialog: LSNewsDetailDialog, record: dict,
            context: dict):
        title = str(record.get("title") or context.get("title") or "")
        news_date = str(record.get("news_date") or "")
        news_time = str(record.get("news_time") or "")
        display_time = (
            format_news_time(news_date, news_time)
            if news_date or news_time else str(context.get("time") or "-"))
        source_name = str(
            record.get("source_name") or context.get("source_name")
            or "출처 미상")
        realkey = str(record.get("realkey") or context.get("realkey") or "")
        metadata = f"{display_time} · {source_name}"
        if realkey:
            metadata += f" · 뉴스번호 {realkey}"

        related_codes = (
            record.get("related_stock_codes")
            or record.get("stock_code")
            or context.get("stock_codes")
            or "")
        if split_ls_news_stock_codes(related_codes):
            _label, _codes, stocks = self._ls_news_stock_display(
                related_codes)
        else:
            stocks = str(context.get("stocks") or "연결 종목 없음")
        dialog.set_metadata(title, metadata, stocks)

    async def _load_ls_news_detail(
            self, window_key: str, dialog: LSNewsDetailDialog,
            context: dict, force: bool = False):
        saved = None
        try:
            saved = ls_realtime_news_detail(
                realkey=str(context.get("realkey") or ""),
                news_key=str(context.get("news_key") or ""),
            )
        except Exception:  # noqa: BLE001 - REST 상세조회로 계속한다.
            log.exception("saved LS news detail load failed")
        if saved and str(saved.get("body") or "").strip() and not force:
            if self._ls_news_detail_windows.get(window_key) is not dialog:
                return
            self._set_ls_news_detail_metadata(dialog, saved, context)
            dialog.set_body(str(saved["body"]), "DB 저장 본문")
            self._start_ls_news_original_url_resolution(
                window_key, dialog, context)
            return

        realkey = str(context.get("realkey") or "").strip()
        if not realkey:
            if self._ls_news_detail_windows.get(window_key) is dialog:
                dialog.set_error(
                    "뉴스번호가 없어 LS 본문을 조회할 수 없습니다.")
            return
        try:
            stream = self._ls_news_stream or LSNewsStream()
            detail = await stream.news_detail(realkey)
            if not str(detail.body or "").strip():
                raise RuntimeError("LS 상세조회 응답에 본문이 없습니다.")
            detail_saved = False
            try:
                detail_saved = update_ls_realtime_news_detail(
                    realkey, detail.body, detail.stock_codes)
            except Exception:  # noqa: BLE001 - 본문 표시는 계속한다.
                log.exception("LS news detail save failed")

            inferred_source = infer_news_source(detail.body)
            source_id = str(
                (saved or {}).get("source_id")
                or context.get("source_id") or "").strip()
            resolved_source = (
                NEWS_SOURCE_NAMES.get(source_id) or inferred_source)
            if resolved_source and source_id:
                self._remember_ls_news_source(source_id, resolved_source)

            refreshed = None
            try:
                refreshed = ls_realtime_news_detail(
                    realkey=realkey,
                    news_key=str(context.get("news_key") or ""),
                )
            except Exception:  # noqa: BLE001 - 받아온 본문을 직접 표시한다.
                log.exception("refreshed LS news detail load failed")
            record = dict(refreshed or saved or {})
            record["body"] = detail.body
            # t3102의 sTitle은 정상 제목 뒤에 고정폭 바이너리가
            # 붙어 오는 케이스가 있다. 화면에는 NWS로 받아 DB에
            # 저장한 정상 제목을 계속 사용한다.
            if not record.get("related_stock_codes") and detail.stock_codes:
                record["related_stock_codes"] = json.dumps(
                    detail.stock_codes, ensure_ascii=False)
            if resolved_source:
                record["source_name"] = resolved_source
            if self._ls_news_detail_windows.get(window_key) is not dialog:
                return
            self._set_ls_news_detail_metadata(dialog, record, context)
            status = (
                "LS 상세조회 완료 · DB 저장"
                if detail_saved
                else "LS 상세조회 완료 · DB 미저장"
            )
            dialog.set_body(detail.body, status)
            self._start_ls_news_original_url_resolution(
                window_key, dialog, context)
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001 - 실시간 제목 수신은 유지한다.
            log.info(
                "LS news detail view failed key=%s type=%s",
                realkey, type(error).__name__)
            if self._ls_news_detail_windows.get(window_key) is dialog:
                dialog.set_error(f"본문 조회 실패: {error}")

    def _ls_news_stock_display(
            self, code: str) -> tuple[str, tuple[str, ...], str]:
        """단일·복수 NWS 종목코드를 짧은 표시명과 전체 설명으로 만든다."""
        stock_codes = split_ls_news_stock_codes(code)
        if not stock_codes:
            return "-", (), "연결 종목 없음"
        stock_names = []
        for stock_code in stock_codes:
            if stock_code not in self._ls_news_stock_names:
                stock = resolve_analysis_stock(stock_code)
                stock_name = str(
                    (stock or {}).get("stock_name") or "").strip()
                self._ls_news_stock_names[stock_code] = (
                    stock_name or stock_code)
            stock_names.append(self._ls_news_stock_names[stock_code])
        label = stock_names[0]
        if len(stock_names) > 1:
            label += f" {len(stock_names) - 1}"
        tooltip_lines = [
            (
                f"{stock_name} ({stock_code})"
                if stock_name != stock_code else stock_code
            )
            for stock_name, stock_code in zip(stock_names, stock_codes)
        ]
        tooltip = (
            f"관련 종목 {len(stock_codes)}개\n" + "\n".join(tooltip_lines)
            if len(stock_codes) > 1 else tooltip_lines[0]
        )
        return label, stock_codes, tooltip

    def _save_ls_news_header(self, *_args):
        if not hasattr(self, "_ls_news_table"):
            return
        self._settings.setValue(
            "analysis_ls_news_header_v2",
            self._ls_news_table.horizontalHeader().saveState(),
        )
        self._settings.sync()

    def _schedule_ls_news_source_resolution(self, item: LSNewsItem):
        source_id = str(item.source_id or "").strip()
        if (
            not source_id or source_id in self._ls_news_source_names
            or source_id in self._ls_news_source_pending
            or self._ls_news_source_attempts.get(source_id, 0) >= 3
            or not item.realkey or self._ls_news_stream is None
        ):
            return
        self._ls_news_source_pending.add(source_id)
        task = asyncio.ensure_future(
            self._resolve_ls_news_source(source_id, item.realkey))
        self._ls_news_source_tasks.add(task)
        task.add_done_callback(self._ls_news_source_tasks.discard)

    def _remember_ls_news_source(self, source_id: str, source_name: str):
        source_id = str(source_id or "").strip()
        source_name = str(source_name or "").strip()
        if not source_id or not source_name:
            return
        # 검증된 뉴스구분자는 제휴 기사의 원문 매체명으로 덮지 않는다.
        # 예: 21번 인포스탁 피드에 팜뉴스 기사가 포함될 수 있다.
        source_name = NEWS_SOURCE_NAMES.get(source_id, source_name)
        self._ls_news_source_names[source_id] = source_name
        self._settings.setValue(
            "analysis_ls_news_sources",
            json.dumps(
                self._ls_news_source_names, ensure_ascii=False,
                sort_keys=True),
        )
        self._settings.sync()
        try:
            update_ls_realtime_news_source(source_id, source_name)
        except Exception:  # noqa: BLE001 - 화면 보정은 계속한다.
            log.exception("LS realtime news source DB update failed")
        for row in range(self._ls_news_table.rowCount()):
            source_item = self._ls_news_table.item(row, 4)
            if (
                source_item is not None
                and source_item.data(Qt.ItemDataRole.UserRole) == source_id
            ):
                source_item.setText(source_name)
        if self._ls_news_search.text().strip():
            self._apply_ls_news_search_filter()

    async def _resolve_ls_news_source(self, source_id: str, realkey: str):
        try:
            detail = await self._ls_news_stream.news_detail(realkey)
            try:
                update_ls_realtime_news_detail(
                    realkey, detail.body, detail.stock_codes)
            except Exception:  # noqa: BLE001 - 매체명 보정은 계속한다.
                log.exception("LS realtime news detail DB update failed")
            source_name = infer_news_source(detail.body)
            if not source_name:
                self._ls_news_source_attempts[source_id] = (
                    self._ls_news_source_attempts.get(source_id, 0) + 1)
                return
            self._remember_ls_news_source(source_id, source_name)
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001 - 제목 수신은 계속 유지한다.
            self._ls_news_source_attempts[source_id] = (
                self._ls_news_source_attempts.get(source_id, 0) + 1)
            log.info(
                "LS news source resolution failed id=%s type=%s",
                source_id, type(error).__name__)
        finally:
            self._ls_news_source_pending.discard(source_id)

    def _ls_news_row_is_new(self, row: int) -> bool:
        if not hasattr(self, "_ls_news_table"):
            return False
        time_item = self._ls_news_table.item(row, 1)
        return bool(
            time_item is not None
            and time_item.data(LS_NEWS_NEW_ROLE)
        )

    def _update_ls_news_new_button(self):
        if not hasattr(self, "_ls_news_clear_new_button"):
            return
        count = max(0, int(self._ls_news_new_count))
        self._ls_news_clear_new_button.setText(
            f"신규해제 ({count:,})" if count else "신규해제")
        self._ls_news_clear_new_button.setEnabled(count > 0)

    def _reset_ls_news_new_markers_state(self):
        self._ls_news_new_count = 0
        self._update_ls_news_new_button()

    def _mark_ls_news_row_new(self, row: int):
        """실시간으로 표시된 한 행을 사용자가 해제할 때까지 강조한다."""
        if self._ls_news_row_is_new(row):
            return
        time_item = self._ls_news_table.item(row, 1)
        if time_item is None:
            return
        time_item.setData(LS_NEWS_NEW_ROLE, True)
        time_item.setBackground(QColor(LS_NEWS_NEW_TIME_BACKGROUND))
        time_item.setForeground(QColor(LS_NEWS_NEW_TIME_FOREGROUND))
        title_item = self._ls_news_table.item(row, 3)
        if title_item is not None:
            title_item.setBackground(QColor(LS_NEWS_NEW_TITLE_BACKGROUND))
            title_item.setForeground(QColor(LS_NEWS_NEW_TITLE_FOREGROUND))
            font = title_item.font()
            font.setBold(True)
            title_item.setFont(font)
        self._ls_news_new_count += 1
        self._update_ls_news_new_button()

    def _clear_ls_news_new_markers(self):
        """목록은 유지하고 새 실시간 뉴스의 강조 표시만 모두 해제한다."""
        if not hasattr(self, "_ls_news_table"):
            return
        cleared = 0
        for row in range(self._ls_news_table.rowCount()):
            if not self._ls_news_row_is_new(row):
                continue
            cleared += 1
            time_item = self._ls_news_table.item(row, 1)
            if time_item is not None:
                time_item.setData(LS_NEWS_NEW_ROLE, None)
            for column in (1, 3):
                item = self._ls_news_table.item(row, column)
                if item is not None:
                    item.setData(Qt.ItemDataRole.BackgroundRole, None)
                    item.setData(Qt.ItemDataRole.ForegroundRole, None)
            title_item = self._ls_news_table.item(row, 3)
            if title_item is not None:
                font = title_item.font()
                font.setBold(False)
                title_item.setFont(font)
        self._reset_ls_news_new_markers_state()
        if cleared:
            self.statusBar().showMessage(
                f"신규 뉴스 강조 {cleared:,}건을 해제했습니다. "
                "목록과 DB는 유지됩니다.",
                3000,
            )
