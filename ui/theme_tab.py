# -*- coding: utf-8 -*-
"""분석창 테마 탭.

AnalysisWindow가 상속해 쓰는 화면 조각이다. 테마 수집 버튼이 앱 전체의
수집 진행 상태를 함께 쓰기 때문에 독립 위젯 대신 믹스인으로 분리했다.
"""
from __future__ import annotations

from PySide6.QtCore import Qt, QUrl, QUrlQuery
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPushButton, QTableWidget,
    QTableWidgetItem, QVBoxLayout,
)

from analysis_db import set_realtime_watch, theme_summary_rows
from gui import NumericTableWidgetItem


class ThemeTabMixin:
    """테마 표 구성과 갱신, 네이버 테마 상세 열기를 담당한다."""

    def _build_theme_page(self, layout: QVBoxLayout):
        controls = QHBoxLayout()
        self._theme_search = QLineEdit()
        self._theme_search.setPlaceholderText("테마명·종목코드·종목명 검색")
        self._theme_search.setClearButtonEnabled(True)
        self._theme_search.returnPressed.connect(self._refresh_theme_table)
        refresh = QPushButton("조회")
        refresh.clicked.connect(self._refresh_theme_table)
        self._theme_btn = QPushButton("키움 테마 수집")
        self._theme_btn.clicked.connect(self._start_theme_collection)
        self._naver_theme_btn = QPushButton("네이버 테마 수집")
        self._naver_theme_btn.clicked.connect(
            self._start_naver_theme_collection)
        controls.addWidget(QLabel("검색"))
        controls.addWidget(self._theme_search, 1)
        controls.addWidget(refresh)
        controls.addWidget(self._theme_btn)
        controls.addWidget(self._naver_theme_btn)
        layout.addLayout(controls)

        self._theme_summary = QLabel("테마 0개")
        layout.addWidget(self._theme_summary)
        columns = (
            "번호", "출처", "테마", "구성종목", "상한가종목", "종목 목록",
        )
        table = QTableWidget(0, len(columns))
        table.setHorizontalHeaderLabels(columns)
        table.setSortingEnabled(True)
        table.setAlternatingRowColors(True)
        table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        table.verticalHeader().setVisible(False)
        table.horizontalHeader().setStretchLastSection(True)
        table.cellClicked.connect(self._theme_table_clicked)
        self._theme_table = table
        layout.addWidget(table)

    def _refresh_theme_table(self):
        if not hasattr(self, "_theme_table"):
            return
        rows = theme_summary_rows(self._theme_search.text())
        table = self._theme_table
        table.setSortingEnabled(False)
        table.setRowCount(len(rows))
        source_names = {
            "NAVER": "네이버", "KIWOOM": "키움", "WICS": "WICS",
            "KRX": "KRX", "DART": "DART",
        }
        source_priority = {
            "NAVER": 1, "KIWOOM": 2, "WICS": 3, "KRX": 4, "DART": 5,
        }
        total_members = 0
        for row_index, row in enumerate(rows):
            total_members += int(row["member_count"] or 0)
            members = str(row["members"] or "").replace(",", ", ")
            values = (
                row_index + 1,
                source_names.get(row["source"], row["source"]),
                row["theme_name"],
                int(row["member_count"] or 0),
                int(row["limit_up_count"] or 0),
                members,
            )
            for column, value in enumerate(values):
                if column == 0:
                    item = NumericTableWidgetItem(str(value), value)
                    item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                elif column == 1:
                    item = NumericTableWidgetItem(
                        str(value or ""),
                        source_priority.get(row["source"], 99),
                    )
                elif column in (3, 4):
                    item = NumericTableWidgetItem(f"{value:,}", value)
                else:
                    item = QTableWidgetItem(str(value or ""))
                if column == 2 and row["source"] == "NAVER":
                    source_code = str(row["source_code"] or "").strip()
                    if source_code:
                        item.setData(
                            Qt.ItemDataRole.UserRole + 4, source_code,
                        )
                        item.setToolTip(
                            "클릭하면 네이버 금융 테마 상세 페이지를 엽니다.")
                if column == 5:
                    item.setToolTip(
                        "클릭하면 이 테마 종목을 관심종목에 모두 추가합니다.\n"
                        f"{members}")
                table.setItem(row_index, column, item)
        table.setSortingEnabled(True)
        table.resizeColumnsToContents()
        table.setColumnWidth(0, 48)
        table.setColumnWidth(2, min(260, max(140, table.columnWidth(2))))
        table.setColumnWidth(5, 500)
        table.sortItems(0, Qt.SortOrder.AscendingOrder)
        self._theme_summary.setText(
            f"현재 테마 {len(rows):,}개 · 종목 연결 {total_members:,}건")

    def _theme_table_clicked(self, row: int, column: int):
        if column == 5:
            self._add_theme_members_to_watchlist(row)
            return
        if column != 2:
            return
        item = self._theme_table.item(row, column)
        source_code = (
            item.data(Qt.ItemDataRole.UserRole + 4)
            if item is not None else ""
        )
        if source_code:
            url = QUrl(
                "https://finance.naver.com/sise/sise_group_detail.naver")
            query = QUrlQuery()
            query.addQueryItem("type", "theme")
            query.addQueryItem("no", str(source_code))
            url.setQuery(query)
            self.statusBar().showMessage(
                f"네이버 테마 열기: {item.text()} (no={source_code})", 5000)
            QDesktopServices.openUrl(url)

    def _add_theme_members_to_watchlist(self, row: int):
        """테마 구성 종목을 관심종목(실시간 감시)에 한 번에 추가한다."""
        item = self._theme_table.item(row, 5)
        theme_item = self._theme_table.item(row, 2)
        if item is None:
            return
        codes = []
        for member in str(item.text() or "").split(","):
            code = member.strip().split(" ", 1)[0].strip()
            if code and code not in codes:
                codes.append(code)
        if not codes:
            return
        theme_name = theme_item.text() if theme_item is not None else "테마"
        answer = QMessageBox.question(
            self, "관심종목 추가",
            f"'{theme_name}' 종목 {len(codes)}개를 관심종목에 추가할까요?",
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        added = 0
        failed = []
        for code in codes:
            try:
                set_realtime_watch(code, True, "THEME_TAB")
                added += 1
            except ValueError as error:
                failed.append(f"{code}: {error}")
        self._refresh_realtime_watch_table()
        self._refresh_realtime_news_table()
        self._refresh_limit_up_table()
        self.watchlist_changed.emit()
        message = f"'{theme_name}' 관심종목 {added}개 추가"
        if failed:
            message += f" · 실패 {len(failed)}개"
        self.statusBar().showMessage(message, 5000)
