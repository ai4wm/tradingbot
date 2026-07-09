# -*- coding: utf-8 -*-
"""[0156] 조건검색실시간 스타일 그리드.

화면 = 위젯(ConditionScreen) 원칙: 나중에 QMdiArea에 넣으면 그대로 다중창이 된다.
웹소켓 계층은 on_included / on_tick / on_excluded 세 메서드만 호출하면 된다.
"""
import logging
import sys
import time
from collections import deque

from PySide6.QtCore import (
    QAbstractTableModel, QModelIndex, QPoint, QRect, QSettings, QSortFilterProxyModel, Qt, QTimer, QUrl,
)
from PySide6.QtGui import QColor, QCursor, QDesktopServices, QFont, QIcon, QPainter, QPixmap, QPolygon
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QHBoxLayout, QHeaderView, QLabel,
    QMainWindow, QPushButton, QSpinBox, QStyle, QStyledItemDelegate, QTableView, QToolTip,
    QVBoxLayout, QWidget,
)

log = logging.getLogger("gui")

COLUMNS = ["등락률", "연상", "종목명", "현재가", "예상체결가", "L일봉H", "예상등락률", "전일거래량", "거래량", "매도잔량", "매수잔량", "예상체결량", "체결/분", "시가총액", "상한가진입시간"]
FIELDS  = ["rate",   "streak", "name",  "price", "exp_price", "bar",    "exp_rate",   "prev_vol", "vol",   "ask_qty",  "bid_qty",  "exp_qty",  "tpm",    "mcap",   "time"]
# 컬럼은 아니지만 L일봉H 그리기에 필요한 저장 필드 (시/저/고/전일종가/상한/하한)
# streak(연상)/mcap(시가총액)/tpm(체결/분)은 저장 안 함: 매번 계산
STORED = (set(FIELDS) - {"streak", "mcap", "tpm"}) | {"open", "low", "high", "base", "upper", "lower"}
BAR_COL = FIELDS.index("bar")
NAME_COL = FIELDS.index("name")
STREAK_COL = FIELDS.index("streak")
MCAP_COL = FIELDS.index("mcap")
TPM_COL = FIELDS.index("tpm")
BAR_ROLE = Qt.UserRole + 1  # 델리게이트에 (open, high, low, close, base, upper, lower) 전달
NXT_ROLE = Qt.UserRole + 2  # NameDelegate에 NXT 종목 여부 전달
MISU_ROLE = Qt.UserRole + 3  # NameDelegate에 미수가능 여부 전달
NEW_ROLE = Qt.UserRole + 4  # NameDelegate에 신규상장 단계 전달 (3=당일 2=15일이내 1=30일이내 0=아님)

LIMIT = 29.5  # 상한/하한 판정 임계 (KRX +-30%)
DESC_FIRST = {"bid_qty", "rate", "price", "exp_price", "exp_rate", "streak", "tpm"}  # 첫 클릭 내림차순 컬럼
RED  = QColor("#e83030")
BLUE = QColor("#2050d0")
PURPLE = QColor("#C080F0")  # 코스닥 종목명
ADMIN = QColor("#FF6A3D")   # 관리종목 종목명 (경고 주황빨강, 코스닥보다 우선)
NXT_MARK = QColor("#FFDD00")  # NXT 좌상단 삼각형 (밝은 노랑)
MISU_MARK = QColor("#33C24D")  # 미수가능 우상단 삼각형 (녹색)
NEW_MARKS = {3: QColor("#FF3DC8"), 2: QColor("#38B8FF"), 1: QColor("#8098B8")}  # 신규: 당일/15일/30일
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


class NameDelegate(QStyledItemDelegate):
    """종목명 셀: 기본 렌더(글자색=코스닥 보라/관리 주황) 후 모서리 삼각형.
    좌상단 노랑=NXT, 우상단 녹색=미수가능(증거금100%는 무표시),
    좌하단=신규상장(마젠타=당일, 하늘=15일이내, 청회=30일이내)."""

    def paint(self, painter, option, index):
        super().paint(painter, option, index)
        nxt, misu = index.data(NXT_ROLE), index.data(MISU_ROLE)
        new = index.data(NEW_ROLE)
        if not (nxt or misu or new):
            return
        r = option.rect
        s = 10
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setPen(Qt.NoPen)
        if nxt:
            painter.setBrush(NXT_MARK)
            painter.drawPolygon(QPolygon([QPoint(r.left(), r.top()),
                                          QPoint(r.left() + s, r.top()),
                                          QPoint(r.left(), r.top() + s)]))
        if misu:
            painter.setBrush(MISU_MARK)
            painter.drawPolygon(QPolygon([QPoint(r.right(), r.top()),
                                          QPoint(r.right() - s, r.top()),
                                          QPoint(r.right(), r.top() + s)]))
        if new:
            painter.setBrush(NEW_MARKS[new])
            painter.drawPolygon(QPolygon([QPoint(r.left(), r.bottom()),
                                          QPoint(r.left() + s, r.bottom()),
                                          QPoint(r.left(), r.bottom() - s)]))
        painter.restore()


def _at_limit(d: dict) -> bool:
    """상한가 상태: 실제(현재가=상한가) 또는 예상(예상등락률≥상한).
    동시호가 땐 체결 전이라 예상으로, 장중엔 실제로 잡힌다."""
    return (d["upper"] > 0 and d["price"] == d["upper"]) or (d["exp_price"] > 0 and d["exp_rate"] >= LIMIT)


def _eff_rate(d: dict) -> float:
    """유효 등락률: 예상값이 살아있으면(동시호가/VI/단일가) 예상등락률, 아니면 실제.
    VI/단일가 종목은 rate가 마지막 체결에 얼어있어 예상으로 비교해야 순위가 맞다."""
    return d["exp_rate"] if d["exp_price"] else d["rate"]


class TieredProxy(QSortFilterProxyModel):
    """상한가정렬 모드(limit_mode):
    상한(실제/예상)&매도잔량0 그룹을 항상 위로 고정하고, 그룹 안은 현재 정렬컬럼으로
    정렬(아무 컬럼이나 헤더 클릭). 비그룹은 아래에 등락률 내림차순 고정.
    모드 off면 전 컬럼 일반 정렬."""

    def __init__(self):
        super().__init__()
        self.limit_mode = False

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        # 세로 헤더 = 순위: 프록시 행번호(정렬 순서)로 1..N. 소스 매핑 안 함(편입순서 X).
        if orientation == Qt.Vertical and role == Qt.DisplayRole:
            return section + 1
        return super().headerData(section, orientation, role)

    def lessThan(self, left, right):
        if self.limit_mode:
            m = self.sourceModel()
            a = m.rows[m.codes[left.row()]]
            b = m.rows[m.codes[right.row()]]
            ga = _at_limit(a) and a["ask_qty"] == 0
            gb = _at_limit(b) and b["ask_qty"] == 0
            desc = self.sortOrder() == Qt.DescendingOrder
            if ga != gb:  # 비그룹은 정렬방향 무관 항상 맨 아래
                return desc if not ga else (not desc)
            if not ga:    # 비그룹끼리: 유효 등락률 내림차순 고정(방향 무관)
                return _eff_rate(a) < _eff_rate(b) if desc else _eff_rate(a) > _eff_rate(b)
            # 그룹끼리: 현재 정렬컬럼으로 일반 비교
        return super().lessThan(left, right)


class StockModel(QAbstractTableModel):
    def __init__(self):
        super().__init__()
        self.codes: list[str] = []          # 행 순서
        self.rows: dict[str, dict] = {}     # code -> {field: value}
        # 예상값 표시 ON은 국면 확정 신호(hot)로만: 0H수신 / 단일가마킹 / VI발동 / 동시호가·VI REST.
        # 0D 23/24는 연속매매 중에도 값이 미세하게 변하며 옴 -> ON 신호로 쓰면 오탐(012160 영흥).
        # 켜진 뒤엔 0D값으로 갱신은 허용. 끄기는 exp_price=0 / 체결재개(거래량↑) / VI해제.
        self._exp_live: set[str] = set()     # 예상 컬럼 표시중
        self.kosdaq: set[str] = set()        # 코스닥 코드 집합 (main이 시작 시 주입)
        self.single: set[str] = set()        # 단일가 매매 종목: 예상값 상시 표시 (main 주입)
        self.nxt: set[str] = set()           # 넥스트레이드(NXT) 거래가능: 좌상단 노랑 삼각형 (main 주입)
        self.misu: set[str] = set()          # 미수가능(증거금<100%): 우상단 녹색 삼각형 (main 주입)
        self.admin: set[str] = set()         # 관리종목: 종목명 경고색 (코스닥보다 우선, main 주입)
        self.limit_cnt: dict[str, int] = {}  # 어제 연속상한 일수 ka10017 (main 주입, 연상 컬럼)
        self.limit_rolled = False            # 장마감 후 조회: cnt에 오늘 연장분 포함 -> +1 억제
        self.new_today: set[str] = set()     # 상장 당일 (main 주입, 좌하단 마젠타)
        self.new15: set[str] = set()         # 상장 15일 이내 (좌하단 하늘)
        self.new30: set[str] = set()         # 상장 16~30일 (좌하단 청회)
        self.shares: dict[str, int] = {}     # 상장주식수 ka10099 (main 주입, 시가총액 컬럼)
        self.ticks: dict[str, deque] = {}    # 체결 틱 시각(monotonic) 최근 60초 (체결/분 컬럼)

    # --- 웹소켓/전략 계층이 부르는 API ---------------------------------
    def add_stock(self, code: str, data: dict):
        if code in self.rows:
            self.update_stock(code, data)
            return
        row = len(self.codes)
        self.beginInsertRows(QModelIndex(), row, row)
        self.codes.append(code)
        self.rows[code] = {f: "" if f in ("name", "time") else 0 for f in STORED}
        self.endInsertRows()
        self.update_stock(code, data)  # exp 게이트/파생/로그를 신규 행에도 동일 적용

    def remove_stock(self, code: str):
        if code not in self.rows:
            return
        row = self.codes.index(code)
        self.beginRemoveRows(QModelIndex(), row, row)
        self.codes.remove(code)
        del self.rows[code]
        self._exp_live.discard(code)
        self.ticks.pop(code, None)
        self.endRemoveRows()

    def set_vi(self, code: str, active: bool, price: int = 0):
        if active and price:  # 발동가로 즉시 채움, 이후 틱이 덮어씀
            self.update_stock(code, {"exp_price": price, "exp_hot": 1})
        elif not active:
            self.update_stock(code, {"exp_price": 0, "exp_qty": 0})  # 해제 즉시 비움

    def update_stock(self, code: str, fields: dict):
        if code not in self.rows:
            return
        row = self.codes.index(code)
        stored = self.rows[code]
        hot = fields.get("exp_hot", 0) or code in self.single  # 0H발/단일가종목 = 국면 확정
        fields = {f: v for f, v in fields.items() if f in STORED}  # 모르는 키 무시
        if fields.get("prev_vol") == 0 and stored.get("prev_vol"):
            fields.pop("prev_vol")  # 전일거래량=정적값. 0(동시호가 역산실패)으로 덮어쓰기 금지
        if "exp_price" in fields:
            if not fields["exp_price"]:
                if code in self._exp_live:
                    self._exp_live.discard(code)
                    log.info("expOFF %s zero", code)
            elif hot:                          # 확정신호 -> 켜고 값 갱신
                if code not in self._exp_live:
                    self._exp_live.add(code)
                    log.info("expON %s %s", code, fields["exp_price"])
            elif code not in self._exp_live:   # 안 켜진 상태의 0D값 = 연속매매 echo -> 무시
                fields.pop("exp_price")
                fields.pop("exp_qty", None)
            # 이미 켜진(VI/단일가) 종목의 0D값은 그대로 통과 -> 실시간 갱신
        if (code in self._exp_live and code not in self.single
                and "exp_price" not in fields and fields.get("vol", 0) > stored["vol"]):
            self._exp_live.discard(code)  # 체결 재개 = 국면 종료 (단일가 종목은 유지)
            fields["exp_price"], fields["exp_qty"] = 0, 0
            log.info("expOFF %s vol", code)
        if fields.get("vol", 0) > stored["vol"]:  # 거래량 증가 = 체결 틱 1건 (체결/분)
            dq = self.ticks.setdefault(code, deque())
            now = time.monotonic()
            dq.append(now)
            while dq and dq[0] < now - 60:
                dq.popleft()
        cols = set()
        for f, v in fields.items():
            if stored.get(f) == v:
                continue
            if f in FIELDS:
                cols.add(FIELDS.index(f))
            if f == "vol":  # 체결/분 의존
                cols.add(TPM_COL)
            if f in ("price", "open", "low", "high", "base", "upper", "lower"):  # L일봉H 의존
                cols.add(BAR_COL)
            if f in ("price", "upper", "exp_price"):  # 연상 판정(_at_limit) 의존
                cols.add(STREAK_COL)
            if f in ("price", "base"):  # 시가총액 의존
                cols.add(MCAP_COL)
        stored.update(fields)
        # 예상등락률은 예상체결가/전일종가에서 파생 (동시호가/VI 때만 값이 옴)
        if "exp_price" in fields or "base" in fields:
            ep, base = stored["exp_price"], stored["base"]
            er = round((ep - base) / base * 100, 2) if (ep and base) else 0.0
            if stored.get("exp_rate") != er:
                stored["exp_rate"] = er
                cols.add(FIELDS.index("exp_rate"))
        if cols:  # 바뀐 셀만 갱신
            self.dataChanged.emit(self.index(row, min(cols)), self.index(row, max(cols)))

    def refresh_tpm(self):
        """체결/분 감쇠 갱신: 틱이 끊긴 종목도 1초마다 재계산되게 컬럼 전체 리페인트."""
        if self.codes:
            self.dataChanged.emit(self.index(0, TPM_COL), self.index(len(self.codes) - 1, TPM_COL))

    # --- Qt 모델 구현 ---------------------------------------------------
    def rowCount(self, parent=QModelIndex()):
        return len(self.codes)

    def columnCount(self, parent=QModelIndex()):
        return len(COLUMNS)

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if orientation == Qt.Horizontal and role in (Qt.DisplayRole, Qt.ToolTipRole):
            return COLUMNS[section]  # 툴팁: 칸 좁혀 헤더 글자 잘려도 오버로 확인
        return None

    def data(self, index, role=Qt.DisplayRole):
        field = FIELDS[index.column()]
        stored = self.rows[self.codes[index.row()]]
        if field == "streak":  # 연상 = 어제cnt + (지금 상한이면 1), 매번 계산 (저장 안 함)
            cnt = self.limit_cnt.get(self.codes[index.row()], 0)
            # rolled(장마감 후 조회): 연장 종목은 cnt에 오늘분 포함 -> +1 생략.
            # 오늘 첫 상한(cnt=0)은 목록에 없어서 여전히 +1 필요.
            n = cnt + (1 if _at_limit(stored) and not (self.limit_rolled and cnt) else 0)
            if role == Qt.DisplayRole:
                return str(n) if n else ""
            if role == Qt.UserRole:
                return n
            if role == Qt.TextAlignmentRole:
                return Qt.AlignCenter
            if role == Qt.ForegroundRole and n:
                return RED
            return None
        if field == "tpm":  # 체결/분 = 최근 60초 체결 틱수, 매번 계산 (저장 안 함)
            t0 = time.monotonic() - 60
            n = sum(1 for t in self.ticks.get(self.codes[index.row()], ()) if t >= t0)
            if role == Qt.DisplayRole:
                return str(n) if n else ""
            if role == Qt.UserRole:
                return n
            if role == Qt.TextAlignmentRole:
                return Qt.AlignRight | Qt.AlignVCenter
            return None
        if field == "mcap":  # 시가총액(억) = 상장주식수 x 현재가(체결 전엔 전일종가), 매번 계산
            v = self.shares.get(self.codes[index.row()], 0) * (stored["price"] or stored["base"]) // 100_000_000
            if role == Qt.DisplayRole:
                return f"{v:,}" if v else ""
            if role == Qt.UserRole:
                return v
            if role == Qt.TextAlignmentRole:
                return Qt.AlignRight | Qt.AlignVCenter
            return None
        value = stored[field]

        if role == BAR_ROLE and field == "bar":  # 델리게이트용
            return (stored["open"], stored["high"], stored["low"], stored["price"],
                    stored["base"], stored["upper"], stored["lower"])
        if role == NXT_ROLE:  # 델리게이트 모서리 삼각형 판단
            return self.codes[index.row()] in self.nxt
        if role == MISU_ROLE:
            return self.codes[index.row()] in self.misu
        if role == NEW_ROLE:
            code = self.codes[index.row()]
            return (3 if code in self.new_today else 2 if code in self.new15
                    else 1 if code in self.new30 else 0)
        if role == Qt.ToolTipRole and field == "name":  # 모서리 삼각형 설명
            code = self.codes[index.row()]
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
            if field == "bar":
                return ""  # 델리게이트가 그림
            if field == "rate":
                return f"{value:+.2f}"
            if field == "exp_rate":
                return f"{value:+.2f}" if value else ""
            if field in ("price", "exp_price", "prev_vol", "vol", "ask_qty", "bid_qty", "exp_qty"):
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
        up, lo, pr, ep = stored["upper"], stored["lower"], stored["price"], stored["exp_price"]
        # 상한/하한가 값이 있으면 실제 도달 여부로 판정(29.75%≠30% 오탐 방지), 없으면 rate 폴백
        if up > 0 and lo > 0:
            is_limit = pr >= up or pr <= lo
            exp_is_limit = ep >= up or (ep > 0 and ep <= lo)
        else:
            is_limit = rate >= LIMIT or rate <= -LIMIT
            exp_is_limit = er >= LIMIT or er <= -LIMIT
        if role == Qt.BackgroundRole:
            if field == "rate" and is_limit:
                return RED if rate > 0 else BLUE
            if field == "exp_rate" and exp_is_limit:
                return RED if er > 0 else BLUE
        if role == Qt.ForegroundRole:
            if field == "name":
                code = self.codes[index.row()]
                if code in self.admin:       # 관리종목 = 경고색 (코스닥보다 우선)
                    return ADMIN
                return PURPLE if code in self.kosdaq else None
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


def _list_reload_icon(style) -> QIcon:
    """새로고침 아이콘(=오른쪽 재조회 버튼과 동일)을 메인으로, 좌하단에 작은 목록 아이콘을
    배지로 얹어 '조건목록 재조회'임을 구분. 재조회 버튼과 크기/모양 일관성 유지."""
    base = style.standardIcon(QStyle.StandardPixmap.SP_BrowserReload).pixmap(18, 18)
    over = style.standardIcon(QStyle.StandardPixmap.SP_FileDialogDetailedView).pixmap(10, 10)
    p = QPainter(base)
    p.drawPixmap(0, base.height() - over.height(), over)  # 좌하단 작은 목록 배지
    p.end()
    return QIcon(base)


class ConditionScreen(QWidget):
    """조건검색실시간 화면 하나. 나중에 QMdiArea에 이 위젯을 여러 개 띄우면 다중창."""

    def __init__(self, prefix: str = "", parent=None):
        super().__init__(parent)
        self.prefix = prefix  # 다중창: 창별 설정 키 접두사 ("", "w2_", ...)
        self.model = StockModel()

        # 툴바: 조건목록 새로고침 / 조건식 선택 / 등록 토글 / 이탈삭제 / 종목수
        self.reload_btn = QPushButton()  # 조건 목록(CNSRLST) 새로 받기: 영웅문서 조건 추가/수정 시
        self.reload_btn.setIcon(_list_reload_icon(self.style()))
        self.reload_btn.setToolTip("조건목록 재조회 — 영웅문에서 새로 만들거나 수정한 조건식을 목록에 반영")
        self.reload_btn.setFixedWidth(32)
        self.condition_combo = QComboBox()
        self.condition_combo.setFixedWidth(220)  # 창 크기와 무관하게 고정 (굴림9 기준 한글 ~20자)
        # 등록/해제 버튼 없음: 콤보에서 조건 고르는 순간 바로 등록됨(영웅문 방식).
        self.refresh_btn = QPushButton()  # 현재 조건 편입목록 새로 받아오기(해제->재등록)
        self.refresh_btn.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_BrowserReload))
        self.refresh_btn.setToolTip("재조회 — 현재 조건의 편입 종목을 지금 다시 받아옵니다")
        self.refresh_btn.setFixedWidth(32)
        self.auto_refresh = QCheckBox("자동재조회")  # 동시호가 때 편입/이탈 수동갱신용
        self.auto_refresh.setToolTip("동시호가 때 편입/이탈이 실시간으로 안 와서 주기적으로 재조회")
        self.refresh_interval = QSpinBox()
        self.refresh_interval.setRange(2, 30)  # 2초 미만은 유량초과 위험
        self.refresh_interval.setValue(3)
        self.refresh_interval.setSuffix("초")
        self.refresh_interval.setFixedWidth(90)
        # 화살표로 값 변경 시 텍스트가 선택돼(어두운 배경) 안 보이는 것 방지.
        # Qt가 시그널 뒤에 선택을 다시 걸기 때문에 이벤트루프 한 틱 뒤에 해제.
        self.refresh_interval.valueChanged.connect(
            lambda _: QTimer.singleShot(0, self.refresh_interval.lineEdit().deselect))
        self.auto_remove = QCheckBox("자동삭제")
        self.auto_remove.setChecked(True)
        self.auto_remove.setToolTip("이탈한 종목을 그리드에서 자동 제거")
        self.sound_check = QCheckBox("소리")
        self.sound_check.setToolTip("새 종목이 편입되면 소리 알림 (실시간/재조회 모두)")
        self.limit_sort = QCheckBox("상한가정렬")
        self.limit_sort.setToolTip("상한(실제/예상)&매도0 종목을 위로 고정, 컬럼 클릭으로 그룹 내 정렬")
        self.unified_check = QPushButton("K")  # KRX<->통합(_AL) 시세 전환 토글, 전 창 공통 (main이 배선)
        self.unified_check.setCheckable(True)
        self.unified_check.setFixedSize(24, 24)
        self.unified_check.setToolTip("시세 소스 전환 — KRX 전용 / KRX+NXT 통합(_AL). "
                                      "편입/이탈(조건검색)은 KRX 기준 그대로")
        self.unified_check.toggled.connect(self._on_unified_style)
        self.rank_btn = QPushButton("순위")
        self.rank_btn.setToolTip("실시간 종목조회순위 [0198] 창 열기/닫기")
        self.rank_btn.setFixedWidth(44)
        self.newwin_btn = QPushButton("창+")
        self.newwin_btn.setToolTip("조건검색 창 하나 더 열기 (다른 조건식 동시 감시)")
        self.newwin_btn.setFixedWidth(44)
        self.count_label = QLabel("종목수: 0")
        self.on_top_btn = QPushButton("📌")  # 항상 맨 위 토글 (창별)
        self.on_top_btn.setCheckable(True)
        self.on_top_btn.setFixedWidth(32)
        self.on_top_btn.setToolTip("항상 맨 위 — 이 창을 다른 창들 위에 계속 고정")

        top = QHBoxLayout()
        top.addWidget(self.reload_btn)
        top.addWidget(self.condition_combo)
        top.addWidget(self.refresh_btn)
        top.addWidget(self.auto_refresh)
        top.addWidget(self.refresh_interval)
        top.addWidget(self.auto_remove)
        top.addWidget(self.sound_check)
        top.addWidget(self.limit_sort)
        top.addWidget(self.unified_check)
        top.addWidget(self.rank_btn)
        top.addWidget(self.newwin_btn)
        top.addStretch(1)  # 남는 공간은 오른쪽으로
        top.addWidget(self.count_label)
        top.addWidget(self.on_top_btn)  # 오른쪽 끝 = 창 크롬(핀) 자리

        # 그리드
        self.proxy = TieredProxy()
        self.proxy.setSourceModel(self.model)
        self.proxy.setSortRole(Qt.UserRole)
        self.table = QTableView()
        self.table.setModel(self.proxy)
        # 정렬 수동 제어: 첫 클릭을 내림차순(큰 값 위)부터. Qt 기본은 오름차순이라 직접 처리.
        self.table.setSortingEnabled(False)
        hdr0 = self.table.horizontalHeader()
        hdr0.setSectionsClickable(True)
        hdr0.setSortIndicatorShown(True)
        hdr0.sectionClicked.connect(self._on_header_clicked)
        self._sort_col, self._sort_order = 0, Qt.DescendingOrder  # 기본 등락률 내림차순
        self.limit_sort.toggled.connect(self._on_limit_sort)
        # 상한가정렬은 그룹 판정이 정렬컬럼 밖의 값(상한/매도잔량/예상등락률)이라 Qt 자동재정렬이
        # 안 걸림 -> 데이터 변경 시 직접 재정렬. 스로틀: 실행중이면 리셋 안 함(디바운스로 하면
        # 틱이 200ms보다 자주 오는 장중엔 계속 리셋돼 영영 안 불림 = 재정렬 멈춤 버그).
        self._resort_timer = QTimer(self)
        self._resort_timer.setSingleShot(True)
        self._resort_timer.timeout.connect(self.proxy.invalidate)
        self.model.dataChanged.connect(self._on_data_changed)
        self._tpm_timer = QTimer(self)  # 체결/분: 틱 끊겨도 값이 줄어들게 주기 갱신
        self._tpm_timer.timeout.connect(self.model.refresh_tpm)
        self._tpm_timer.start(1000)
        self.table.verticalHeader().setVisible(True)  # 순위(정렬 순서대로 1..N 자동)
        self.table.verticalHeader().setDefaultSectionSize(22)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        self.table.horizontalHeader().setStretchLastSection(True)
        # 헤더 글자 왼쪽 정렬: 가운데면 칸 좁힐 때 앞자리부터 잘림 (시가총액->총액)
        self.table.horizontalHeader().setDefaultAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self.table.setColumnWidth(NAME_COL, 110)
        self.table.setColumnWidth(STREAK_COL, 34)
        self.table.setColumnWidth(BAR_COL, 70)
        self.table.setItemDelegateForColumn(BAR_COL, BarDelegate(self.table))
        self.table.setItemDelegateForColumn(NAME_COL, NameDelegate(self.table))
        self.table.setSelectionBehavior(QTableView.SelectRows)
        # 폰트는 앱 전역(main.py: 굴림체9 NoAA)에서 상속 — 그리드/툴바 통일
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
        state = self._settings.value(self.prefix + "header")
        if state is not None:
            self.table.horizontalHeader().restoreState(state)
            # restoreState가 옛 정렬값(가운데)까지 되살림 -> 왼쪽 재적용
            self.table.horizontalHeader().setDefaultAlignment(Qt.AlignLeft | Qt.AlignVCenter)
            sec = self.table.horizontalHeader().sortIndicatorSection()
            if sec >= 0:  # 마지막 정렬 컬럼/방향 복원
                self._sort_col = sec
                self._sort_order = self.table.horizontalHeader().sortIndicatorOrder()
        self._apply_sort()
        if self._settings.value(self.prefix + "limit_sort", "false") == "true":  # 상한가정렬 복원
            self.limit_sort.setChecked(True)
        if self._settings.value(self.prefix + "on_top", "false") == "true":  # 항상위 복원
            self.on_top_btn.setChecked(True)  # 연결 전이라 핸들러 안 불림(시각상태만)
            QTimer.singleShot(0, lambda: self._apply_on_top(True))  # 창 붙은 뒤 실제 적용
        self.on_top_btn.toggled.connect(self._on_top_toggle)
        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.timeout.connect(self._save_layout)
        hdr = self.table.horizontalHeader()
        hdr.sectionResized.connect(lambda *a: self._save_timer.start(400))
        hdr.sectionMoved.connect(lambda *a: self._save_timer.start(400))

    def _on_unified_style(self, on: bool):
        # 통합 = 노랑 배경(NXT 마크색)에 '통', KRX = 기본 버튼에 'K'
        self.unified_check.setText("통" if on else "K")
        self.unified_check.setStyleSheet(
            "QPushButton{background:#FFDD00;color:black;font-weight:bold}" if on else "")

    def _on_data_changed(self, *a):
        # 스로틀: 이미 대기중이면 리셋하지 않음 -> 틱이 몰려도 200ms마다 반드시 재정렬됨
        if self.limit_sort.isChecked() and not self._resort_timer.isActive():
            self._resort_timer.start(200)

    def _update_count(self):
        self.count_label.setText(f"종목수: {self.model.rowCount()}")

    def _save_layout(self):
        self._settings.setValue(self.prefix + "header", self.table.horizontalHeader().saveState())
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

    def _apply_sort(self):
        self.table.horizontalHeader().setSortIndicator(self._sort_col, self._sort_order)
        self.proxy.sort(self._sort_col, self._sort_order)

    def _on_header_clicked(self, col: int):
        # 상한가정렬 중에도 헤더 클릭 허용: 그룹 내 정렬 기준이 바뀐다
        if col == self._sort_col:  # 같은 컬럼 재클릭 -> 방향 토글
            self._sort_order = (Qt.AscendingOrder if self._sort_order == Qt.DescendingOrder
                                else Qt.DescendingOrder)
        else:  # 새 컬럼 첫 클릭: DESC_FIRST 컬럼만 내림차순, 나머지는 오름차순부터
            first = Qt.DescendingOrder if FIELDS[col] in DESC_FIRST else Qt.AscendingOrder
            self._sort_col, self._sort_order = col, first
        self._apply_sort()
        self._save_timer.start(400)  # 정렬 상태도 기억

    def _on_limit_sort(self, on: bool):
        self.proxy.limit_mode = on
        self.proxy.invalidate()  # 모드 전환 즉시 재정렬 (정렬컬럼/방향은 그대로)

    def _apply_on_top(self, on: bool):
        w = self.window()  # central widget이라 최상위 QMainWindow
        geo = w.geometry()  # 창 재생성 때 위치 유실 -> 보존
        w.setWindowFlag(Qt.WindowStaysOnTopHint, on)
        w.show()  # 플래그 변경 후 재표시 필수 (안 하면 창이 숨음)
        if not geo.isEmpty():  # 시작 복원 경로(창 뜨기 전, geo 무의미)는 건너뜀
            w.setGeometry(geo)  # 재생성된 창을 원위치로

    def _on_top_toggle(self, on: bool):
        self._apply_on_top(on)
        self._settings.setValue(self.prefix + "on_top", "true" if on else "false")
        self._apply_sort()
        self._settings.setValue(self.prefix + "limit_sort", "true" if on else "false")
        self._settings.sync()

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
