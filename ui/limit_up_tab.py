# -*- coding: utf-8 -*-
"""분석창 상한가 탭.

AnalysisWindow가 상속해 쓰는 화면 조각이다. 상한가 표는 뉴스·테마 탭과
관심종목 상태를 함께 갱신해야 해서 독립 위젯 대신 믹스인으로 분리했다.
"""
from __future__ import annotations

import logging

from PySide6.QtCore import QDate, QPoint, Qt, QUrl
from PySide6.QtGui import QColor, QDesktopServices
from PySide6.QtWidgets import (
    QApplication, QDateEdit, QHBoxLayout, QLabel, QLineEdit, QMessageBox,
    QPushButton, QTableWidget, QTableWidgetItem, QVBoxLayout,
)

from analysis_db import (
    delete_limit_up_record, limit_up_rows, realtime_watch_codes,
    set_realtime_watch,
)
from gui import NumericTableWidgetItem


log = logging.getLogger("limit_up_tab")


class LimitUpTabMixin:
    """상한가 탭 구성과 표 갱신, 기록 삭제를 담당한다."""

    def _build_limit_up_page(self, layout: QVBoxLayout):
        controls = QHBoxLayout()
        self._limit_from = QDateEdit(QDate.currentDate().addMonths(-6))
        self._limit_from.setCalendarPopup(True)
        self._limit_from.setDisplayFormat("yyyy-MM-dd")
        self._limit_to = QDateEdit(QDate.currentDate())
        self._limit_to.setCalendarPopup(True)
        self._limit_to.setDisplayFormat("yyyy-MM-dd")
        # 상한가 조회 기간과 원천 수집 기간을 하나로 사용한다.
        self._date_from = self._limit_from
        self._date_to = self._limit_to
        refresh = QPushButton("조회")
        refresh.clicked.connect(self._refresh_limit_up_table)
        disclosure_btn = QPushButton("선택 종목 공시 보기")
        disclosure_btn.clicked.connect(self._open_selected_disclosures)
        self._dart_btn = QPushButton("DART 공시 수집")
        self._dart_btn.clicked.connect(self._start_dart_collection)
        limit_refresh_btn = QPushButton("연상 재수집")
        limit_refresh_btn.clicked.connect(self.limit_count_collect_requested.emit)
        controls.addWidget(QLabel("기간"))
        controls.addWidget(self._limit_from)
        controls.addWidget(QLabel("~"))
        controls.addWidget(self._limit_to)
        controls.addWidget(refresh)
        controls.addWidget(disclosure_btn)
        controls.addWidget(self._dart_btn)
        controls.addWidget(limit_refresh_btn)
        controls.addStretch(1)
        layout.addLayout(controls)

        collection_controls = QHBoxLayout()
        self._krx_btn = QPushButton("KRX 상한가 수집")
        self._krx_btn.setToolTip(
            "선택 기간의 KRX 공식 일봉과 종가 상한가를 수동 저장합니다.")
        self._krx_btn.clicked.connect(self._start_krx_collection)
        self._kiwoom_limit_btn = QPushButton("키움 상한가 수집")
        self._kiwoom_limit_btn.setToolTip(
            "선택 기간 중 오늘과 DB에 없는 키움 일봉만 수집합니다. "
            "오늘은 묶음 시세로 빠르게 갱신합니다.")
        self._kiwoom_limit_btn.clicked.connect(
            self._start_history_collection)
        self._collect_btn = QPushButton("상한가 진입시간 보완")
        self._collect_btn.clicked.connect(self._start_intraday_enrichment)
        self._delete_limit_btn = QPushButton("선택 기록 삭제")
        self._delete_limit_btn.setToolTip(
            "표에서 선택한 날짜·종목의 상한가 기록을 확인 후 삭제합니다.")
        self._delete_limit_btn.setStyleSheet(
            "QPushButton { color:#ffffff; background:#b71c1c;"
            " border:1px solid #ff8a80; font-weight:700; padding:3px 8px; }"
            "QPushButton:hover { background:#d32f2f; }")
        self._delete_limit_btn.clicked.connect(
            self._delete_selected_limit_up_record)
        collection_controls.addWidget(QLabel("수동 저장"))
        collection_controls.addWidget(self._krx_btn)
        collection_controls.addWidget(self._kiwoom_limit_btn)
        collection_controls.addWidget(self._collect_btn)
        collection_controls.addWidget(self._delete_limit_btn)
        collection_controls.addStretch(1)
        layout.addLayout(collection_controls)

        search_controls = QHBoxLayout()
        self._limit_stock_search = QLineEdit()
        self._limit_stock_search.setPlaceholderText("종목코드·종목명 검색")
        self._limit_stock_search.setClearButtonEnabled(True)
        self._limit_stock_search.returnPressed.connect(
            self._refresh_limit_up_table)
        self._limit_theme_search = QLineEdit()
        self._limit_theme_search.setPlaceholderText("테마 검색")
        self._limit_theme_search.setClearButtonEnabled(True)
        self._limit_theme_search.returnPressed.connect(
            self._refresh_limit_up_table)
        search_btn = QPushButton("검색")
        search_btn.clicked.connect(self._refresh_limit_up_table)
        search_controls.addWidget(QLabel("종목검색"))
        search_controls.addWidget(self._limit_stock_search)
        search_controls.addWidget(QLabel("테마검색"))
        search_controls.addWidget(self._limit_theme_search)
        search_controls.addWidget(search_btn)
        search_controls.addStretch(1)
        layout.addLayout(search_controls)

        self._limit_summary = QLabel("상한가 0건")
        layout.addWidget(self._limit_summary)
        columns = (
            "번호", "거래일", "종목코드", "종목명", "시장", "상한가진입",
            "종가", "등락률", "거래량", "거래대금", "연속", "감시", "공시",
            "테마",
        )
        self._limit_table = QTableWidget(0, len(columns))
        self._limit_table.setHorizontalHeaderLabels(columns)
        self._limit_table.horizontalHeaderItem(5).setToolTip(
            "거래일별로 묶어 같은 날 먼저 상한가에 진입한 순서로 정렬")
        self._limit_table.setSortingEnabled(True)
        self._limit_table.setAlternatingRowColors(True)
        self._limit_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._limit_table.verticalHeader().setVisible(False)
        # 마지막 컬럼도 자동 확장하지 않고 사용자가 경계선을 끌어 폭을 조절한다.
        self._limit_table.horizontalHeader().setStretchLastSection(False)
        saved_header = self._settings.value("analysis_limit_header_v3")
        self._limit_header_restored = bool(
            saved_header is not None
            and self._limit_table.horizontalHeader().restoreState(saved_header)
        )
        self._limit_table.horizontalHeader().sectionResized.connect(
            self._save_limit_header)
        self._limit_table.horizontalHeader().sortIndicatorChanged.connect(
            self._limit_sort_changed)
        self._limit_table.cellClicked.connect(
            self._limit_table_clicked)
        self._limit_table.setContextMenuPolicy(
            Qt.ContextMenuPolicy.CustomContextMenu)
        self._limit_table.customContextMenuRequested.connect(
            self._limit_table_right_clicked)
        layout.addWidget(self._limit_table)

    def _limit_sort_changed(self, column: int, _order):
        """진입시간 정렬 중에는 아직 수집되지 않은 행을 제외한다."""
        table = self._limit_table
        for row in range(table.rowCount()):
            entry_item = table.item(row, 5)
            missing = not entry_item or entry_item.text().strip() in ("", "-")
            table.setRowHidden(row, column == 5 and missing)

    def _limit_dates(self) -> tuple[str, str]:
        return (
            self._limit_from.date().toString("yyyyMMdd"),
            self._limit_to.date().toString("yyyyMMdd"),
        )

    def _refresh_limit_up_table(self):
        if not hasattr(self, "_limit_table"):
            return
        date_from, date_to = self._limit_dates()
        stock_query = self._limit_stock_search.text().strip()
        theme_query = self._limit_theme_search.text().strip()
        rows = limit_up_rows(
            date_from, date_to, stock_query, theme_query)
        watched_codes = realtime_watch_codes()
        table = self._limit_table
        table.setSortingEnabled(False)
        table.setRowCount(len(rows))
        for row_index, row in enumerate(rows):
            values = (
                row_index + 1, row["trade_date"], row["stock_code"],
                row["stock_name"], row["market"],
                row["last_entry_time"] or "-",
                f"{row['close_price'] or 0:,}",
                f"{row['change_rate'] or 0:.2f}%",
                f"{row['volume'] or 0:,}", f"{row['trading_value'] or 0:,}",
                str(row["consecutive_days"]),
                "★" if row["stock_code"] in watched_codes else "☆",
                str(row["disclosure_count"]), row["theme_names"] or "-",
            )
            for column, value in enumerate(values):
                number = {
                    0: row_index + 1,
                    6: row["close_price"] or 0,
                    7: row["change_rate"] or 0,
                    8: row["volume"] or 0,
                    9: row["trading_value"] or 0,
                    10: row["consecutive_days"] or 0,
                    12: row["disclosure_count"] or 0,
                }.get(column)
                if column == 5:
                    entry_digits = "".join(
                        character for character in str(row["last_entry_time"] or "")
                        if character.isdigit()
                    )
                    # 최신 거래일을 먼저 묶고, 날짜 안에서는 이른 진입부터 정렬한다.
                    entry_order = (
                        -int(row["trade_date"]),
                        int(entry_digits or "999999"),
                    )
                    item = NumericTableWidgetItem(value, entry_order)
                elif number is not None:
                    item = NumericTableWidgetItem(str(value), number)
                else:
                    item = QTableWidgetItem(value)
                item.setData(
                    Qt.ItemDataRole.UserRole + 2, row["stock_code"])
                item.setData(
                    Qt.ItemDataRole.UserRole + 3, row["stock_name"])
                if column == 0:
                    item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                if column == 11:
                    item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                    item.setForeground(
                        QColor("#f4b400")
                        if row["stock_code"] in watched_codes
                        else QColor("#808080"))
                    item.setToolTip(
                        "클릭하면 실시간 뉴스 감시를 등록하거나 해제합니다.")
                if column == 13 and row["theme_names"]:
                    item.setToolTip(row["theme_names"])
                if column in (6, 7, 8, 9, 10, 12):
                    item.setTextAlignment(
                        Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                table.setItem(row_index, column, item)
        table.setSortingEnabled(True)
        if not self._limit_header_restored:
            table.resizeColumnsToContents()
            table.setColumnWidth(0, 48)
            table.setColumnWidth(
                13, min(280, max(140, table.columnWidth(13))))
            table.sortItems(0, Qt.SortOrder.AscendingOrder)
            self._limit_header_restored = True
            self._save_limit_header()
        stock_count = len({row["stock_code"] for row in rows})
        search_suffix = (
            " · 검색 적용" if stock_query or theme_query else "")
        self._limit_summary.setText(
            f"상한가 {len(rows):,}건 · 종목 {stock_count:,}개{search_suffix}")
        header = table.horizontalHeader()
        self._limit_sort_changed(
            header.sortIndicatorSection(), header.sortIndicatorOrder())

    def _save_limit_header(self, *_args):
        if not hasattr(self, "_limit_table"):
            return
        self._settings.setValue(
            "analysis_limit_header_v3",
            self._limit_table.horizontalHeader().saveState(),
        )
        self._settings.sync()

    def _limit_table_clicked(self, row: int, column: int):
        """종목코드 복사, 기업정보 및 공시 열기를 컬럼별로 처리한다."""
        stock_code = self._limit_stock_code(row)
        if column == 2 and stock_code:
            QApplication.clipboard().setText(stock_code)
            self.statusBar().showMessage(
                f"종목코드 {stock_code}를 복사했습니다.", 3000)
        elif column == 3 and stock_code:
            QDesktopServices.openUrl(QUrl(
                f"https://finance.naver.com/item/coinfo.naver?code={stock_code}"))
        elif column == 11 and stock_code:
            watched = stock_code in realtime_watch_codes()
            try:
                set_realtime_watch(
                    stock_code, not watched, "LIMIT_UP_TABLE")
            except ValueError as error:
                QMessageBox.warning(self, "실시간 감시", str(error))
                return
            self._refresh_limit_up_table()
            self._selected_watch_code = stock_code
            self._refresh_realtime_watch_table()
            self._refresh_realtime_news_table()
            self.watchlist_changed.emit()
            if not watched and self._news_auto.isChecked():
                self._start_realtime_news_collection(True, False)
            state = "등록" if not watched else "해제"
            self.statusBar().showMessage(
                f"{stock_code} 실시간 뉴스 감시를 {state}했습니다.", 3000)
        elif column == 12:
            self._open_disclosures_for_row(row)

    def _limit_table_right_clicked(self, position: QPoint):
        """종목명을 오른쪽 클릭하면 네이버 종목 토론실을 연다."""
        item = self._limit_table.itemAt(position)
        if item is None or item.column() != 3:
            return
        stock_code = (
            item.data(Qt.ItemDataRole.UserRole + 2) or "")
        if stock_code:
            QDesktopServices.openUrl(QUrl(
                f"https://finance.naver.com/item/board.naver?code={stock_code}"))

    def _limit_stock_code(self, row: int) -> str:
        item = self._limit_table.item(row, 0)
        if item is None:
            return ""
        return str(
            item.data(Qt.ItemDataRole.UserRole + 2) or "").strip()

    def _delete_selected_limit_up_record(self):
        """선택한 상한가 이벤트를 명시적 확인 뒤 삭제한다."""
        row = self._limit_table.currentRow()
        if row < 0:
            QMessageBox.information(
                self, "상한가 기록 삭제",
                "상한가 표에서 삭제할 행을 먼저 선택해 주세요.")
            return
        date_item = self._limit_table.item(row, 1)
        code_item = self._limit_table.item(row, 2)
        name_item = self._limit_table.item(row, 3)
        trade_date = str(date_item.text() if date_item else "").strip()
        stock_code = self._limit_stock_code(row)
        if not stock_code and code_item is not None:
            stock_code = code_item.text().strip()
        stock_name = str(name_item.text() if name_item else stock_code).strip()
        if len(trade_date) == 8 and trade_date.isdigit():
            date_text = (
                f"{trade_date[:4]}-{trade_date[4:6]}-{trade_date[6:8]}")
        else:
            date_text = trade_date
        answer = QMessageBox.question(
            self,
            "상한가 기록 삭제 확인",
            f"{date_text}  {stock_name} ({stock_code})\n\n"
            "이 상한가 기록을 삭제하시겠습니까?\n"
            "조건검색이 만든 임시 일봉은 함께 삭제하고, "
            "KRX·키움 정식 일봉은 보존합니다.",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            result = delete_limit_up_record(trade_date, stock_code)
        except (OSError, ValueError) as error:
            QMessageBox.critical(
                self, "상한가 기록 삭제", f"삭제하지 못했습니다.\n{error}")
            return
        if not result["deleted"]:
            QMessageBox.information(
                self, "상한가 기록 삭제",
                "이미 삭제됐거나 해당 기록을 찾을 수 없습니다.")
            self._refresh_limit_up_table()
            return
        log.warning(
            "manual limit-up record deleted: date=%s code=%s name=%s "
            "source=%s condition_price=%s",
            trade_date, stock_code, stock_name, result["price_source"],
            result["condition_price_deleted"])
        self._refresh_limit_up_table()
        self._refresh_theme_table()
        detail = (
            " · 조건검색 임시 일봉도 삭제"
            if result["condition_price_deleted"] else
            " · 정식 일봉은 보존"
        )
        self._collection_status.setText(
            f"삭제 완료 · {date_text} {stock_name} ({stock_code}){detail}")
        self.statusBar().showMessage(self._collection_status.text(), 7000)
