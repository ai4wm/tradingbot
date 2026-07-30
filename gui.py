# -*- coding: utf-8 -*-
"""[0156] 조건검색실시간 스타일 그리드.

화면 = 위젯(ConditionScreen) 원칙: 나중에 QMdiArea에 넣으면 그대로 다중창이 된다.
웹소켓 계층은 on_included / on_tick / on_excluded 세 메서드만 호출하면 된다.
"""
import logging
import math
import sys
import time
from collections import deque
from dataclasses import dataclass

from PySide6.QtCore import (
    QAbstractTableModel, QEvent, QModelIndex, QPoint, QRect, QSettings,
    QSortFilterProxyModel, Qt, QTimer, QUrl, Signal,
)
from PySide6.QtGui import (
    QColor, QCursor, QDesktopServices, QFont, QIcon, QKeySequence, QPainter,
    QPen, QPixmap, QPolygon,
)
from PySide6.QtWidgets import (
    QApplication, QButtonGroup, QCheckBox, QComboBox, QHBoxLayout, QHeaderView, QLabel,
    QDialog, QGridLayout, QLineEdit, QMainWindow, QProxyStyle, QPushButton, QSpinBox,
    QStyle, QStyledItemDelegate, QStyleOptionViewItem, QTableView, QToolTip,
    QVBoxLayout, QWidget,
)

log = logging.getLogger("gui")
audit_log = logging.getLogger("trade.audit")

# 순위/변동: ★조회순위(ka00198) 모드 전용 -> 다른 화면에선 숨김 (set_view_mode)
COLUMNS = ["순위",  "변동",      "등락률", "연상", "종목명", "테마",   "현재가", "예상체결가", "주문",  "L일봉H", "예상등락률", "전일거래량", "거래량", "매도잔량", "매수잔량", "예상체결량", "대금/분",       "시총(억)", "상한가진입시간", "3단매도(만)", "자동취소",       "청산키"]
FIELDS  = ["qrank", "qrank_chg", "rate",   "streak", "name", "theme", "price", "exp_price", "order", "bar",    "exp_rate",   "prev_vol", "vol",   "ask_qty",  "bid_qty",  "exp_qty",  "minute_value", "mcap",   "time",             "balance_sell", "auto_cancel_arm", "exit_hotkey"]
# 컬럼은 아니지만 L일봉H 그리기에 필요한 저장 필드 (시/저/고/전일종가/상한/하한)
# streak(연상)/mcap(시가총액)은 저장 안 함: 매번 계산
BOOK_FIELDS = {f"{side}_{kind}{level if level > 1 else ''}"
               for side in ("ask", "bid") for kind in ("price", "qty")
               for level in range(1, 6)}
STORED = (set(FIELDS) - {
    "streak", "theme", "mcap", "order", "balance_sell",
    "auto_cancel_arm", "exit_hotkey"}) | {
    "open", "low", "high", "base", "upper", "lower"} | BOOK_FIELDS
BAR_COL = FIELDS.index("bar")
RATE_COL = FIELDS.index("rate")
PRICE_COL = FIELDS.index("price")
NAME_COL = FIELDS.index("name")
THEME_COL = FIELDS.index("theme")
TIME_COL = FIELDS.index("time")
VOLUME_COL = FIELDS.index("vol")
BID_QTY_COL = FIELDS.index("bid_qty")
MINUTE_VALUE_COL = FIELDS.index("minute_value")
NON_LIMIT_IGNORED_SORT_COLS = {TIME_COL, BID_QTY_COL}
STREAK_COL = FIELDS.index("streak")
MCAP_COL = FIELDS.index("mcap")
ORDER_COL = FIELDS.index("order")
BALANCE_SELL_COL = FIELDS.index("balance_sell")
AUTO_CANCEL_ARM_COL = FIELDS.index("auto_cancel_arm")
EXIT_HOTKEY_COL = FIELDS.index("exit_hotkey")
BALANCE_SELL_MARKET_LAST_KEY = "balance_sell_market_last"
RANK_COLS = (FIELDS.index("qrank"), FIELDS.index("qrank_chg"))
RANK_CHANGE_COL = FIELDS.index("qrank_chg")
RANK_DEFAULT_WIDTHS = {RANK_COLS[0]: 42, RANK_COLS[1]: 48}
RANK_PERIODS = {  # 순위 계열 기준시간 콤보: (표시, data). 모드 따라 교체
    "rank":   [("30초", "5"), ("1분", "1"), ("10분", "2"), ("1시간", "3"), ("당일", "4")],  # ka00198 qry_tp
    "vsurge": [("1분", "1"), ("3분", "3"), ("5분", "5"), ("10분", "10"), ("30분", "30"), ("60분", "60")],  # ka10023 집계분(tm)
}
BAR_ROLE = Qt.UserRole + 1  # 델리게이트에 (open, high, low, close, base, upper, lower) 전달
NXT_ROLE = Qt.UserRole + 2  # NameDelegate에 NXT 종목 여부 전달
MISU_ROLE = Qt.UserRole + 3  # NameDelegate에 미수가능 여부 전달
NEW_ROLE = Qt.UserRole + 4  # NameDelegate에 신규상장 단계 전달 (3=당일 2=15일이내 1=30일이내 0=아님)
SHORT_OVERHEAT_ROLE = Qt.UserRole + 5  # NameDelegate에 단기과열(30분 단일가) 여부 전달
BUY_TREND_ROLE = Qt.UserRole + 6  # 매수% 추세: 최근 20초 비중 vs 이전 40초 (-1/0/+1)
ORDER_CANCEL_ROLE = Qt.UserRole + 13  # 주문 셀 오른쪽 즉시 잔량취소 영역
THEME_LEADER_ROLE = Qt.UserRole + 14  # 테마정렬에서 각 묶음의 첫 대장 종목
THEME_SINGLETON_ROLE = Qt.UserRole + 15  # 현재 조건검색에서 단독인 테마 종목
THEME_COLOR_ROLE = Qt.UserRole + 16  # 테마정렬에서 종목명 셀 왼쪽 세로바 색

# 단타 예측: (표시명, 과거 관찰구간(초), 최소 표본기간(초), 모멘텀 스케일(bp),
#              선행압력/매수흐름/모멘텀/가격지속/VWAP/체결가속/체결지속 가중치, 종합 가중치)
# 3·5·10분은 예측 목표구간이며, 관찰구간은 각각 1·3·5분이다.
PREDICT_HORIZONS = (
    ("3분", 60, 20, 80,  (0.30, 0.22, 0.13, 0.08, 0.05, 0.07, 0.15), 0.30),
    ("5분", 180, 60, 150, (0.22, 0.22, 0.18, 0.12, 0.08, 0.05, 0.13), 0.45),
    ("10분", 300, 120, 250, (0.13, 0.18, 0.22, 0.17, 0.13, 0.05, 0.12), 0.25),
)
PROGRAM_PREDICT_WEIGHTS = (0.10, 0.15, 0.20)  # 3·5·10분 예측에서 프로그램 수급 최대 반영률
LIMIT = 29.5  # 상한/하한 판정 임계 (KRX +-30%)
# ponytail: 매크로가 2주+로 갈아타면 이 값을 올리거나 금액기준(delta*price)으로 교체
DESC_FIRST = {
    "bid_qty", "rate", "price", "exp_price", "exp_rate", "streak",
    "qrank_chg", "minute_value",
}  # 첫 클릭 내림차순 컬럼
RED  = QColor("#e83030")
BLUE = QColor("#2050d0")
PURPLE = QColor("#C080F0")  # 코스닥 종목명
ADMIN = QColor("#FF6A3D")   # 관리종목 종목명 (경고 주황빨강, 코스닥보다 우선)
NXT_EXCLUDE_BG_DARK = QColor("#6B2B2B")   # 다크 테마: NXT 매매 제외 확인용 종목명 배경
NXT_EXCLUDE_BG_LIGHT = QColor("#FFD6D6")  # 라이트 테마: NXT 매매 제외 확인용 종목명 배경
SHORT_OVERHEAT_BG_DARK = QColor("#704800")   # 다크 테마: 단기과열 30분 단일가
SHORT_OVERHEAT_BG_LIGHT = QColor("#FFE0A6")  # 라이트 테마: 단기과열 30분 단일가
MISU_MARK = QColor("#33C24D")  # 미수가능 우상단 삼각형 (녹색)
NEW_MARKS = {3: QColor("#FF3DC8"), 2: QColor("#38B8FF"), 1: QColor("#8098B8")}  # 신규: 당일/15일/30일
WHITE = QColor("white")
TRACK = QColor("#d8d8d8")
CENTER = QColor("#707070")  # L일봉H 0% 중심선
WATCH_BG = QColor("#FFD54F")  # 실시간 뉴스 감시: 연상 셀 배경
WATCH_TEXT = QColor("#111111")
THEME_LEADER = QColor("#F4B400")  # 테마 대장 표시: 등락률 색과 분리한 금색
THEME_SINGLETON = QColor("#90A4AE")  # 단독 테마 표시: 대장과 구분하는 회색
THEME_SINGLETON_BAR = QColor("#455A64")  # 단독 테마 전용 세로바: 어두운 청회색
THEME_GROUP_COLORS = (
    QColor("#00C2A8"), QColor("#F4B400"), QColor("#9B7BFF"),
    QColor("#FF6E8A"), QColor("#4FC3F7"), QColor("#8BC34A"),
)


def _theme_group_color(group: tuple[str, str]) -> QColor | None:
    """같은 테마는 항상 같은 색, 다음 테마는 팔레트의 다음 계열 색으로 표시한다."""
    if not group or group[0] == "none":
        return None
    # Python hash는 실행마다 달라지므로 종목 순서/재시작과 무관한 문자 합산값을 쓴다.
    value = sum((position + 1) * ord(character)
                for position, character in enumerate(f"{group[0]}:{group[1]}"))
    return THEME_GROUP_COLORS[value % len(THEME_GROUP_COLORS)]


def _theme_family(theme: str) -> str:
    """자동 수집된 세부 테마만 보수적으로 상위 계열로 묶는다."""
    value = str(theme or "").casefold()
    # 수동 등록한 AI·챗봇·피지컬AI는 서로 다른 수급 테마다.
    # 이들을 하나로 합치면 피지컬AI/로봇 장세에서 일반 AI 종목이 섞인다.
    if value in {
        "인공지능(ai)", "ai 챗봇(챗gpt 등)",
        "피지컬 ai/휴머노이드 로봇",
    }:
        return ""
    # 백신 테마의 '신종플루, AI 등'은 인공지능 계열로 오인하지 않는다.
    if "신종플루" in value:
        return ""
    if ("인공지능" in value or "ai 챗봇" in value
            or "피지컬 ai" in value or "온디바이스 ai" in value):
        return "AI·인공지능"
    return ""


def _theme_group_precedence(group: tuple[str, str]) -> int:
    """같은 종목의 넓은 테마와 세부 테마가 겹칠 때 세부 테마를 우선한다."""
    if group[0] != "theme":
        return 1
    if group[1] == "2차전지(장비)":
        return 0
    if group[1] == "2차전지":
        return 2
    return 1


@dataclass(slots=True)
class PredictionBucket:
    """종목별 1초 체결 요약. 장기 단타점수 메모리를 종목당 300행으로 제한한다."""

    sec: int
    buy_qty: int = 0
    sell_qty: int = 0
    buy_count: int = 0
    sell_count: int = 0
    open_price: int = 0
    close_price: int = 0
    traded_value: int = 0
    traded_qty: int = 0
    tick_count: int = 0


@dataclass(slots=True)
class ProgramBucket:
    """0w 누적 프로그램매매 수량을 1초 단위 차분으로 압축한다."""

    sec: int
    buy_qty: int = 0
    sell_qty: int = 0


def _draw_selection_lines(painter, rect, palette):
    painter.save()
    # 한 색으로는 흰/검정 배경 모두 대비가 부족하므로 현재 시스템 팔레트에 맞춰 전환.
    dark = palette.base().color().lightness() < 128
    painter.setPen(QColor("#4FC3F7") if dark else QColor("#0057FF"))
    painter.drawLine(rect.left(), rect.top(), rect.right(), rect.top())
    painter.drawLine(rect.left(), rect.bottom(), rect.right(), rect.bottom())
    painter.restore()


def _is_current_row(option, index):
    """네이티브 선택 대신 현재 클릭한 행을 자체 선택 표시로 사용."""
    view = option.widget
    current = view.currentIndex() if view is not None else QModelIndex()
    return current.isValid() and current.row() == index.row()


class VisibleCheckStyle(QProxyStyle):
    """비활성 창에서도 체크 상태가 다크 배경에 묻히지 않게 직접 그린다."""

    def drawPrimitive(self, element, option, painter, widget=None):
        if (element == QStyle.PE_IndicatorCheckBox
                and option.state & QStyle.State_On
                and option.state & QStyle.State_Enabled):
            painter.save()
            painter.setRenderHint(QPainter.Antialiasing, True)
            r = option.rect.adjusted(1, 1, -1, -1)
            painter.setPen(QPen(QColor("#8E249F"), 1))
            painter.setBrush(QColor("#B638C7"))
            painter.drawRoundedRect(r, 3, 3)
            painter.setPen(QPen(WHITE, 2, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
            painter.drawPolyline(QPolygon([
                QPoint(r.left() + r.width() * 2 // 9, r.center().y()),
                QPoint(r.left() + r.width() * 4 // 9, r.bottom() - r.height() * 2 // 9),
                QPoint(r.right() - r.width() // 7, r.top() + r.height() // 4),
            ]))
            painter.restore()
            return
        super().drawPrimitive(element, option, painter, widget)


class PreserveTextColorDelegate(QStyledItemDelegate):
    """셀은 평상시 그대로 그리고 선택 행에는 위/아래 선만 추가."""

    def paint(self, painter, option, index):
        selected = _is_current_row(option, index)
        opt = QStyleOptionViewItem(option)
        opt.state &= ~(QStyle.State_Selected | QStyle.State_HasFocus)
        super().paint(painter, opt, index)
        if selected:
            _draw_selection_lines(painter, option.rect, option.palette)


class OrderDelegate(QStyledItemDelegate):
    """주문 상태 왼쪽 + 종목별 잔량취소 오른쪽."""

    CANCEL_WIDTH = 34

    def paint(self, painter, option, index):
        if not index.data(ORDER_CANCEL_ROLE):
            super().paint(painter, option, index)
            return
        opt = QStyleOptionViewItem(option)
        opt.state &= ~(QStyle.State_Selected | QStyle.State_HasFocus)
        self.initStyleOption(opt, index)
        text = opt.text
        opt.text = ""
        style = opt.widget.style() if opt.widget else QApplication.style()
        style.drawControl(QStyle.CE_ItemViewItem, opt, painter, opt.widget)
        r = option.rect
        cancel_rect = QRect(
            r.right() - self.CANCEL_WIDTH + 1, r.top(),
            self.CANCEL_WIDTH, r.height())
        status_rect = QRect(
            r.left(), r.top(),
            max(0, r.width() - self.CANCEL_WIDTH), r.height())
        painter.save()
        painter.setPen(opt.palette.text().color())
        painter.drawText(status_rect, Qt.AlignCenter, text)
        painter.fillRect(cancel_rect.adjusted(1, 1, -1, -1), QColor("#D85A35"))
        painter.setPen(WHITE)
        painter.drawText(cancel_rect, Qt.AlignCenter, "취소")
        painter.restore()
        if _is_current_row(option, index):
            _draw_selection_lines(painter, option.rect, option.palette)


class BarDelegate(QStyledItemDelegate):
    """L일봉H: 가로 일봉 캔들. 축 = 하한가(왼쪽)~전일종가(0%,가운데)~상한가(오른쪽).
    심지=저가~고가, 몸통=시가~종가. 양봉(종가>=시가) 빨강, 음봉 파랑.
    점상한가는 O=H=L=C=상한가라 오른쪽 끝에 세로선으로 표시됨."""

    def paint(self, painter, option, index):
        if _is_current_row(option, index):
            _draw_selection_lines(painter, option.rect, option.palette)
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
        painter.fillRect(QRect(x0, r.top() + 2, max(2, x1 - x0 + 1), r.height() - 4), color)
        painter.restore()


class NameDelegate(QStyledItemDelegate):
    """종목명 셀: 단기과열은 주황색, NXT는 제외 확인용 적갈색 배경.
    우상단 녹색=미수가능(증거금100%는 무표시),
    좌하단=신규상장(마젠타=당일, 하늘=15일이내, 청회=30일이내)."""

    def paint(self, painter, option, index):
        # 일부 Windows 스타일은 textElideMode=ElideNone도 무시한다. 배경/선택은
        # 스타일에 맡기고 글자는 직접 그려 `…` 변환 경로 자체를 타지 않게 한다.
        selected = _is_current_row(option, index)
        opt = QStyleOptionViewItem(option)
        opt.state &= ~(QStyle.State_Selected | QStyle.State_HasFocus)
        self.initStyleOption(opt, index)
        nxt = bool(index.data(NXT_ROLE))
        short_overheat = bool(index.data(SHORT_OVERHEAT_ROLE))
        text = opt.text
        opt.text = ""
        style = opt.widget.style() if opt.widget else QApplication.style()
        style.drawControl(QStyle.CE_ItemViewItem, opt, painter, opt.widget)
        if short_overheat:
            # 단기과열과 NXT가 동시에 잡히면 주문 주의도가 높은 단기과열을 우선한다.
            background = (
                SHORT_OVERHEAT_BG_DARK
                if opt.palette.base().color().lightness() < 128
                else SHORT_OVERHEAT_BG_LIGHT
            )
            painter.fillRect(option.rect, background)
        elif nxt:
            # NXT 가능 종목은 실제 필터가 아니라 사용자가 주문 전 제외 대상을
            # 즉시 알아보도록 종목명 셀 전체를 테마 밝기에 맞춰 표시한다.
            background = (
                NXT_EXCLUDE_BG_DARK
                if opt.palette.base().color().lightness() < 128
                else NXT_EXCLUDE_BG_LIGHT
            )
            painter.fillRect(option.rect, background)
        leader = bool(index.data(THEME_LEADER_ROLE))
        theme_color = index.data(THEME_COLOR_ROLE)
        text_rect = style.subElementRect(
            QStyle.SE_ItemViewItemText, opt, opt.widget).adjusted(3, 0, -2, 0)
        painter.save()
        painter.setClipRect(option.rect)
        if isinstance(theme_color, QColor):
            # 테마정렬일 때만: 같은 테마는 같은 색 세로바, 다음 테마는 다른 색.
            painter.fillRect(QRect(option.rect.left(), option.rect.top(), 4,
                                   option.rect.height()), theme_color)
            text_rect.adjust(5, 0, 0, 0)
        if leader:
            leader_font = QFont(opt.font)
            leader_font.setBold(True)
            painter.setFont(leader_font)
            painter.setPen(THEME_LEADER)
            marker_rect = QRect(
                text_rect.left() + 3, text_rect.top(), 12, text_rect.height())
            painter.drawText(marker_rect, Qt.AlignLeft | Qt.AlignVCenter, "★")
            text_rect.adjust(15, 0, 0, 0)
        else:
            painter.setFont(opt.font)
        painter.setPen(opt.palette.text().color())
        painter.drawText(text_rect, Qt.AlignLeft | Qt.AlignVCenter | Qt.TextSingleLine, text)
        painter.restore()
        misu = index.data(MISU_ROLE)
        new = index.data(NEW_ROLE)
        if not (misu or new):
            if selected:
                _draw_selection_lines(painter, option.rect, option.palette)
            return
        r = option.rect
        s = 10
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.setPen(Qt.NoPen)
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
        if selected:
            _draw_selection_lines(painter, option.rect, option.palette)


def _in_opening_auction() -> bool:
    """개장 동시호가 여부. 프로젝트의 시각 기준은 로컬 KST다."""
    return "0830" <= time.strftime("%H%M") < "0900"


def _limit_tier(d: dict, opening_auction: bool = False) -> int:
    """상한가정렬 우선순위.

    개장 동시호가의 누적거래량은 0이 아닌 값이 남아 있을 수 있으므로, 이때는
    거래량과 무관하게 예상상한을 매도잔량 0/있음으로 먼저 완전히 분리한다.
    장중 실제 상한가도 매도잔량 0/있음 순으로 연속 배치한 뒤 예상상한과
    일반 종목을 표시한다.
    """
    actual_limit = d["upper"] > 0 and d["price"] == d["upper"]
    expected_limit = d["exp_price"] > 0 and (
        d["exp_price"] >= d["upper"] if d["upper"] > 0 else d["exp_rate"] >= LIMIT
    )
    if opening_auction and expected_limit:
        return 0 if d["ask_qty"] == 0 else 1
    if d["vol"] == 0 and expected_limit:
        return 0 if d["ask_qty"] == 0 else 1
    if actual_limit:
        return 2 if d["ask_qty"] == 0 else 3
    if expected_limit:
        return 4 if d["ask_qty"] == 0 else 5
    return 6


class TieredProxy(QSortFilterProxyModel):
    """상한가정렬 모드(limit_mode):
    개장 동시호가 예상상한, 실제 상한, 장중 예상상한을 각각
    매도잔량 0 -> 매도잔량 있음 순으로 분리해 위에 고정한다.
    그룹 안은 현재 정렬컬럼과 정렬방향을 따른다.
    모드 off면 전 컬럼 일반 정렬."""

    def __init__(self):
        super().__init__()
        self.limit_mode = False
        self.theme_mode = False
        self.theme_labels: dict[str, tuple[str, ...]] = {}
        self.relation_groups: dict[str, tuple[str, ...]] = {}
        self._theme_sort_keys: dict[str, tuple] = {}
        # 현재 테마정렬에서 실제로 선택된 묶음.
        self._theme_group_keys: dict[str, tuple[str, str]] = {}
        self._theme_group_colors: dict[tuple[str, str], QColor] = {}
        self._opening_auction = _in_opening_auction()
        # 상한가진입시간 정렬 중 비상한 그룹이 유지할 마지막 일반 정렬 기준.
        self._non_limit_sort_col = FIELDS.index("rate")
        self._non_limit_sort_order = Qt.DescendingOrder

    def set_theme_labels(self, labels: dict[str, tuple[str, ...]]):
        """실시간 조건검색용 종목별 테마 연결표를 갱신한다."""
        self.theme_labels = {
            str(code).removesuffix("_AL"): tuple(names)
            for code, names in labels.items() if names
        }
        self.invalidate()

    def set_relation_groups(self, groups: dict[str, tuple[str, ...]]):
        """자체 테마가 없는 종목을 보완할 관계 종목표를 설정한다."""
        self.relation_groups = {
            str(name): tuple(str(code).removesuffix("_AL") for code in codes)
            for name, codes in groups.items() if len(codes) >= 2
        }
        self.invalidate()

    @staticmethod
    def _theme_entry_time(row: dict) -> int:
        digits = "".join(character for character in str(row.get("time") or "")
                         if character.isdigit())
        return int(digits or "999999")

    @staticmethod
    def _theme_at_limit(row: dict) -> bool:
        return int(row.get("upper") or 0) > 0 and (
            int(row.get("price") or 0) >= int(row.get("upper") or 0))

    def _refresh_theme_sort_keys(self):
        """테마 강도와 테마 안 종목 순서를 현재 조건검색 행으로 계산한다."""
        self._theme_sort_keys = {}
        self._theme_group_keys = {}
        self._theme_group_colors = {}
        model = self.sourceModel()
        if model is None or not hasattr(model, "rows"):
            return
        if not self.theme_mode:
            if getattr(model, "theme_leaders", set()):
                model.theme_leaders = set()
                if model.codes:
                    model.dataChanged.emit(
                        model.index(0, NAME_COL),
                        model.index(len(model.codes) - 1, THEME_COL),
                    )
            return
        groups: dict[tuple[str, str], list[str]] = {}
        for code in model.codes:
            for theme in self.theme_labels.get(code.removesuffix("_AL"), ()):
                groups.setdefault(("theme", theme), []).append(code)
                family = _theme_family(theme)
                if family:
                    groups.setdefault(("theme_family", family), []).append(code)

        # 관계 그룹은 둘 이상이 현재 조건검색에 동시에 있을 때만 적용한다.
        # 단, 자체 테마가 있는 종목의 분류를 덮어쓰지는 않는다.
        relations_by_code: dict[str, list[tuple[str, str]]] = {}
        for name, members in self.relation_groups.items():
            present = [code for code in model.codes if code.removesuffix("_AL") in members]
            if len(present) < 2:
                continue
            group = ("relation", name)
            groups[group] = present
            for code in present:
                relations_by_code.setdefault(code, []).append(group)

        # 상한가 진입 테마는 가장 이른 진입시각이 우선이며, 나머지 테마는
        # 현재 조건검색 종목 중 최고 등락률이 높은 순서로 배치한다.
        strengths: dict[tuple[str, str], tuple] = {}
        for group, codes in groups.items():
            rows = [model.rows[code] for code in codes]
            limit_times = [self._theme_entry_time(row) for row in rows
                           if self._theme_at_limit(row)]
            top_rate = max(float(row.get("rate") or 0) for row in rows)
            strengths[group] = (
                0 if limit_times else 1,
                min(limit_times) if limit_times else 999999,
                -top_rate,
                -len(codes),
                f"{group[0]}:{group[1]}",
            )

        grouped_codes: dict[tuple[str, str], list[str]] = {}
        for code in model.codes:
            row = model.rows[code]
            relation_candidates = relations_by_code.get(code, ())
            theme_candidates = [
                ("theme", name)
                for name in self.theme_labels.get(code.removesuffix("_AL"), ())
            ]
            family_candidates = []
            for name in self.theme_labels.get(code.removesuffix("_AL"), ()):
                family = _theme_family(name)
                if family:
                    family_candidates.append(("theme_family", family))
            # 종목 자체의 테마를 관계 묶음보다 우선한다. 관계정보는 자체
            # 테마가 없는 종목을 보완할 때만 사용해, 계열사 때문에 다른
            # 종목의 AI/의료 등 세부 테마가 섞이지 않게 한다.
            candidates = family_candidates or theme_candidates or relation_candidates
            group = min(
                candidates,
                key=lambda item: (
                    _theme_group_precedence(item),
                    strengths.get(item, (2, 999999, 0, 0, item[1])),
                ),
            ) if candidates else ("none", "미분류")
            self._theme_group_keys[code] = group
            theme_strength = strengths.get(
                group, (2, 999999, 0, 0, group[1]))
            at_limit = self._theme_at_limit(row)
            self._theme_sort_keys[code] = (
                theme_strength,
                0 if at_limit else 1,
                self._theme_entry_time(row) if at_limit else 999999,
                -float(row.get("rate") or 0),
                str(row.get("name") or ""),
                code,
            )
            if group[0] != "none":
                grouped_codes.setdefault(group, []).append(code)

        # 해시 색상은 서로 다른 인접 그룹이 같은 색이 될 수 있다. 실제 화면 순서대로
        # 팔레트를 배정해 위·아래 테마의 세로바가 반드시 다르게 보이게 한다.
        ordered_groups = sorted(
            grouped_codes,
            key=lambda group: min(self._theme_sort_keys[code]
                                  for code in grouped_codes[group]),
        )
        self._theme_group_colors = {
            group: THEME_GROUP_COLORS[position % len(THEME_GROUP_COLORS)]
            for position, group in enumerate(ordered_groups)
        }

        # 대장(★): 테마 안에 실제 상한가 종목이 있으면 가장 빠른 진입 종목,
        # 없으면 현재 등락률 최상위 종목. 따라서 비상한 장세에서는 수시로
        # 바뀔 수 있지만, 상한가가 나온 뒤에는 진입 순서를 우선한다.
        def leader_key(code: str) -> tuple:
            row = model.rows[code]
            rate = -float(row.get("rate") or 0)
            if self._theme_at_limit(row):
                return (0, self._theme_entry_time(row), rate,
                        str(row.get("name") or ""), code)
            return (1, 999999, rate, str(row.get("name") or ""), code)

        leaders = {
            min(codes, key=leader_key)
            for codes in grouped_codes.values() if len(codes) >= 2
        }
        singletons = {
            codes[0] for codes in grouped_codes.values() if len(codes) == 1
        }
        if (getattr(model, "theme_leaders", set()) != leaders
                or getattr(model, "theme_singletons", set()) != singletons):
            model.theme_leaders = leaders
            model.theme_singletons = singletons
            if model.codes:
                model.dataChanged.emit(
                    model.index(0, NAME_COL),
                    model.index(len(model.codes) - 1, THEME_COL),
                )


    def sort(self, column, order=Qt.AscendingOrder):
        self._opening_auction = _in_opening_auction()
        if column not in NON_LIMIT_IGNORED_SORT_COLS:
            self._non_limit_sort_col = column
            self._non_limit_sort_order = order
        super().sort(column, order)

    def invalidate(self):
        self._opening_auction = _in_opening_auction()
        self._refresh_theme_sort_keys()
        super().invalidate()

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        # 세로 헤더 = 순위: 프록시 행번호(정렬 순서)로 1..N. 소스 매핑 안 함(편입순서 X).
        if orientation == Qt.Vertical and role == Qt.DisplayRole:
            return section + 1
        return super().headerData(section, orientation, role)

    def data(self, index, role=Qt.DisplayRole):
        if index.isValid() and FIELDS[index.column()] == "theme" and self.theme_mode:
            model = self.sourceModel()
            source_index = self.mapToSource(index)
            if model is not None and source_index.isValid() and hasattr(model, "codes"):
                code = model.codes[source_index.row()]
                group = self._theme_group_keys.get(code, ("none", "미분류"))
                name = group[1] if group[0] != "none" else "미분류"
                if role == Qt.DisplayRole:
                    return name
                if role == Qt.UserRole:
                    return name
                if role == Qt.ForegroundRole:
                    return (THEME_SINGLETON if code in getattr(model, "theme_singletons", set())
                            else self._theme_group_colors.get(
                                group, _theme_group_color(group)))
                if role == Qt.FontRole and code in getattr(model, "theme_leaders", set()):
                    font = QFont()
                    font.setBold(True)
                    return font
                if role == Qt.ToolTipRole:
                    labels = model.theme_labels.get(code.removesuffix("_AL"), ())
                    return "테마: " + " · ".join(labels) if labels else "테마 미분류"
                if role == Qt.TextAlignmentRole:
                    return Qt.AlignLeft | Qt.AlignVCenter
        if role == THEME_COLOR_ROLE:
            if not self.theme_mode or not index.isValid():
                return None
            model = self.sourceModel()
            source_index = self.mapToSource(index)
            if model is None or not source_index.isValid() or not hasattr(model, "codes"):
                return None
            code = model.codes[source_index.row()]
            # 단독 테마는 ◇ 표식만 사용하고 세로바는 표시하지 않는다.
            if code in getattr(model, "theme_singletons", set()):
                return None
            group = self._theme_group_keys.get(code, ("none", "미분류"))
            return self._theme_group_colors.get(group, _theme_group_color(group))
        return super().data(index, role)

    def lessThan(self, left, right):
        if self.theme_mode:
            model = self.sourceModel()
            left_code = model.codes[left.row()]
            right_code = model.codes[right.row()]
            left_key = self._theme_sort_keys.get(left_code, ())
            right_key = self._theme_sort_keys.get(right_code, ())
            # 편입 직후 테마/시세 백필 전에는 키가 아직 없다. 빈 키를
            # 일반 키와 직접 비교하면 정렬방향에 따라 새 행이 맨 위로
            # 튀므로, 키가 없는 행은 백필 전까지 항상 아래에 둔다.
            left_missing = not left_key
            right_missing = not right_key
            if left_missing != right_missing:
                if left_missing:
                    return self.sortOrder() == Qt.DescendingOrder
                return self.sortOrder() != Qt.DescendingOrder
            if left_missing:
                return left_code < right_code
            # 테마 강도 순서는 헤더의 오름/내림 토글과 무관하게 고정한다.
            # Qt는 내림차순일 때 lessThan 결과를 뒤집으므로 반대로 비교한다.
            return (left_key > right_key if self.sortOrder() == Qt.DescendingOrder
                    else left_key < right_key)
        if self.limit_mode:
            m = self.sourceModel()
            a = m.rows[m.codes[left.row()]]
            b = m.rows[m.codes[right.row()]]
            ta = _limit_tier(a, self._opening_auction)
            tb = _limit_tier(b, self._opening_auction)
            desc = self.sortOrder() == Qt.DescendingOrder
            if ta != tb:  # 우선순위 그룹 순서는 현재 정렬방향과 무관하게 고정
                return ta > tb if desc else ta < tb
            if ta in (2, 3) and left.column() == TIME_COL:
                # 실제 상한가 그룹에서는 진입시간 미수신 종목을 항상 뒤로 보낸다.
                a_has_time, b_has_time = bool(a["time"]), bool(b["time"])
                if a_has_time != b_has_time:
                    return not a_has_time if desc else a_has_time
            if ta == 6 and left.column() in NON_LIMIT_IGNORED_SORT_COLS:
                # 진입시간/매수잔량은 비상한 그룹에 적용하지 않고 직전 정렬을 유지한다.
                fallback_left = m.index(left.row(), self._non_limit_sort_col)
                fallback_right = m.index(right.row(), self._non_limit_sort_col)
                reverse = ((self._non_limit_sort_order == Qt.DescendingOrder)
                           != desc)
                if reverse:
                    return super().lessThan(fallback_right, fallback_left)
                return super().lessThan(fallback_left, fallback_right)
            # 같은 우선순위 그룹끼리: 현재 정렬컬럼으로 일반 비교
        return super().lessThan(left, right)


class ThemeGroupedTableView(QTableView):
    """거래상태 가로 구분선과 핵심 거래 열 세로 안내선을 표시한다."""

    def paintEvent(self, event):
        super().paintEvent(event)
        proxy = self.model()
        painter = QPainter(self.viewport())
        painter.setRenderHint(QPainter.Antialiasing, False)

        # 상한가 정렬 순서는 그대로 두고, 09시 이후에도 예상상한 상태로
        # 아직 첫 거래가 시작되지 않은 종목군의 끝만 가로선으로 구분한다.
        if (
            isinstance(proxy, TieredProxy)
            and proxy.limit_mode
            and not _in_opening_auction()
            and proxy.rowCount() > 1
        ):
            source = proxy.sourceModel()
            last_waiting_row = -1
            if source is not None and hasattr(source, "codes"):
                for row in range(proxy.rowCount()):
                    source_index = proxy.mapToSource(proxy.index(row, 0))
                    if not source_index.isValid():
                        break
                    code = source.codes[source_index.row()]
                    if _limit_tier(source.rows[code], False) not in (0, 1):
                        break
                    last_waiting_row = row
            if 0 <= last_waiting_row < proxy.rowCount() - 1:
                y = (
                    self.rowViewportPosition(last_waiting_row)
                    + self.rowHeight(last_waiting_row) - 1
                )
                if 0 <= y < self.viewport().height():
                    painter.setPen(QPen(QColor("#FFD54F"), 2))
                    painter.drawLine(0, y, self.viewport().width(), y)

        # 셀 배경과 숫자색은 그대로 두고, 가장 중요한 두 열의 위치만
        # 끊김 없는 세로선으로 표시한다. 논리 열 기준이라 사용자가 열을
        # 옮기거나 너비를 바꿔도 선은 해당 열을 그대로 따라간다.
        if proxy is not None and proxy.rowCount() > 0:
            last_row = proxy.rowCount() - 1
            line_bottom = min(
                self.viewport().height() - 1,
                self.rowViewportPosition(last_row)
                + self.rowHeight(last_row) - 1,
            )
            if line_bottom >= 0:
                for column, color, width in (
                    (VOLUME_COL, QColor("#FFB300"), 2),
                    (BID_QTY_COL, QColor("#FFB300"), 1),
                ):
                    if self.isColumnHidden(column):
                        continue
                    left = self.columnViewportPosition(column)
                    right = left + self.columnWidth(column) - 1
                    painter.setPen(QPen(color, width))
                    painter.drawLine(left, 0, left, line_bottom)
                    painter.drawLine(right, 0, right, line_bottom)
        painter.end()


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
        self.short_overheat: set[str] = set()  # 단기과열 30분 단일가: 종목명 주황 배경
        self.liquidation: set[str] = set()   # 정리매매: 가격제한폭 없음 (main 주입)
        self.nxt: set[str] = set()           # 넥스트레이드(NXT) 거래가능: 종목명 제외 배경 (main 주입)
        self.misu: set[str] = set()          # 미수가능(증거금<100%): 우상단 녹색 삼각형 (main 주입)
        self.admin: set[str] = set()         # 관리종목: 종목명 경고색 (코스닥보다 우선, main 주입)
        self.limit_cnt: dict[str, tuple[int, int]] = {}  # (어제까지 연속상한 일수, 어제 종가) (main 주입, 연상 컬럼)
        self.watched: set[str] = set()     # 실시간 뉴스 감시 종목 (main 주입)
        self.theme_labels: dict[str, tuple[str, ...]] = {}
        self.relation_labels: dict[str, tuple[str, ...]] = {}
        self.relation_evidence: dict[str, tuple[str, ...]] = {}
        self.theme_leaders: set[str] = set()
        self.theme_singletons: set[str] = set()
        self.new_today: set[str] = set()     # 상장 당일 (main 주입, 좌하단 마젠타)
        self.new15: set[str] = set()         # 상장 15일 이내 (좌하단 하늘)
        self.new30: set[str] = set()         # 상장 16~30일 (좌하단 청회)
        self.shares: dict[str, int] = {}     # 상장주식수 ka10099 (main 주입, 시가총액 컬럼)
        self.ticks: dict[str, deque] = {}    # (체결시각, 부호있는 개별체결량, 체결가) 최근 60초
        self.minute_value_ticks: dict[str, deque] = {}  # (체결시각, 체결대금) 최근 60초
        self._minute_volume_ready: set[str] = set()  # 누적거래량 최초 기준점 확보 종목
        self.quotes: dict[str, deque] = {}   # (시각, 1~5호가 (매도/매수 가격·잔량)) 최근 15초
        self.prediction_history: dict[str, deque] = {}  # 최근 5분 1초 체결 요약
        self.program_history: dict[str, deque] = {}  # 최근 5분 0w 매수/매도수량 차분
        self._program_cumulative: dict[str, tuple] = {}  # 마지막 (매수수량누적, 매도수량누적, 출처)
        self._program_since: dict[str, float] = {}  # 현재 출처 누적값을 관찰하기 시작한 시각
        self._prediction_cache: dict[str, tuple] = {}   # 같은 초의 반복 data() 계산 방지
        self.order_target_code = ""       # 주문 컬럼에서 선택한 현재 대상종목
        self.order_status: dict[str, str] = {}
        self.order_cancellable: set[str] = set()
        # 종목이 행에서 빠졌다 다시 들어와도 같은 실행 세션에서는
        # 사용자가 적용한 3단 기준을 보존한다.
        self.balance_sell_settings: dict[str, dict] = {}
        self.balance_sell_stage: dict[str, int] = {}
        self.balance_alert_stage: dict[str, int] = {}
        self.balance_alert_ticks: dict[str, int] = {}
        self.balance_blink_on = False
        # 계좌 주문 출처와 무관한 종목별 자동취소 감시. 앱 재시작 시에는
        # 무조건 해제되며 사용자가 해당 행을 직접 눌러야만 켜진다.
        self.account_auto_cancel_armed: set[str] = set()
        # 종목별 당일 수동 청산키. 창을 닫거나 앱을 재시작하면 사라진다.
        self.exit_hotkeys: dict[str, tuple[int, str]] = {}

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

    def add_stocks(self, entries: list[tuple[str, dict]]) -> int:
        """초기 조건 스냅샷의 대량 편입을 한 번의 모델 삽입으로 처리한다."""
        new_entries = [(code, data) for code, data in entries if code not in self.rows]
        if not new_entries:
            return 0
        first = len(self.codes)
        self.beginInsertRows(QModelIndex(), first, first + len(new_entries) - 1)
        for code, data in new_entries:
            stored = {field: "" if field in ("name", "time") else 0 for field in STORED}
            # 초기 스냅샷은 종목코드만 들어온다. REST 백필 전까지 이름만 즉시 보인다.
            stored["name"] = str(data.get("name") or code)
            self.codes.append(code)
            self.rows[code] = stored
        self.endInsertRows()
        return len(new_entries)

    def remove_stock(self, code: str):
        if code not in self.rows:
            return
        row = self.codes.index(code)
        self.beginRemoveRows(QModelIndex(), row, row)
        self.codes.remove(code)
        del self.rows[code]
        self._exp_live.discard(code)
        self.ticks.pop(code, None)
        self.minute_value_ticks.pop(code, None)
        self._minute_volume_ready.discard(code)
        self.quotes.pop(code, None)
        self.prediction_history.pop(code, None)
        self.program_history.pop(code, None)
        self._program_cumulative.pop(code, None)
        self._program_since.pop(code, None)
        self._prediction_cache.pop(code, None)
        self.order_status.pop(code, None)
        self.order_cancellable.discard(code)
        self.account_auto_cancel_armed.discard(code)
        self.endRemoveRows()

    def set_order_target(self, code: str):
        """이전/새 주문 대상 셀만 다시 그린다."""
        if code == self.order_target_code:
            return
        old = self.order_target_code
        self.order_target_code = code if code in self.rows else ""
        for changed in (old, self.order_target_code):
            if changed in self.rows:
                row = self.codes.index(changed)
                cell = self.index(row, ORDER_COL)
                self.dataChanged.emit(cell, cell)

    def set_watched_codes(self, codes):
        """실시간 뉴스 감시 상태가 바뀐 연상 셀만 다시 그린다."""
        updated = set(codes or ())
        changed = self.watched ^ updated
        self.watched = updated
        for code in changed:
            if code in self.rows:
                row = self.codes.index(code)
                cell = self.index(row, STREAK_COL)
                self.dataChanged.emit(cell, cell)

    def refresh_streaks(self):
        """연상 보완 조회가 끝난 뒤 현재 행의 연상 셀을 다시 그린다."""
        if not self.codes:
            return
        self.dataChanged.emit(
            self.index(0, STREAK_COL),
            self.index(len(self.codes) - 1, STREAK_COL),
            [Qt.ItemDataRole.DisplayRole, Qt.ItemDataRole.ForegroundRole,
             Qt.ItemDataRole.BackgroundRole])

    def refresh_market_markers(self):
        """시장 분류 조회가 끝난 즉시 종목명 색상·표식을 다시 그린다."""
        if not self.codes:
            return
        self.dataChanged.emit(
            self.index(0, NAME_COL),
            self.index(len(self.codes) - 1, NAME_COL),
            [Qt.ItemDataRole.ForegroundRole, Qt.ItemDataRole.ToolTipRole,
             NXT_ROLE, SHORT_OVERHEAT_ROLE, MISU_ROLE, NEW_ROLE])

    def set_order_status(self, code: str, text: str, cancellable: bool = False):
        if code not in self.rows:
            return
        if text:
            self.order_status[code] = text
        else:
            self.order_status.pop(code, None)
        if cancellable:
            self.order_cancellable.add(code)
        else:
            self.order_cancellable.discard(code)
        row = self.codes.index(code)
        cell = self.index(row, ORDER_COL)
        self.dataChanged.emit(cell, cell)

    def set_account_auto_cancel_armed(self, code: str, armed: bool):
        if armed:
            self.account_auto_cancel_armed.add(code)
        else:
            self.account_auto_cancel_armed.discard(code)
        if code in self.rows:
            row = self.codes.index(code)
            cell = self.index(row, AUTO_CANCEL_ARM_COL)
            self.dataChanged.emit(cell, cell)

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
        tick_qty = fields.get("tick_qty")  # STORED 필터 전에 보존: +매수체결 / -매도체결
        real_type = str(fields.get("_real_type") or "")
        volume_in_fields = "vol" in fields
        if ("program_buy_qty" in fields and "program_sell_qty" in fields):
            self._ingest_program(
                code, fields["program_buy_qty"], fields["program_sell_qty"],
                fields.get("_program_source", ""), time.monotonic())
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
        dvol = fields.get("vol", 0) - stored["vol"]  # FID 15가 없을 때 체결 틱 폴백
        volume_ready = code in self._minute_volume_ready
        if volume_in_fields:
            if fields.get("vol", 0) < stored["vol"]:
                # 장/시세출처가 바뀌어 누적거래량이 초기화되면 이전 창도 함께 비운다.
                self.minute_value_ticks.pop(code, None)
                fields["minute_value"] = 0
                volume_ready = False
            self._minute_volume_ready.add(code)
        ticked = tick_qty not in (None, 0) or dvol > 0
        quote_changed = any(f in fields for f in BOOK_FIELDS)
        if quote_changed:
            levels = []
            for level in range(1, 6):
                suffix = "" if level == 1 else str(level)
                names = tuple(f"{side}_{kind}{suffix}" for side, kind in (
                    ("ask", "price"), ("ask", "qty"), ("bid", "price"), ("bid", "qty")))
                ap, aq, bp, bq = (int(fields.get(f, stored[f])) for f in names)
                levels.append((ap, max(0, aq), bp, max(0, bq)))
            if levels[0][0] and levels[0][2]:
                qq = self.quotes.setdefault(code, deque())
                snap = (time.monotonic(), tuple(levels))
                if not qq or qq[-1][1] != snap[1]:
                    qq.append(snap)
                while qq and qq[0][0] < snap[0] - 15:
                    qq.popleft()
        if ticked:
            dq = self.ticks.setdefault(code, deque())
            now = time.monotonic()
            qty = int(tick_qty or 0)
            price = int(fields.get("price", stored["price"]))
            dq.append((now, qty, price))
            while dq and dq[0][0] < now - 60:
                dq.popleft()

            # 같은 체결이 0B·0D 양쪽에 와도 누적거래량 증가분은 한 번만 잡힌다.
            # 첫 REST 백필은 기준점만 세우고, 이후 웹소켓 실시간 증가분만 보완한다.
            if real_type and volume_in_fields and volume_ready and dvol > 0:
                value_qty = dvol
            elif qty and (not real_type or not volume_in_fields or not volume_ready):
                value_qty = abs(qty)
            else:
                value_qty = 0
            traded_value = max(0, int(value_qty)) * price
            value_ticks = self.minute_value_ticks.setdefault(code, deque())
            minute_value = int(fields.get(
                "minute_value", stored.get("minute_value") or 0))
            if traded_value:
                value_ticks.append((now, traded_value))
                minute_value += traded_value
            while value_ticks and value_ticks[0][0] < now - 60:
                _, old_value = value_ticks.popleft()
                minute_value -= old_value
            fields["minute_value"] = max(0, minute_value)
            # 3·5·10분 점수는 원본 틱 대신 1초 요약으로 계산해 종목 수가 많아도
            # 메모리와 재계산량이 체결 건수에 비례해 폭증하지 않게 한다.
            history = self.prediction_history.setdefault(code, deque())
            sec = int(now)
            if not history or history[-1].sec != sec:
                history.append(PredictionBucket(sec, open_price=price, close_price=price))
            bucket = history[-1]
            if not bucket.open_price and price:
                bucket.open_price = price
            bucket.close_price = price or bucket.close_price
            bucket.tick_count += 1
            if qty > 0:
                bucket.buy_qty += qty
                bucket.buy_count += 1
            elif qty < 0:
                bucket.sell_qty += -qty
                bucket.sell_count += 1
            if traded_value:
                bucket.traded_value += traded_value
                bucket.traded_qty += max(0, int(value_qty))
            while history and history[0].sec <= sec - 300:
                history.popleft()
        cols = set()
        for f, v in fields.items():
            if stored.get(f) == v:
                continue
            if f in FIELDS:
                cols.add(FIELDS.index(f))
            if f == "bid_qty":  # 미설정 3단매도 자동 제안값도 함께 갱신
                cols.add(BALANCE_SELL_COL)
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

    def refresh_minute_values(self):
        """거래가 끊겨도 최근 60초 창에서 빠진 체결대금을 1초마다 반영한다."""
        now = time.monotonic()
        changed_rows = []
        for row, code in enumerate(self.codes):
            raw_ticks = self.ticks.get(code)
            if raw_ticks:
                while raw_ticks and raw_ticks[0][0] < now - 60:
                    raw_ticks.popleft()
            value_ticks = self.minute_value_ticks.get(code)
            if not value_ticks:
                continue
            value = int(self.rows[code].get("minute_value") or 0)
            while value_ticks and value_ticks[0][0] < now - 60:
                _, old_value = value_ticks.popleft()
                value -= old_value
            value = max(0, value)
            if self.rows[code].get("minute_value") != value:
                self.rows[code]["minute_value"] = value
                changed_rows.append(row)
        if changed_rows:
            self.dataChanged.emit(
                self.index(min(changed_rows), MINUTE_VALUE_COL),
                self.index(max(changed_rows), MINUTE_VALUE_COL),
                [Qt.DisplayRole, Qt.UserRole])

    def _ingest_program(self, code, buy_qty, sell_qty, source, now):
        """0w 누적수량을 안전하게 차분한다. 최초/출처변경/누적감소는 기준만 교체한다."""
        buy = max(0, int(buy_qty or 0))
        sell = max(0, int(sell_qty or 0))
        previous = self._program_cumulative.get(code)
        self._program_cumulative[code] = (buy, sell, source)
        if (previous is None or previous[2] != source
                or buy < previous[0] or sell < previous[1]):
            self.program_history.pop(code, None)
            self._program_since[code] = now
            return True

        delta_buy, delta_sell = buy - previous[0], sell - previous[1]
        if not delta_buy and not delta_sell:
            return False
        history = self.program_history.setdefault(code, deque())
        sec = int(now)
        if not history or history[-1].sec != sec:
            history.append(ProgramBucket(sec))
        history[-1].buy_qty += delta_buy
        history[-1].sell_qty += delta_sell
        while history and history[0].sec <= sec - 300:
            history.popleft()
        return True

    @staticmethod
    def _program_interval(program_history, market_history, lower, upper):
        """지정 구간의 프로그램 수량방향과 전체 체결수량 대비 참여율을 반환한다."""
        program = [b for b in program_history if lower < b.sec <= upper]
        buy = sum(b.buy_qty for b in program)
        sell = sum(b.sell_qty for b in program)
        gross = buy + sell
        net = buy - sell
        buy_pct = buy / gross * 100 if gross else None
        traded_qty = sum(
            b.traded_qty for b in market_history if lower < b.sec <= upper)
        participation = gross / traded_qty * 100 if traded_qty else None
        return buy, sell, gross, net, buy_pct, participation

    @classmethod
    def _program_metrics(cls, program_history, market_history, now, lookback):
        return cls._program_interval(
            program_history, market_history, now - lookback, now)

    @staticmethod
    def _combined_buy_pct(items):
        """부호 있는 체결 목록의 수량·건수 통합 매수비중."""
        buy_qty = sum(q for _, q, _ in items if q > 0)
        sell_qty = sum(-q for _, q, _ in items if q < 0)
        buy_count = sum(1 for _, q, _ in items if q > 0)
        sell_count = sum(1 for _, q, _ in items if q < 0)
        if not buy_count + sell_count:
            return None
        qty_pct = buy_qty / (buy_qty + sell_qty) * 100
        count_pct = buy_count / (buy_count + sell_count) * 100
        return qty_pct * 0.7 + count_pct * 0.3 - abs(qty_pct - count_pct) * 0.2

    @classmethod
    def _prediction_score(cls, items, stored, quotes=()):
        """최근 10초 호가·체결로 단타 계산의 선행압력을 만든다."""
        if len(items) < 5 or items[-1][0] - items[0][0] < 5:
            return None  # 편입 직후/순간 버스트는 표본 부족으로 표시하지 않음
        flow = cls._combined_buy_pct(items)
        cutoff = items[-1][0] - 5
        recent = [x for x in items if x[0] >= cutoff]
        previous = [x for x in items if x[0] < cutoff]
        recent_buys = sum(1 for _, q, _ in recent if q > 0)
        previous_buys = sum(1 for _, q, _ in previous if q > 0)
        speed = 50 + (recent_buys - previous_buys) / max(
            1, recent_buys + previous_buys) * 50
        quote_items = [x for x in quotes if x[0] >= items[-1][0] - 10]
        weights = (0.40, 0.25, 0.15, 0.12, 0.08)
        level_scores = []
        for level in range(5):
            ofi, depths = 0, []
            for prev, cur in zip(quote_items, quote_items[1:]):
                pa, paq, pb, pbq = prev[1][level]
                ca, caq, cb, cbq = cur[1][level]
                if not (pa and pb and ca and cb):
                    continue
                bid_flow = cbq if cb > pb else cbq - pbq if cb == pb else -pbq
                ask_flow = caq if ca < pa else caq - paq if ca == pa else -paq
                ofi += bid_flow - ask_flow
                depths.append((paq + pbq + caq + cbq) / 4)
            avg_depth = sum(depths) / len(depths) if depths else 0
            level_scores.append(50 + 50 * math.tanh(ofi / avg_depth) if avg_depth else 50)
        ofi_score = sum(score * weight for score, weight in zip(level_scores, weights))
        micro_score = 50
        if quote_items:
            ask_p, ask_q, bid_p, bid_q = quote_items[-1][1][0]
            if ask_p > bid_p > 0 and ask_q + bid_q:
                micro = (ask_p * bid_q + bid_p * ask_q) / (ask_q + bid_q)
                half_spread = (ask_p - bid_p) / 2
                micro_score = max(0, min(100, 50 + (micro - (ask_p + bid_p) / 2)
                                                  / half_spread * 50))
        first_price, last_price = items[0][2], items[-1][2]
        if first_price and last_price:
            change_bp = (last_price - first_price) / first_price * 10_000
            price_response = max(0, min(100, 50 + change_bp * 2))
        else:
            price_response = 50
        score = (ofi_score * 0.40 + flow * 0.25 + micro_score * 0.15
                 + price_response * 0.10 + speed * 0.10)
        return max(0, min(100, score))

    @staticmethod
    def _bucket_buy_pct(buckets):
        """1초 요약 버킷의 수량·건수 통합 매수비중."""
        buy_qty = sum(b.buy_qty for b in buckets)
        sell_qty = sum(b.sell_qty for b in buckets)
        buy_count = sum(b.buy_count for b in buckets)
        sell_count = sum(b.sell_count for b in buckets)
        if not buy_count + sell_count or not buy_qty + sell_qty:
            return None
        qty_pct = buy_qty / (buy_qty + sell_qty) * 100
        count_pct = buy_count / (buy_count + sell_count) * 100
        return qty_pct * 0.7 + count_pct * 0.3 - abs(qty_pct - count_pct) * 0.2

    @classmethod
    def _horizon_score(cls, history, pressure, now, lookback, min_span,
                       momentum_scale, weights, program_history=(),
                       program_since=None, program_weight=0):
        """한 예측구간의 흐름·추세 지속성을 0~100 상승압력으로 계산."""
        cutoff = int(now - lookback)
        buckets = [b for b in history if b.sec >= cutoff and b.tick_count]
        if (len(buckets) < 3 or buckets[-1].sec - buckets[0].sec < min_span):
            return None
        flow = cls._bucket_buy_pct(buckets)
        if flow is None:
            return None

        first_price = next((b.open_price for b in buckets if b.open_price), 0)
        last_price = next((b.close_price for b in reversed(buckets) if b.close_price), 0)
        if first_price and last_price:
            change_bp = (last_price - first_price) / first_price * 10_000
            momentum = 50 + 50 * math.tanh(change_bp / momentum_scale)
        else:
            momentum = 50

        prices = [b.close_price for b in buckets if b.close_price]
        changes = [cur - prev for prev, cur in zip(prices, prices[1:])]
        travel = sum(abs(change) for change in changes)
        persistence = (50 + 50 * (prices[-1] - prices[0]) / travel
                       if len(prices) >= 2 and travel else 50)

        traded_qty = sum(b.traded_qty for b in buckets)
        if traded_qty and last_price:
            vwap = sum(b.traded_value for b in buckets) / traded_qty
            vwap_bp = (last_price - vwap) / vwap * 10_000
            vwap_score = 50 + 50 * math.tanh(vwap_bp / max(1, momentum_scale / 2))
        else:
            vwap_score = 50

        split = now - lookback / 3
        recent_ticks = sum(b.tick_count for b in buckets if b.sec >= split)
        previous_ticks = sum(b.tick_count for b in buckets if b.sec < split)
        recent_rate = recent_ticks / (lookback / 3)
        previous_rate = previous_ticks / (lookback * 2 / 3)
        denom = recent_rate + previous_rate
        acceleration = (recent_rate - previous_rate) / denom if denom else 0
        direction = max(-1, min(1, (flow - 50) / 50))
        acceleration_score = 50 + 50 * acceleration * direction

        activity_persistence = cls._activity_persistence_score(
            buckets, now, direction)

        components = (pressure, flow, momentum, persistence, vwap_score,
                      acceleration_score, activity_persistence)
        score = sum(value * weight for value, weight in zip(components, weights))
        program_score = cls._program_prediction_score(
            program_history, history, now, lookback, min_span, program_since)
        if program_score is not None and program_weight:
            score = score * (1 - program_weight) + program_score * program_weight
        return max(0, min(100, score))

    @classmethod
    def _program_prediction_score(cls, program_history, market_history, now,
                                  lookback, min_span, observed_since):
        """프로그램 수량방향·최근 지속·시장 참여율을 0~100 보조점수로 만든다."""
        if observed_since is None or now - observed_since < min_span:
            return None
        stats = cls._program_metrics(
            program_history, market_history, now, lookback)
        gross, net, participation = stats[2], stats[3], stats[5]
        if not gross or participation is None:
            return None

        overall_direction = net / gross
        width = lookback / 3
        segment_directions = []
        for part in range(3):
            lower = now - lookback + width * part
            upper = lower + width
            segment = cls._program_interval(
                program_history, market_history, lower, upper)
            segment_directions.append(segment[3] / segment[2] if segment[2] else 0)
        recent_direction = sum(
            direction * weight for direction, weight
            in zip(segment_directions, (0.20, 0.30, 0.50)))
        direction = overall_direction * 0.40 + recent_direction * 0.60
        # 프로그램이 전체 체결수량의 20%면 최대 관련도로 본다. 그보다 작으면
        # 방향은 유지하되 예측에 미치는 크기만 비례해서 줄인다.
        relevance = min(1, max(0, participation) / 20)
        return max(0, min(100, 50 + 50 * direction * relevance))

    @staticmethod
    def _activity_persistence_score(buckets, now, direction):
        """관찰구간을 3등분해 체결활동의 유지·소멸을 매매 방향과 결합한다."""
        if not buckets:
            return 50
        start = float(buckets[0].sec)
        span = now - start
        if span < 60:  # 짧은 순간 버스트를 지속으로 오인하지 않는다.
            return 50
        width = span / 3
        counts = []
        for part in range(3):
            lower = start + width * part
            upper = start + width * (part + 1)
            counts.append(sum(
                b.tick_count for b in buckets
                if b.sec >= lower and (b.sec < upper or part == 2 and b.sec <= now)))
        rates = [count / width for count in counts]
        peak_rate = max(rates)
        if not peak_rate:
            return 50

        # 300건/분이면 활동도 가중치를 최대로 반영한다. 유지율 60% 미만은
        # 지속 신호가 아니라 소멸 신호로 뒤집어 매수·매도 방향에 맞게 감점한다.
        activity_level = min(1, peak_rate * 60 / 300)
        prior_peak = max(rates[0], rates[1])
        retention = rates[2] / prior_peak if prior_peak else 1
        continuity = min(rates) / peak_rate
        persistence_signal = (continuity if retention >= 0.60
                              else -(0.60 - retention) / 0.60)
        score = 50 + 50 * activity_level * persistence_signal * direction
        return max(0, min(100, score))

    @classmethod
    def _multi_horizon_prediction(cls, items, stored, history, quotes=(), now=None,
                                  program_history=(), program_since=None):
        """3·5·10분 상승압력과 5분 중심 종합점수를 반환한다."""
        now = time.monotonic() if now is None else now
        recent = [item for item in items if item[0] >= now - 10 and item[1]]
        pressure = cls._prediction_score(recent, stored, quotes)
        if pressure is None:
            return None, (None,) * len(PREDICT_HORIZONS)

        scores = []
        for idx, (_, lookback, min_span, scale, weights, _) in enumerate(PREDICT_HORIZONS):
            scores.append(cls._horizon_score(
                history, pressure, now, lookback, min_span, scale, weights,
                program_history, program_since, PROGRAM_PREDICT_WEIGHTS[idx]))
        available = [(score, spec[-1]) for score, spec in zip(scores, PREDICT_HORIZONS)
                     if score is not None]
        if not available:
            return None, tuple(scores)
        total_weight = sum(weight for _, weight in available)
        combined = sum(score * weight for score, weight in available) / total_weight
        return max(0, min(100, combined)), tuple(scores)

    def _prediction_values(self, code, stored, now):
        """같은 초에는 다중구간 계산 결과를 재사용한다."""
        stamp = int(now)
        cached = self._prediction_cache.get(code)
        if cached and cached[0] == stamp:
            return cached[1]
        result = self._multi_horizon_prediction(
            self.ticks.get(code, ()), stored,
            self.prediction_history.get(code, ()), self.quotes.get(code, ()), now,
            self.program_history.get(code, ()), self._program_since.get(code))
        self._prediction_cache[code] = (stamp, result)
        return result

    # --- Qt 모델 구현 ---------------------------------------------------
    def rowCount(self, parent=QModelIndex()):
        return len(self.codes)

    def columnCount(self, parent=QModelIndex()):
        return len(COLUMNS)

    def set_theme_labels(self, labels: dict[str, tuple[str, ...]]):
        """종목명 셀의 테마 안내와 테마 정렬이 공유하는 연결표를 설정한다."""
        self.theme_labels = {
            str(code).removesuffix("_AL"): tuple(names)
            for code, names in labels.items() if names
        }
        if self.codes:
            self.dataChanged.emit(
                self.index(0, NAME_COL),
                self.index(len(self.codes) - 1, THEME_COL),
            )

    def set_relation_groups(self, groups: dict[str, tuple[str, ...]]):
        """종목명 도구설명에 표시할 최대주주·계열 등 관계 묶음을 설정한다."""
        labels: dict[str, list[str]] = {}
        for group, codes in groups.items():
            for code in codes:
                labels.setdefault(str(code).removesuffix("_AL"), []).append(group)
        self.relation_labels = {
            code: tuple(names) for code, names in labels.items()
        }
        if self.codes:
            self.dataChanged.emit(
                self.index(0, NAME_COL),
                self.index(len(self.codes) - 1, NAME_COL),
            )

    def set_relation_evidence(self, evidence: dict[str, tuple[str, ...]]):
        """DART 최대주주 관계의 지분율·보고서 근거를 종목명 도구설명에 반영한다."""
        self.relation_evidence = {
            str(code).removesuffix("_AL"): tuple(values)
            for code, values in evidence.items() if values
        }
        if self.codes:
            self.dataChanged.emit(
                self.index(0, NAME_COL),
                self.index(len(self.codes) - 1, NAME_COL),
            )

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if orientation == Qt.Horizontal and role == Qt.ToolTipRole:
            if FIELDS[section] == "rate":
                return "등락률 · 셀 클릭=분석창 뉴스·종토방 바로 열기"
            if FIELDS[section] == "theme":
                return "대표 테마 · ★ 대장과 현재 편입 종목수 · ◇ 단독 테마"
            if FIELDS[section] == "streak":
                return "연속 상한가 일수 · 셀 클릭=실시간 뉴스 감시 등록/해제"
            if FIELDS[section] == "minute_value":
                return "최근 60초 실제 체결대금 · 억원 단위 · 1초마다 갱신"
            if FIELDS[section] == "auto_cancel_arm":
                return (
                    "선택 종목 계좌 자동취소 · 앱/영웅문 주문 포함 · "
                    "기본 해제")
            return COLUMNS[section]  # 칸 좁혀 헤더 글자 잘려도 오버로 확인
        if orientation == Qt.Horizontal and role == Qt.DisplayRole:
            return COLUMNS[section]
        return None

    def data(self, index, role=Qt.DisplayRole):
        field = FIELDS[index.column()]
        code = self.codes[index.row()]
        stored = self.rows[code]
        if field == "theme":
            themes = self.theme_labels.get(code.removesuffix("_AL"), ())
            primary = themes[0] if themes else ""
            if role == Qt.DisplayRole:
                if not primary:
                    return "미분류"
                return primary
            if role == Qt.UserRole:
                return primary
            if role == Qt.ForegroundRole:
                if code in self.theme_singletons:
                    return THEME_SINGLETON
                return _theme_group_color(("theme", primary)) if primary else QColor("#777777")
            if role == Qt.FontRole and code in self.theme_leaders:
                font = QFont()
                font.setBold(True)
                return font
            if role == Qt.TextAlignmentRole:
                return Qt.AlignLeft | Qt.AlignVCenter
            if role == Qt.ToolTipRole:
                return "테마: " + " · ".join(themes) if themes else "테마 미분류"
            return None
        if field == "balance_sell":
            setting = self.balance_sell_settings.get(code)
            stage = self.balance_sell_stage.get(code, 0)
            if role == Qt.DisplayRole:
                if setting:
                    keys = ["first", "second"]
                    if int(setting.get("third", 0)) > 0:
                        keys.append("third")
                    return " / ".join(
                        _shares_in_ten_thousands(setting[key])
                        for key in keys)
                bid = max(0, int(stored.get("bid_qty") or 0))
                if bid:
                    first, _, _ = _balance_sell_suggestion(bid)
                    return _shares_in_ten_thousands(first)
                return "설정"
            if role == Qt.UserRole:
                return setting["first"] if setting else 0
            if role == Qt.TextAlignmentRole:
                return Qt.AlignLeft | Qt.AlignVCenter
            if role == Qt.BackgroundRole and setting:
                alert = self.balance_alert_stage.get(code, 0)
                if alert and self.balance_blink_on:
                    return (
                        QColor("#FFF176") if alert == 1
                        else QColor("#FF9800") if alert == 2
                        else QColor("#D50000"))
                return QColor("#CDECCF")
            if role == Qt.ForegroundRole and setting:
                if (
                    self.balance_alert_stage.get(code, 0) >= 3
                    and self.balance_blink_on
                ):
                    return WHITE
                return QColor("#111111")
            if role == Qt.FontRole and setting:
                font = QFont()
                font.setBold(True)
                return font
            if role == Qt.ToolTipRole:
                if setting:
                    return (
                        f"적용 중인 보조 설정\n"
                        f"주문방식: "
                        f"{'시장가' if setting.get('market_sell', False) else '지정가'}\n"
                        f"1차 {setting['first']:,}주 이하"
                        f"{' (꺼짐)' if not setting['first'] else ''}: "
                        f"{int(float(setting.get('first_ratio', 0)) * 100)}% 매도\n"
                        f"2차 {setting['second']:,}주 이하"
                        f"{' (꺼짐)' if not setting['second'] else ''}: "
                        f"{int(float(setting.get('second_ratio', .5)) * 100)}%까지 매도\n"
                        f"3차 {setting['third']:,}주 이하"
                        f"{' (꺼짐)' if not setting['third'] else ''}: "
                        f"{int(float(setting.get('third_ratio', 1)) * 100)}%까지 매도\n"
                        "클릭하면 수정")
                return (
                    "현재 잔량의 50%부터 3단 기준을 자동 제안합니다.\n"
                    "클릭하여 잔량매도 기준 설정")
            return None
        if field == "exit_hotkey":
            hotkey = self.exit_hotkeys.get(code)
            if role == Qt.DisplayRole:
                return hotkey[1] if hotkey else "설정"
            if role == Qt.UserRole:
                return hotkey[1] if hotkey else ""
            if role == Qt.TextAlignmentRole:
                return Qt.AlignCenter
            if role == Qt.BackgroundRole and hotkey:
                return QColor("#FFD54F")
            if role == Qt.ForegroundRole and hotkey:
                return QColor("#111111")
            if role == Qt.FontRole and hotkey:
                font = QFont()
                font.setBold(True)
                return font
            if role == Qt.ToolTipRole:
                return (
                    f"{hotkey[1]}: 미체결 매수 취소 + 체결분 전량 청산"
                    if hotkey else
                    "클릭 후 원하는 키 또는 키 조합을 누르면 등록\n"
                    "Delete/Backspace: 등록 해제")
            return None
        if field == "auto_cancel_arm":
            armed = code in self.account_auto_cancel_armed
            if role == Qt.DisplayRole:
                return "ON" if armed else "□"
            if role == Qt.UserRole:
                return 1 if armed else 0
            if role == Qt.TextAlignmentRole:
                return Qt.AlignCenter
            if role == Qt.BackgroundRole and armed:
                return QColor("#FFF176")
            if role == Qt.ForegroundRole and armed:
                return QColor("#111111")
            if role == Qt.FontRole and armed:
                font = QFont()
                font.setBold(True)
                return font
            if role == Qt.ToolTipRole:
                return (
                    "선택한 이 종목만 자동취소 감시 중 · 앱/영웅문 주문 포함\n"
                    "각 매수 주문번호별 체결량이 100주에 도달하면 "
                    "그 주문의 잔량 전부 취소\n"
                    "클릭하면 즉시 해제"
                    if armed else
                    "해제 상태 · 클릭하면 선택한 이 종목만 감시 시작\n"
                    "앱과 영웅문에서 낸 매수주문 모두 포함")
            return None
        if field == "order":
            selected = code == self.order_target_code
            status = self.order_status.get(code, "")
            if role == Qt.DisplayRole:
                return status or ("대상" if selected else "선택")
            if role == Qt.UserRole:
                return 1 if selected else 0
            if role == Qt.TextAlignmentRole:
                return Qt.AlignCenter
            failed = status in ("장종료", "오류", "수량부족", "분할부족")
            if role == Qt.BackgroundRole:
                if failed:
                    return RED
                if status or selected:
                    return QColor("#FFF176")
            if role == Qt.ForegroundRole:
                if failed:
                    return WHITE
                if status or selected:
                    return QColor("#111")
            if role == Qt.FontRole and (status or selected):
                font = QFont()
                font.setBold(True)
                return font
            if role == Qt.ToolTipRole:
                return (
                    "상태 클릭=대상 선택, 오른쪽 취소=이 종목 잔량 즉시취소"
                    if code in self.order_cancellable
                    else "클릭하여 주문 대상종목으로 지정")
            if role == ORDER_CANCEL_ROLE:
                return code in self.order_cancellable
            return None
        if field == "streak":  # 연상 = 어제까지 일수 + (지금 상한이면 1), 매번 계산 (저장 안 함)
            cnt, yclose = self.limit_cnt.get(self.codes[index.row()], (0, 0))
            # +1은 실제 체결 상한(현재가=상한가)만: 예상등락률(동시호가/VI 예상)로는 안 셈.
            # upper==어제종가면 휴장일 묵은 세션(이미 cnt에 포함) -> +1 억제 (003680 사건).
            today_limit = (stored["upper"] > 0 and stored["price"] == stored["upper"]
                           and stored["upper"] != yclose)
            n = cnt + (1 if today_limit else 0)
            if role == Qt.DisplayRole:
                return str(n) if n else ""
            if role == Qt.UserRole:
                return n
            if role == Qt.TextAlignmentRole:
                return Qt.AlignCenter
            if role == Qt.BackgroundRole and code in self.watched:
                return WATCH_BG
            if role == Qt.ForegroundRole:
                if code in self.watched:
                    return WATCH_TEXT
                if n:
                    return RED
            if role == Qt.FontRole and code in self.watched:
                font = QFont()
                font.setBold(True)
                return font
            if role == Qt.ToolTipRole:
                return (
                    "실시간 뉴스 감시 중 · 클릭하면 해제"
                    if code in self.watched
                    else "클릭하면 실시간 뉴스 감시에 등록")
            return None
        if field == "buy_pct":  # 최근 1분 수량 70% + 건수 30% - 불일치 감점
            now = time.monotonic()
            dq = self.ticks.get(self.codes[index.row()], ())
            current = [(t, q, p) for t, q, p in dq if t >= now - 60 and q]
            recent = [(t, q, p) for t, q, p in current if t >= now - 20]
            previous = [(t, q, p) for t, q, p in current if t < now - 20]
            pct = self._combined_buy_pct(current)
            rp, pp = self._combined_buy_pct(recent), self._combined_buy_pct(previous)
            trend = (1 if len(recent) >= 3 and len(previous) >= 5 and rp is not None and pp is not None and rp > pp + 5
                     else -1 if len(recent) >= 3 and len(previous) >= 5 and rp is not None and pp is not None and rp < pp - 5
                     else 0)
            if role == Qt.DisplayRole:
                return f"{pct:.0f}%" if pct is not None else ""
            if role == Qt.UserRole:
                return pct if pct is not None else -1
            if role == BUY_TREND_ROLE:
                return trend
            if role == Qt.TextAlignmentRole:
                return Qt.AlignRight | Qt.AlignVCenter
            if role == Qt.ForegroundRole and pct is not None:
                return RED if pct >= 55 else BLUE if pct <= 45 else None
            return None
        if field == "predict":  # 3·5·10분 단타 상승압력 종합점수
            now = time.monotonic()
            score, horizon_scores = self._prediction_values(
                self.codes[index.row()], stored, now)
            if role == Qt.DisplayRole:
                if score is None:
                    return ""
                # 화살표 2칸 + 점수 3칸을 고정해 단계/자릿수 변화 때 좌우로 흔들리지 않게 한다.
                arrow = "▲▲" if score >= 70 else "▲ " if score >= 60 else \
                        "▼▼" if score <= 30 else "▼ " if score <= 40 else "－ "
                return f"{arrow}{score:3.0f}"
            if role == Qt.UserRole:
                return score if score is not None else -1
            if role == Qt.ToolTipRole:
                parts = [f"{spec[0]} {value:.0f}" if value is not None else f"{spec[0]} 준비중"
                         for spec, value in zip(PREDICT_HORIZONS, horizon_scores)]
                combined = f"종합 {score:.0f}" if score is not None else "종합 준비중"
                return ("단타 상승압력 점수 (확률 아님)\n"
                        + " | ".join(parts) + "\n" + combined)
            if role == Qt.TextAlignmentRole:
                return Qt.AlignCenter
            if role == Qt.ForegroundRole and score is not None:
                return RED if score >= 60 else BLUE if score <= 40 else None
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
        if field == "qrank":  # ★조회순위 모드 전용: 조회수 순위
            v = stored[field]
            if role == Qt.DisplayRole:
                return str(v) if v else ""
            if role == Qt.UserRole:
                return v
            if role == Qt.TextAlignmentRole:
                return Qt.AlignCenter
            return None
        if field == "qrank_chg":  # 직전 집계 대비 순위 변동 (rank.py 변동과 동일 표기)
            v = stored[field]
            if role == Qt.DisplayRole:
                return "" if not v else f"▲{v}" if v > 0 else f"▼{-v}"
            if role == Qt.UserRole:
                return v
            if role == Qt.TextAlignmentRole:
                return Qt.AlignCenter
            if role == Qt.ForegroundRole and v:
                return RED if v > 0 else BLUE
            return None
        if field == "minute_value":
            value = max(0, int(stored.get(field) or 0))
            if role == Qt.DisplayRole:
                return f"{value / 100_000_000:,.1f}억" if value else ""
            if role == Qt.UserRole:
                return value
            if role == Qt.TextAlignmentRole:
                return Qt.AlignRight | Qt.AlignVCenter
            if role == Qt.ToolTipRole:
                return f"최근 60초 실제 체결대금\n{value:,}원"
            return None
        value = stored[field]

        if role == BAR_ROLE and field == "bar":  # 델리게이트용
            return (stored["open"], stored["high"], stored["low"], stored["price"],
                    stored["base"], stored["upper"], stored["lower"])
        if role == NXT_ROLE:  # 델리게이트 종목명 배경 판단
            return self.codes[index.row()] in self.nxt
        if role == SHORT_OVERHEAT_ROLE:
            return self.codes[index.row()] in self.short_overheat
        if role == MISU_ROLE:
            return self.codes[index.row()] in self.misu
        if role == NEW_ROLE:
            code = self.codes[index.row()]
            return (3 if code in self.new_today else 2 if code in self.new15
                    else 1 if code in self.new30 else 0)
        if role == THEME_LEADER_ROLE:
            return self.codes[index.row()] in self.theme_leaders
        if role == THEME_SINGLETON_ROLE:
            return self.codes[index.row()] in self.theme_singletons
        if role == Qt.ToolTipRole and field == "name":  # 모서리 삼각형 설명
            code = self.codes[index.row()]
            parts = []
            themes = self.theme_labels.get(code.removesuffix("_AL"), ())
            if themes:
                parts.append("테마: " + " · ".join(themes))
            relations = self.relation_labels.get(code.removesuffix("_AL"), ())
            if relations:
                parts.append("관계 묶음: " + " · ".join(relations))
            evidence = self.relation_evidence.get(code.removesuffix("_AL"), ())
            if evidence:
                parts.append("DART 근거: " + "\n".join(evidence))
            if code in self.theme_leaders:
                parts.append("★ 현재 테마 대장")
            elif code in self.theme_singletons:
                parts.append("◇ 현재 조건검색의 단독 테마 종목")
            if code in self.short_overheat:
                parts.append("종목명 주황색 배경 = 단기과열(30분 단일가)")
            if code in self.nxt and code in self.short_overheat:
                parts.append("NXT 거래가능(단기과열 배경 우선)")
            elif code in self.nxt:
                parts.append("종목명 적갈색 배경 = NXT 거래가능(매매 제외 확인용)")
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
        code = self.codes[index.row()]
        # 정리매매는 가격제한폭이 없으므로 API가 명목 상/하한가를 주더라도 배경색을 칠하지 않는다.
        if code in self.liquidation:
            is_limit = exp_is_limit = False
        # 상한/하한가 값이 있으면 실제 도달 여부로 판정(29.75%≠30% 오탐 방지), 없으면 rate 폴백
        elif up > 0 and lo > 0:
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


def _compact_shares(value: int) -> str:
    value = max(0, int(value or 0))
    if value >= 10_000:
        return f"{value / 10_000:.1f}만".replace(".0만", "만")
    return f"{value:,}"


def _stored_bool(value) -> bool:
    """QSettings가 bool 또는 문자열로 돌려주는 값을 동일하게 해석한다."""
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in ("1", "true", "yes", "on")


def _shares_in_ten_thousands(value: int) -> str:
    """3단매도 셀에는 '만' 없이 만 주 단위 숫자만 표시한다."""
    value = max(0, int(value or 0))
    if value <= 0:
        return "-"
    amount = value / 10_000
    if amount.is_integer():
        return str(int(amount))
    return f"{amount:.2f}".rstrip("0").rstrip(".")


def _clean_balance_value(value: float) -> int:
    """잔량 기준을 한눈에 읽히는 절대수량 단위로 내림한다."""
    value = max(1, int(value))
    if value >= 1_000_000:
        unit = 100_000
    elif value >= 100_000:
        unit = 10_000
    elif value >= 10_000:
        unit = 1_000
    elif value >= 1_000:
        unit = 100
    else:
        unit = 10 if value >= 100 else 1
    return max(1, value // unit * unit)


def _balance_sell_suggestion(current: int) -> tuple[int, int, int]:
    """장초 대량취소를 감안한 잔량 규모별 3단 절대수량 제안."""
    current = max(0, int(current or 0))
    tiers = (
        (10_000_000, (3_000_000, 1_500_000, 500_000)),
        (5_000_000, (2_000_000, 1_000_000, 300_000)),
        (2_000_000, (1_000_000, 500_000, 200_000)),
        (1_000_000, (500_000, 300_000, 100_000)),
        (500_000, (300_000, 150_000, 50_000)),
        (200_000, (100_000, 50_000, 20_000)),
    )
    for minimum, values in tiers:
        if current >= minimum:
            return values
    if current <= 0:
        return 0, 0, 0
    first = _clean_balance_value(current * .50)
    second = min(first - 1, _clean_balance_value(current * .25))
    third = min(second - 1, _clean_balance_value(current * .10))
    return max(3, first), max(2, second), max(1, third)


class BalanceStepSpinBox(QSpinBox):
    """보조키에 따라 잔량 증감 단위를 1만·10만·100만으로 전환한다."""

    def stepBy(self, steps: int):
        modifiers = QApplication.keyboardModifiers()
        unit = (
            1_000_000 if modifiers & Qt.ControlModifier
            else 100_000 if modifiers & Qt.ShiftModifier
            else 10_000
        )
        # QAbstractSpinBox가 Ctrl 입력 때 steps 자체를 10배로 넘기므로 크기는
        # 사용하지 않고 위/아래 방향만 취한다. 그렇지 않으면 100만×10이 된다.
        direction = 1 if steps > 0 else -1 if steps < 0 else 0
        self.setValue(self.value() + direction * unit)


class BalanceSellDialog(QDialog):
    """기존 적용값과 편집값을 분리하는 3단 잔량매도 설정창."""

    def __init__(self, screen, code: str, parent=None):
        super().__init__(parent)
        self.screen = screen
        self.code = code
        self.config = screen.model.balance_sell_settings.get(code)
        self._settings = getattr(screen, "_settings", None)
        if self._settings is None:
            self._settings = QSettings("layout.ini", QSettings.IniFormat)
        self._market_sell_key = BALANCE_SELL_MARKET_LAST_KEY
        self._manual_edit = self.config is not None
        self._third_before_second_full = None
        name = screen.model.rows.get(code, {}).get("name") or code
        self.setWindowTitle(f"3단 잔량매도 설정 — {name}")
        self.setModal(True)
        self.setMinimumWidth(430)

        self.applied_label = QLabel()
        self.applied_label.setWordWrap(True)
        self.first_edit = BalanceStepSpinBox()
        self.second_edit = BalanceStepSpinBox()
        self.third_edit = BalanceStepSpinBox()
        for edit in (self.first_edit, self.second_edit, self.third_edit):
            edit.setRange(0, 2_147_483_647)
            edit.setSingleStep(10_000)
            edit.setGroupSeparatorShown(True)
            edit.setAlignment(Qt.AlignRight)
            edit.setSuffix(" 주")
            edit.setToolTip(
                "화살표/휠: 1만 · Shift+화살표/휠: 10만 · "
                "Ctrl+화살표/휠: 100만")
            edit.valueChanged.connect(self._mark_manual)
            edit.lineEdit().returnPressed.connect(self._apply)
        self.first_sell_combo = QComboBox()
        self.second_sell_combo = QComboBox()
        self.third_sell_combo = QComboBox()
        for combo in (
                self.first_sell_combo, self.second_sell_combo,
                self.third_sell_combo):
            for label, ratio in (
                    ("0% · 소리만", 0.0), ("50% 매도", .50),
                    ("100% 전량매도", 1.0)):
                combo.addItem(label, ratio)
            combo.currentIndexChanged.connect(self._mark_manual)
        self.second_sell_combo.currentIndexChanged.connect(
            self._on_second_sell_changed)
        self.market_sell_check = QCheckBox("시장가 매도")
        self.market_sell_check.setToolTip(
            "마지막 체크/해제 상태를 즉시 저장해 다음 설정창과 앱 재실행 때 "
            "복원합니다. 실제 주문에는 해당 종목에서 설정 적용해야 반영됩니다.")
        self.market_sell_check.setStyleSheet(
            "QCheckBox:checked{color:#E53935;font-weight:bold}")
        saved_market_sell = self._settings.value(self._market_sell_key)
        if saved_market_sell is None:
            # 직전 버전의 공통값 또는 종목별 체크 기록을 마지막 선택값으로
            # 한 번 이전한다. 이후에는 이 단일 키만 읽고 쓴다.
            previous_global = self._settings.value(
                "balance_sell_market_enabled")
            legacy_values = [
                self._settings.value(key)
                for key in self._settings.allKeys()
                if key.startswith("balance_sell_market/")
            ]
            if previous_global is not None:
                market_sell = _stored_bool(previous_global)
            elif legacy_values:
                market_sell = any(
                    _stored_bool(value) for value in legacy_values)
            else:
                market_sell = bool(
                    self.config
                    and self.config.get("market_sell", False))
            if (
                previous_global is not None
                or legacy_values
                or self.config is not None
            ):
                self._settings.setValue(
                    self._market_sell_key,
                    "true" if market_sell else "false")
                self._settings.sync()
        else:
            market_sell = _stored_bool(saved_market_sell)
        self.market_sell_check.setChecked(market_sell)
        self.error_label = QLabel()
        self.error_label.setWordWrap(True)
        self.error_label.setStyleSheet("color:#E53935;font-weight:bold")
        self.market_sell_check.toggled.connect(
            self._on_market_sell_toggled)

        self.apply_btn = QPushButton("설정 적용  Enter")
        cancel_btn = QPushButton("취소  Esc")
        off_btn = QPushButton("감시 해제")
        rebase_btn = QPushButton("현재 잔량으로 다시 계산")
        self.apply_btn.setDefault(True)
        self.apply_btn.setAutoDefault(True)
        for button in (cancel_btn, off_btn, rebase_btn):
            button.setAutoDefault(False)
        self.apply_btn.clicked.connect(self._apply)
        cancel_btn.clicked.connect(self.reject)
        off_btn.clicked.connect(self._disable)
        rebase_btn.clicked.connect(self._rebase_now)
        off_btn.setEnabled(self.config is not None)

        grid = QGridLayout()
        grid.addWidget(QLabel("현재 적용값"), 0, 0)
        grid.addWidget(self.applied_label, 0, 1, 1, 2)
        grid.addWidget(self.market_sell_check, 0, 3)
        grid.addWidget(QLabel("1차"), 1, 0)
        grid.addWidget(self.first_edit, 1, 1)
        grid.addWidget(QLabel("이하 → 경고음 +"), 1, 2)
        grid.addWidget(self.first_sell_combo, 1, 3)
        grid.addWidget(QLabel("2차"), 2, 0)
        grid.addWidget(self.second_edit, 2, 1)
        grid.addWidget(QLabel("이하 → 경고음 +"), 2, 2)
        grid.addWidget(self.second_sell_combo, 2, 3)
        grid.addWidget(QLabel("3차"), 3, 0)
        grid.addWidget(self.third_edit, 3, 1)
        grid.addWidget(QLabel("이하 → 완료음 +"), 3, 2)
        grid.addWidget(self.third_sell_combo, 3, 3)

        buttons = QHBoxLayout()
        buttons.addWidget(rebase_btn)
        buttons.addStretch(1)
        buttons.addWidget(cancel_btn)
        buttons.addWidget(self.apply_btn)
        buttons.addWidget(off_btn)

        layout = QVBoxLayout(self)
        layout.addLayout(grid)
        layout.addWidget(self.error_label)
        layout.addLayout(buttons)

        if self.config:
            self.first_edit.setValue(self.config["first"])
            self.second_edit.setValue(self.config["second"])
            self.third_edit.setValue(self.config["third"])
            ratios = (
                float(self.config.get("first_ratio", 0.0)),
                float(self.config.get("second_ratio", .50)),
                float(self.config.get("third_ratio", 1.0)),
            )
            for combo, ratio in zip((
                    self.first_sell_combo, self.second_sell_combo,
                    self.third_sell_combo), ratios):
                combo_index = combo.findData(ratio)
                combo.setCurrentIndex(combo_index if combo_index >= 0 else 0)
            self._on_second_sell_changed()
            self.applied_label.setText(
                ("시장가 · " if self.config.get("market_sell", False)
                 else "지정가 · ")
                + f"{_compact_shares(self.config['first'])}↓ "
                f"{self.first_sell_combo.currentText()} / "
                f"{_compact_shares(self.config['second'])}↓ "
                f"{self.second_sell_combo.currentText()} / "
                + (
                    "3차 제외"
                    if float(self.second_sell_combo.currentData() or 0) >= 1
                    else f"{_compact_shares(self.config['third'])}↓ "
                         f"{self.third_sell_combo.currentText()}"
                ))
        else:
            self.first_sell_combo.setCurrentIndex(0)
            self.third_sell_combo.setCurrentIndex(2)
            self.second_sell_combo.setCurrentIndex(2)
            self.applied_label.setText("없음 — 실제 주문은 실행되지 않습니다")
            self._refresh_suggestion()
            self._manual_edit = False

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._refresh_live)
        self._timer.start(150)
        self._refresh_live()
        self.apply_btn.setFocus()
        # 창이 실제 표시된 뒤에도 Enter 기본 버튼에 포커스를 유지한다.
        QTimer.singleShot(0, self.apply_btn.setFocus)

    def _mark_manual(self):
        self._manual_edit = True
        self.error_label.clear()

    def _on_market_sell_toggled(self, checked: bool):
        """시장가 체크박스의 마지막 선택 상태를 즉시 기억한다."""
        self._settings.setValue(
            self._market_sell_key, "true" if checked else "false")
        self._settings.sync()
        self._mark_manual()

    def _on_second_sell_changed(self, *_):
        full = float(self.second_sell_combo.currentData() or 0) >= 1.0
        if full:
            if self.third_edit.value() > 0:
                self._third_before_second_full = (
                    self.third_edit.value(),
                    self.third_sell_combo.currentIndex(),
                )
            self.third_edit.blockSignals(True)
            self.third_sell_combo.blockSignals(True)
            self.third_edit.setValue(0)
            self.third_sell_combo.setCurrentIndex(0)
            self.third_edit.blockSignals(False)
            self.third_sell_combo.blockSignals(False)
            self.third_edit.setEnabled(False)
            self.third_sell_combo.setEnabled(False)
            self.error_label.setText(
                "2차 전량매도 선택: 3차 단계는 자동 제외됩니다.")
        else:
            self.third_edit.setEnabled(True)
            self.third_sell_combo.setEnabled(True)
            if self.third_edit.value() == 0 and self._third_before_second_full:
                value, combo_index = self._third_before_second_full
                self.third_edit.setValue(value)
                self.third_sell_combo.setCurrentIndex(combo_index)
            self.error_label.clear()

    def _current_bid(self) -> int:
        return max(
            0, int(self.screen.model.rows.get(self.code, {}).get("bid_qty") or 0))

    def _refresh_suggestion(self):
        current = self._current_bid()
        if current <= 0:
            return
        first, second, third = _balance_sell_suggestion(current)
        if float(self.second_sell_combo.currentData() or 0) >= 1.0:
            third = 0
        for edit in (self.first_edit, self.second_edit, self.third_edit):
            edit.blockSignals(True)
        self.first_edit.setValue(first)
        self.second_edit.setValue(second)
        self.third_edit.setValue(third)
        for edit in (self.first_edit, self.second_edit, self.third_edit):
            edit.blockSignals(False)

    def _rebase_now(self):
        """취소물량이 나온 뒤 현재 안정잔량을 새 출발점으로 다시 제안한다."""
        self._refresh_suggestion()
        self._manual_edit = True
        self.error_label.setText(
            "현재 잔량으로 다시 계산했습니다. Enter를 눌러야 적용됩니다.")
        self.apply_btn.setFocus()

    def _refresh_live(self):
        if not self._manual_edit and self.config is None:
            self._refresh_suggestion()

    def _apply(self):
        row = self.screen.model.rows.get(self.code, {})
        current = self._current_bid()
        upper = int(row.get("upper") or 0)
        bid_price = int(row.get("bid_price") or 0)
        first = self.first_edit.value()
        second = self.second_edit.value()
        third = self.third_edit.value()
        if float(self.second_sell_combo.currentData() or 0) >= 1.0:
            third = 0
        if not upper or bid_price != upper:
            self.error_label.setText(
                "현재 최우선 매수호가가 상한가가 아니므로 적용할 수 없습니다.")
            return
        if any(value > current for value in (first, second, third)):
            self.error_label.setText(
                f"각 단계 기준은 현재 매수잔량 {current:,}주 이하여야 합니다.")
            return
        self.screen.set_balance_sell_setting(
            self.code, {
                "first": first, "second": second, "third": third,
                "first_ratio": float(self.first_sell_combo.currentData()),
                "second_ratio": float(self.second_sell_combo.currentData()),
                "third_ratio": float(self.third_sell_combo.currentData()),
                "market_sell": self.market_sell_check.isChecked(),
            })
        self.accept()

    def _disable(self):
        self.screen.set_balance_sell_setting(self.code, None)
        self.accept()


class ConditionScreen(QWidget):
    """조건검색실시간 화면 하나. 나중에 QMdiArea에 이 위젯을 여러 개 띄우면 다중창."""

    order_target_selected = Signal(str, int)  # 종목코드, 상한가 -> main이 kt00010 조회
    order_target_changed = Signal(str)
    order_requested = Signal(str, str, int, bool, int, int)
    cancel_requested = Signal(str)
    emergency_exit_requested = Signal(str, int, bool)
    order_status_acknowledged = Signal(str)
    exit_hotkey_changed = Signal(str, object)
    balance_sell_changed = Signal(str, object)
    account_auto_cancel_changed = Signal(str, bool)
    watch_toggled = Signal(str, bool)
    analysis_stock_requested = Signal(str)
    market_overview_requested = Signal()

    def __init__(self, prefix: str = "", parent=None):
        super().__init__(parent)
        self.prefix = prefix  # 다중창: 창별 설정 키 접두사 ("", "w2_", ...)
        self._settings = QSettings("layout.ini", QSettings.IniFormat)
        self.model = StockModel()

        # 툴바: 조건목록 새로고침 / 조건식 선택 / 등록 토글 / 이탈삭제 / 종목수
        self.reload_btn = QPushButton()  # 조건 목록(CNSRLST) 새로 받기: 영웅문서 조건 추가/수정 시
        self.reload_btn.setIcon(_list_reload_icon(self.style()))
        self.reload_btn.setToolTip("조건목록 재조회 — 영웅문에서 새로 만들거나 수정한 조건식을 목록에 반영")
        self.reload_btn.setFixedWidth(32)
        self.condition_combo = QComboBox()
        self.condition_combo.setFixedWidth(220)  # 창 크기와 무관하게 고정 (굴림9 기준 한글 ~20자)
        # 등록/해제 버튼 없음: 콤보에서 조건 고르는 순간 바로 등록됨(영웅문 방식).
        self.rank_period = QComboBox()  # 순위 계열 기준시간 — 모드 따라 내용 교체(set_rank_period)
        self.rank_period.setFixedWidth(80)
        self.rank_period.setVisible(False)
        self._rank_period_mode = None
        self.refresh_btn = QPushButton()  # 현재 조건 편입목록 새로 받아오기(해제->재등록)
        self.refresh_btn.setIcon(self.style().standardIcon(QStyle.StandardPixmap.SP_BrowserReload))
        self.refresh_btn.setToolTip("재조회 — 현재 조건의 편입 종목을 지금 다시 받아옵니다")
        self.refresh_btn.setFixedWidth(32)
        self.auto_refresh = QCheckBox("재조회")  # 동시호가 때 편입/이탈 수동갱신용
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
        self.auto_remove = QCheckBox("자동삭제")  # 복원/저장은 _settings 준비 후(아래)
        self.auto_remove.setToolTip(
            "체크: 이탈 종목 행을 즉시 제거 · 해제: 이탈 신호는 받되 행과 실시간 추적은 유지")
        self.sound_check = QCheckBox("소리")
        self.sound_check.setToolTip("새 종목이 편입되면 소리 알림 (실시간/재조회 모두)")
        self.limit_sort = QCheckBox("상한↑")
        self.limit_sort.setToolTip(
            "상한가 우선순위를 위에 고정하고 각 그룹은 선택한 컬럼으로 정렬"
            " (진입시간·매수잔량은 비상한 종목 제외)")
        self.theme_sort = QCheckBox("테마정렬")
        self.theme_sort.setToolTip(
            "테마별로 묶어 정렬 · 상한가 진입이 빠른 테마 우선, "
            "그 외는 테마 내 최고 등락률 순 · 테마 안에서는 상한가 진입시각과 등락률 순")
        self._checkbox_style = VisibleCheckStyle()
        self._checkbox_style.setParent(self)
        for checkbox in (
                self.auto_refresh, self.auto_remove, self.sound_check,
                self.limit_sort, self.theme_sort):
            checkbox.setStyle(self._checkbox_style)
        self.unified_check = QPushButton("K")  # KRX<->통합 조건검색·시세 전환, 전 창 공통
        self.unified_check.setCheckable(True)
        self.unified_check.setFixedSize(24, 24)
        self.unified_check.setToolTip(
            "시장 전환 — K: KRX 조건검색·시세 / 통: KRX+NXT 통합 조건검색·시세")
        self.unified_check.toggled.connect(self._on_unified_style)
        self.rank_btn = QPushButton("순위")
        self.rank_btn.setToolTip("실시간 종목조회순위 [0198] 창 열기/닫기")
        self.rank_btn.setFixedWidth(44)
        self.newwin_btn = QPushButton("창+")
        self.newwin_btn.setToolTip("조건검색 창 하나 더 열기 (다른 조건식 동시 감시)")
        self.newwin_btn.setFixedWidth(44)
        self.ip_label = QLabel()  # 공인 IP (App이 메인창만 채움). IP 바뀌면 빨강 강조
        self.ip_label.setVisible(False)
        self.count_label = QLabel("종목수: 0")
        self.theme_btn = QPushButton("🖥")  # 시스템/다크/라이트 앱 전체 테마 순환 (메인창만 배선)
        self.theme_btn.setFixedWidth(32)
        self.theme_btn.setToolTip("테마: 시스템")
        self.on_top_btn = QPushButton("📌")  # 항상 맨 위 토글 (창별)
        self.on_top_btn.setCheckable(True)
        self.on_top_btn.setFixedWidth(32)
        self.on_top_btn.setToolTip("항상 맨 위 — 이 창을 다른 창들 위에 계속 고정")

        top = QHBoxLayout()
        top.addWidget(self.reload_btn)
        top.addWidget(self.condition_combo)
        top.addWidget(self.rank_period)
        top.addWidget(self.refresh_btn)
        top.addWidget(self.auto_refresh)
        top.addWidget(self.refresh_interval)
        top.addWidget(self.auto_remove)
        top.addWidget(self.sound_check)
        top.addWidget(self.limit_sort)
        top.addWidget(self.theme_sort)
        top.addWidget(self.unified_check)
        top.addWidget(self.rank_btn)
        top.addWidget(self.newwin_btn)
        top.addStretch(1)  # 남는 공간은 오른쪽으로
        top.addWidget(self.ip_label)
        top.addWidget(self.count_label)
        top.addWidget(self.theme_btn)
        top.addWidget(self.on_top_btn)  # 오른쪽 끝 = 창 크롬(핀) 자리

        self.market_overview_btn = QPushButton(
            "시황  국내 시장지수·외국인 수급 데이터 대기")
        self.market_overview_btn.setMinimumHeight(25)
        self.market_overview_btn.setToolTip(
            "클릭하면 분석창의 시장 현황 탭을 엽니다.")
        self.market_overview_btn.setStyleSheet(
            "QPushButton { text-align:left; padding:3px 8px;"
            " border:1px solid #6F7782; border-radius:3px; }")
        self.market_overview_btn.clicked.connect(
            self.market_overview_requested.emit)

        # 주문 UI 1단계: 실제 계좌/TR 연결 전 배치와 조작감부터 검증한다.
        # 계좌·자금 현황줄 — 실제 값은 주문 계층을 붙일 때 갱신한다.
        self.estimated_asset_value = QLabel("-")
        self.estimated_asset_value.setMinimumWidth(95)
        self.account_available_value = QLabel("-")
        self.account_available_value.setMinimumWidth(85)
        self.margin_order_check = QCheckBox("미수")
        self.margin_order_check.setStyle(self._checkbox_style)
        self.margin_order_check.setToolTip(
            "체크하면 미수 포함 주문가능금액·수량으로 계산")
        self.order_limit_edit = QLineEdit()
        self.order_limit_edit.setPlaceholderText("자동")
        self.order_limit_edit.setFixedWidth(110)
        self.order_limit_edit.setAlignment(Qt.AlignRight)
        self.order_limit_edit.setToolTip("이번 주문에 사용할 최대금액. 비워두면 계좌 주문가능금액 사용")
        self.order_limit_edit.textChanged.connect(self._refresh_order_funds_display)
        self.order_reserved_value = QLabel("0원")
        self.order_reserved_value.setMinimumWidth(75)
        self.order_remaining_value = QLabel("-")
        self.order_remaining_value.setMinimumWidth(85)
        self.withdrawable_value = QLabel("-")
        self.withdrawable_value.setMinimumWidth(85)
        self.withdrawable_value.setToolTip(
            "키움 예수금 상세의 현재 인출가능금액")
        self.loan_withdrawable_value = QLabel("-")
        self.loan_withdrawable_value.setMinimumWidth(85)
        self.loan_withdrawable_value.setToolTip(
            "키움 응답에 제공되는 매도담보대출 포함 인출가능금액")
        self.orderable_qty_value = QLabel("-")
        self.orderable_qty_value.setMinimumWidth(70)
        self.margin_rate_value = QLabel("증거금 -")
        self.margin_rate_value.setMinimumWidth(120)
        self._account_available = 0
        self._cash_orderable = 0
        self._misu_orderable = 0
        self._order_reserved = 0
        self._order_target_code = ""
        self._orderable_detail = None
        self._margin_preferred = (
            self._settings.value(
                self.prefix + "margin_order", "false") == "true")
        self._margin_auto_change = True
        self.margin_order_check.setChecked(self._margin_preferred)
        self._margin_auto_change = False
        self.margin_order_check.toggled.connect(self._on_margin_order_toggled)

        account_bar = QHBoxLayout()
        account_bar.setSpacing(6)
        account_bar.addWidget(QLabel("추정자산"))
        account_bar.addWidget(self.estimated_asset_value)
        account_bar.addWidget(QLabel("계좌가능"))
        account_bar.addWidget(self.account_available_value)
        account_bar.addWidget(self.margin_order_check)
        account_bar.addWidget(QLabel("사용한도"))
        account_bar.addWidget(self.order_limit_edit)
        account_bar.addWidget(QLabel("예약"))
        account_bar.addWidget(self.order_reserved_value)
        account_bar.addWidget(QLabel("남은금액"))
        account_bar.addWidget(self.order_remaining_value)
        account_bar.addWidget(QLabel("인출가능금액"))
        account_bar.addWidget(self.withdrawable_value)
        account_bar.addWidget(QLabel("대출인출가능금액"))
        account_bar.addWidget(self.loan_withdrawable_value)
        account_bar.addStretch(1)

        # 주문 실행줄 — 종목을 고른 뒤 이 줄에서 분할/취소/주문방식을 즉시 결정한다.
        self.order_target_value = QLabel("종목을 선택하세요")
        self.order_target_value.setMinimumWidth(130)

        self.split_group = QButtonGroup(self)
        self.split_group.setExclusive(True)
        self.split_buttons = {}
        for count in range(1, 10):
            button = QPushButton(str(count))
            button.setCheckable(True)
            button.setFixedSize(28, 24)
            self.split_group.addButton(button, count)
            self.split_buttons[count] = button
        self.split_buttons[9].setChecked(True)
        self.split_group.idClicked.connect(self._on_split_changed)

        self.cancel_group = QButtonGroup(self)
        self.cancel_group.setExclusive(True)
        self.auto_cancel_btn = QPushButton("자동취소")
        self.manual_cancel_btn = QPushButton("수동취소")
        for button in (self.auto_cancel_btn, self.manual_cancel_btn):
            button.setCheckable(True)
            button.setFixedSize(72, 24)
            self.cancel_group.addButton(button)
        # 행의 '자동취소'는 계좌 전체 주문 감시이고, 이 두 버튼은 새 앱 주문의
        # 취소 방식이다. 기본은 안전하게 수동취소로 둔다.
        self.manual_cancel_btn.setChecked(True)
        self.auto_cancel_btn.setToolTip(
            "이 앱에서 새로 전송할 주문에 자동취소를 적용")
        self.manual_cancel_btn.setToolTip(
            "이 앱에서 새로 전송할 주문은 사용자가 직접 취소")

        order_choice_style = (
            "QPushButton{padding:0px 4px}"
            "QPushButton:checked{background:#FFF176;color:#111;font-weight:bold;"
            "border:1px solid #D6A900;padding:0px 4px}"
        )
        for button in (*self.split_buttons.values(),
                       self.auto_cancel_btn, self.manual_cancel_btn):
            button.setStyleSheet(order_choice_style)

        self.fixed_qty_order_btn = QPushButton("100주씩")
        self.remaining_order_btn = QPushButton("분할매수")
        for button in (self.fixed_qty_order_btn, self.remaining_order_btn):
            button.setFixedHeight(24)
            button.setEnabled(False)
            button.setToolTip(
                "주문허용 체크 후 클릭하면 KRX 상한가 지정가 주문을 전송합니다")
        self.order_enable_check = QCheckBox("주문허용")
        self.order_enable_check.setStyle(self._checkbox_style)
        self.order_enable_check.setToolTip("체크한 동안 주문 버튼이 실제 주문을 전송합니다")
        self.order_preview_value = QLabel("예상주문  종목을 선택하세요")
        self.order_preview_value.setTextFormat(Qt.RichText)
        self.order_preview_value.setMinimumHeight(20)
        self.order_preview_value.setStyleSheet(
            "QLabel{padding:1px 5px;border:1px solid #C8C8C8;"
            "background:#F5F5F5;color:#222}")
        # 상세상태 문자열은 내부 보관만 하고, 화면 표시는 종목별 주문 컬럼이 담당한다.
        self.order_status_value = QLabel()
        self.order_enable_check.toggled.connect(self._refresh_order_actions)
        self.fixed_qty_order_btn.clicked.connect(
            lambda: self._request_order("fixed"))
        self.remaining_order_btn.clicked.connect(
            lambda: self._request_order("remaining"))

        order_bar = QHBoxLayout()
        order_bar.setSpacing(4)
        order_bar.addWidget(QLabel("대상"))
        order_bar.addWidget(self.order_target_value)
        order_bar.addWidget(QLabel("주문가능수량"))
        order_bar.addWidget(self.orderable_qty_value)
        order_bar.addWidget(self.margin_rate_value)
        order_bar.addWidget(QLabel("분할"))
        for count in range(1, 10):
            order_bar.addWidget(self.split_buttons[count])
        order_bar.addSpacing(6)
        order_bar.addWidget(self.auto_cancel_btn)
        order_bar.addWidget(self.manual_cancel_btn)
        order_bar.addSpacing(8)
        order_bar.addWidget(self.order_enable_check)
        order_bar.addWidget(self.fixed_qty_order_btn)
        order_bar.addWidget(self.remaining_order_btn)
        order_bar.addStretch(1)

        order_preview_bar = QHBoxLayout()
        order_preview_bar.setContentsMargins(0, 0, 0, 0)
        order_preview_bar.addWidget(self.order_preview_value)

        # 그리드
        self.proxy = TieredProxy()
        self.proxy.setSourceModel(self.model)
        self.proxy.setSortRole(Qt.UserRole)
        self.table = ThemeGroupedTableView()
        self.table.setModel(self.proxy)
        # 주문허용 체크 등으로 포커스가 테이블 밖으로 이동해도 청산키는
        # 활성 조건검색창 전체에서 받아야 한다. 여러 창 중 활성 창만 처리한다.
        QApplication.instance().installEventFilter(self)
        self._hotkey_capture_code = ""
        self.global_hotkeys = False
        # 정렬 수동 제어: 첫 클릭을 내림차순(큰 값 위)부터. Qt 기본은 오름차순이라 직접 처리.
        self.table.setSortingEnabled(False)
        hdr0 = self.table.horizontalHeader()
        hdr0.setSectionsClickable(True)
        hdr0.setSortIndicatorShown(True)
        hdr0.sectionClicked.connect(self._on_header_clicked)
        self._sort_col, self._sort_order = FIELDS.index("rate"), Qt.DescendingOrder  # 기본 등락률 내림차순
        self.limit_sort.toggled.connect(self._on_limit_sort)
        self.theme_sort.toggled.connect(self._on_theme_sort)
        # 상한가정렬은 그룹 판정이 정렬컬럼 밖의 값(상한/매도잔량/예상등락률)이라 Qt 자동재정렬이
        # 안 걸림 -> 데이터 변경 시 직접 재정렬. 스로틀: 실행중이면 리셋 안 함(디바운스로 하면
        # 틱이 200ms보다 자주 오는 장중엔 계속 리셋돼 영영 안 불림 = 재정렬 멈춤 버그).
        self._resort_timer = QTimer(self)
        self._resort_timer.setSingleShot(True)
        self._resort_timer.timeout.connect(self.proxy.invalidate)
        self.model.dataChanged.connect(self._on_data_changed)
        self.model.rowsInserted.connect(self._on_data_changed)
        self.model.rowsRemoved.connect(self._on_data_changed)
        self._balance_blink_timer = QTimer(self)
        self._balance_blink_timer.timeout.connect(
            self._refresh_balance_alert_blink)
        self._balance_blink_timer.start(380)
        self._minute_value_timer = QTimer(self)
        self._minute_value_timer.timeout.connect(
            self.model.refresh_minute_values)
        self._minute_value_timer.start(1000)
        self.table.verticalHeader().setVisible(True)  # 순위(정렬 순서대로 1..N 자동)
        self.table.verticalHeader().setDefaultSectionSize(22)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        # StretchLastSection 금지: 마지막 컬럼 경계선이 사라지고 폭 조절이 잠김
        # 헤더 글자 왼쪽 정렬: 가운데면 칸 좁힐 때 앞자리부터 잘림 (시가총액->총액)
        self.table.horizontalHeader().setDefaultAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self.table.setColumnWidth(NAME_COL, 110)
        self.table.setColumnWidth(THEME_COL, 118)
        self.table.setColumnWidth(STREAK_COL, 34)
        self.table.setColumnWidth(ORDER_COL, 86)
        self.table.setColumnWidth(BALANCE_SELL_COL, 125)
        self.table.setColumnWidth(AUTO_CANCEL_ARM_COL, 62)
        self.table.setColumnWidth(EXIT_HOTKEY_COL, 56)
        self.table.setColumnWidth(BAR_COL, 70)
        self.table.setColumnWidth(MINUTE_VALUE_COL, 72)
        for col, width in RANK_DEFAULT_WIDTHS.items():
            self.table.setColumnWidth(col, width)
        self.table.setItemDelegate(PreserveTextColorDelegate(self.table))
        self.table.setItemDelegateForColumn(BAR_COL, BarDelegate(self.table))
        self.table.setItemDelegateForColumn(NAME_COL, NameDelegate(self.table))
        self.table.setItemDelegateForColumn(ORDER_COL, OrderDelegate(self.table))
        self.table.setSelectionBehavior(QTableView.SelectRows)
        self.table.setSelectionMode(QTableView.NoSelection)  # Windows 네이티브 선택 세로 바 차단
        # 행을 클릭한 뒤 위/아래 화살표로 현재 행을 옮길 수 있게 포커스는 허용한다.
        # 델리게이트에서 State_HasFocus를 제거하므로 Windows 포커스 세로 바는 그리지 않는다.
        self.table.setFocusPolicy(Qt.StrongFocus)
        self.table.selectionModel().currentChanged.connect(lambda *_: self.table.viewport().update())
        # 폰트는 앱 전역(main.py: 굴림체9 NoAA)에서 상속 — 그리드/툴바 통일
        self.table.setEditTriggers(QTableView.NoEditTriggers)
        self.table.clicked.connect(self._on_cell_clicked)
        self.table.setContextMenuPolicy(Qt.CustomContextMenu)
        self.table.customContextMenuRequested.connect(self._on_context_menu)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)
        layout.addLayout(top)
        layout.addLayout(account_bar)
        layout.addLayout(order_bar)
        layout.addLayout(order_preview_bar)
        layout.addWidget(self.table)

        self.model.rowsInserted.connect(self._update_count)
        self.model.rowsRemoved.connect(self._update_count)

        # 컬럼 너비/순서 기억: 저장된 상태 복원 후, 변경 시 debounce 저장
        state = self._settings.value(self.prefix + "header")
        # 컬럼 수가 바뀐 옛 저장분은 restoreState가 False -> 기본 레이아웃/정렬 유지
        if state is not None and self.table.horizontalHeader().restoreState(state):
            # restoreState가 옛 정렬값(가운데)까지 되살림 -> 왼쪽 재적용
            self.table.horizontalHeader().setDefaultAlignment(Qt.AlignLeft | Qt.AlignVCenter)
            sec = self.table.horizontalHeader().sortIndicatorSection()
            if sec >= 0:  # 마지막 정렬 컬럼/방향 복원
                self._sort_col = sec
                self._sort_order = self.table.horizontalHeader().sortIndicatorOrder()
        # saveState는 컬럼 수가 달라지면 통째로 복원에 실패한다. 이름별 너비를 다시
        # 덮어써 새 컬럼이 추가돼도 기존 컬럼 크기는 그대로 유지한다.
        removed_bad_width = False
        for col, field in enumerate(FIELDS):
            key = self.prefix + "colwidth_" + field
            width = self._settings.value(key)
            if width is None:
                continue
            try:
                width = int(width)
            except (TypeError, ValueError):
                width = 0
            if width > 0:
                self.table.setColumnWidth(col, width)
            else:  # 구버전이 숨김 컬럼 폭 0을 저장한 값은 즉시 폐기
                self._settings.remove(key)
                removed_bad_width = True
        if removed_bad_width:
            self._settings.sync()
        if self.table.columnWidth(BALANCE_SELL_COL) > 170:
            self.table.setColumnWidth(BALANCE_SELL_COL, 125)
        # 저장된 구버전 헤더 순서와 무관하게
        # 3단매도 -> 자동취소 -> 청산키를 우측 끝에 둔다.
        header = self.table.horizontalHeader()
        last_visual = self.table.horizontalHeader().count() - 1
        hotkey_visual = header.visualIndex(EXIT_HOTKEY_COL)
        if hotkey_visual != last_visual:
            header.moveSection(hotkey_visual, last_visual)
        auto_cancel_visual = header.visualIndex(AUTO_CANCEL_ARM_COL)
        if auto_cancel_visual != last_visual - 1:
            header.moveSection(auto_cancel_visual, last_visual - 1)
        balance_visual = header.visualIndex(BALANCE_SELL_COL)
        if balance_visual != last_visual - 2:
            header.moveSection(balance_visual, last_visual - 2)
        self._apply_sort()
        self._view_mode = None  # normal / rank / holdings (None=초기)
        self.set_view_mode("normal")  # 순위/변동 기본 숨김
        # 테마 열은 정렬 사용 여부와 무관하게 항상 실제 분류를 표시해야 한다.
        # 이전에는 테마정렬을 켤 때만 DB 연결표를 읽어, 체크가 꺼진 채
        # 시작하면 삼성전자·SK하이닉스처럼 분류가 있는 종목도 미분류였다.
        self._load_theme_classification()
        self.rank_period.activated.connect(self._save_rank_period)
        self.set_rank_period("rank")  # 기본: 조회순위 기준시간 (급증 선택 시 main이 교체)
        if self._settings.value(self.prefix + "limit_sort", "false") == "true":  # 상한가정렬 복원
            self.limit_sort.setChecked(True)
        if self._settings.value(self.prefix + "theme_sort", "false") == "true":
            self.theme_sort.setChecked(True)
        self.auto_remove.setChecked(  # 자동삭제 복원 (기본 켜짐)
            self._settings.value(self.prefix + "auto_remove", "true") == "true")
        self.auto_remove.toggled.connect(self._save_auto_remove)
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

    @staticmethod
    def _money_text(value: int) -> str:
        return f"{max(0, int(value)):,}원"

    def _refresh_order_funds_display(self):
        usable = self._usable_order_funds()
        remaining = max(0, usable - self._order_reserved)
        self.order_reserved_value.setText(self._money_text(self._order_reserved))
        self.order_remaining_value.setText(self._money_text(remaining))
        self._refresh_order_target_display()

    def _usable_order_funds(self) -> int:
        raw = self.order_limit_edit.text().replace(",", "").replace("원", "").strip()
        try:
            manual_limit = max(0, int(raw)) if raw else self._account_available
        except ValueError:
            manual_limit = self._account_available
        return min(self._account_available, manual_limit)

    def _refresh_order_target_display(self):
        code = self._order_target_code
        if not code or code not in self.model.rows:
            self.order_target_value.setText("종목을 선택하세요")
            self.orderable_qty_value.setText("-")
            self.orderable_qty_value.setToolTip("주문 대상종목을 선택하세요")
            self.margin_rate_value.setText("증거금 -")
            self._refresh_order_actions()
            return
        stored = self.model.rows[code]
        name = stored.get("name") or code
        upper = int(stored.get("upper") or 0)
        self.order_target_value.setText(f"{name} ({code})")
        detail = self._orderable_detail
        if not upper:
            self.orderable_qty_value.setText("-")
            self.orderable_qty_value.setToolTip("상한가 정보 대기중")
        elif not detail or detail["code"] != code or detail["price"] != upper:
            self.orderable_qty_value.setText("조회중")
            self.orderable_qty_value.setToolTip(f"상한가 {upper:,}원 기준 조회중")
            self.margin_rate_value.setText("증거금 조회중")
        else:
            misu = self.margin_order_check.isChecked()
            api_qty = detail["margin_qty"] if misu else detail["cash_qty"]
            remaining = max(0, self._usable_order_funds() - self._order_reserved)
            limited_qty = remaining // upper
            qty = min(api_qty, limited_qty)
            self.orderable_qty_value.setText(f"{qty:,}주")
            mode = (
                f"미수 적용 {detail['applied_margin_rate']}%"
                if misu else "현금 100%")
            self.orderable_qty_value.setToolTip(
                f"상한가 {upper:,}원 기준 · {mode} · 계좌조회 {api_qty:,}주")
            stock_rate = detail["stock_margin_rate"]
            applied_rate = f"{detail['applied_margin_rate']}%"
            self.margin_rate_value.setText(
                f"증거금 {stock_rate} / 적용 {applied_rate}"
                if stock_rate and stock_rate != applied_rate
                else f"증거금 {applied_rate}")
        self._refresh_order_actions()

    def _select_order_target(self, code: str):
        self._order_target_code = code
        self._orderable_detail = None
        self.model.set_order_target(code)
        self.order_target_changed.emit(code)
        misu_available = code in self.model.misu
        self._margin_auto_change = True
        self.margin_order_check.setChecked(
            self._margin_preferred if misu_available else False)
        self._margin_auto_change = False
        self.margin_order_check.setEnabled(misu_available)
        self.margin_order_check.setToolTip(
            "미수 포함 주문가능금액·수량으로 계산"
            if misu_available else "이 종목은 미수 불가능")
        self._refresh_order_target_display()
        upper = int(self.model.rows[code].get("upper") or 0)
        if upper:
            self.order_target_selected.emit(code, upper)

    def set_orderable_quantity(self, code: str, price: int, detail: dict):
        """main의 kt00010 결과. 빠르게 다른 종목을 고른 경우 이전 응답은 폐기."""
        if code != self._order_target_code:
            return
        current_upper = int(self.model.rows.get(code, {}).get("upper") or 0)
        if price != current_upper:
            return
        self._orderable_detail = detail
        selected_amount = (
            detail["margin_amount"] if self.margin_order_check.isChecked()
            else detail["cash_amount"])
        self._account_available = selected_amount
        self.account_available_value.setText(self._money_text(selected_amount))
        self._refresh_order_funds_display()

    def set_orderable_quantity_error(self, code: str, price: int, message: str):
        """현재 선택 종목의 주문가능수량 조회 실패를 즉시 표시한다."""
        if code != self._order_target_code:
            return
        current_upper = int(self.model.rows.get(code, {}).get("upper") or 0)
        if price != current_upper:
            return
        self.orderable_qty_value.setText("조회실패")
        self.orderable_qty_value.setToolTip(message or "주문가능수량 조회 실패")
        self.margin_rate_value.setText("증거금 조회실패")
        self._refresh_order_actions()

    def _current_orderable_qty(self) -> int:
        code = self._order_target_code
        detail = self._orderable_detail
        if not code or not detail or detail["code"] != code:
            return 0
        upper = int(self.model.rows.get(code, {}).get("upper") or 0)
        if not upper or detail["price"] != upper:
            return 0
        api_qty = (
            detail["margin_qty"] if self.margin_order_check.isChecked()
            else detail["cash_qty"])
        remaining = max(0, self._usable_order_funds() - self._order_reserved)
        return min(api_qty, remaining // upper)

    def _refresh_order_actions(self, *_):
        available_qty = self._current_orderable_qty()
        selected_count = self.split_group.checkedId()
        fixed_count = min(selected_count, available_qty // 100)
        remaining_count = (
            min(selected_count, max(1, available_qty // 100))
            if available_qty > 0 else 0)
        common_enabled = (
            self.order_enable_check.isChecked()
            and bool(self._order_target_code)
            and not self.model.order_status.get(self._order_target_code))
        self.fixed_qty_order_btn.setEnabled(common_enabled and fixed_count > 0)
        self.remaining_order_btn.setEnabled(common_enabled and remaining_count > 0)
        self.fixed_qty_order_btn.setText(
            f"100주씩 ({fixed_count}회)" if fixed_count
            else "100주씩")
        self.remaining_order_btn.setText(
            f"분할매수 ({remaining_count}회)" if remaining_count
            else "분할매수")
        self._refresh_order_preview(
            available_qty, selected_count, fixed_count, remaining_count)

    @staticmethod
    def _order_slots(actual_count: int, selected_count: int) -> str:
        filled = (
            '<span style="color:#18A558;font-weight:bold">■</span>'
            * actual_count)
        empty = (
            '<span style="color:#B8B8B8">□</span>'
            * max(0, selected_count - actual_count))
        return filled + empty

    def _refresh_order_preview(
            self, available_qty: int, selected_count: int,
            fixed_count: int, remaining_count: int):
        if not self._order_target_code or not self._orderable_detail:
            self.order_preview_value.setText("예상주문&nbsp;&nbsp;종목을 선택하거나 조회를 기다리세요")
            self.order_preview_value.setToolTip("")
            return

        upper = int(
            self.model.rows.get(
                self._order_target_code, {}).get("upper") or 0)
        fixed_total = fixed_count * 100
        excluded = max(0, available_qty - fixed_total)
        fixed_slots = self._order_slots(fixed_count, selected_count)
        if fixed_count:
            fixed_text = (
                f"{fixed_slots}&nbsp; 설정 {selected_count}회 → "
                f"<b>실제 {fixed_count}회</b> · 100주씩 · 총 {fixed_total:,}주")
            if excluded:
                fixed_text += f" · <span style='color:#D66A00'>미주문 {excluded:,}주</span>"
        else:
            fixed_text = (
                f"{self._order_slots(0, selected_count)}&nbsp; "
                "<span style='color:#C62828'>최소 100주 필요</span>")

        if remaining_count:
            base, extra = divmod(available_qty, remaining_count)
            per_order = (
                f"{base + 1:,}/{base:,}주씩" if extra else f"{base:,}주씩")
            split_text = (
                f"{self._order_slots(remaining_count, selected_count)}&nbsp; "
                f"<b>{remaining_count}회</b> · {per_order}")
        else:
            split_text = "주문가능수량 없음"

        self.order_preview_value.setText(
            f"<b>상한가 지정가</b>&nbsp;&nbsp; 100주씩 {fixed_text}"
            f"&nbsp;&nbsp;│&nbsp;&nbsp; 분할매수 {split_text}"
            f"&nbsp;&nbsp;·&nbsp;&nbsp;"
            f"<b>{fixed_total * upper:,}원</b>")
        self.order_preview_value.setToolTip(
            "■ 실제 전송되는 주문 · □ 설정했지만 수량 부족으로 전송되지 않는 주문")

    def _request_order(self, mode: str):
        code = self._order_target_code
        if not self.order_enable_check.isChecked() or not code:
            return
        count = self.split_group.checkedId()
        available_qty = self._current_orderable_qty()
        if mode == "fixed":
            count = min(count, available_qty // 100)
            total_qty = 100 * count
            if count < 1:
                self.model.set_order_status(code, "수량부족")
                self.order_status_value.setText(
                    f"상태 수량부족 · 가능 {available_qty:,}주 / 최소 100주")
                audit_log.info(
                    "order blocked code=%s available=%d required=100",
                    code, available_qty)
                self._refresh_order_actions()
                return
        else:
            count = (
                min(count, max(1, available_qty // 100))
                if available_qty > 0 else 0)
            total_qty = available_qty
        if total_qty < count:
            self.model.set_order_status(code, "분할부족")
            self.order_status_value.setText("상태 분할 횟수보다 가능수량이 적습니다")
            audit_log.info(
                "order blocked code=%s total=%d split=%d",
                code, total_qty, count)
            self._refresh_order_actions()
            return
        price = int(self.model.rows[code].get("upper") or 0)
        auto_cancel = self.auto_cancel_btn.isChecked()
        self.model.set_order_status(code, "대기")
        self.order_status_value.setText(
            f"상태 상한가 지정가 {price:,}원 전송대기 · "
            f"{'자동' if auto_cancel else '수동'} · {count}회 · "
            f"{total_qty:,}주")
        self._refresh_order_actions()
        self.order_requested.emit(
            code, mode, count, auto_cancel, total_qty, price)

    def _on_split_changed(self, _count: int):
        code = self._order_target_code
        if code and self.model.order_status.get(code) in ("수량부족", "분할부족"):
            self.model.set_order_status(code, "")
        self._refresh_order_actions()

    def set_order_state(
            self, code: str, compact: str, detail: str, has_remaining: bool):
        self.model.set_order_status(code, compact, has_remaining)
        if code == self._order_target_code:
            self.order_status_value.setText(detail)
        self._refresh_order_actions()

    def _on_margin_order_toggled(self, checked: bool):
        if not self._margin_auto_change:
            self._margin_preferred = checked
            self._settings.setValue(
                self.prefix + "margin_order",
                "true" if checked else "false")
            self._settings.sync()
        detail = self._orderable_detail
        if detail and detail["code"] == self._order_target_code:
            self._account_available = (
                detail["margin_amount"] if checked else detail["cash_amount"])
        else:
            self._account_available = (
                self._misu_orderable if checked else self._cash_orderable)
        self.account_available_value.setText(self._money_text(self._account_available))
        self._refresh_order_funds_display()

    def set_account_summary(self, summary: dict):
        """실계좌 조회값을 주문 자금줄에 표시."""
        estimated = int(summary.get("estimated_assets", 0) or 0)
        self._cash_orderable = int(summary.get("cash_orderable", 0) or 0)
        margin_amounts = summary.get("orderable_by_margin", {})
        self._misu_orderable = int(
            margin_amounts.get(20, margin_amounts.get("20", 0)) or 0)
        self._account_available = (
            self._misu_orderable if self.margin_order_check.isChecked()
            else self._cash_orderable)
        self.estimated_asset_value.setText(self._money_text(estimated))
        self.account_available_value.setText(self._money_text(self._account_available))
        withdrawable = summary.get("withdrawable")
        loan_withdrawable = summary.get("loan_withdrawable")
        self.withdrawable_value.setText(
            self._money_text(int(withdrawable))
            if withdrawable is not None else "-")
        self.loan_withdrawable_value.setText(
            self._money_text(int(loan_withdrawable))
            if loan_withdrawable is not None else "-")
        self._refresh_order_funds_display()

    def set_market_overview(self, text: str, regime: str = "중립"):
        """메인 조건검색창에 국내 핵심 시황을 한 줄로 표시한다."""
        self.market_overview_btn.setText(text)
        self.market_overview_btn.setToolTip(
            text + "\n클릭하면 분석창의 시장 현황 탭을 엽니다.")
        colors = {
            "위험선호": ("#173F2A", "#B9F6CA", "#38A169"),
            "위험회피": ("#4A2020", "#FFD0D0", "#E05252"),
            "중립": ("#263544", "#D6E8FF", "#6F91B5"),
        }
        background, foreground, border = colors.get(
            regime, colors["중립"])
        self.market_overview_btn.setStyleSheet(
            "QPushButton { text-align:left; padding:3px 8px;"
            f" background:{background}; color:{foreground};"
            f" border:1px solid {border}; border-radius:3px;"
            " font-weight:600; }"
            "QPushButton:hover { border-width:2px; }")

    def set_order_reserved(self, amount: int):
        self._order_reserved = max(0, int(amount))
        self._refresh_order_funds_display()

    def set_balance_sell_setting(self, code: str, setting):
        """검증이 끝난 임시 편집값을 현재 적용값과 한 번에 교체한다."""
        if setting is None:
            self.model.balance_sell_settings.pop(code, None)
            self.model.balance_sell_stage.pop(code, None)
            self.model.balance_alert_stage.pop(code, None)
            self.model.balance_alert_ticks.pop(code, None)
        else:
            self.model.balance_sell_settings[code] = {
                "first": int(setting["first"]),
                "second": int(setting["second"]),
                "third": int(setting["third"]),
                "first_ratio": float(setting.get("first_ratio", 0.0)),
                "second_ratio": float(setting.get("second_ratio", 0.5)),
                "third_ratio": float(setting.get("third_ratio", 1.0)),
                "market_sell": bool(setting.get("market_sell", False)),
            }
            self._settings.setValue(
                BALANCE_SELL_MARKET_LAST_KEY,
                "true" if setting.get("market_sell", False) else "false")
            self._settings.sync()
            self.model.balance_sell_stage[code] = 0
        if code in self.model.rows:
            row = self.model.codes.index(code)
            cell = self.model.index(row, BALANCE_SELL_COL)
            self.model.dataChanged.emit(cell, cell)
        self.balance_sell_changed.emit(code, setting)

    def set_balance_sell_stage(self, code: str, stage: int):
        previous = self.model.balance_sell_stage.get(code, 0)
        stage = max(previous, int(stage))
        self.model.balance_sell_stage[code] = stage
        if stage > previous:
            self.model.balance_alert_stage[code] = stage
            self.model.balance_alert_ticks[code] = 6  # 약 2초, 색상 점멸 3회
            self.model.balance_blink_on = True
        if code in self.model.rows:
            row = self.model.codes.index(code)
            cell = self.model.index(row, BALANCE_SELL_COL)
            self.model.dataChanged.emit(cell, cell)
            if stage > previous and stage == 1:
                # 1차 경고음을 놓치지 않도록 정렬된 표에서 해당 종목 행을
                # 현재 행으로 표시하고, 화면 밖이면 보이는 위치까지 이동한다.
                visible_cell = self.proxy.mapFromSource(cell)
                if visible_cell.isValid():
                    self.table.setCurrentIndex(visible_cell)
                    self.table.scrollTo(visible_cell)

    def _refresh_balance_alert_blink(self):
        if not self.model.balance_alert_stage:
            return
        self.model.balance_blink_on = not self.model.balance_blink_on
        for code in tuple(self.model.balance_alert_stage):
            remaining = self.model.balance_alert_ticks.get(code, 0) - 1
            if remaining <= 0:
                self.model.balance_alert_stage.pop(code, None)
                self.model.balance_alert_ticks.pop(code, None)
            else:
                self.model.balance_alert_ticks[code] = remaining
            if code not in self.model.rows:
                continue
            row = self.model.codes.index(code)
            cell = self.model.index(row, BALANCE_SELL_COL)
            self.model.dataChanged.emit(cell, cell)

    def _acknowledge_balance_alert(self, code: str):
        if code not in self.model.balance_alert_stage:
            return
        self.model.balance_alert_stage.pop(code, None)
        self.model.balance_alert_ticks.pop(code, None)
        if code in self.model.rows:
            row = self.model.codes.index(code)
            cell = self.model.index(row, BALANCE_SELL_COL)
            self.model.dataChanged.emit(cell, cell)

    def _on_unified_style(self, on: bool):
        # 통합 = 노랑 배경(NXT 마크색)에 '통', KRX = 기본 버튼에 'K'
        self.unified_check.setText("통" if on else "K")
        self.unified_check.setStyleSheet(
            "QPushButton{background:#FFDD00;color:black;font-weight:bold}" if on else "")

    def _on_data_changed(self, *a):
        first = last = None
        if (len(a) >= 2 and isinstance(a[0], QModelIndex)
                and isinstance(a[1], QModelIndex)):
            first, last = a[0].column(), a[1].column()

        # 대금/분은 체결마다 바뀌므로 Qt 자동정렬에 맡기면 한 틱마다 행 전체가
        # 재배치되어 화면이 버벅인다. 값은 즉시 갱신하되 순서만 200ms 단위로 묶는다.
        minute_sort_changed = (
            self._sort_col == MINUTE_VALUE_COL
            and (first is None or first <= MINUTE_VALUE_COL <= last)
        )

        # 테마순서는 등락률·현재가(상한 판정)·진입시간 변화에만 영향받는다.
        # 호가 등 매 틱 갱신마다 전체 테마를 다시 정렬하지 않는다.
        theme_sort_changed = False
        if self.theme_sort.isChecked() and not self.limit_sort.isChecked():
            theme_sort_changed = (
                first is None
                or any(first <= column <= last
                       for column in (RATE_COL, PRICE_COL, TIME_COL))
            )

        needs_resort = (
            self.limit_sort.isChecked()
            or theme_sort_changed
            or minute_sort_changed
        )
        # 스로틀: 이미 대기중이면 리셋하지 않음 -> 틱이 몰려도 200ms마다 반드시 재정렬됨
        if needs_resort and not self._resort_timer.isActive():
            self._resort_timer.start(200)

    def _update_count(self):
        self.count_label.setText(f"종목수: {self.model.rowCount()}")

    def _save_layout(self):
        header = self.table.horizontalHeader()
        self._settings.setValue(self.prefix + "header", header.saveState())
        for col, field in enumerate(FIELDS):
            # 숨김 컬럼은 sectionSize=0이다. 이를 저장하면 순위 화면에서 다시
            # 표시해도 폭 0으로 남으므로 마지막 정상 너비를 보존한다.
            if not header.isSectionHidden(col) and header.sectionSize(col) > 0:
                self._settings.setValue(
                    self.prefix + "colwidth_" + field, header.sectionSize(col))
        self._settings.sync()  # 강제 종료돼도 디스크에 남게

    @staticmethod
    def _exit_hotkey(event) -> tuple[int, str]:
        """수정키 단독을 제외한 Qt의 모든 단일 키/조합을 표시 가능한 값으로 만든다."""
        key = int(event.key())
        if key in {
            int(Qt.Key.Key_Shift), int(Qt.Key.Key_Control),
            int(Qt.Key.Key_Alt), int(Qt.Key.Key_Meta),
            int(Qt.Key.Key_AltGr),
        }:
            return 0, ""
        combination = event.keyCombination()
        combined = int(combination.toCombined())
        label = QKeySequence(combination).toString(
            QKeySequence.SequenceFormat.NativeText)
        if not label:
            label = event.text().strip() or f"KEY {key}"
        return combined, label

    def _refresh_exit_hotkey_cell(self, code: str):
        if code not in self.model.rows:
            return
        row = self.model.codes.index(code)
        cell = self.model.index(row, EXIT_HOTKEY_COL)
        self.model.dataChanged.emit(cell, cell)

    def eventFilter(self, watched, event):
        try:
            return self._handle_table_key_event(watched, event)
        except Exception:  # noqa: BLE001
            # Qt 가상 메서드 밖으로 예외가 빠지면 GUI 프로세스 자체가 종료될
            # 수 있으므로 특수키 처리 오류는 해당 입력만 차단하고 반드시 기록한다.
            log.exception(
                "exit hotkey event failed key=%s capture=%s",
                getattr(event, "key", lambda: "?")(),
                self._hotkey_capture_code)
            QToolTip.showText(QCursor.pos(), "청산키 처리 오류 · 로그를 확인하세요")
            return True

    def _handle_table_key_event(self, watched, event):
        in_this_screen = (
            watched is self.table
            or (
                isinstance(watched, QWidget)
                and (watched is self or self.isAncestorOf(watched))
                and self.window().isActiveWindow()
            )
        )
        if in_this_screen and event.type() == QEvent.Type.KeyPress:
            key = int(event.key())
            if self._hotkey_capture_code:
                code = self._hotkey_capture_code
                if key == int(Qt.Key.Key_Escape):
                    self._hotkey_capture_code = ""
                    log.info("exit hotkey capture cancelled code=%s", code)
                    QToolTip.showText(QCursor.pos(), "청산키 설정 취소")
                    return True
                if key in (int(Qt.Key.Key_Delete), int(Qt.Key.Key_Backspace)):
                    previous = self.model.exit_hotkeys.get(code)
                    self.model.exit_hotkeys.pop(code, None)
                    self._hotkey_capture_code = ""
                    self._refresh_exit_hotkey_cell(code)
                    self.exit_hotkey_changed.emit(code, None)
                    audit_log.info(
                        "exit hotkey cleared code=%s key=%s",
                        code, previous[1] if previous else "-")
                    QToolTip.showText(QCursor.pos(), "청산키 해제")
                    return True
                combined, label = self._exit_hotkey(event)
                if not combined:
                    QToolTip.showText(
                        QCursor.pos(), "Ctrl·Alt·Shift 같은 수정키는 다른 키와 함께 누르세요")
                    return True
                # 한 창 안에서는 하나의 키가 한 종목만 청산하도록 중복을 교체한다.
                for other, assigned in tuple(self.model.exit_hotkeys.items()):
                    if assigned[0] == combined:
                        self.model.exit_hotkeys.pop(other, None)
                        self._refresh_exit_hotkey_cell(other)
                        self.exit_hotkey_changed.emit(other, None)
                self.model.exit_hotkeys[code] = (combined, label)
                self._hotkey_capture_code = ""
                self._refresh_exit_hotkey_cell(code)
                self.exit_hotkey_changed.emit(code, {
                    "key": key,
                    "modifiers": int(event.modifiers().value),
                    "text": event.text(),
                    "label": label,
                })
                log.warning("exit hotkey assigned code=%s key=%s", code, label)
                QToolTip.showText(QCursor.pos(), f"{code} 청산키 {label} 등록")
                return True

            focus = QApplication.focusWidget()
            if isinstance(focus, (QLineEdit, QSpinBox, QComboBox)):
                # 금액·수량·조건식 입력/선택 중에는 등록된 문자키가 우연히
                # 청산을 실행하지 않게 일반 입력으로 넘긴다.
                return super().eventFilter(watched, event)
            if event.isAutoRepeat():
                combined, _ = self._exit_hotkey(event)
                return True if any(
                    assigned[0] == combined
                    for assigned in self.model.exit_hotkeys.values()) else False
            combined, _ = self._exit_hotkey(event)
            code = next((
                stock_code for stock_code, assigned
                in self.model.exit_hotkeys.items()
                if assigned[0] == combined), "")
            if code:
                if self.global_hotkeys:
                    # Windows RegisterHotKey가 활성/비활성 상태 모두 동일하게
                    # 전달한다. 여기서도 실행하면 활성 창에서 두 번 청산된다.
                    return super().eventFilter(watched, event)
                row = self.model.rows.get(code, {})
                price = int(row.get("bid_price4") or 0)
                if price <= 0:
                    log.warning(
                        "exit hotkey no-bid4 code=%s key=%s",
                        code, self.model.exit_hotkeys[code][1])
                log.warning(
                    "exit hotkey triggered code=%s key=%s price=%s enabled=%s",
                    code, self.model.exit_hotkeys[code][1], price,
                    self.order_enable_check.isChecked())
                self.emergency_exit_requested.emit(
                    code, price, self.order_enable_check.isChecked())
                return True
        return super().eventFilter(watched, event)

    def _on_cell_clicked(self, index):
        """등락률=분석, 연상=뉴스, 주문=대상, 자동취소/3단매도/청산키=설정."""
        source = self.proxy.mapToSource(index)
        code = self.model.codes[source.row()]
        if index.column() == RATE_COL:
            self.analysis_stock_requested.emit(code)
        elif index.column() == STREAK_COL:
            self.watch_toggled.emit(code, code not in self.model.watched)
        elif index.column() == ORDER_COL:
            order_status = self.model.order_status.get(code, "")
            cell_rect = self.table.visualRect(index)
            cursor_x = self.table.viewport().mapFromGlobal(QCursor.pos()).x()
            cancel_area = (
                cursor_x >= cell_rect.right() - OrderDelegate.CANCEL_WIDTH + 1)
            # 취소 버튼이 실제로 그려진 주문만 취소한다. 수동취소가 기본 선택인
            # 상태에서 단순히 셀 오른쪽을 눌러도 빈 계좌 취소조회가 나가면 안 된다.
            if cancel_area and code in self.model.order_cancellable:
                self.model.set_order_status(
                    code, self.model.order_status.get(code, ""), False)
                self.cancel_requested.emit(code)
                return
            if (order_status in ("장종료", "오류", "수량부족", "분할부족",
                                 "취소없음", "대상없음")
                    or order_status.endswith("완료")):
                self.model.set_order_status(code, "")
                self.order_status_value.setText("")
                self.order_status_acknowledged.emit(code)
                log.info(
                    "order status acknowledged code=%s status=%s",
                    code, order_status)
            self._select_order_target(code)
        elif index.column() == BALANCE_SELL_COL:
            self._acknowledge_balance_alert(code)
            dialog = BalanceSellDialog(self, code, self)
            dialog.adjustSize()
            # 네이티브 창을 생성해야 Windows 제목 표시줄까지 포함한 실제
            # 프레임 크기를 얻을 수 있다. 표시 전이라 화면에는 나타나지 않는다.
            dialog.winId()
            frame_width = dialog.frameGeometry().width()
            frame_height = dialog.frameGeometry().height()

            # 설정창 하단을 표 바로 위에 두고, 오른쪽 끝은 클릭한
            # 3단매도 셀에 맞춘다. 화면 가장자리에서는 작업영역 안으로
            # 밀어 넣어 제목 표시줄과 버튼이 잘리지 않게 한다.
            cell_rect = self.table.visualRect(index)
            cell_right = self.table.viewport().mapToGlobal(
                cell_rect.topRight()).x()
            table_top = self.table.mapToGlobal(QPoint(0, 0)).y()
            gap = 8
            target_x = cell_right - frame_width + 1
            target_y = table_top - frame_height - gap
            anchor = QPoint(cell_right, table_top)
            target_screen = QApplication.screenAt(anchor)
            if target_screen is None:
                target_screen = QApplication.primaryScreen()
            if target_screen is not None:
                available = target_screen.availableGeometry()
                target_x = min(
                    max(target_x, available.left() + gap),
                    available.right() - frame_width - gap + 1,
                )
                target_y = min(
                    max(target_y, available.top() + gap),
                    available.bottom() - frame_height - gap + 1,
                )
            dialog.move(target_x, target_y)
            dialog.exec()
        elif index.column() == AUTO_CANCEL_ARM_COL:
            armed = code not in self.model.account_auto_cancel_armed
            self.account_auto_cancel_changed.emit(code, armed)
        elif index.column() == EXIT_HOTKEY_COL:
            self._hotkey_capture_code = code
            self.table.setFocus(Qt.FocusReason.MouseFocusReason)
            current = self.model.exit_hotkeys.get(code)
            QToolTip.showText(
                QCursor.pos(),
                (f"{code}: 새 청산키를 누르세요"
                 + (f" (현재 {current[1]})" if current else "")
                 + "\nDelete/Backspace: 해제 · Esc: 취소"))
        elif index.column() == NAME_COL:
            QApplication.clipboard().setText(code)
            QToolTip.showText(QCursor.pos(), f"{code} 복사됨")

    def _on_context_menu(self, pos):
        """종목명 우클릭 -> 네이버 종목토론실 브라우저로 열기."""
        index = self.table.indexAt(pos)
        if not index.isValid() or index.column() != FIELDS.index("name"):
            return
        code = self.model.codes[self.proxy.mapToSource(index).row()]
        QDesktopServices.openUrl(QUrl(f"https://finance.naver.com/item/board.naver?code={code}"))

    def _sync_dynamic_sort_mode(self):
        """고빈도·복합 정렬은 타이머가 맡고 일반 컬럼만 Qt 자동정렬을 쓴다."""
        throttled = (
            self._sort_col == MINUTE_VALUE_COL
            or self.limit_sort.isChecked()
            or self.theme_sort.isChecked()
        )
        self.proxy.setDynamicSortFilter(not throttled)

    def _apply_sort(self):
        self._sync_dynamic_sort_mode()
        self.table.horizontalHeader().setSortIndicator(self._sort_col, self._sort_order)
        self.proxy.sort(self._sort_col, self._sort_order)

    def _on_header_clicked(self, col: int):
        # 상한가정렬 중에도 헤더 클릭 허용: 그룹 내 정렬 기준이 바뀐다
        if col in (ORDER_COL, BALANCE_SELL_COL, EXIT_HOTKEY_COL):
            return
        # 테마정렬은 테마 강도 순서를 고정하므로 대금/분 전체 순위와 양립하지
        # 않는다. 대금/분 헤더를 누르면 사용자가 요청한 전체 종목 순위를 우선한다.
        if col == MINUTE_VALUE_COL and self.theme_sort.isChecked():
            self.theme_sort.setChecked(False)
        if col == self._sort_col:  # 같은 컬럼 재클릭 -> 방향 토글
            self._sort_order = (Qt.AscendingOrder if self._sort_order == Qt.DescendingOrder
                                else Qt.DescendingOrder)
        else:  # 새 컬럼 첫 클릭: DESC_FIRST 컬럼만 내림차순, 나머지는 오름차순부터
            first = Qt.DescendingOrder if FIELDS[col] in DESC_FIRST else Qt.AscendingOrder
            self._sort_col, self._sort_order = col, first
        self._apply_sort()
        self._save_timer.start(400)  # 정렬 상태도 기억

    def _mkey(self, name: str) -> str:
        """화면별 설정 키. 일반 조건식/순위/보유종목 설정을 서로 분리한다."""
        mode_prefix = {"rank": "rankmode_", "holdings": "holdingsmode_"}.get(
            self._view_mode, "")
        return self.prefix + mode_prefix + name

    def set_view_mode(self, mode: str) -> bool:
        """일반/순위/보유종목 전환 및 화면별 창 크기·상한정렬 복원."""
        if mode == self._view_mode:
            return False  # 재폴마다 불림 -> 실제 전환에만 동작
        prev, self._view_mode = self._view_mode, mode
        for c in RANK_COLS:
            self.table.setColumnHidden(
                c,
                mode != "rank"
                or (c == RANK_CHANGE_COL and self._rank_period_mode == "tval"),
            )
        # 대금/분은 모든 화면에서 공통으로 쓴다. 예전 버전의 헤더 저장값이나
        # 직전 화면 모드가 숨김 상태를 남겼어도 전환할 때 항상 다시 표시한다.
        self.table.setColumnHidden(MINUTE_VALUE_COL, False)
        if self.table.columnWidth(MINUTE_VALUE_COL) <= 0:
            self.table.setColumnWidth(MINUTE_VALUE_COL, 72)
        if mode == "rank":
            # 과거 설정에 숨김 폭 0이 남아 있어도 모든 순위 계열에서 즉시 복구한다.
            for col, default_width in RANK_DEFAULT_WIDTHS.items():
                if self.table.columnWidth(col) <= 0:
                    self.table.setColumnWidth(col, default_width)
        self.rank_period.setVisible(mode == "rank")
        if prev is None:  # 시작 경로: geometry/설정은 창 클래스와 __init__이 이미 복원
            return True
        w = self.window()
        if hasattr(w, "_key"):  # 전환: 이전 모드 크기 저장 -> 키 교체 -> 새 모드 크기 복원
            w._save_geo()
            w._key = self._mkey("geometry")
            geo = self._settings.value(w._key)
            if geo is not None:
                w.restoreGeometry(geo)
        self.limit_sort.setChecked(  # 상한가정렬: 새 화면 저장값 로드
            self._settings.value(self._mkey("limit_sort"), "false") == "true")
        self.theme_sort.setChecked(
            self._settings.value(self._mkey("theme_sort"), "false") == "true")
        return True

    def set_ip(self, ip: str, changed: bool):
        """상단바 공인 IP 표시. changed=True면 빨강 배경+볼드로 확 띄움 (API 차단 경보).
        한번 바뀌면 재시작까지 빨강 유지 (키움에 IP 재등록 필요하니까)."""
        self.ip_label.setVisible(True)
        if changed:
            self.ip_label.setText(f" ⚠ IP 변경됨 {ip} — API 재등록 필요 ")
            self.ip_label.setStyleSheet("background:#e83030; color:white; font-weight:bold;")
        else:
            self.ip_label.setText(f"IP {ip}")
            self.ip_label.setStyleSheet("color:#33C24D;")

    def set_rank_period(self, mode: str):
        """순위 계열 기준시간 콤보 내용 교체 + 저장값 복원 (창별·모드별).
        기준시간 없는 모드(대금상위 등)는 콤보 숨김. mode: RANK_PERIODS 키 또는 그 외."""
        periods = RANK_PERIODS.get(mode)
        self.rank_period.setVisible(bool(periods))  # 콤보 표시/숨김은 여기서 소유
        self.table.setColumnHidden(
            RANK_CHANGE_COL,
            self._view_mode != "rank" or mode == "tval")
        if not periods or mode == self._rank_period_mode:
            self._rank_period_mode = mode
            return
        self._rank_period_mode = mode
        c = self.rank_period
        c.blockSignals(True)  # 재구성 중 activated 저장 방지
        c.clear()
        for name, data in periods:
            c.addItem(name, data)
        saved = self._settings.value(self.prefix + "rankperiod_" + mode, c.itemData(0))
        idx = c.findData(saved)
        c.setCurrentIndex(idx if idx >= 0 else 0)
        c.setToolTip("조회순위 집계 구간" if mode == "rank" else "거래량급증 집계 구간(분)")
        c.blockSignals(False)

    def _save_rank_period(self, _):
        self._settings.setValue(self.prefix + "rankperiod_" + self._rank_period_mode,
                                self.rank_period.currentData())
        self._settings.sync()

    def _save_auto_remove(self, on: bool):
        self._settings.setValue(self.prefix + "auto_remove", "true" if on else "false")
        self._settings.sync()

    def _on_limit_sort(self, on: bool):
        if on and self.theme_sort.isChecked():
            self.theme_sort.setChecked(False)
        self.proxy.limit_mode = on
        self._sync_dynamic_sort_mode()
        self.proxy.invalidate()  # 모드 전환 즉시 재정렬 (정렬컬럼/방향은 그대로)
        self._settings.setValue(self._mkey("limit_sort"), "true" if on else "false")
        self._settings.sync()

    def _load_theme_classification(self) -> bool:
        """저장된 테마·관계 연결표를 화면 모델과 정렬 프록시에 반영한다."""
        try:
            # GUI 모듈을 DB 초기화와 분리하기 위해 실제 화면 생성 시 불러온다.
            from analysis_db import (
                active_relation_groups, active_theme_labels,
                dart_relation_evidence_labels,
            )
            labels = active_theme_labels()
            relation_groups = active_relation_groups()
            relation_evidence = dart_relation_evidence_labels()
        except Exception as error:  # noqa: BLE001
            log.warning("theme labels unavailable: %s", error)
            labels = {}
            relation_groups = {}
            relation_evidence = {}
        self.model.set_theme_labels(labels)
        self.model.set_relation_groups(relation_groups)
        self.model.set_relation_evidence(relation_evidence)
        self.proxy.set_theme_labels(labels)
        self.proxy.set_relation_groups(relation_groups)
        return bool(labels)

    def _on_theme_sort(self, on: bool):
        """실시간 조건검색을 현재 강한 테마 흐름으로 묶어 정렬한다."""
        if on and self.limit_sort.isChecked():
            self.limit_sort.setChecked(False)
        if on:
            if not self._load_theme_classification():
                QToolTip.showText(
                    QCursor.pos(),
                    "저장된 테마 분류가 없어 미분류 종목은 등락률 순으로 표시합니다.",
                    self.theme_sort,
                )
        self.proxy.theme_mode = on
        self._sync_dynamic_sort_mode()
        self.proxy.invalidate()
        self._settings.setValue(self._mkey("theme_sort"), "true" if on else "false")
        self._settings.sync()

    def refresh_theme_sort(self):
        """새로 저장된 관계 데이터를 현재 테마정렬 화면에 즉시 반영한다."""
        if self.theme_sort.isChecked():
            self._on_theme_sort(True)

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
        self._settings.sync()

    # --- 웹소켓 계층 연결점 ----------------------------------------------
    def on_included(self, code: str, data: dict):
        """조건 편입 (CNSRREQ I)"""
        self.model.add_stock(code, data)

    def on_included_many(self, codes: list[str]):
        """초기 조건 스냅샷의 대량 편입."""
        self.model.add_stocks([(code, {"name": code}) for code in codes])

    def on_excluded(self, code: str):
        """조건 이탈 (CNSRREQ D)"""
        if self.auto_remove.isChecked():
            if code == self._order_target_code:
                self._order_target_code = ""
                self.model.set_order_target("")
                self.margin_order_check.setEnabled(True)
                self._refresh_order_target_display()
            self.model.remove_stock(code)

    def on_tick(self, code: str, fields: dict):
        """실시간 시세 (0B 체결 / 0D 호가)"""
        self.model.update_stock(code, fields)
        if code == self._order_target_code:
            self._refresh_order_target_display()


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
