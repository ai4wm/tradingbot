# -*- coding: utf-8 -*-
"""[0198] 실시간 종목조회순위 창. ka00198을 주기 폴링(창이 보일 때만)."""
import asyncio
import threading

from PySide6.QtCore import QAbstractTableModel, QModelIndex, QSettings, Qt, QTimer, QUrl
from PySide6.QtGui import QColor, QCursor, QDesktopServices
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QHBoxLayout, QHeaderView, QLabel, QPushButton,
    QSpinBox, QTableView, QToolTip, QVBoxLayout, QWidget,
)

from api import MarketInfo
from gui import (ADMIN, MISU_ROLE, NEW_ROLE, NXT_ROLE, PURPLE, NameDelegate,
                 PreserveTextColorDelegate)

RED = QColor("#e83030")
BLUE = QColor("#2050d0")
WHITE = QColor("white")
LIMIT = 29.5  # 상/하한 판정 (gui.py와 동일)

COLUMNS = ["순위", "종목명", "변동", "기준시점주가", "기준등락률", "직전대비"]
FIELDS  = ["rank", "name", "rank_chg", "price", "rate", "prev_rate"]
PERIODS = [("30초", "5"), ("1분", "1"), ("10분", "2"), ("1시간", "3"), ("당일누적", "4")]


def _alert_kind(prev_codes: list, rows: list[dict], top_on: bool, jump_on: bool,
                jump_n: int, top_n: int = 1):
    """새 집계 스냅샷에서 알림 종류 판정: 'top'=정확히 top_n위 종목 변경, 'jump'=순위 급상승,
    None=없음. prev_codes=직전 스냅샷의 순위순 코드 리스트."""
    if not rows:
        return None
    top = (top_on and len(rows) >= top_n and len(prev_codes) >= top_n
           and rows[top_n - 1]["code"] != prev_codes[top_n - 1])
    jump = jump_on and any(r["rank_chg"] >= jump_n for r in rows)
    return "top" if top else "jump" if jump else None  # 동시 발생 땐 1위변경 우선


TONES = {  # (주파수Hz, 길이ms) 나열 -> 멜로디. 시스템 테마 무관하게 또렷한 전용음
    # 1위 변경(가장 중요): 도-미-솔↑ 상승 팡파레, 마지막 음 길게 -> 확실히 각인 (jump와 확 구분)
    "top":  [(1047, 130), (1319, 130), (1568, 420)],
    "jump": [(1100, 1000)],  # 급상승: 이 PC에서 실제 확인한 1초 긴 단일음
    "in":   [(784, 140), (1047, 140), (1319, 280)],  # 조건 편입: 뚜-뚜-띠~↑ (3음 차임, 길고 또렷)
}


def _beep(kind: str):
    def play():
        try:
            import winsound
            for freq, ms in TONES[kind]:
                winsound.Beep(freq, ms)
        except Exception:  # noqa: BLE001
            QApplication.beep()
    threading.Thread(target=play, daemon=True).start()  # Beep은 블로킹 -> 스레드에서


class RankModel(QAbstractTableModel):
    def __init__(self):
        super().__init__()
        self.rows: list[dict] = []
        self.kosdaq: set[str] = set()
        self.nxt: set[str] = set()
        self.misu: set[str] = set()
        self.admin: set[str] = set()
        self.new_today: set[str] = set()
        self.new15: set[str] = set()
        self.new30: set[str] = set()

    def set_rows(self, rows: list[dict]):
        self.beginResetModel()
        self.rows = rows
        self.endResetModel()

    def rowCount(self, parent=QModelIndex()):
        return len(self.rows)

    def columnCount(self, parent=QModelIndex()):
        return len(COLUMNS)

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if orientation == Qt.Horizontal and role in (Qt.DisplayRole, Qt.ToolTipRole):
            return COLUMNS[section]  # 툴팁: 칸 좁혀 헤더 글자 잘려도 오버로 확인
        return None

    def data(self, index, role=Qt.DisplayRole):
        r = self.rows[index.row()]
        f = FIELDS[index.column()]
        v = r[f]
        code = r["code"]
        if role == NXT_ROLE:
            return code in self.nxt
        if role == MISU_ROLE:
            return code in self.misu
        if role == NEW_ROLE:
            return (3 if code in self.new_today else 2 if code in self.new15
                    else 1 if code in self.new30 else 0)
        if role == Qt.ToolTipRole and f == "name":
            parts = []
            if code in self.nxt:
                parts.append("좌상단 노랑 = NXT 거래가능")
            if code in self.misu:
                parts.append("우상단 녹색 = 미수가능")
            new = self.data(index, NEW_ROLE)
            if new:
                parts.append("좌하단 " + {3: "마젠타 = 오늘 상장", 2: "하늘 = 상장 15일 이내",
                                          1: "청회 = 상장 16~30일"}[new])
            return "\n".join(parts) or None
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
            # prev_rate=마지막 스트레치 컬럼: 왼쪽 정렬이라야 데이터가 붙어 창 폭 줄이기 좋음
            return (Qt.AlignLeft if f in ("name", "prev_rate") else Qt.AlignRight) | Qt.AlignVCenter
        if code in self.new_today:
            is_limit = f == "rate" and (-40.0 <= v <= -39.5 or 299.5 <= v <= 300.0)
        else:
            is_limit = f == "rate" and LIMIT <= abs(v) <= 30.0
        if role == Qt.BackgroundRole and is_limit:
            return RED if v > 0 else BLUE
        if role == Qt.ForegroundRole:
            if f == "name":
                if code in self.admin:
                    return ADMIN
                return PURPLE if code in self.kosdaq else None
            if is_limit:
                return WHITE  # 상/하한 배경 위 흰 글씨
            key = r["rate"] if f in ("price", "rate") else v if f in ("prev_rate", "rank_chg") else 0
            return RED if key > 0 else BLUE if key < 0 else None
        return None


class RankScreen(QWidget):
    def __init__(self, rest, parent=None):
        super().__init__(parent)
        self.rest = rest
        self.setWindowTitle("[0198] 실시간 종목조회순위")
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
        self.interval.setFixedWidth(80)  # 기본 sizeHint가 과대 -> 상단 가로폭 절약
        # 화살표로 값 변경 시 텍스트 선택(어두운 배경에 가림) 방지 — jump_n과 동일
        self.interval.valueChanged.connect(
            lambda _: QTimer.singleShot(0, self.interval.lineEdit().deselect))
        self.refresh_btn = QPushButton("조회")
        self.time_label = QLabel("")
        self.time_label.setToolTip("집계 기준시각")
        self.on_top_btn = QPushButton("📌")  # 항상 맨 위 토글
        self.on_top_btn.setCheckable(True)
        self.on_top_btn.setFixedWidth(32)
        self.on_top_btn.setToolTip("항상 맨 위 — 이 창을 다른 창들 위에 계속 고정")

        top = QHBoxLayout()
        top.addWidget(QLabel("기준"))
        top.addWidget(self.period)
        top.addWidget(QLabel("갱신"))
        top.addWidget(self.interval)
        top.addWidget(self.refresh_btn)
        top.addStretch(1)
        top.addWidget(self.time_label)

        self.model = RankModel()
        self.table = QTableView()  # 폰트는 앱 전역(main.py: 굴림체9 NoAA) 상속
        self.table.setModel(self.model)
        self.table.setItemDelegate(PreserveTextColorDelegate(self.table))
        self.table.setItemDelegateForColumn(FIELDS.index("name"), NameDelegate(self.table))
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(22)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        self.table.horizontalHeader().setStretchLastSection(True)
        # 행 선택으로 표가 활성화돼도 Windows 스타일이 헤더 전체를 굵게 바꾸지 않게 고정.
        self.table.horizontalHeader().setStyleSheet("QHeaderView::section { font-weight: normal; }")
        # 헤더 글자 왼쪽 정렬: 가운데면 칸 좁힐 때 앞자리부터 잘림 (조건창과 동일)
        self.table.horizontalHeader().setDefaultAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self.table.setColumnWidth(0, 40)
        self.table.setColumnWidth(1, 110)
        self.table.setColumnWidth(2, 46)  # 변동(▲n/▼n)은 종목명 옆 좁은 컬럼 (HTS 동일)
        self.table.setSelectionBehavior(QTableView.SelectRows)
        self.table.setSelectionMode(QTableView.NoSelection)
        self.table.setFocusPolicy(Qt.StrongFocus)  # 클릭 후 위/아래 화살표로 순위 자리 이동
        self.table.selectionModel().currentChanged.connect(lambda *_: self.table.viewport().update())
        self.table.setEditTriggers(QTableView.NoEditTriggers)
        self.table.clicked.connect(self._on_cell_clicked)
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._on_context_menu)

        # 하단: 사운드 알림 옵션 (새 집계 스냅샷에서만 판정 -> 중복 알림 없음)
        self.top_n = QComboBox()  # 스핀보다 폭 절약: 드롭다운 1개 화살표
        self.top_n.addItems([str(i) for i in range(1, 20)])
        self.top_n.setCurrentText(str(self._settings.value("rank_top_n", 1)))
        self.top_n.setFixedWidth(60)
        self.top_n.currentTextChanged.connect(lambda v: self._save_opt("rank_top_n", int(v)))
        self.alert_top = QCheckBox("위변경")
        self.alert_top.setToolTip("선택한 N위 자리의 종목이 바뀌면 소리 알림")
        self.alert_top.setChecked(self._settings.value("rank_alert_top", "false") == "true")
        self.alert_jump = QCheckBox("급상승≥")
        self.alert_jump.setToolTip("순위가 N계단 이상 뛰어오르면 소리 알림")
        self.alert_jump.setChecked(self._settings.value("rank_alert_jump", "false") == "true")
        self.jump_n = QSpinBox()
        self.jump_n.setRange(1, 19)
        self.jump_n.setValue(int(self._settings.value("rank_jump_n", 3)))
        self.alert_top.toggled.connect(lambda on: self._save_opt("rank_alert_top", on))
        self.alert_jump.toggled.connect(lambda on: self._save_opt("rank_alert_jump", on))
        self.jump_n.valueChanged.connect(lambda v: self._save_opt("rank_jump_n", v))
        # 화살표로 값 변경 시 텍스트 선택(어두운 배경에 가림) 방지 — gui.py 간격 스핀과 동일
        self.jump_n.valueChanged.connect(
            lambda _: QTimer.singleShot(0, self.jump_n.lineEdit().deselect))
        self._last_tm = ""    # 마지막 판정한 집계 시각
        self._last_alert_signature = None  # 시각이 같아도 순위/변동이 바뀌면 다시 판정
        self._last_codes = []  # 직전 스냅샷 순위순 코드 리스트

        bottom = QHBoxLayout()
        bottom.addWidget(self.top_n)
        bottom.addWidget(self.alert_top)
        bottom.addWidget(self.alert_jump)
        bottom.addWidget(self.jump_n)
        bottom.addStretch(1)
        bottom.addWidget(self.on_top_btn)  # 하단 오른쪽 구석 (상단 가로폭 확보)

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
            # restoreState가 옛 정렬값(가운데)까지 되살림 -> 왼쪽 재적용
            self.table.horizontalHeader().setDefaultAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self.on_top_btn.toggled.connect(self._on_top_toggle)
        if self._settings.value("rank_on_top", "false") == "true":  # 항상위 복원
            self.on_top_btn.setChecked(True)  # 창 뜨기 전 = 플래그만 걸림(재생성 튐 없음)
        # 크기/컬럼 변경 시 디바운스 저장 (닫을 때만 저장하면 앱 종료 경로 따라 유실)
        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.timeout.connect(self._save_layout)
        self.table.horizontalHeader().sectionResized.connect(lambda *a: self._save_timer.start(400))

    def set_market(self, market: MarketInfo) -> None:
        self.model.kosdaq, self.model.nxt, self.model.misu, self.model.admin = (
            market.kosdaq, market.nxt, market.misu, market.admin)
        self.model.new_today, self.model.new15, self.model.new30 = (
            market.new_today, market.new15, market.new30)
        if self.model.rows:
            self.model.dataChanged.emit(
                self.model.index(0, FIELDS.index("name")),
                self.model.index(len(self.model.rows) - 1, FIELDS.index("rate")),
                [Qt.BackgroundRole, Qt.ForegroundRole, Qt.ToolTipRole,
                 NXT_ROLE, MISU_ROLE, NEW_ROLE])

    def _save_opt(self, key: str, v):
        self._settings.setValue(key, "true" if v is True else "false" if v is False else v)
        self._settings.sync()

    def _apply_on_top(self, on: bool):
        w = self.window()  # top-level 창 = 자기 자신
        was_visible = w.isVisible()  # setWindowFlag이 창을 숨기므로 '전에' 잡아야 함
        geo = w.geometry()  # 창 재생성 때 위치 유실 -> 보존 (안 하면 이동된 위치가 저장돼 복원 오염)
        w.setWindowFlag(Qt.WindowStaysOnTopHint, on)
        if was_visible:    # 떠 있던 창만 재표시(플래그 변경 후 숨겨짐). 숨은 채 복원이면 show 안 함
            w.show()
            w.setGeometry(geo)  # 재생성된 창을 원위치로

    def _on_top_toggle(self, on: bool):
        self._apply_on_top(on)
        self._save_opt("rank_on_top", on)

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
        current = self.table.currentIndex()
        selected_row = current.row() if current.isValid() else -1
        selected_col = current.column() if current.isValid() else 0
        self.model.set_rows(rows)
        if 0 <= selected_row < len(rows):
            # 종목이 아니라 선택한 순위 자리를 유지: 3위 선택이면 갱신 후에도 3위.
            index = self.model.index(selected_row, min(selected_col, self.model.columnCount() - 1))
            self.table.setCurrentIndex(index)
        t = rows[0]["time"] if rows else ""
        self.time_label.setText(f"{t[:2]}:{t[2:4]}:{t[4:6]}" if len(t) == 6 else "데이터 없음")
        signature = (t, tuple((r["code"], r["rank_chg"]) for r in rows))
        if rows and signature != self._last_alert_signature:
            # 첫 조회라도 급상승 기준을 충족하면 반드시 알림. 순위 변경은 이전 목록이
            # 없으므로 자동으로 False. 시각·순위·변동이 모두 같은 반복조회만 중복 방지한다.
            kind = _alert_kind(self._last_codes, rows, self.alert_top.isChecked(),
                               self.alert_jump.isChecked(), self.jump_n.value(),
                               int(self.top_n.currentText()))
            if kind:
                _beep(kind)
            self._last_tm = t
            self._last_alert_signature = signature
            self._last_codes = [r["code"] for r in rows]

    def _on_cell_clicked(self, index):
        if index.column() != FIELDS.index("name"):
            return
        code = self.model.rows[index.row()]["code"]
        QApplication.clipboard().setText(code)
        QToolTip.showText(QCursor.pos(), f"{code} 복사됨")

    def _on_context_menu(self, pos):
        index = self.table.indexAt(pos)
        if not index.isValid() or index.column() != FIELDS.index("name"):
            return
        code = self.model.rows[index.row()]["code"]
        QDesktopServices.openUrl(QUrl(f"https://finance.naver.com/item/board.naver?code={code}"))

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
        {"rank": 8, "code": "387690", "name": "신규48", "price": 14800,
         "rate": 48.0, "prev_rate": 0.0, "rank_chg": 0, "time": "224200"},
    ])
    d = lambda r, c, role=Qt.DisplayRole: m.data(m.index(r, c), role)  # noqa: E731
    assert d(0, 0) == 1 and d(0, 1) == "삼성전자" and d(0, 3) == "291,000"
    assert d(0, 4) == "-8.49" and d(0, 2) == ""
    assert d(1, 2) == "▲2" and d(2, 2) == "▼2"
    assert d(1, 2, Qt.ForegroundRole) is RED and d(2, 2, Qt.ForegroundRole) is BLUE
    assert d(0, 3, Qt.ForegroundRole) is BLUE and d(2, 3, Qt.ForegroundRole) is RED
    assert d(3, 4, Qt.BackgroundRole) is RED and d(3, 4, Qt.ForegroundRole) is WHITE  # 상한 배경
    assert d(0, 4, Qt.BackgroundRole) is None  # 일반 등락률은 배경 없음
    m.new_today = {"387690"}
    assert d(4, 4, Qt.BackgroundRole) is None and d(4, 4, Qt.ForegroundRole) is RED
    m.kosdaq, m.nxt, m.misu, m.new_today = {"005930"}, {"005930"}, {"005930"}, {"005930"}
    assert d(0, 1, Qt.ForegroundRole) == PURPLE
    assert d(0, 1, NXT_ROLE) and d(0, 1, MISU_ROLE) and d(0, 1, NEW_ROLE) == 3
    screen = RankScreen(None)
    assert screen.table.contextMenuPolicy() == Qt.CustomContextMenu
    screen.model.set_rows(m.rows)
    changed = []
    screen.model.dataChanged.connect(lambda *args: changed.append(args))
    screen.set_market(MarketInfo(kosdaq={"005930"}))
    assert len(changed) == 1
    assert changed[0][0].column() == FIELDS.index("name")
    assert changed[0][1].column() == FIELDS.index("rate")
    assert Qt.BackgroundRole in changed[0][2]
    screen.close()
    # 알림 판정: 상위 N위 변경 / 급상승 임계 / 없음
    rows = m.rows  # 순위순 코드: 005930, 042660, 002990, 042660
    assert _alert_kind(["000000"], rows, True, False, 3) == "top"     # 1위 코드 바뀜
    assert _alert_kind(["005930"], rows, True, True, 2) == "jump"     # 1위 유지, ▲2 >= 2
    assert _alert_kind(["000000"], rows, True, True, 2) == "top"      # 1위변경+급상승 동시 -> 1위 우선
    assert _alert_kind(["005930"], rows, True, True, 3) is None       # 임계 미달
    assert _alert_kind([], rows, True, True, 3) is None               # 이전 없음(첫 수신)
    assert _alert_kind([], rows, True, True, 2) == "jump"             # 첫 수신도 급상승 기준 충족
    # top_n=3: 상위3 구성 {005930,042660,002990} 유지면 top 아님, 새 코드 끼면 top
    assert _alert_kind(["005930", "042660", "002990"], rows, True, False, 3, 3) is None
    assert _alert_kind(["042660", "005930", "002990"], rows, True, False, 3, 3) is None  # 1·2위만 변경
    assert _alert_kind(["005930", "042660", "999999"], rows, True, False, 3, 3) == "top"
    assert _alert_kind("000000", rows, False, False, 1) is None      # 알림 꺼짐
    print("rank self-check OK")


if __name__ == "__main__":
    _demo()
