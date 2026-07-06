# -*- coding: utf-8 -*-
"""[0156] 조건검색실시간 스타일 그리드.

화면 = 위젯(ConditionScreen) 원칙: 나중에 QMdiArea에 넣으면 그대로 다중창이 된다.
웹소켓 계층은 on_included / on_tick / on_excluded 세 메서드만 호출하면 된다.
"""
import sys
import time

from PySide6.QtCore import (
    QAbstractTableModel, QModelIndex, QRect, QSettings, QSortFilterProxyModel, Qt, QTimer, QUrl,
)
from PySide6.QtGui import QColor, QCursor, QDesktopServices, QFont
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QHBoxLayout, QHeaderView, QLabel,
    QMainWindow, QPushButton, QSpinBox, QStyle, QStyledItemDelegate, QTableView, QToolTip,
    QVBoxLayout, QWidget,
)

COLUMNS = ["등락률", "종목명", "현재가", "예상체결가", "L일봉H", "예상등락률", "전일거래량", "거래량", "매도잔량", "매수잔량", "상한가진입시간"]
FIELDS  = ["rate",   "name",  "price", "exp_price", "bar",    "exp_rate",   "prev_vol", "vol",   "ask_qty",  "bid_qty",  "time"]
# 컬럼은 아니지만 L일봉H 그리기에 필요한 저장 필드 (시/저/고/전일종가/상한/하한)
STORED = set(FIELDS) | {"open", "low", "high", "base", "upper", "lower"}
BAR_COL = FIELDS.index("bar")
BAR_ROLE = Qt.UserRole + 1  # 델리게이트에 (open, high, low, close, base, upper, lower) 전달

LIMIT = 29.5  # 상한/하한 판정 임계 (KRX +-30%)
RED  = QColor("#e83030")
BLUE = QColor("#2050d0")
WHITE = QColor("white")
TRACK = QColor("#d8d8d8")
CENTER = QColor("#707070")  # L일봉H 0% 중심선


class BarDelegate(QStyledItemDelegate):
    """L일봉H: 가로 일봉 캔들. 축 = 하한가(왼쪽)~전일종가(0%,가운데)~상한가(오른쪽).
    심지=저가~고가, 몸통=시가~종가. 양봉(종가>=시가) 빨강, 음봉 파랑.
    점상한가는 O=H=L=C=상한가라 오른쪽 끝에 세로선으로 표시됨."""

    def paint(self, painter, option, index):
        if option.state & QStyle.State_Selected:
            painter.fillRect(option.rect, option.palette.highlight())
        data = index.data(BAR_ROLE)
        if not data:
            return
        op, high, low, close, base, upper, lower = data  # 시/고/저/종/전일종가/상한/하한
        if not close or not base:
            return
        # 상/하한가 없으면 ±30%로 폴백
        upper = upper or int(base * 1.3)
        lower = lower or int(base * 0.7)
        r = option.rect.adjusted(4, 4, -4, -4)

        def x(p):  # 하한~전일종가~상한 -> 0..1 (전일종가=0.5)
            if p >= base:
                pos = 0.5 + 0.5 * (p - base) / (upper - base) if upper > base else 0.5
            else:
                pos = 0.5 - 0.5 * (base - p) / (base - lower) if base > lower else 0.5
            pos = max(0.0, min(1.0, pos))
            return r.left() + int(round(r.width() * pos))

        # 몸통 색: 시가!=종가면 양/음봉, 평평(점상/점하)하면 전일종가 대비
        if op and close != op:
            color = RED if close > op else BLUE
        else:
            color = RED if close > base else BLUE if close < base else QColor("#888")

        painter.save()
        yc = r.center().y()
        cx = x(base)  # 0% 중심선 (전일종가)
        painter.setPen(CENTER)
        painter.drawLine(cx, r.top(), cx, r.bottom())
        painter.setPen(QColor("#888"))
        painter.drawLine(x(low), yc, x(high), yc)  # 심지: 저가~고가
        lo, hi = (op, close) if op else (close, close)  # 시가 없으면 종가 마커
        x0, x1 = sorted((x(lo), x(hi)))
        painter.fillRect(QRect(x0, r.top() + 2, max(2, x1 - x0), r.height() - 4), color)
        painter.restore()


def _at_limit(d: dict) -> bool:
    """상한가 상태: 실제(현재가=상한가) 또는 예상(예상등락률≥상한).
    동시호가 땐 체결 전이라 예상으로, 장중엔 실제로 잡힌다."""
    return (d["upper"] > 0 and d["price"] == d["upper"]) or (d["exp_price"] > 0 and d["exp_rate"] >= LIMIT)


class TieredProxy(QSortFilterProxyModel):
    """매수세 정렬 모드(buy_mode):
      ① 예상등락률=상한 & 매도잔량=0  -> 매수잔량 큰 순
      ② 예상등락률 데이터 있음          -> 예상등락률 큰 순
      ③ 예상등락률 데이터 없음          -> 등락률 큰 순
    모드 off면 기본(헤더 클릭) 정렬. 오름차순 정렬 기준 key(작을수록 위)로 표현."""

    def __init__(self):
        super().__init__()
        self.buy_mode = False

    def _key(self, src_row: int):
        m = self.sourceModel()
        d = m.rows[m.codes[src_row]]
        if _at_limit(d) and d["ask_qty"] == 0:
            return (0, -d["bid_qty"])   # 티어0: 상한(실제/예상)&매도0 -> 매수잔량 내림차순
        if d["exp_price"] > 0:
            return (1, -d["exp_rate"])  # 티어1: 예상등락률 내림차순
        return (2, -d["rate"])          # 티어2: 등락률 내림차순

    def _bottom_sort(self, a_in, b_in, a_key, b_key):
        """그룹 소속(a_in/b_in)끼리만 key로 정렬, 비소속은 정렬방향 무관 항상 맨 아래."""
        if a_in and b_in:
            return a_key < b_key
        if a_in != b_in:
            desc = self.sortOrder() == Qt.DescendingOrder
            return desc if not a_in else (not desc)  # 비소속을 화면 맨 아래로
        return False

    def lessThan(self, left, right):
        if self.buy_mode:
            return self._key(left.row()) < self._key(right.row())
        m = self.sourceModel()
        a = m.rows[m.codes[left.row()]]
        b = m.rows[m.codes[right.row()]]
        col = left.column()
        if col == FIELDS.index("time"):     # 상한가진입시간: 시간 있는(상한가) 것만
            return self._bottom_sort(bool(a["time"]), bool(b["time"]), a["time"], b["time"])
        if col == FIELDS.index("bid_qty"):  # 매수잔량: 상한(실제/예상)&매도0 종목만
            ga = _at_limit(a) and a["ask_qty"] == 0
            gb = _at_limit(b) and b["ask_qty"] == 0
            return self._bottom_sort(ga, gb, a["bid_qty"], b["bid_qty"])
        return super().lessThan(left, right)


class StockModel(QAbstractTableModel):
    def __init__(self):
        super().__init__()
        self.codes: list[str] = []          # 행 순서
        self.rows: dict[str, dict] = {}     # code -> {field: value}
        self._exp_seen: dict[str, float] = {}  # code -> 마지막 예상체결가 수신 시각(monotonic)

    # --- 웹소켓/전략 계층이 부르는 API ---------------------------------
    def add_stock(self, code: str, data: dict):
        if code in self.rows:
            self.update_stock(code, data)
            return
        row = len(self.codes)
        self.beginInsertRows(QModelIndex(), row, row)
        self.codes.append(code)
        self.rows[code] = {f: data.get(f, "" if f in ("name", "time") else 0) for f in STORED}
        self.endInsertRows()

    def remove_stock(self, code: str):
        if code not in self.rows:
            return
        row = self.codes.index(code)
        self.beginRemoveRows(QModelIndex(), row, row)
        self.codes.remove(code)
        del self.rows[code]
        self._exp_seen.pop(code, None)
        self.endRemoveRows()

    def update_stock(self, code: str, fields: dict):
        if code not in self.rows:
            return
        row = self.codes.index(code)
        stored = self.rows[code]
        fields = {f: v for f, v in fields.items() if f in STORED}  # 모르는 키 무시
        cols = set()
        for f, v in fields.items():
            if stored.get(f) == v:
                continue
            if f in FIELDS:
                cols.add(FIELDS.index(f))
            if f in ("price", "open", "low", "high", "base", "upper", "lower"):  # L일봉H 의존
                cols.add(BAR_COL)
        stored.update(fields)
        # 예상체결가 수신 시각 기록(도착 기준). 값 변화 없어도 도착하면 갱신 -> staleness 판정용.
        if "exp_price" in fields:
            if fields["exp_price"]:
                self._exp_seen[code] = time.monotonic()
            else:
                self._exp_seen.pop(code, None)
        # 예상등락률은 실시간 예상체결가/전일종가에서 파생 (동시호가/VI 때만 값이 옴)
        if "exp_price" in fields or "base" in fields:
            ep, base = stored["exp_price"], stored["base"]
            er = round((ep - base) / base * 100, 2) if (ep and base) else 0.0
            if stored.get("exp_rate") != er:
                stored["exp_rate"] = er
                cols.add(FIELDS.index("exp_rate"))
        if cols:  # 바뀐 셀만 갱신
            self.dataChanged.emit(self.index(row, min(cols)), self.index(row, max(cols)))

    def clear_stale_exp(self, timeout: float = 3.0):
        """예상체결가가 timeout초 이상 안 들어오면 예상 컬럼 비움(동시호가/VI 종료 시 값 잔류 방지).
        피드가 0을 보내며 끝나지 않고 그냥 송신을 멈추므로 도착 기준 staleness로 판정."""
        now = time.monotonic()
        for code, ts in list(self._exp_seen.items()):
            if now - ts > timeout:
                self.update_stock(code, {"exp_price": 0})  # -> exp_rate도 0 파생, 셀 갱신

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
        stored = self.rows[self.codes[index.row()]]
        value = stored[field]

        if role == BAR_ROLE and field == "bar":  # 델리게이트용
            return (stored["open"], stored["high"], stored["low"], stored["price"],
                    stored["base"], stored["upper"], stored["lower"])
        if role == Qt.DisplayRole:
            if field == "bar":
                return ""  # 델리게이트가 그림
            if field == "rate":
                return f"{value:+.2f}"
            if field == "exp_rate":
                return f"{value:+.2f}" if value else ""
            if field in ("price", "exp_price", "prev_vol", "vol", "ask_qty", "bid_qty"):
                return f"{value:,}" if value else ""
            return value
        if role == Qt.UserRole:  # 정렬용 원본값
            return value
        if role == Qt.TextAlignmentRole:
            if field in ("name", "time"):
                return Qt.AlignLeft | Qt.AlignVCenter
            return Qt.AlignRight | Qt.AlignVCenter
        rate = stored["rate"]
        er = stored["exp_rate"]
        is_limit = rate >= LIMIT or rate <= -LIMIT          # 등락률 상한/하한
        exp_is_limit = er >= LIMIT or er <= -LIMIT          # 예상등락률 상한/하한
        if role == Qt.BackgroundRole:
            if field == "rate" and is_limit:
                return RED if rate > 0 else BLUE
            if field == "exp_rate" and exp_is_limit:
                return RED if er > 0 else BLUE
        if role == Qt.ForegroundRole:
            if field == "rate":
                if is_limit:
                    return WHITE  # 상/하한 배경 위 흰 글씨
                return RED if rate > 0 else BLUE if rate < 0 else None
            if field == "price":
                return RED if rate > 0 else BLUE if rate < 0 else None
            if field == "exp_rate":
                if exp_is_limit:
                    return WHITE
                return RED if er > 0 else BLUE if er < 0 else None
            if field == "exp_price":  # 예상체결가는 예상등락률 부호로 색만
                return RED if er > 0 else BLUE if er < 0 else None
        return None


class ConditionScreen(QWidget):
    """조건검색실시간 화면 하나. 나중에 QMdiArea에 이 위젯을 여러 개 띄우면 다중창."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.model = StockModel()

        # 툴바: 조건식 선택 / 등록 토글 / 이탈삭제 / 종목수
        self.condition_combo = QComboBox()
        self.condition_combo.setFixedWidth(320)  # 창 크기와 무관하게 고정
        # 등록/해제 버튼 없음: 콤보에서 조건 고르는 순간 바로 등록됨(영웅문 방식).
        self.refresh_btn = QPushButton()  # 현재 조건 편입목록 새로 받아오기(해제->재등록)
        self.refresh_btn.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_BrowserReload))
        self.refresh_btn.setToolTip("재조회")
        self.refresh_btn.setFixedWidth(32)
        self.auto_refresh = QCheckBox("자동재조회")  # 동시호가 때 편입/이탈 수동갱신용
        self.auto_refresh.setToolTip("동시호가 때 편입/이탈이 실시간으로 안 와서 주기적으로 재조회")
        self.refresh_interval = QSpinBox()
        self.refresh_interval.setRange(2, 30)  # 2초 미만은 유량초과 위험
        self.refresh_interval.setValue(3)
        self.refresh_interval.setSuffix("초")
        self.refresh_interval.setFixedWidth(72)
        self.auto_remove = QCheckBox("이탈삭제")
        self.auto_remove.setChecked(True)
        self.buy_sort = QCheckBox("매수세정렬")
        self.buy_sort.setToolTip("예상상한&매도0 → 매수잔량순 / 예상등락률순 / 등락률순")
        self.count_label = QLabel("종목수: 0")

        top = QHBoxLayout()
        top.addWidget(self.condition_combo)
        top.addWidget(self.refresh_btn)
        top.addWidget(self.auto_refresh)
        top.addWidget(self.refresh_interval)
        top.addWidget(self.auto_remove)
        top.addWidget(self.buy_sort)
        top.addStretch(1)  # 남는 공간은 오른쪽으로
        top.addWidget(self.count_label)

        # 그리드
        self.proxy = TieredProxy()
        self.proxy.setSourceModel(self.model)
        self.proxy.setSortRole(Qt.UserRole)
        self.table = QTableView()
        self.table.setModel(self.proxy)
        self.table.setSortingEnabled(True)
        self.table.sortByColumn(0, Qt.DescendingOrder)  # 등락률 내림차순
        self.buy_sort.toggled.connect(self._on_buy_sort)
        # 매수세정렬은 정렬키가 정렬컬럼(0) 밖의 값(매수잔량 등)이라 Qt 자동재정렬이
        # 안 걸림 -> 데이터 변경 시 직접 재정렬(디바운스로 틱마다 과다정렬 방지).
        self._resort_timer = QTimer(self)
        self._resort_timer.setSingleShot(True)
        self._resort_timer.timeout.connect(self.proxy.invalidate)
        self.model.dataChanged.connect(
            lambda *a: self._resort_timer.start(200) if self.buy_sort.isChecked() else None)
        self.table.verticalHeader().setVisible(False)
        self.table.verticalHeader().setDefaultSectionSize(22)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        self.table.horizontalHeader().setStretchLastSection(True)
        self.table.setColumnWidth(1, 110)
        self.table.setColumnWidth(BAR_COL, 70)
        self.table.setItemDelegateForColumn(BAR_COL, BarDelegate(self.table))
        self.table.setSelectionBehavior(QTableView.SelectRows)
        self.table.setFont(QFont("맑은 고딕", 9))
        self.table.setEditTriggers(QTableView.NoEditTriggers)
        self.table.clicked.connect(self._on_cell_clicked)
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._on_context_menu)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.addLayout(top)
        layout.addWidget(self.table)

        self.model.rowsInserted.connect(self._update_count)
        self.model.rowsRemoved.connect(self._update_count)

        # 컬럼 너비/순서 기억: 저장된 상태 복원 후, 변경 시 debounce 저장
        self._settings = QSettings("layout.ini", QSettings.IniFormat)
        state = self._settings.value("header")
        if state is not None:
            self.table.horizontalHeader().restoreState(state)
        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.timeout.connect(self._save_layout)
        hdr = self.table.horizontalHeader()
        hdr.sectionResized.connect(lambda *a: self._save_timer.start(400))
        hdr.sectionMoved.connect(lambda *a: self._save_timer.start(400))

        # 예상체결가가 멎으면 자동으로 비우는 staleness 타이머
        self._exp_timer = QTimer(self)
        self._exp_timer.timeout.connect(lambda: self.model.clear_stale_exp())
        self._exp_timer.start(1000)

    def _update_count(self):
        self.count_label.setText(f"종목수: {self.model.rowCount()}")

    def _save_layout(self):
        self._settings.setValue("header", self.table.horizontalHeader().saveState())
        self._settings.sync()  # 강제 종료돼도 디스크에 남게

    def _on_cell_clicked(self, index):
        """종목명 클릭 -> 종목코드 클립보드 복사."""
        if index.column() != FIELDS.index("name"):
            return
        code = self.model.codes[self.proxy.mapToSource(index).row()]
        QApplication.clipboard().setText(code)
        QToolTip.showText(QCursor.pos(), f"{code} 복사됨")

    def _on_context_menu(self, pos):
        """종목명 우클릭 -> 네이버 종목토론실 브라우저로 열기."""
        index = self.table.indexAt(pos)
        if not index.isValid() or index.column() != FIELDS.index("name"):
            return
        code = self.model.codes[self.proxy.mapToSource(index).row()]
        QDesktopServices.openUrl(QUrl(f"https://finance.naver.com/item/board.naver?code={code}"))

    def _on_buy_sort(self, on: bool):
        self.proxy.buy_mode = on
        if on:  # 커스텀 3티어 정렬 적용 (헤더 클릭 정렬은 잠금)
            self.table.setSortingEnabled(False)
            self.proxy.sort(0, Qt.AscendingOrder)
        else:   # 기본 등락률 내림차순 복귀
            self.table.setSortingEnabled(True)
            self.table.sortByColumn(0, Qt.DescendingOrder)

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
                "price": price, "exp_price": price + random.randint(-2, 2) * 5,
                "open": int(price * random.uniform(0.92, 1.05)),
                "low": int(price * 0.9), "high": int(price * 1.1),
                "base": (b := int(price / random.uniform(1.0, 1.3))),  # 전일종가(상승분 역산)
                "upper": int(b * 1.3), "lower": int(b * 0.7),
                "prev_vol": random.randint(50_000, 30_000_000),
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
