# -*- coding: utf-8 -*-
"""[0156] 조건검색실시간 스타일 그리드.

화면 = 위젯(ConditionScreen) 원칙: 나중에 QMdiArea에 넣으면 그대로 다중창이 된다.
웹소켓 계층은 on_included / on_tick / on_excluded 세 메서드만 호출하면 된다.
"""
import sys

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QSortFilterProxyModel, Qt, QTimer
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QHBoxLayout, QHeaderView, QLabel,
    QMainWindow, QPushButton, QTableView, QVBoxLayout, QWidget,
)

COLUMNS = ["등락률", "종목명", "업종", "현재가", "전일거래량", "거래량", "매도잔량", "매수잔량", "편입시간"]
FIELDS  = ["rate",   "name",  "sector", "price", "prev_vol", "vol",   "ask_qty",  "bid_qty",  "time"]

RED  = QColor("#e83030")
BLUE = QColor("#2050d0")
WHITE = QColor("white")


class StockModel(QAbstractTableModel):
    def __init__(self):
        super().__init__()
        self.codes: list[str] = []          # 행 순서
        self.rows: dict[str, dict] = {}     # code -> {field: value}

    # --- 웹소켓/전략 계층이 부르는 API ---------------------------------
    def add_stock(self, code: str, data: dict):
        if code in self.rows:
            self.update_stock(code, data)
            return
        row = len(self.codes)
        self.beginInsertRows(QModelIndex(), row, row)
        self.codes.append(code)
        self.rows[code] = {f: data.get(f, 0 if f != "name" else "") for f in FIELDS}
        self.endInsertRows()

    def remove_stock(self, code: str):
        if code not in self.rows:
            return
        row = self.codes.index(code)
        self.beginRemoveRows(QModelIndex(), row, row)
        self.codes.remove(code)
        del self.rows[code]
        self.endRemoveRows()

    def update_stock(self, code: str, fields: dict):
        if code not in self.rows:
            return
        row = self.codes.index(code)
        stored = self.rows[code]
        fields = {f: v for f, v in fields.items() if f in FIELDS}  # 모르는 키 무시
        changed = [FIELDS.index(f) for f, v in fields.items() if stored.get(f) != v]
        stored.update(fields)
        if changed:  # 바뀐 셀만 갱신
            self.dataChanged.emit(self.index(row, min(changed)), self.index(row, max(changed)))

    # --- Qt 모델 구현 ---------------------------------------------------
    def rowCount(self, parent=QModelIndex()):
        return len(self.codes)

    def columnCount(self, parent=QModelIndex()):
        return len(COLUMNS)

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if role == Qt.DisplayRole and orientation == Qt.Horizontal:
            return COLUMNS[section]
        return None

    def data(self, index, role=Qt.DisplayRole):
        field = FIELDS[index.column()]
        value = self.rows[self.codes[index.row()]][field]

        if role == Qt.DisplayRole:
            if field == "rate":
                return f"{value:+.2f}"
            if field in ("price", "prev_vol", "vol", "ask_qty", "bid_qty"):
                return f"{value:,}" if value else ""
            return value
        if role == Qt.UserRole:  # 정렬용 원본값
            return value
        if role == Qt.TextAlignmentRole:
            if field in ("name", "sector", "time"):
                return Qt.AlignLeft | Qt.AlignVCenter
            return Qt.AlignRight | Qt.AlignVCenter
        if role == Qt.BackgroundRole and field == "rate":
            return RED if value > 0 else BLUE if value < 0 else None
        if role == Qt.ForegroundRole:
            if field == "rate":
                return WHITE
            if field == "price":
                rate = self.rows[self.codes[index.row()]]["rate"]
                return RED if rate > 0 else BLUE if rate < 0 else None
        return None


class ConditionScreen(QWidget):
    """조건검색실시간 화면 하나. 나중에 QMdiArea에 이 위젯을 여러 개 띄우면 다중창."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.model = StockModel()

        # 툴바: 조건식 선택 / 등록 토글 / 이탈삭제 / 종목수
        self.condition_combo = QComboBox()
        self.start_btn = QPushButton("등록")
        self.start_btn.setCheckable(True)
        self.auto_remove = QCheckBox("이탈삭제")
        self.auto_remove.setChecked(True)
        self.count_label = QLabel("종목수: 0")

        top = QHBoxLayout()
        top.addWidget(self.condition_combo, 1)
        top.addWidget(self.start_btn)
        top.addWidget(self.auto_remove)
        top.addWidget(self.count_label)

        # 그리드
        proxy = QSortFilterProxyModel()
        proxy.setSourceModel(self.model)
        proxy.setSortRole(Qt.UserRole)
        self.table = QTableView()
        self.table.setModel(proxy)
        self.table.setSortingEnabled(True)
        self.table.sortByColumn(0, Qt.DescendingOrder)  # 등락률 내림차순
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(22)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setColumnWidth(1, 110)
        self.table.setSelectionBehavior(QTableView.SelectRows)
        self.table.setFont(QFont("맑은 고딕", 9))
        self.table.setEditTriggers(QTableView.NoEditTriggers)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.addLayout(top)
        layout.addWidget(self.table)

        self.model.rowsInserted.connect(self._update_count)
        self.model.rowsRemoved.connect(self._update_count)

    def _update_count(self):
        self.count_label.setText(f"종목수: {self.model.rowCount()}")

    # --- 웹소켓 계층 연결점 ----------------------------------------------
    def on_included(self, code: str, data: dict):
        """조건 편입 (CNSRREQ I)"""
        self.model.add_stock(code, data)

    def on_excluded(self, code: str):
        """조건 이탈 (CNSRREQ D)"""
        if self.auto_remove.isChecked():
            self.model.remove_stock(code)

    def on_tick(self, code: str, fields: dict):
        """실시간 시세 (0B 체결 / 0D 호가)"""
        self.model.update_stock(code, fields)


def _demo(screen: ConditionScreen):
    """더미 데이터 데모. ponytail: 웹소켓 붙이면 이 함수 삭제."""
    import random
    samples = [
        ("001", "케이피엠테크", "유통", 4620), ("002", "텔콘RF제약", "제약", 2720),
        ("003", "대원", "건설", 5100), ("004", "레이저쎌", "기계/장", 5730),
        ("005", "금호건설", "건설", 12350), ("006", "금호전기", "전기/전", 963),
        ("007", "마키나락스", "IT 서비", 30400), ("008", "아센디오", "오락/문", 1004),
    ]
    pending = list(samples)

    def tick():
        import time
        if pending and random.random() < 0.4:  # 편입
            code, name, sector, price = pending.pop(0)
            screen.on_included(code, {
                "rate": round(random.uniform(15, 30), 2), "name": name, "sector": sector,
                "price": price, "prev_vol": random.randint(50_000, 30_000_000),
                "vol": random.randint(100_000, 40_000_000),
                "ask_qty": random.randint(0, 500_000), "bid_qty": random.randint(1_000, 2_000_000),
                "time": time.strftime("%H:%M:%S"),
            })
        for code in list(screen.model.codes):  # 시세 틱
            if random.random() < 0.5:
                row = screen.model.rows[code]
                screen.on_tick(code, {
                    "price": max(1, row["price"] + random.randint(-3, 5) * 5),
                    "rate": round(min(30.0, row["rate"] + random.uniform(-0.1, 0.1)), 2),
                    "vol": row["vol"] + random.randint(0, 50_000),
                    "bid_qty": max(0, row["bid_qty"] + random.randint(-10_000, 10_000)),
                })

    timer = QTimer(screen)
    timer.timeout.connect(tick)
    timer.start(200)
    screen.condition_combo.addItem("10-180@@상한예상 상한근접")
    return timer


if __name__ == "__main__":
    app = QApplication(sys.argv)
    win = QMainWindow()
    win.setWindowTitle("[0156] 조건검색실시간")
    screen = ConditionScreen()
    win.setCentralWidget(screen)
    win.resize(900, 560)
    _demo(screen)
    win.show()
    sys.exit(app.exec())
