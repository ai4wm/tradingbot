# -*- coding: utf-8 -*-
"""[0198] 실시간 종목조회순위 창. ka00198을 주기 폴링(창이 보일 때만)."""
import asyncio

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QSettings, Qt, QTimer
from PySide6.QtGui import QColor, QCursor, QFont
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QHBoxLayout, QHeaderView, QLabel, QPushButton,
    QSpinBox, QTableView, QToolTip, QVBoxLayout, QWidget,
)

RED = QColor("#e83030")
BLUE = QColor("#2050d0")
WHITE = QColor("white")
LIMIT = 29.5  # 상/하한 판정 (gui.py와 동일)

COLUMNS = ["순위", "종목명", "변동", "기준시점주가", "기준등락률", "직전대비"]
FIELDS  = ["rank", "name", "rank_chg", "price", "rate", "prev_rate"]
PERIODS = [("30초", "5"), ("1분", "1"), ("10분", "2"), ("1시간", "3"), ("당일누적", "4")]


def _alert_kind(prev_top: str, rows: list[dict], top_on: bool, jump_on: bool, jump_n: int):
    """새 집계 스냅샷에서 알림 종류 판정: 'top'=1위 변경, 'jump'=순위 급상승, None=없음."""
    if not rows:
        return None
    if top_on and prev_top and rows[0]["code"] != prev_top:
        return "top"
    if jump_on and any(r["rank_chg"] >= jump_n for r in rows):
        return "jump"
    return None


def _beep(kind: str):
    try:
        import winsound
        alias = "SystemExclamation" if kind == "top" else "SystemAsterisk"
        winsound.PlaySound(alias, winsound.SND_ALIAS | winsound.SND_ASYNC | winsound.SND_NODEFAULT)
    except Exception:  # noqa: BLE001
        QApplication.beep()


class RankModel(QAbstractTableModel):
    def __init__(self):
        super().__init__()
        self.rows: list[dict] = []

    def set_rows(self, rows: list[dict]):
        self.beginResetModel()
        self.rows = rows
        self.endResetModel()

    def rowCount(self, parent=QModelIndex()):
        return len(self.rows)

    def columnCount(self, parent=QModelIndex()):
        return len(COLUMNS)

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if role == Qt.DisplayRole and orientation == Qt.Horizontal:
            return COLUMNS[section]
        return None

    def data(self, index, role=Qt.DisplayRole):
        r = self.rows[index.row()]
        f = FIELDS[index.column()]
        v = r[f]
        if role == Qt.DisplayRole:
            if f == "price":
                return f"{v:,}"
            if f in ("rate", "prev_rate"):
                return f"{v:+.2f}"
            if f == "rank_chg":
                return f"▲{v}" if v > 0 else f"▼{-v}" if v < 0 else ""
            return v
        if role == Qt.TextAlignmentRole:
            if f == "rank_chg":
                return Qt.AlignCenter
            return (Qt.AlignLeft if f == "name" else Qt.AlignRight) | Qt.AlignVCenter
        if role == Qt.BackgroundRole:
            if f == "rate" and (v >= LIMIT or v <= -LIMIT):  # 상/하한 = 배경색
                return RED if v > 0 else BLUE
        if role == Qt.ForegroundRole:
            if f == "rate" and (v >= LIMIT or v <= -LIMIT):
                return WHITE  # 상/하한 배경 위 흰 글씨
            key = r["rate"] if f in ("price", "rate") else v if f in ("prev_rate", "rank_chg") else 0
            return RED if key > 0 else BLUE if key < 0 else None
        return None


class RankScreen(QWidget):
    def __init__(self, rest, parent=None):
        super().__init__(parent)
        self.rest = rest
        self.setWindowTitle("[0198] 실시간 종목조회순위")
        self.setFont(QFont("돋움체", 10))  # 조건검색 그리드와 동일 서체 (툴바/헤더 포함)
        self._settings = QSettings("layout.ini", QSettings.IniFormat)

        self.period = QComboBox()
        for name, tp in PERIODS:
            self.period.addItem(name, tp)
        idx = self.period.findData(self._settings.value("rank_period", "5"))
        self.period.setCurrentIndex(max(idx, 0))
        self.interval = QSpinBox()
        self.interval.setRange(5, 300)
        self.interval.setValue(int(self._settings.value("rank_interval", 30)))
        self.interval.setSuffix("초")
        self.refresh_btn = QPushButton("조회")
        self.time_label = QLabel("")

        top = QHBoxLayout()
        top.addWidget(QLabel("기준"))
        top.addWidget(self.period)
        top.addWidget(QLabel("갱신"))
        top.addWidget(self.interval)
        top.addWidget(self.refresh_btn)
        top.addStretch(1)
        top.addWidget(self.time_label)

        self.model = RankModel()
        self.table = QTableView()
        self.table.setModel(self.model)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(22)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setColumnWidth(0, 40)
        self.table.setColumnWidth(1, 110)
        self.table.setColumnWidth(2, 46)  # 변동(▲n/▼n)은 종목명 옆 좁은 컬럼 (HTS 동일)
        self.table.setSelectionBehavior(QTableView.SelectRows)
        self.table.setEditTriggers(QTableView.NoEditTriggers)
        self.table.clicked.connect(self._on_cell_clicked)

        # 하단: 사운드 알림 옵션 (새 집계 스냅샷에서만 판정 -> 중복 알림 없음)
        self.alert_top = QCheckBox("1위 변경 알림")
        self.alert_top.setChecked(self._settings.value("rank_alert_top", "false") == "true")
        self.alert_jump = QCheckBox("순위 급상승 알림 ≥")
        self.alert_jump.setChecked(self._settings.value("rank_alert_jump", "false") == "true")
        self.jump_n = QSpinBox()
        self.jump_n.setRange(1, 19)
        self.jump_n.setValue(int(self._settings.value("rank_jump_n", 3)))
        self.alert_top.toggled.connect(lambda on: self._save_opt("rank_alert_top", on))
        self.alert_jump.toggled.connect(lambda on: self._save_opt("rank_alert_jump", on))
        self.jump_n.valueChanged.connect(lambda v: self._save_opt("rank_jump_n", v))
        self._last_tm = ""   # 마지막 판정한 집계 시각
        self._last_top = ""  # 마지막 1위 종목코드

        bottom = QHBoxLayout()
        bottom.addWidget(self.alert_top)
        bottom.addWidget(self.alert_jump)
        bottom.addWidget(self.jump_n)
        bottom.addStretch(1)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.addLayout(top)
        layout.addWidget(self.table)
        layout.addLayout(bottom)

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._poll)
        self.refresh_btn.clicked.connect(self._poll)
        self.period.activated.connect(self._on_period)
        self.interval.valueChanged.connect(self._on_interval)

        geo = self._settings.value("rank_geometry")
        if geo is not None:
            self.restoreGeometry(geo)
        else:
            self.resize(440, 560)
        state = self._settings.value("rank_header")
        if state is not None:
            self.table.horizontalHeader().restoreState(state)
        # 크기/컬럼 변경 시 디바운스 저장 (닫을 때만 저장하면 앱 종료 경로 따라 유실)
        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.timeout.connect(self._save_layout)
        self.table.horizontalHeader().sectionResized.connect(lambda *a: self._save_timer.start(400))

    def _save_opt(self, key: str, v):
        self._settings.setValue(key, "true" if v is True else "false" if v is False else v)
        self._settings.sync()

    def _on_period(self, _):
        self._settings.setValue("rank_period", self.period.currentData())
        self._settings.sync()
        self._poll()

    def _on_interval(self, sec: int):
        self._settings.setValue("rank_interval", sec)
        self._settings.sync()
        if self._timer.isActive():
            self._timer.start(sec * 1000)

    def _poll(self):
        asyncio.ensure_future(self._fetch())

    async def _fetch(self):
        try:
            rows = await self.rest.inquiry_rank(self.period.currentData())
        except Exception as e:  # noqa: BLE001
            self.time_label.setText(str(e)[:40])
            return
        self.model.set_rows(rows)
        t = rows[0]["time"] if rows else ""
        self.time_label.setText(f"{t[:2]}:{t[2:4]}:{t[4:6]} 기준" if len(t) == 6 else "데이터 없음")
        if rows and t != self._last_tm:  # 새 집계 스냅샷에서만 알림 판정
            if self._last_tm:  # 창 연 직후 첫 수신은 제외
                kind = _alert_kind(self._last_top, rows, self.alert_top.isChecked(),
                                   self.alert_jump.isChecked(), self.jump_n.value())
                if kind:
                    _beep(kind)
            self._last_tm, self._last_top = t, rows[0]["code"]

    def _on_cell_clicked(self, index):
        if index.column() != FIELDS.index("name"):
            return
        code = self.model.rows[index.row()]["code"]
        QApplication.clipboard().setText(code)
        QToolTip.showText(QCursor.pos(), f"{code} 복사됨")

    def _save_layout(self):
        self._settings.setValue("rank_geometry", self.saveGeometry())
        self._settings.setValue("rank_header", self.table.horizontalHeader().saveState())
        self._settings.sync()

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._save_timer.start(400)

    def moveEvent(self, e):
        super().moveEvent(e)
        self._save_timer.start(400)

    def showEvent(self, e):
        super().showEvent(e)
        self._timer.start(self.interval.value() * 1000)
        self._poll()

    def hideEvent(self, e):
        self._timer.stop()  # 안 보일 땐 폴링 중지 (REST 큐 비움)
        self._save_layout()
        super().hideEvent(e)

    def closeEvent(self, e):
        self._save_layout()
        super().closeEvent(e)  # 닫기 = 숨김 (재클릭 시 재사용)


def _demo():
    app = QApplication.instance() or QApplication([])
    m = RankModel()
    m.set_rows([
        {"rank": 1, "code": "005930", "name": "삼성전자", "price": 291000,
         "rate": -8.49, "prev_rate": 0.0, "rank_chg": 0, "time": "224200"},
        {"rank": 3, "code": "042660", "name": "한화오션", "price": 88600,
         "rate": -23.69, "prev_rate": 0.0, "rank_chg": 2, "time": "224200"},
        {"rank": 5, "code": "002990", "name": "금호건설", "price": 14000,
         "rate": 13.36, "prev_rate": 0.0, "rank_chg": -2, "time": "224200"},
        {"rank": 7, "code": "042660", "name": "점상", "price": 88600,
         "rate": 29.9, "prev_rate": 0.0, "rank_chg": 0, "time": "224200"},
    ])
    d = lambda r, c, role=Qt.DisplayRole: m.data(m.index(r, c), role)  # noqa: E731
    assert d(0, 0) == 1 and d(0, 1) == "삼성전자" and d(0, 3) == "291,000"
    assert d(0, 4) == "-8.49" and d(0, 2) == ""
    assert d(1, 2) == "▲2" and d(2, 2) == "▼2"
    assert d(1, 2, Qt.ForegroundRole) is RED and d(2, 2, Qt.ForegroundRole) is BLUE
    assert d(0, 3, Qt.ForegroundRole) is BLUE and d(2, 3, Qt.ForegroundRole) is RED
    assert d(3, 4, Qt.BackgroundRole) is RED and d(3, 4, Qt.ForegroundRole) is WHITE  # 상한 배경
    assert d(0, 4, Qt.BackgroundRole) is None  # 일반 등락률은 배경 없음
    # 알림 판정: 1위 변경 / 급상승 임계 / 없음
    rows = m.rows
    assert _alert_kind("000000", rows, True, False, 3) == "top"      # 1위 코드 바뀜
    assert _alert_kind("005930", rows, True, True, 2) == "jump"      # 1위 유지, ▲2 >= 2
    assert _alert_kind("005930", rows, True, True, 3) is None        # 임계 미달
    assert _alert_kind("", rows, True, True, 3) is None              # 이전 1위 없음(첫 수신)
    assert _alert_kind("000000", rows, False, False, 1) is None      # 알림 꺼짐
    print("rank self-check OK")


if __name__ == "__main__":
    _demo()
