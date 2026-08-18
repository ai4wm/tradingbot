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
    QColor, QCursor, QDesktopServices, QFont, QFontMetrics, QIcon,
    QKeySequence, QPainter, QPen, QPixmap, QPolygon,
)
from PySide6.QtWidgets import (
    QApplication, QButtonGroup, QCheckBox, QComboBox, QHBoxLayout, QHeaderView, QLabel,
    QDialog, QGridLayout, QLineEdit, QMainWindow, QMenu, QProxyStyle, QPushButton,
    QSizePolicy, QSpinBox, QStyle, QStyledItemDelegate, QStyleOptionViewItem,
    QTableView, QTableWidgetItem, QToolTip, QVBoxLayout, QWidget,
)

from order import fixed_quantities
from theme_keywords import STRONG_EVENT_THEMES

log = logging.getLogger("gui")
audit_log = logging.getLogger("trade.audit")

# 순위/변동: ★조회순위(ka00198) 모드 전용 -> 다른 화면에선 숨김 (set_view_mode)
# 새 컬럼은 반드시 맨 뒤에 붙인다. 저장된 폭·순서가 논리 인덱스 기준이라
# 중간에 끼우면 사용자 layout.ini의 폭이 엉뚱한 컬럼에 붙는다. 원하는 자리는
# 사용자가 헤더를 끌어 옮긴다(setSectionsMovable).
COLUMNS = ["순위",  "변동",      "등락률", "연상", "종목명", "테마",   "현재가", "예상체결가", "주문",  "L일봉H", "예상등락률", "전일거래량", "거래량", "매도잔량", "매수잔량", "예상체결량", "대금/분",       "시총(억)", "상한가진입시간", "3단매도(만)", "자동취소",       "청산키",     "거래대금"]
FIELDS  = ["qrank", "qrank_chg", "rate",   "streak", "name", "theme", "price", "exp_price", "order", "bar",    "exp_rate",   "prev_vol", "vol",   "ask_qty",  "bid_qty",  "exp_qty",  "minute_value", "mcap",   "time",             "balance_sell", "auto_cancel_arm", "exit_hotkey", "acc_value"]
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
ACC_VALUE_COL = FIELDS.index("acc_value")
MINUTE_VALUE_DISPLAY_STEP = 10_000_000  # 화면의 0.1억원과 정렬 구간을 일치시킨다.
NON_LIMIT_IGNORED_SORT_COLS = {TIME_COL, BID_QTY_COL}
STREAK_COL = FIELDS.index("streak")
MCAP_COL = FIELDS.index("mcap")
ORDER_COL = FIELDS.index("order")
BALANCE_SELL_COL = FIELDS.index("balance_sell")
AUTO_CANCEL_ARM_COL = FIELDS.index("auto_cancel_arm")
EXIT_HOTKEY_COL = FIELDS.index("exit_hotkey")
BALANCE_SELL_MARKET_LAST_KEY = "balance_sell_market_last"
BALANCE_SELL_STAGE_KEYS = ("first", "second", "third")
BALANCE_SELL_STAGE_LAST_KEYS = tuple(
    f"balance_sell_stage_last/{key}" for key in BALANCE_SELL_STAGE_KEYS)


def _balance_stages_done(setting: dict, stage: int) -> bool:
    """켜 둔 단계를 모두 지났는가. 지났으면 더 나갈 매도가 없다."""
    active = sum(1 for key in BALANCE_SELL_STAGE_KEYS
                 if int(setting.get(key, 0)) > 0)
    return bool(active) and stage >= active
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


ORDER_TRIM_KEEP = 100  # 나머지취소가 미체결 매수 각 건에 남기는 수량
TRIM_BUTTON_TEXT = "나머지취소"

NEWS_THEME_MARK = "•"  # 최근 뉴스로 붙은 테마 앞에 붙인다
NO_NEWS_MARK = "?"  # 오늘 재료 기사가 없는 종목: 왜 오르는지 모른다는 뜻
# 노랑은 라이트 배경에서 대비가 안 나온다. 배경 밝기에 따라 빨강을 바꾼다.
NO_NEWS_MARK_LIGHT = QColor("#D32F2F")
NO_NEWS_MARK_DARK = QColor("#FF6B6B")


PRIORITY_THEMES = STRONG_EVENT_THEMES


def _theme_cell_text(labels, news_themes, has_news: bool = True,
                     separator: str = "·") -> str:
    """테마 이름을 잇되 뉴스로 붙은 것에만 표식을 단다.

    색은 테마 묶음 구분에 이미 쓰고 있어 표식으로 나타낸다.

    순서는 사건 재료(인수합병·제3자배정) → 그 밖의 뉴스 재료 → 분류 테마다.
    호출부가 강도로 뽑은 테마를 앞에 넘겨도 여기서 다시 세운다. 칸이 좁아
    앞 한두 개만 읽히는데, 오늘 왜 오르는지가 거기 있어야 한다.
    """
    marks = set(news_themes or ())
    labels = sorted(
        labels,
        key=lambda name: (name not in PRIORITY_THEMES, name not in marks))
    text = separator.join(
        (NEWS_THEME_MARK + name) if name in marks else name
        for name in labels
    )
    return text if has_news else NO_NEWS_MARK + text


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
    # 같은 테마를 '피지컬 AI/…'와 '피지컬 AI·…' 두 표기로 등록하므로
    # 구분자를 맞춘 뒤 비교해야 수동 등록분이 계열로 빨려들지 않는다.
    if value.replace("·", "/") in {
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


class NumericTableWidgetItem(QTableWidgetItem):
    """표시는 서식을 유지하면서 실제 숫자로 정렬하는 셀."""

    def __init__(self, text: str, value):
        super().__init__(text)
        self.setData(Qt.ItemDataRole.UserRole, value)

    def __lt__(self, other):
        left = self.data(Qt.ItemDataRole.UserRole)
        right = other.data(Qt.ItemDataRole.UserRole)
        if left is not None and right is not None:
            return left < right
        return super().__lt__(other)


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


class ClipTextDelegate(PreserveTextColorDelegate):
    """긴 글자를 `…`로 줄이지 않고 셀 경계에서 그대로 잘라 보여 준다.

    NameDelegate와 같은 이유로 textElideMode를 쓰지 않는다. 일부 Windows
    스타일이 ElideNone을 무시하므로 배경·선택만 스타일에 맡기고 글자는 직접
    그려 `…` 변환 경로를 아예 타지 않게 한다."""

    def paint(self, painter, option, index):
        selected = _is_current_row(option, index)
        opt = QStyleOptionViewItem(option)
        opt.state &= ~(QStyle.State_Selected | QStyle.State_HasFocus)
        self.initStyleOption(opt, index)
        text = opt.text
        opt.text = ""
        style = opt.widget.style() if opt.widget else QApplication.style()
        style.drawControl(QStyle.CE_ItemViewItem, opt, painter, opt.widget)
        # Qt가 CE_ItemViewItem에서 쓰는 좌우 여백과 같은 값으로 맞춰, 델리게이트만
        # 바뀌고 글자 위치는 다른 열과 어긋나지 않게 한다.
        margin = style.pixelMetric(
            QStyle.PM_FocusFrameHMargin, None, opt.widget) + 1
        text_rect = style.subElementRect(
            QStyle.SE_ItemViewItemText, opt, opt.widget).adjusted(
                margin, 0, -margin, 0)
        painter.save()
        painter.setClipRect(option.rect)
        painter.setFont(opt.font)
        if text.startswith(NO_NEWS_MARK):
            # 재료 없음 표식만 빨강으로 따로 그린다. 뒤의 테마 이름은 묶음
            # 색을 그대로 써야 해서 두 번에 나눠 그린다.
            dark = opt.palette.base().color().lightness() < 128
            painter.setPen(
                NO_NEWS_MARK_DARK if dark else NO_NEWS_MARK_LIGHT)
            painter.drawText(
                text_rect, opt.displayAlignment | Qt.TextSingleLine,
                NO_NEWS_MARK)
            text_rect = text_rect.adjusted(
                QFontMetrics(opt.font).horizontalAdvance(NO_NEWS_MARK),
                0, 0, 0)
            text = text[len(NO_NEWS_MARK):]
        painter.setPen(opt.palette.text().color())
        painter.drawText(
            text_rect, opt.displayAlignment | Qt.TextSingleLine, text)
        painter.restore()
        if selected:
            _draw_selection_lines(painter, option.rect, option.palette)


class OrderDelegate(PreserveTextColorDelegate):
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


def _minute_value_display_bucket(value) -> int:
    """대금/분 화면 표시와 정렬이 함께 사용할 0.1억원 단위 값."""
    raw = max(0, int(value or 0))
    return (raw + MINUTE_VALUE_DISPLAY_STEP // 2) // MINUTE_VALUE_DISPLAY_STEP


# 상한가정렬 묶음 번호. 작을수록 위다. 구분선은 점상 대기(0) 바로 아래다.
# 0·1·2는 모두 아직 정규장 가격이 안 잡힌(등락률 0) 종목이다.
TIER_WAIT_CLEAN = 0    # 예상상한 대기 · 매도잔량 0 · 매수잔량 있음 (점상)
TIER_WAIT = 1          # 예상상한 대기 · 매도잔량 있음
TIER_PREOPEN = 2       # 장 시작 전 나머지 · 예상등락률 내림차순
TIER_LIMIT_CLEAN = 3   # 실제 상한가 · 매도잔량 0
TIER_LIMIT = 4         # 실제 상한가 · 매도잔량 있음
TIER_NO_ASK = 5        # 거래 중인데 매도호가가 빈 종목 (상한가 직전)
TIER_PLAIN = 6         # 그 밖의 일반 종목


def _limit_tier(d: dict) -> int:
    """상한가정렬 우선순위.

    맨 위(0)는 점상 대기, 즉 예상등락률이 상한가인데 매도잔량이 없고 매수만
    쌓인 종목이다. 구분선은 여기까지다. 점상알림 대상과 선 안이 정확히 같은
    집합이어야 알림이 새지 않는다. 매도잔량이 있으면 예상등락률이 상한가여도
    선 아래(1)로 내리고, 나머지 장 시작 전 종목(2)이 예상등락률 내림차순으로
    뒤따른다. 여기까지가 아직 정규장 가격이 안 잡힌 대기열이다.

    그 아래가 실제 상한가(3·4)이고, 거래 중인데 매도호가 자체가 빈 종목(5)이
    뒤따른다. 매도가 비었다는 건 상한가 직전이라는 뜻이다.

    '아직 가격이 안 잡혔다'는 시각이나 누적거래량이 아니라 등락률 0으로 본다.
    시각으로 자르면 예상등락률이 큰 종목의 랜덤엔드 연장(09:00 + 2분 + 0~20초)
    동안 대기 종목이 묶음에서 빠진다. 누적거래량은 장전 시간외종가(08:30~08:40)
    체결로 이미 0이 아니다. 그 체결은 전일 종가로 이뤄지므로 등락률만 0으로
    남고, 시초가가 잡히는 순간 등락률이 튀어 두 묶음이 저절로 갈린다.

    이미 거래된 종목은 예상값이 있어도 대기열에 넣지 않는다. VI든 단기과열이든
    실제 등락률이 뜬 이상 장 시작 전이 아니다. 대신 등락률로 정렬할 때 예상값이
    살아 있으면 그 값으로 비교해(`StockModel.data`) 곧 체결될 가격이 순서에
    반영되게 한다.
    """
    actual_limit = d["upper"] > 0 and d["price"] == d["upper"]
    expected_limit = d["exp_price"] > 0 and (
        d["exp_price"] >= d["upper"] if d["upper"] > 0 else d["exp_rate"] >= LIMIT
    )
    if expected_limit and not d["rate"]:
        # 매수잔량까지 있어야 점상 대기다. 양쪽 다 비어 있으면 호가가 아직
        # 안 들어온 것이라 같은 자리에 두면 안 된다.
        return (TIER_WAIT_CLEAN
                if d["ask_qty"] == 0 and d["bid_qty"] > 0 else TIER_WAIT)
    if not d["rate"] and d["exp_price"] > 0:
        # 예상값까지 있어야 장 시작 전이다. 등락률 0만 보면 시초가가 전일종가와
        # 같은 종목이 하루 종일 대기열에 남는다. 예상값은 체결이 재개되면
        # 모델이 끄므로, 세 대기 묶음이 09:03 이후 함께 사라진다.
        return TIER_PREOPEN
    if actual_limit:
        return TIER_LIMIT_CLEAN if d["ask_qty"] == 0 else TIER_LIMIT
    if d["ask_qty"] == 0 and d["price"] > 0:
        # 현재가까지 있어야 '거래 중인데 매도가 빈' 종목이다. 방금 편입돼
        # 시세가 아직 안 들어온 행은 값이 전부 0이라 이 자리에 오면 안 된다.
        return TIER_NO_ASK
    return TIER_PLAIN


class TieredProxy(QSortFilterProxyModel):
    """상한가정렬 모드(limit_mode):
    장 시작 전 예상상한 대기, 실제 상한, 장중 예상상한을 각각
    매도잔량 0 -> 매도잔량 있음 순으로 분리해 위에 고정한다.
    그룹 안은 현재 정렬컬럼과 정렬방향을 따른다.
    모드 off면 전 컬럼 일반 정렬."""

    def __init__(self):
        super().__init__()
        self.limit_mode = False
        self.theme_mode = False
        # 세로 헤더를 눌러 맨 위에 붙인 종목. 어떤 정렬에서도 위에 남는다.
        self.pinned: set[str] = set()
        self.theme_labels: dict[str, tuple[str, ...]] = {}
        self.relation_groups: dict[str, tuple[str, ...]] = {}
        self._theme_sort_keys: dict[str, tuple] = {}
        # 현재 테마정렬에서 실제로 선택된 묶음.
        self._theme_group_keys: dict[str, tuple[str, str]] = {}
        self._theme_group_colors: dict[tuple[str, str], QColor] = {}
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
        # 테마정렬을 안 켜도 강도는 계산한다. 테마 컬럼의 이름 순서를 그날
        # 강한 쪽부터 적기 위해서다. 대장 표시와 색 세로바는 켰을 때만 쓴다.
        if not self.theme_mode and getattr(model, "theme_leaders", set()):
            model.theme_leaders = set()
            if model.codes:
                model.dataChanged.emit(
                    model.index(0, NAME_COL),
                    model.index(len(model.codes) - 1, THEME_COL),
                )
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
        # 최고 등락률까지 같으면 '대장을 뺀 나머지'의 평균으로 가른다. 한 종목이
        # 여러 테마에 걸리면 그 종목이 모든 테마의 최고가 되어 동점이 나는데,
        # 이때 종목수나 이름순으로 정하면 재료와 무관한 테마가 대표로 붙는다.
        # 나머지까지 따라 오르는 쪽이 실제로 함께 움직이는 테마다. 자기 혼자인
        # 테마는 0이 되어 밀리므로, 평균만 쓸 때처럼 단독 테마가 이기지 않는다.
        strengths: dict[tuple[str, str], tuple] = {}
        for group, codes in groups.items():
            rows = [model.rows[code] for code in codes]
            limit_times = [self._theme_entry_time(row) for row in rows
                           if self._theme_at_limit(row)]
            rates = [float(row.get("rate") or 0) for row in rows]
            top_rate = max(rates)
            rest_rate = ((sum(rates) - top_rate) / (len(rates) - 1)
                         if len(rates) > 1 else 0.0)
            strengths[group] = (
                0 if limit_times else 1,
                min(limit_times) if limit_times else 999999,
                -top_rate,
                -rest_rate,
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
                key=lambda item: strengths.get(
                    item, (2, 999999, 0, 0, 0, item[1])),
            ) if candidates else ("none", "미분류")
            self._theme_group_keys[code] = group
            theme_strength = strengths.get(
                group, (2, 999999, 0, 0, 0, group[1]))
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

        if not self.theme_mode:
            return  # 강도만 쓰고 색·대장은 켰을 때만 만든다

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


    def refresh_theme_keys(self):
        """테마정렬이 꺼진 화면에서 테마 컬럼의 강도 순서만 갱신한다.

        켜져 있으면 재정렬 타이머가 이미 계산하므로 건너뛴다. 강도는 초 단위로
        따라가면 충분해서 정렬 스로틀(200ms)과 따로 느리게 돈다.
        """
        if self.theme_mode:
            return
        model = self.sourceModel()
        if model is None or not getattr(model, "codes", None):
            return
        before = dict(self._theme_group_keys)
        self._refresh_theme_sort_keys()
        if before != self._theme_group_keys:
            model.dataChanged.emit(
                model.index(0, THEME_COL),
                model.index(len(model.codes) - 1, THEME_COL),
            )

    def sort(self, column, order=Qt.AscendingOrder):
        if column not in NON_LIMIT_IGNORED_SORT_COLS:
            self._non_limit_sort_col = column
            self._non_limit_sort_order = order
        super().sort(column, order)

    def invalidate(self):
        self._refresh_theme_sort_keys()
        super().invalidate()

    def row_code(self, row: int) -> str:
        """프록시 행 -> 종목코드. 정렬 뒤 화면 순서를 소스로 되짚는다."""
        model = self.sourceModel()
        source_index = self.mapToSource(self.index(row, 0))
        if model is None or not source_index.isValid():
            return ""
        if not hasattr(model, "codes") or source_index.row() >= len(model.codes):
            return ""
        return model.codes[source_index.row()]

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        # 세로 헤더 = 순위: 프록시 행번호(정렬 순서)로 1..N. 소스 매핑 안 함(편입순서 X).
        if orientation == Qt.Vertical and role == Qt.DisplayRole:
            # 고정 종목은 번호 대신 표식을 넣는다. 어차피 맨 위 몇 줄이라
            # 순위를 잃지 않고, 컬럼을 새로 만들지 않아도 된다.
            if self.pinned and self.row_code(section) in self.pinned:
                return "📌"
            return section + 1
        return super().headerData(section, orientation, role)

    def data(self, index, role=Qt.DisplayRole):
        if (role == Qt.DisplayRole and index.isValid()
                and FIELDS[index.column()] == "theme" and not self.theme_mode):
            # 정렬을 안 켜도 그날 강한 테마를 앞에 적는다. 순서만 바꾸고
            # 색·대장 표시는 건드리지 않는다.
            model = self.sourceModel()
            source_index = self.mapToSource(index)
            if model is not None and source_index.isValid() and hasattr(
                    model, "codes"):
                code = model.codes[source_index.row()]
                plain = code.removesuffix("_AL")
                labels = model.theme_labels.get(plain, ())
                group = self._theme_group_keys.get(code)
                if labels and group is not None and group[1] in labels:
                    return _theme_cell_text(
                        [group[1], *(t for t in labels if t != group[1])],
                        model.news_themes.get(plain), plain in model.news_codes)
        if index.isValid() and FIELDS[index.column()] == "theme" and self.theme_mode:
            model = self.sourceModel()
            source_index = self.mapToSource(index)
            if model is not None and source_index.isValid() and hasattr(model, "codes"):
                code = model.codes[source_index.row()]
                group = self._theme_group_keys.get(code, ("none", "미분류"))
                name = group[1] if group[0] != "none" else "미분류"
                if role == Qt.DisplayRole:
                    # 묶음 이름을 앞에 두고 나머지 테마를 뒤에 잇는다.
                    # 같은 묶음끼리 앞부분이 같아 정렬 결과를 읽기 쉽다.
                    labels = [
                        theme for theme
                        in model.theme_labels.get(code.removesuffix("_AL"), ())
                        if theme != name
                    ]
                    plain = code.removesuffix("_AL")
                    return _theme_cell_text(
                        [name, *labels], model.news_themes.get(plain),
                        plain in model.news_codes)
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
                    return model.data(
                        model.index(source_index.row(), THEME_COL),
                        Qt.ToolTipRole)
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
        if self.pinned:
            model = self.sourceModel()
            left_pinned = model.codes[left.row()] in self.pinned
            right_pinned = model.codes[right.row()] in self.pinned
            if left_pinned != right_pinned:
                # 어떤 정렬·방향에서도 위에 남긴다. Qt는 내림차순일 때
                # lessThan 결과를 뒤집으므로 반대로 돌려준다.
                return (right_pinned if self.sortOrder() == Qt.DescendingOrder
                        else left_pinned)
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
            ta = _limit_tier(a)
            tb = _limit_tier(b)
            desc = self.sortOrder() == Qt.DescendingOrder
            if ta != tb:  # 우선순위 그룹 순서는 현재 정렬방향과 무관하게 고정
                return ta > tb if desc else ta < tb
            if ta == TIER_PREOPEN and a["exp_rate"] != b["exp_rate"]:
                # 장 시작 전 대기열은 헤더 정렬컬럼·방향과 무관하게 예상등락률
                # 내림차순으로 고정한다. 상한가에 붙은 순서가 그대로 위다.
                return (a["exp_rate"] < b["exp_rate"] if desc
                        else a["exp_rate"] > b["exp_rate"])
            if ta in (TIER_LIMIT_CLEAN, TIER_LIMIT) and left.column() == TIME_COL:
                # 실제 상한가 그룹에서는 진입시간 미수신 종목을 항상 뒤로 보낸다.
                a_has_time, b_has_time = bool(a["time"]), bool(b["time"])
                if a_has_time != b_has_time:
                    return not a_has_time if desc else a_has_time
            if ta == TIER_PLAIN and left.column() in NON_LIMIT_IGNORED_SORT_COLS:
                # 진입시간/매수잔량은 비상한 그룹에 적용하지 않고 직전 정렬을 유지한다.
                fallback_left = m.index(left.row(), self._non_limit_sort_col)
                fallback_right = m.index(right.row(), self._non_limit_sort_col)
                reverse = ((self._non_limit_sort_order == Qt.DescendingOrder)
                           != desc)
                if reverse:
                    return super().lessThan(fallback_right, fallback_left)
                return super().lessThan(fallback_left, fallback_right)
            # 같은 우선순위 그룹끼리: 현재 정렬컬럼으로 일반 비교
        if left.column() == MINUTE_VALUE_COL:
            # 화면에는 0.1억원 단위로 보이는데 원 단위로 정렬하면 같은 숫자로
            # 보이는 두 행이 체결마다 자리를 바꾼다. 표시 구간으로 비교하고,
            # 같은 구간은 편입 순서로 고정해 순위가 흔들리지 않게 한다.
            left_bucket = _minute_value_display_bucket(
                left.data(Qt.UserRole))
            right_bucket = _minute_value_display_bucket(
                right.data(Qt.UserRole))
            if left_bucket != right_bucket:
                return left_bucket < right_bucket
            if self.sortOrder() == Qt.DescendingOrder:
                return left.row() > right.row()
            return left.row() < right.row()
        return super().lessThan(left, right)


class ThemeGroupedTableView(QTableView):
    """거래상태 가로 구분선과 핵심 거래 열 세로 안내선을 표시한다."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self._blink_codes: dict[str, int] = {}  # code -> 남은 토글 수
        self._blink_on = False
        self._blink_timer = QTimer(self)
        self._blink_timer.timeout.connect(self._tick_blink)

    def blink_rows(self, codes, times: int = 8, interval_ms: int = 220):
        """해당 종목 행 테두리를 times/2회 깜박인다. 이미 깜박이는 행은 연장."""
        if not codes:
            return
        for code in codes:
            self._blink_codes[code] = times
        self._blink_on = True
        if not self._blink_timer.isActive():
            self._blink_timer.start(interval_ms)
        self.viewport().update()

    def _tick_blink(self):
        self._blink_on = not self._blink_on
        for code, left in tuple(self._blink_codes.items()):
            if left <= 1:
                del self._blink_codes[code]
            else:
                self._blink_codes[code] = left - 1
        if not self._blink_codes:
            self._blink_timer.stop()
            self._blink_on = False
        self.viewport().update()

    def waiting_group(self) -> tuple[int, set[str], int]:
        """구분선 위 그룹의 마지막 행과 그 종목코드, 그리고 상한가 묶음의 끝.

        선 위는 점상 대기(예상상한 · 매도잔량 0 · 매수잔량 있음)만이다.
        매도잔량이 있으면 예상등락률이 상한가여도 선 아래로 내린다.

        장이 열린 뒤에는 실제 상한가이면서 매도잔량이 0인 묶음(3)의 끝에도
        같은 선을 긋는다. 두 선의 뜻은 '매도잔량 0 묶음이 여기까지'로 같다.
        점상알림 대상(jumsang)은 첫 묶음에서만 모은다 — 알림이 새면 안 된다.
        """
        proxy = self.model()
        if not (isinstance(proxy, TieredProxy) and proxy.limit_mode):
            return -1, set(), -1
        source = proxy.sourceModel()
        if source is None or not hasattr(source, "codes"):
            return -1, set(), -1
        last_row, jumsang, limit_row = -1, set(), -1
        waiting = True  # 아직 첫 묶음(점상 대기)을 지나는 중
        for row in range(proxy.rowCount()):
            source_index = proxy.mapToSource(proxy.index(row, 0))
            if not source_index.isValid():
                break
            code = source.codes[source_index.row()]
            tier = _limit_tier(source.rows[code])
            if waiting and tier == TIER_WAIT_CLEAN:
                jumsang.add(code)
            if code in proxy.pinned:
                # 고정 종목은 순위와 무관하게 맨 위에 붙는다. 여기서 끊으면
                # 일반 종목 하나만 고정해도 구분선이 통째로 사라진다.
                continue
            if waiting:
                if tier == TIER_WAIT_CLEAN:
                    last_row = row
                    continue
                waiting = False
            if tier == TIER_LIMIT_CLEAN:
                limit_row = row
            elif tier > TIER_LIMIT_CLEAN:
                break  # 상한가 묶음을 지났으므로 더 볼 것이 없다
        return last_row, jumsang, limit_row

    def paintEvent(self, event):
        super().paintEvent(event)
        proxy = self.model()
        painter = QPainter(self.viewport())
        painter.setRenderHint(QPainter.Antialiasing, False)

        # 상한가 정렬 순서는 그대로 두고, 09시 이후에도 예상상한 상태로
        # 아직 첫 거래가 시작되지 않은 종목군의 끝만 가로선으로 구분한다.
        if proxy is not None and proxy.rowCount() > 1:
            last_waiting_row, _, last_limit_row = self.waiting_group()
            painter.setPen(QPen(QColor("#FFD54F"), 2))
            for group_row in (last_waiting_row, last_limit_row):
                if not 0 <= group_row < proxy.rowCount() - 1:
                    continue
                y = (
                    self.rowViewportPosition(group_row)
                    + self.rowHeight(group_row) - 1
                )
                if 0 <= y < self.viewport().height():
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
                    (ORDER_COL, QColor("#FFB300"), 1),
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

        # 점상알림으로 올라온 행: 소리와 같이 테두리를 몇 번 깜박인다.
        source = proxy.sourceModel() if proxy is not None else None
        if (self._blink_codes and self._blink_on
                and source is not None and hasattr(source, "codes")):
            painter.setPen(QPen(QColor("#00E5FF"), 2))
            for row in range(proxy.rowCount()):
                source_index = proxy.mapToSource(proxy.index(row, 0))
                if (not source_index.isValid()
                        or source.codes[source_index.row()] not in self._blink_codes):
                    continue
                y, h = self.rowViewportPosition(row), self.rowHeight(row)
                if y + h >= 0 and y < self.viewport().height():
                    painter.drawRect(0, y, self.viewport().width() - 1, h - 1)
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
        # 뉴스로 붙은 테마와 오늘 재료 기사가 있는 종목. 표식에만 쓴다.
        self.news_themes: dict[str, tuple[str, ...]] = {}
        self.news_codes: set[str] = set()
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
        self._minute_volume_source: dict[str, str] = {}  # 종목별 KRX/NXT/통합 누적값 출처
        self._minute_volume_day: dict[str, str] = {}  # 날짜가 바뀔 때만 누적값 기준 초기화
        self.quotes: dict[str, deque] = {}   # (시각, 1~5호가 (매도/매수 가격·잔량)) 최근 15초
        self.prediction_history: dict[str, deque] = {}  # 최근 5분 1초 체결 요약
        self.program_history: dict[str, deque] = {}  # 최근 5분 0w 매수/매도수량 차분
        self._program_cumulative: dict[str, tuple] = {}  # 마지막 (매수수량누적, 매도수량누적, 출처)
        self._program_since: dict[str, float] = {}  # 현재 출처 누적값을 관찰하기 시작한 시각
        self._prediction_cache: dict[str, tuple] = {}   # 같은 초의 반복 data() 계산 방지
        self.order_target_code = ""       # 주문 컬럼에서 선택한 현재 대상종목
        self.order_status: dict[str, str] = {}
        self.order_cancellable: set[str] = set()
        # 주문허용이 꺼져 있으면 취소 영역을 그리지 않는다. 실제 주문을
        # 보내는 자리라 매수 버튼과 같은 관문을 지나야 한다.
        self.order_enabled = False
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
        self._minute_volume_source.pop(code, None)
        self._minute_volume_day.pop(code, None)
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

    def set_order_enabled(self, on: bool):
        """주문허용 상태를 반영해 취소 영역을 한꺼번에 켜고 끈다."""
        if self.order_enabled == on:
            return
        self.order_enabled = on
        if self.codes:
            self.dataChanged.emit(
                self.index(0, ORDER_COL),
                self.index(len(self.codes) - 1, ORDER_COL))

    def set_cancellable(self, code: str, on: bool):
        """미체결 매수가 있으면 주문 셀에 취소 영역을 그린다.

        앱이 낸 주문 배치와 별개로 계좌 장부만 보고 정한다. 영웅문에서 낸
        주문이나 앱을 다시 띄운 뒤에도 취소가 눌리게 하려면 이 경로가 있어야
        한다. 배치 상태는 앱이 껐다 켜면 사라지지만 미체결은 계좌에 남는다.
        """
        if code not in self.rows or (code in self.order_cancellable) == on:
            return
        if on:
            self.order_cancellable.add(code)
        else:
            self.order_cancellable.discard(code)
        cell = self.index(self.codes.index(code), ORDER_COL)
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
        volume_source = (
            str(fields.get("_real_suffix") or "")
            if "_real_suffix" in fields else None
        )
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
        volume_ready = code in self._minute_volume_ready
        stale_volume = False
        if volume_in_fields:
            fields["vol"] = max(0, int(fields.get("vol") or 0))
            volume_day = time.strftime("%Y%m%d")
            previous_source = self._minute_volume_source.get(code)
            source_changed = (
                volume_source is not None
                and previous_source is not None
                and volume_source != previous_source
            )
            day_changed = (
                code in self._minute_volume_day
                and self._minute_volume_day[code] != volume_day
            )
            if source_changed or day_changed:
                # 사용자가 KRX/NXT/통합 시세를 실제로 바꾸거나 날짜가 바뀐
                # 경우에만 새 누적거래량 기준을 잡는다.
                self.minute_value_ticks.pop(code, None)
                fields["minute_value"] = 0
                volume_ready = False
            elif fields["vol"] < stored["vol"]:
                # REST 백필이나 순서가 뒤바뀐 실시간 패킷은 최신 누적거래량보다
                # 작을 수 있다. 이를 출처 초기화로 오인해 최근 60초 기록을
                # 지우지 말고, 해당 누적값과 체결량만 무시한다.
                log.debug(
                    "stale cumulative volume ignored %s incoming=%d current=%d",
                    code, fields["vol"], stored["vol"])
                fields.pop("vol")
                volume_in_fields = False
                stale_volume = True
            if volume_source is not None:
                self._minute_volume_source[code] = volume_source
            self._minute_volume_day[code] = volume_day
            if volume_in_fields:
                self._minute_volume_ready.add(code)
        dvol = (
            fields["vol"] - stored["vol"]
            if volume_in_fields else 0
        )  # FID 15가 없을 때 체결 틱 폴백
        effective_tick_qty = 0 if stale_volume else tick_qty
        ticked = effective_tick_qty not in (None, 0) or dvol > 0
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
            qty = int(effective_tick_qty or 0)
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

    def set_news_marks(self, news_themes: dict[str, tuple[str, ...]],
                       news_codes: set[str]):
        """뉴스로 붙은 테마와 오늘 재료 기사가 있는 종목을 설정한다."""
        self.news_themes = {
            str(code).removesuffix("_AL"): tuple(names)
            for code, names in news_themes.items() if names
        }
        self.news_codes = {
            str(code).removesuffix("_AL") for code in news_codes}
        if self.codes:
            self.dataChanged.emit(
                self.index(0, THEME_COL),
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
                # 가진 테마를 모두 적는다. 대표만 적으면 오늘 재료가 안 보인다.
                plain = code.removesuffix("_AL")
                if not themes:
                    return "미분류" if plain in self.news_codes else "?미분류"
                return _theme_cell_text(
                    themes, self.news_themes.get(plain),
                    plain in self.news_codes)
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
                if not themes:
                    return "테마 미분류"
                plain = code.removesuffix("_AL")
                lines = ["테마: " + " · ".join(themes)]
                news = self.news_themes.get(plain)
                if news:
                    lines.append(f"{NEWS_THEME_MARK} 최근 뉴스 재료: "
                                 + " · ".join(news))
                if plain not in self.news_codes:
                    lines.append("오늘 재료 기사 없음 (시세속보 제외)")
                return "\n".join(lines)
            return None
        if field == "balance_sell":
            setting = self.balance_sell_settings.get(code)
            stage = self.balance_sell_stage.get(code, 0)
            if role == Qt.DisplayRole:
                if setting:
                    return " / ".join(
                        _shares_in_ten_thousands(setting[key])
                        for key in BALANCE_SELL_STAGE_KEYS
                        if int(setting.get(key, 0)) > 0)
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
                # 켜 둔 단계를 모두 지나면 더 나갈 매도가 없다. 소리는 그
                # 순간에만 들리므로 자리를 비운 뒤에도 알 수 있게 색으로 남긴다.
                # 옅은 회색은 옅은 초록과 명도가 붙어 한눈에 안 갈리므로
                # 어두운 남회색으로 둔다(경보색인 주황·빨강과도 안 겹친다).
                if _balance_stages_done(setting, stage):
                    return QColor("#37474F")
                return QColor("#CDECCF")
            if role == Qt.ForegroundRole and setting:
                alert = self.balance_alert_stage.get(code, 0)
                if alert and self.balance_blink_on:
                    # 점멸 중에는 배경이 경보색이다. 소진 여부와 무관하게
                    # 그 배경에 맞춘다(3차 빨강만 흰 글씨).
                    return WHITE if alert >= 3 else QColor("#111111")
                if _balance_stages_done(setting, stage):
                    return WHITE  # 어두운 소진 배경 위
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
                        f"1번 {setting['first']:,}주 이하"
                        f"{' (꺼짐)' if not setting['first'] else ''}: "
                        f"{int(float(setting.get('first_ratio', 0)) * 100)}% 매도\n"
                        f"2번 {setting['second']:,}주 이하"
                        f"{' (꺼짐)' if not setting['second'] else ''}: "
                        f"{int(float(setting.get('second_ratio', .5)) * 100)}%까지 매도\n"
                        f"3번 {setting['third']:,}주 이하"
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
                    if self.order_enabled and code in self.order_cancellable
                    else "클릭하여 주문 대상종목으로 지정")
            if role == ORDER_CANCEL_ROLE:
                return self.order_enabled and code in self.order_cancellable
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
                display_bucket = _minute_value_display_bucket(value)
                return f"{display_bucket / 10:,.1f}억" if value else ""
            if role == Qt.UserRole:
                return value
            if role == Qt.TextAlignmentRole:
                return Qt.AlignRight | Qt.AlignVCenter
            if role == Qt.ToolTipRole:
                return f"최근 60초 실제 체결대금\n{value:,}원"
            return None
        if field == "acc_value":  # 장 시작부터의 누적거래대금
            value = max(0, int(stored.get(field) or 0))
            if role == Qt.DisplayRole:
                return f"{value / 100_000_000:,.0f}억" if value else ""
            if role == Qt.UserRole:
                return value
            if role == Qt.TextAlignmentRole:
                return Qt.AlignRight | Qt.AlignVCenter
            if role == Qt.ToolTipRole:
                return f"당일 누적거래대금\n{value:,}원"
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
            if field == "rate" and stored["exp_price"] > 0:
                # 동시호가·VI로 예상값이 살아 있으면 곧 체결될 가격이 그것이다.
                # 표시는 실제 등락률 그대로 두고 정렬만 예상등락률로 비교해,
                # +18%인데 예상 +29%인 종목이 +29% 자리에 서게 한다.
                return stored["exp_rate"]
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
        # 500만주 위로는 더 올리지 않는다. 200만/100만/50만이 상한이다.
        (5_000_000, (2_000_000, 1_000_000, 500_000)),
        (2_000_000, (1_000_000, 500_000, 300_000)),
        (1_000_000, (500_000, 300_000, 200_000)),
        (500_000, (300_000, 150_000, 50_000)),
        (200_000, (100_000, 50_000, 20_000)),
    )
    for minimum, values in tiers:
        if current >= minimum:
            return values
    if current <= 0:
        return 0, 0, 0
    first = _clean_balance_value(current * .60)
    second = min(first - 1, _clean_balance_value(current * .40))
    third = min(second - 1, _clean_balance_value(current * .30))
    return max(3, first), max(2, second), max(1, third)


class BalanceStepSpinBox(QSpinBox):
    """잔량 증감 단위를 화살표 1만·휠 10만으로 두고 보조키로 바꾼다."""

    _wheel = False

    def wheelEvent(self, event):
        # 휠은 기본 10만. 포커스·고해상도 휠 처리는 Qt 기본 구현에 맡긴다.
        self._wheel = True
        try:
            super().wheelEvent(event)
        finally:
            self._wheel = False

    def stepBy(self, steps: int):
        modifiers = QApplication.keyboardModifiers()
        base, shifted = (
            (100_000, 10_000) if self._wheel else (10_000, 100_000))
        unit = (
            1_000_000 if modifiers & Qt.ControlModifier
            else shifted if modifiers & Qt.ShiftModifier
            else base
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
                "화살표: 1만(Shift 10만) · 휠: 10만(Shift 1만) · "
                "Ctrl: 100만")
            edit.valueChanged.connect(self._mark_manual)
            edit.lineEdit().returnPressed.connect(self._apply)
        # 단계별 사용 체크. 해제한 단계는 기준 0으로 적용돼 감시·주문에서 빠진다.
        self.first_check = QCheckBox("1")
        self.second_check = QCheckBox("2")
        self.third_check = QCheckBox("3")
        for stage_check in self.stage_checks():
            stage_check.setToolTip(
                "해제하면 이 단계는 경고음도 주문도 실행하지 않습니다."
                " 마지막 체크 상태를 기억합니다.")
            stage_check.toggled.connect(self._on_stage_toggled)
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
        rebase_btn = QPushButton(
            "현재 잔량으로 재설정" if self.config else "현재 잔량으로 다시 계산")
        rebase_btn.setToolTip(
            "현재 매수잔량으로 3단 기준을 다시 잡습니다."
            + (" 이미 설정된 종목은 바로 적용됩니다." if self.config else ""))
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
        grid.addWidget(self.first_check, 1, 0)
        grid.addWidget(self.first_edit, 1, 1)
        grid.addWidget(QLabel("이하 → 경고음 +"), 1, 2)
        grid.addWidget(self.first_sell_combo, 1, 3)
        grid.addWidget(self.second_check, 2, 0)
        grid.addWidget(self.second_edit, 2, 1)
        grid.addWidget(QLabel("이하 → 경고음 +"), 2, 2)
        grid.addWidget(self.second_sell_combo, 2, 3)
        grid.addWidget(self.third_check, 3, 0)
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

        # 적용된 설정이 있으면 그 설정의 기준값(0=해제)이 곧 체크 상태다.
        # 없으면 마지막으로 쓰던 체크 상태를 되살린다(기본 전부 사용).
        for stage_check, key, last_key in zip(
                self.stage_checks(), BALANCE_SELL_STAGE_KEYS,
                BALANCE_SELL_STAGE_LAST_KEYS):
            if self.config:
                enabled = int(self.config.get(key, 0)) > 0
            else:
                saved = self._settings.value(last_key)
                enabled = True if saved is None else _stored_bool(saved)
            stage_check.blockSignals(True)
            stage_check.setChecked(enabled)
            stage_check.blockSignals(False)

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
            self.applied_label.setText(
                ("시장가 · " if self.config.get("market_sell", False)
                 else "지정가 · ")
                + " / ".join(
                    f"{_compact_shares(self.config[key])}↓ {combo.currentText()}"
                    if int(self.config.get(key, 0)) > 0 else f"{label}번 제외"
                    for key, label, combo in zip(
                        BALANCE_SELL_STAGE_KEYS, ("1", "2", "3"),
                        (self.first_sell_combo, self.second_sell_combo,
                         self.third_sell_combo))))
        else:
            self.first_sell_combo.setCurrentIndex(0)
            self.third_sell_combo.setCurrentIndex(2)
            self.second_sell_combo.setCurrentIndex(2)
            self.applied_label.setText("없음 — 실제 주문은 실행되지 않습니다")
            self._refresh_suggestion()
            self._manual_edit = False
        self._sync_stage_enabled()

        self._timer = QTimer(self)
        self._timer.timeout.connect(self._refresh_live)
        self._timer.start(150)
        self._refresh_live()
        self.apply_btn.setFocus()
        # 창이 실제 표시된 뒤에도 Enter 기본 버튼에 포커스를 유지한다.
        QTimer.singleShot(0, self.apply_btn.setFocus)

    def stage_checks(self):
        return (self.first_check, self.second_check, self.third_check)

    def _stage_rows(self):
        return zip(
            self.stage_checks(),
            (self.first_edit, self.second_edit, self.third_edit),
            (self.first_sell_combo, self.second_sell_combo,
             self.third_sell_combo))

    def _sync_stage_enabled(self):
        """해제한 단계의 입력만 잠근다. 번호 간 종속은 없다."""
        for stage_check, edit, combo in self._stage_rows():
            enabled = stage_check.isChecked()
            edit.setEnabled(enabled)
            combo.setEnabled(enabled)

    def _on_stage_toggled(self, checked: bool):
        for stage_check, last_key in zip(
                self.stage_checks(), BALANCE_SELL_STAGE_LAST_KEYS):
            self._settings.setValue(
                last_key, "true" if stage_check.isChecked() else "false")
        self._settings.sync()
        self._sync_stage_enabled()
        self._mark_manual()

    def _mark_manual(self):
        self._manual_edit = True
        self.error_label.clear()

    def _on_market_sell_toggled(self, checked: bool):
        """시장가 체크박스의 마지막 선택 상태를 즉시 기억한다."""
        self._settings.setValue(
            self._market_sell_key, "true" if checked else "false")
        self._settings.sync()
        self._mark_manual()

    def _current_bid(self) -> int:
        return max(
            0, int(self.screen.model.rows.get(self.code, {}).get("bid_qty") or 0))

    def _refresh_suggestion(self):
        current = self._current_bid()
        if current <= 0:
            return
        first, second, third = _balance_sell_suggestion(current)
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
        if self.config:
            # 이미 감시 중인 종목은 되묻지 않고 바로 새 기준으로 갈아 끼운다.
            self._apply()
            return
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
        # 체크 해제한 단계는 기준 0으로 저장한다. 감시·주문 판정이 이미
        # 기준 0을 "그 단계 없음"으로 처리하므로 실행 쪽은 그대로 둔다.
        first = self.first_edit.value() if self.first_check.isChecked() else 0
        second = self.second_edit.value() if self.second_check.isChecked() else 0
        third = self.third_edit.value() if self.third_check.isChecked() else 0
        if not (first or second or third):
            self.error_label.setText(
                "사용할 단계를 하나 이상 체크하세요. 감시를 끄려면 '감시 해제'.")
            return
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


class BidQtyPopup(QWidget):
    """매수잔량 한 종목만 크게 띄우는 타이틀바 없는 항상 위 창.

    끌어서 이동, 우하단으로 크기 조절, 더블클릭으로 닫기. 위치·크기는
    종목코드별로 layout.ini에 남는다. 조건 이탈 종목은 화면이 자동으로 닫는다.
    """

    GRIP = 14
    SPAWN_GAP = 2  # 새 종목 창을 직전 창 옆에 붙일 때 남기는 틈

    def __init__(self, screen: "ConditionScreen", code: str, name: str):
        super().__init__(
            screen,
            Qt.WindowType.Tool | Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.WindowStaysOnTopHint,
        )
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        # 배경을 비워 라벨의 둥근 모서리가 창 모양이 되게 한다.
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setMouseTracking(True)
        self.setMinimumSize(90, 44)
        self.setMaximumSize(1200, 800)
        self._screen = screen
        self.code = code
        self._name = name
        self._key = screen.prefix + "bidqty_popup_" + code
        # 종목 기록이 없을 때 쓰는 기준 크기·자리. 창별로 따로 남긴다.
        self._last_key = screen.prefix + "bidqty_popup_last"
        self._text = ""
        self._drag_offset = None
        self._resize_from = None
        # 레이아웃 없이 라벨을 직접 채운다. 레이아웃을 쓰면 글자가 커질수록
        # 창 최소 크기가 따라 커져 다시 줄일 수 없다.
        self._label = QLabel(self)
        self._label.setAlignment(Qt.AlignCenter)
        self._label.setAttribute(
            Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        try:
            self._alpha = int(screen._settings.value(self._key + "_alpha", 235))
        except (TypeError, ValueError):
            self._alpha = 235
        self._alpha = min(255, max(60, self._alpha))
        if not self._place_beside_last():
            geometry = screen._settings.value(self._key)
            if geometry is None or not self.restoreGeometry(geometry):
                geometry = screen._settings.value(self._last_key)
                if geometry is None or not self.restoreGeometry(geometry):
                    self.resize(240, 96)
        self._refresh()

    def _place_beside_last(self) -> bool:
        """떠 있는 창이 있으면 같은 크기로 그 오른쪽에 붙인다.

        옆에 붙이는 것이 종목별 저장 위치보다 우선이다. 예전에 한 번이라도
        띄운 종목은 자기 기록으로 돌아가 버려, 여러 종목을 늘어놓는 중에
        한 종목만 엉뚱한 자리에 뜬다. 창이 하나도 없을 때만 기록을 쓴다.
        """
        opened = list(self._screen._bid_popups.values())
        last = opened[-1] if opened else None
        if last is None or last is self:
            return False
        self.resize(last.size())
        right = last.x() + last.width() + self.SPAWN_GAP
        area = self.screen().availableGeometry() if self.screen() else None
        if area is not None and right + self.width() > area.right():
            # 오른쪽이 모자라면 첫 창의 왼쪽 변에 맞춰 다음 줄을 시작한다.
            # 마지막 창 아래에 붙이면 줄이 오른쪽으로 계단처럼 밀린다.
            first = opened[0]
            bottom = max(popup.y() + popup.height() for popup in opened)
            self.move(first.x(), bottom + self.SPAWN_GAP)
        else:
            self.move(right, last.y())
        return True

    def set_value(self, qty: int):
        text = f"{qty:,}"
        if text != self._text:
            self._text = text
            self._refresh()

    def _refresh(self):
        big = max(12, int(self.height() * 0.5))
        small = max(8, int(big * 0.34))
        self._label.setStyleSheet(
            f"QLabel {{ background-color: rgba(23, 28, 34, {self._alpha});"
            f" border: 2px solid rgba(224, 93, 93, {self._alpha});"
            " border-radius: 10px; }")
        self._label.setText(
            f"<div style='font-size:{small}px; color:#C9A968'>{self._name}</div>"
            f"<div style='font-size:{big}px; font-weight:900; color:#FFC24D'>"
            f"{self._text}</div>")
        self.setToolTip(
            f"{self._name} 매수잔량\n"
            f"배경 불투명도 {self._alpha * 100 // 255}% (휠로 조절)\n"
            "끌어서 이동 · 우하단 모서리로 크기 조절\n"
            "더블클릭하면 닫습니다.")

    def wheelEvent(self, event):
        """휠로 배경 불투명도를 조절하고 바로 저장한다."""
        step = 15 if event.angleDelta().y() > 0 else -15
        self._alpha = min(255, max(60, self._alpha + step))
        self._screen._settings.setValue(self._key + "_alpha", self._alpha)
        self._screen._settings.sync()
        self._refresh()

    def showEvent(self, event):
        super().showEvent(event)
        self._ensure_on_screen()

    def _ensure_on_screen(self):
        """타이틀바가 없어 화면 밖으로 나가면 잡을 수 없다. 안으로 되돌린다."""
        areas = [screen.availableGeometry()
                 for screen in QApplication.screens()]
        if not areas or any(area.intersects(self.frameGeometry())
                            for area in areas):
            return
        area = areas[0]
        self.move(area.left() + 40, area.top() + 40)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._label.setGeometry(self.rect())
        self._refresh()

    def _in_grip(self, pos) -> bool:
        return (pos.x() >= self.width() - self.GRIP
                and pos.y() >= self.height() - self.GRIP)

    def mousePressEvent(self, event):
        if event.button() != Qt.MouseButton.LeftButton:
            return
        if self._in_grip(event.position().toPoint()):
            self._resize_from = (event.globalPosition().toPoint(), self.size())
        else:
            self._drag_offset = (event.globalPosition().toPoint()
                                 - self.frameGeometry().topLeft())

    def mouseMoveEvent(self, event):
        point = event.position().toPoint()
        self.setCursor(
            Qt.CursorShape.SizeFDiagCursor if self._in_grip(point)
            else Qt.CursorShape.SizeAllCursor)
        if self._resize_from is not None:
            origin, size = self._resize_from
            delta = event.globalPosition().toPoint() - origin
            self.resize(max(90, size.width() + delta.x()),
                        max(44, size.height() + delta.y()))
        elif self._drag_offset is not None:
            self.move(event.globalPosition().toPoint() - self._drag_offset)

    def mouseReleaseEvent(self, _event):
        if self._resize_from is not None or self._drag_offset is not None:
            self._save_geo()
        self._resize_from = None
        self._drag_offset = None

    def mouseDoubleClickEvent(self, _event):
        self.close()  # 타이틀바가 없으니 더블클릭이 닫기다

    def _save_geo(self):
        geometry = self.saveGeometry()
        self._screen._settings.setValue(self._key, geometry)
        # 다음에 처음 띄우는 종목이 기준으로 삼는다.
        self._screen._settings.setValue(self._last_key, geometry)
        self._screen._settings.sync()

    def closeEvent(self, event):
        self._save_geo()
        self._screen._bid_popups.pop(self.code, None)
        super().closeEvent(event)


class ConditionScreen(QWidget):
    """조건검색실시간 화면 하나. 나중에 QMdiArea에 이 위젯을 여러 개 띄우면 다중창."""

    order_target_selected = Signal(str, int)  # 종목코드, 상한가 -> main이 kt00010 조회
    order_target_changed = Signal(str)
    order_requested = Signal(str, str, int, bool, int, int)
    cancel_requested = Signal(str)
    trim_requested = Signal(str)  # 미체결 매수에서 100주만 남기고 부분취소
    emergency_exit_requested = Signal(str, int, bool)
    order_status_acknowledged = Signal(str)
    exit_hotkey_changed = Signal(str, object)
    balance_sell_changed = Signal(str, object)
    account_auto_cancel_changed = Signal(str, bool)
    watch_toggled = Signal(str, bool)
    analysis_stock_requested = Signal(str)
    market_overview_requested = Signal()
    realtime_news_requested = Signal()
    account_summary_requested = Signal()
    jumsang_entered = Signal(str)  # 매도잔량 0으로 상한가정렬 tier 0에 올라온 종목

    def __init__(self, prefix: str = "", parent=None):
        super().__init__(parent)
        self.prefix = prefix  # 다중창: 창별 설정 키 접두사 ("", "w2_", ...)
        self._settings = QSettings("layout.ini", QSettings.IniFormat)
        self.model = StockModel()
        self._bid_popups: dict[str, BidQtyPopup] = {}

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
        self.auto_refresh.setToolTip(
            "동시호가 때 편입/이탈이 실시간으로 안 와서 주기적으로 재조회"
            " · 본창은 오전 09:02:20에 자동 해제")
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
            "그 외는 테마 내 최고 등락률 순 · 테마 안에서는 상한가 진입시각과 등락률 순\n"
            "강도는 시장 전체가 아니라 이 창에 검색된 종목만으로 계산합니다.\n"
            "조건이 좁으면 테마당 종목이 적어 강도 비교가 무뎌집니다.")
        self._jumsang_group: set[str] | None = None
        self.jumsang_check = QCheckBox("점상알림")
        self.jumsang_check.setToolTip(
            "09:01~09:03 상한가정렬 첫 구분선 위 그룹(아직 첫 거래 전)에"
            " 새로 올라온 종목을 소리로 알림 · 상한↑ 켜져 있어야 동작")
        self._checkbox_style = VisibleCheckStyle()
        self._checkbox_style.setParent(self)
        for checkbox in (
                self.auto_refresh, self.auto_remove, self.sound_check,
                self.limit_sort, self.theme_sort, self.jumsang_check):
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
        self.news_btn = QPushButton("뉴스")
        self.news_btn.setToolTip(
            "분석창을 열고 LS증권 실시간 뉴스 탭으로 이동")
        self.news_btn.setFixedWidth(44)
        self.news_btn.clicked.connect(self.realtime_news_requested.emit)
        self.newwin_btn = QPushButton("창+")
        self.newwin_btn.setToolTip("조건검색 창 하나 더 열기 (다른 조건식 동시 감시)")
        self.newwin_btn.setFixedWidth(44)
        self.count_label = QLabel()
        self.count_label.setTextFormat(Qt.TextFormat.RichText)
        self._update_count()
        self.font_size_combo = QComboBox()
        for font_size in (9, 10, 11, 12):
            self.font_size_combo.addItem(str(font_size), font_size)
        self.font_size_combo.setCurrentIndex(
            self.font_size_combo.findData(10))
        self.font_size_combo.setFixedWidth(68)
        self.font_size_combo.setToolTip(
            "앱 전체 기본 글자 크기 — 뉴스 본문 크기는 본문 창의 A−/A+로 별도 조절")
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
        top.addWidget(self.news_btn)
        top.addWidget(self.newwin_btn)
        top.addStretch(1)  # 남는 공간은 오른쪽으로
        top.addWidget(self.count_label)
        top.addWidget(self.font_size_combo)
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
        self._available_base = 0
        self._summary_base = 0
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
        self.estimated_asset_btn = QPushButton("추정자산")
        self.estimated_asset_btn.setFixedHeight(22)
        self.estimated_asset_btn.setToolTip("클릭하면 추정자산·주문가능금액을 다시 조회합니다.")
        self.estimated_asset_btn.clicked.connect(
            self.account_summary_requested.emit)
        account_bar.addWidget(self.estimated_asset_btn)
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
        account_bar.addWidget(self.jumsang_check)

        # 주문 실행줄 — 종목을 고른 뒤 이 줄에서 분할/취소/주문방식을 즉시 결정한다.
        self.order_target_value = QLabel("종목을 선택하세요")
        self.order_target_value.setMinimumWidth(130)
        # 선택 종목의 체결(보유)·미체결. 앱이 웹소켓 주문체결로 유지하는 장부를
        # 그대로 보여 주므로 표시를 위해 계좌를 다시 조회하지 않는다.
        # 주문줄이 이미 꽉 차서 예상주문 줄 오른쪽에 붙인다.
        self.pending_order_value = QLabel("체결·미체결 -")
        self.pending_order_value.setTextFormat(Qt.RichText)
        self.pending_order_value.setMinimumHeight(20)
        self.pending_order_value.setStyleSheet(
            "QLabel{padding:1px 5px;border:1px solid #C8C8C8;"
            "background:#F5F5F5;color:#222}")
        self.pending_order_value.setToolTip(
            "선택 종목의 보유수량과 미체결 매수·매도 (앱·영웅문 주문 모두 포함)\n"
            "묶임 = 미체결 매도로 잡혀 지금 팔 수 없는 수량")

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
        # 실제 주문을 보내는 두 버튼만 테두리 색으로 구분해 한눈에 찾게 한다.
        for button, line, fill, over, dim in (
                (self.fixed_qty_order_btn,
                 "#EF6C00", "#FFF3E0", "#FFE0B2", "#F0C9A5"),
                (self.remaining_order_btn,
                 "#C62828", "#FFEBEE", "#FFCDD2", "#EAB3B3")):
            button.setStyleSheet(
                f"QPushButton{{border:2px solid {line};border-radius:4px;"
                f"background:{fill};color:#212121;font-weight:bold;"
                "padding:0px 8px}"
                f"QPushButton:hover{{background:{over}}}"
                f"QPushButton:disabled{{border-color:{dim};background:#FAFAFA;"
                "color:#9E9E9E}")
        # 미체결 매수 각 건에서 100주만 남기고 나머지를 취소한다. 매수 버튼과
        # 색을 달리해 손이 미끄러져도 매수가 나가지 않게 한다.
        self.trim_order_btn = QPushButton(TRIM_BUTTON_TEXT)
        self.trim_order_btn.setFixedHeight(24)
        # 폭은 Qt가 스타일시트 여백까지 넣어 계산한 값을 그대로 쓴다. 폰트를
        # 직접 재면 앱 전역 글꼴·볼드가 반영되지 않아 글자가 잘린다.
        # Fixed 정책이라 줄이 꽉 차도 이 버튼만은 안 줄어든다.
        self.trim_order_btn.setSizePolicy(
            QSizePolicy.Fixed, QSizePolicy.Fixed)
        self.trim_order_btn.setEnabled(False)
        self.trim_order_btn.setToolTip(
            f"미체결 매수 주문마다 {ORDER_TRIM_KEEP}주만 남기고 나머지를"
            " 부분취소합니다 · 주문허용 체크 후 사용")
        self.trim_order_btn.setStyleSheet(
            "QPushButton{border:2px solid #424242;border-radius:4px;"
            "background:#EEEEEE;color:#212121;font-weight:bold;"
            "padding:0px 11px}"
            "QPushButton:hover{background:#DCDCDC}"
            "QPushButton:disabled{border-color:#BDBDBD;background:#FAFAFA;"
            "color:#9E9E9E}")
        self.trim_order_btn.clicked.connect(self._request_trim)
        # 미체결 100주 초과분 합계. main이 장부에서 계산해 내려준다.
        self._trim_qty = 0
        # 컬럼 배치만 두 벌로 나눈다. 나머지 화면 설정은 두 상태가 함께 쓴다.
        self.expected_layout_check = QCheckBox("예상")
        self.expected_layout_check.setStyle(self._checkbox_style)
        self.expected_layout_check.setToolTip(
            "컬럼 순서·너비·정렬을 이 상태 전용으로 따로 기억합니다"
            " · 재조회·소리·정렬 체크 등 나머지 설정은 공유합니다")
        self.expected_layout_check.setChecked(
            self._settings.value(self.prefix + "expected_layout", "false")
            == "true")
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
        # setChecked를 끝낸 뒤에 잇는다. 먼저 이으면 초기 복원이 저장을 부른다.
        self.expected_layout_check.toggled.connect(self._on_expected_layout)
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
        order_bar.addWidget(self.expected_layout_check)

        order_preview_bar = QHBoxLayout()
        order_preview_bar.setContentsMargins(0, 0, 0, 0)
        # 미체결과 버튼은 내용 크기 그대로 오른쪽 끝에 붙고, 남는 폭은 왼쪽
        # 예상주문이 다 가져간다. 비율로 나누면 내용 길이와 무관하게 잘려서
        # 짧은 미체결 쪽에 빈칸이 남고 긴 예상주문이 먼저 끊긴다.
        # 좁아질 때 줄어드는 순서는 예상주문 -> 미체결 -> 버튼(안 줄어듦)이다.
        # 예상주문이 먼저 제 폭을 다 가져가고(Maximum = 내용폭 이상으로는
        # 안 커짐), 미체결이 남는 폭을 쓴다(Ignored + stretch). 창이 좁아지면
        # 미체결이 먼저 없어지고 그다음 예상주문이 줄어든다.
        # minimumWidth(1)이 필요하다. 0은 '설정 안 함'으로 취급돼 글자 전체
        # 폭인 minimumSizeHint가 그대로 먹히고, 그러면 예상주문이 안 줄어들어
        # 취소 버튼이 화면 밖으로 밀려난다.
        self.order_preview_value.setSizePolicy(
            QSizePolicy.Maximum, QSizePolicy.Preferred)
        self.order_preview_value.setMinimumWidth(1)
        self.pending_order_value.setSizePolicy(
            QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.pending_order_value.setMinimumWidth(0)
        order_preview_bar.addWidget(self.order_preview_value)
        order_preview_bar.addWidget(self.pending_order_value, 1)
        order_preview_bar.addWidget(self.trim_order_btn)

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
        self._resort_timer.timeout.connect(self._resort_proxy)
        self.model.dataChanged.connect(self._on_data_changed)
        self.model.rowsInserted.connect(self._on_data_changed)
        self.model.rowsRemoved.connect(self._on_data_changed)
        self.model.rowsRemoved.connect(self._close_orphan_bid_popups)
        self._balance_blink_timer = QTimer(self)
        self._balance_blink_timer.timeout.connect(
            self._refresh_balance_alert_blink)
        self._balance_blink_timer.start(380)
        self._minute_value_timer = QTimer(self)
        self._minute_value_timer.timeout.connect(
            self.model.refresh_minute_values)
        # ponytail: 매수잔량이 실제로 어떤 간격·크기로 들어오는지 한 번 재려고
        # 붙인 계측이다. 간격을 알아야 속도·가속도 계산 주기를 정할 수 있다.
        # 값을 확인하면 이 타이머 연결과 아래 세 조각을 지운다.
        self._minute_value_timer.timeout.connect(self._refresh_bidqty_probe)
        self._bidqty_probe: list[tuple] = []
        self._bidqty_probe_on = False
        self._minute_value_timer.start(1000)
        self.table.verticalHeader().setVisible(True)  # 순위(정렬 순서대로 1..N 자동)
        self.table.verticalHeader().setDefaultSectionSize(22)
        # 순위 칸은 표시 전용이라 비어 있다. 여기에 고정 토글을 붙이면 이미
        # 클릭이 걸린 종목명·연상·매수잔량 셀과 부딪히지 않는다.
        self.table.verticalHeader().setToolTip(
            "클릭하면 그 종목을 맨 위에 고정합니다. 다시 누르면 해제")
        self.table.verticalHeader().sectionClicked.connect(self._toggle_pin)
        self.proxy.pinned = {
            code for code in str(
                self._settings.value(self.prefix + "pinned", "") or ""
            ).split(",") if code
        }
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
        self.table.setColumnWidth(ACC_VALUE_COL, 72)
        for col, width in RANK_DEFAULT_WIDTHS.items():
            self.table.setColumnWidth(col, width)
        self.table.setItemDelegate(PreserveTextColorDelegate(self.table))
        self.table.setItemDelegateForColumn(BAR_COL, BarDelegate(self.table))
        self.table.setItemDelegateForColumn(NAME_COL, NameDelegate(self.table))
        self.table.setItemDelegateForColumn(THEME_COL, ClipTextDelegate(self.table))
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
        self._restore_layout()
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
        # 뉴스 테마는 장중에 계속 붙으므로 주기적으로 다시 읽는다.
        # DB 조회 한 번이라 부담이 없고, 값이 같으면 화면도 그대로다.
        self._theme_reload_timer = QTimer(self)
        self._theme_reload_timer.timeout.connect(self._load_theme_classification)
        self._theme_reload_timer.start(300000)  # 5분
        # 테마 강도는 초 단위면 충분하다. 정렬 스로틀과 따로 느리게 돈다.
        self._theme_key_timer = QTimer(self)
        self._theme_key_timer.timeout.connect(self.proxy.refresh_theme_keys)
        self._theme_key_timer.start(1000)
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
        # 컬럼 순서는 사용자가 직접 끌어 정한다. FIELDS 배열을 고치면 저장된
        # 폭이 논리 인덱스 기준이라 엉뚱한 컬럼에 붙는다.
        hdr.setSectionsMovable(True)
        # 컬럼 줄에서 찾는 게 자연스럽다. 표 본문 우클릭도 같은 메뉴를 연다.
        hdr.setContextMenuPolicy(Qt.CustomContextMenu)
        hdr.customContextMenuRequested.connect(
            lambda pos: self._show_column_menu(hdr.mapToGlobal(pos)))
        hdr.sectionResized.connect(lambda *a: self._save_timer.start(400))
        hdr.sectionMoved.connect(lambda *a: self._save_timer.start(400))

    @staticmethod
    def _money_text(value: int) -> str:
        return f"{max(0, int(value)):,}원"

    def _effective_reserved(self) -> int:
        """계좌 조회값이 아직 반영하지 못한 앱 주문액만 남긴다.

        키움 주문가능금액은 미체결 접수분 증거금을 이미 뺀 값이라, 응답이
        나간 시점까지의 앱 주문액을 또 빼면 같은 주문을 두 번 차감한다.
        """
        return max(0, self._order_reserved - self._available_base)

    def _refresh_order_funds_display(self):
        usable = self._usable_order_funds()
        effective = self._effective_reserved()
        remaining = max(0, usable - effective)
        self.order_reserved_value.setText(self._money_text(self._order_reserved))
        self.order_reserved_value.setToolTip(
            f"앱 주문 누적 {self._order_reserved:,}원 · "
            f"계좌조회 미반영 {effective:,}원")
        self.order_remaining_value.setText(self._money_text(remaining))
        self._refresh_order_target_display()

    def _usable_order_funds(self) -> int:
        raw = self.order_limit_edit.text().replace(",", "").replace("원", "").strip()
        try:
            manual_limit = max(0, int(raw)) if raw else self._account_available
        except ValueError:
            manual_limit = self._account_available
        return min(self._account_available, manual_limit)

    def set_pending_orders(self, code: str, buy: tuple[int, int],
                           sell: tuple[int, int], position: tuple[int, int],
                           trim: int = 0):
        """선택 종목의 체결(보유)·미체결 요약을 예상주문 줄 오른쪽에 표시한다.

        buy/sell은 (건수, 수량), position은 (보유, 매도가능)이다.
        trim은 미체결 매수에서 100주씩 남기고 취소할 수 있는 수량 합계다."""
        # 취소 영역은 선택 종목이 아니어도 그려야 하므로 먼저 반영한다.
        self.model.set_cancellable(code, buy[0] > 0)
        if not buy[0] and self.model.order_status.get(code) == "취소전송":
            # 계좌 취소는 '취소전송'에서 멈춘다. 거래소 확인은 웹소켓이 장부를
            # 비우는 것으로만 오므로, 미체결이 0이 되는 순간을 완료로 읽는다.
            self.model.set_order_status(code, "취소완료")
        if code != self._order_target_code:
            return
        self._trim_qty = max(0, int(trim))
        self._refresh_order_actions()
        held, sellable = position
        parts = []
        if held:
            locked = max(0, held - sellable)
            parts.append(
                f"체결 <b>{held:,}주</b>"
                + (f" (묶임 {locked:,})" if locked else ""))
        # 색을 빼고 상자의 기본 글자색(#222)으로 통일한다. 주황·파랑은 밝은
        # 상자 위에서 대비가 안 나와 건수·수량이 안 읽혔다.
        if buy[0]:
            parts.append(f"<b>미체결매수 {buy[0]}건 {buy[1]:,}주</b>")
        if sell[0]:
            parts.append(f"<b>미체결매도 {sell[0]}건 {sell[1]:,}주</b>")
        self.pending_order_value.setText(
            "&nbsp;&nbsp;·&nbsp;&nbsp;".join(parts) if parts
            else "체결·미체결 없음")

    def _refresh_order_target_display(self):
        code = self._order_target_code
        if not code or code not in self.model.rows:
            self.order_target_value.setText("종목을 선택하세요")
            self._trim_qty = 0
            self.pending_order_value.setText("체결·미체결 -")
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
            remaining = max(
                0, self._usable_order_funds() - self._effective_reserved())
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
        self._available_base = int(detail.get("reserved_base") or 0)
        self.account_available_value.setText(self._money_text(selected_amount))
        self._refresh_order_funds_display()

    def order_target(self) -> tuple[str, int]:
        """현재 주문 대상종목과 그 상한가. 없으면 빈 값."""
        code = self._order_target_code
        return code, int(self.model.rows.get(code, {}).get("upper") or 0)

    def clear_orderable_quantity(self):
        """재조회가 끝날 때까지 옛 주문가능수량으로 주문하지 못하게 막는다."""
        self._orderable_detail = None
        self._refresh_order_target_display()
        self._refresh_order_actions()

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
        remaining = max(
            0, self._usable_order_funds() - self._effective_reserved())
        return min(api_qty, remaining // upper)

    @staticmethod
    def _fixed_total(available_qty: int, count: int) -> int:
        """100주씩 총수량. 설정 횟수가 남으면 자투리를 마지막 1건으로 붙인다.

        설정 횟수를 다 채웠으면 자투리는 붙이지 않는다. 사용자가 정한
        주문 건수를 넘기지 않는 것이 먼저다.
        """
        full = min(count, available_qty // 100)
        if full < 1:
            return 0
        rest = available_qty - full * 100
        return full * 100 + (rest if full < count else 0)

    def _refresh_order_actions(self, *_):
        # 주문허용 토글이 이 함수에 이미 연결돼 있다. 취소 영역도 같이 따라간다.
        self.model.set_order_enabled(self.order_enable_check.isChecked())
        available_qty = self._current_orderable_qty()
        selected_count = self.split_group.checkedId()
        fixed_total = self._fixed_total(available_qty, selected_count)
        fixed_count = len(fixed_quantities(fixed_total))
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
        # 취소는 주문 진행 상태와 무관하게 낼 수 있어야 한다. 주문허용과
        # 실제로 취소할 수량이 있는지만 본다.
        self.trim_order_btn.setEnabled(
            self.order_enable_check.isChecked() and self._trim_qty > 0)
        # 수량은 바로 왼쪽 미체결 표시에 이미 있다. 버튼에 또 적으면 폭이
        # 흔들리고 좁은 줄을 더 먹는다. 도구설명으로만 알린다.
        self.trim_order_btn.setToolTip(
            f"미체결 매수에서 {ORDER_TRIM_KEEP}주씩 남기고 "
            f"{self._trim_qty:,}주를 취소합니다" if self._trim_qty
            else f"미체결 매수 주문마다 {ORDER_TRIM_KEEP}주만 남기고 나머지를"
                 " 부분취소합니다 · 주문허용 체크 후 사용")
        self._refresh_order_preview(
            available_qty, selected_count, fixed_total, remaining_count)

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
            fixed_total: int, remaining_count: int):
        if not self._order_target_code or not self._orderable_detail:
            self.order_preview_value.setText("예상주문&nbsp;&nbsp;종목을 선택하거나 조회를 기다리세요")
            self.order_preview_value.setToolTip("")
            return

        upper = int(
            self.model.rows.get(
                self._order_target_code, {}).get("upper") or 0)
        fixed_plan = fixed_quantities(fixed_total)
        fixed_count = len(fixed_plan)
        excluded = max(0, available_qty - fixed_total)
        fixed_slots = self._order_slots(fixed_count, selected_count)
        if fixed_count:
            odd_lot = fixed_plan[-1] if fixed_plan[-1] < 100 else 0
            unit_text = (
                f"100주씩+{odd_lot:,}주" if odd_lot else "100주씩")
            fixed_text = (
                f"{fixed_slots}&nbsp;설정 {selected_count}회 → "
                f"<b>실제 {fixed_count}회</b> · {unit_text}"
                f" · 총 {fixed_total:,}주")
            if excluded:
                fixed_text += f" · <b>미주문 {excluded:,}주</b>"
        else:
            fixed_text = (
                f"{self._order_slots(0, selected_count)}&nbsp;"
                "<b>최소 100주 필요</b>")

        if remaining_count:
            base, extra = divmod(available_qty, remaining_count)
            per_order = (
                f"{base + 1:,}/{base:,}주씩" if extra else f"{base:,}주씩")
            split_text = (
                f"{self._order_slots(remaining_count, selected_count)}&nbsp;"
                f"<b>{remaining_count}회</b> · {per_order}")
        else:
            split_text = "주문가능수량 없음"

        # 100주가 안 되면 100주씩은 0회라 금액이 0원으로 나왔다. 그래도
        # 분할매수는 가능수량 전부를 주문하므로 그 금액을 대신 보여 준다.
        amount_qty = fixed_total or available_qty
        self.order_preview_value.setText(
            f"<b>상한가</b>&nbsp;100주씩 {fixed_text}"
            f"&nbsp;│&nbsp;분할매수 {split_text}"
            f"&nbsp;·&nbsp;"
            f"<b>{amount_qty * upper:,}원</b>")
        self.order_preview_value.setToolTip(
            "■ 실제 전송되는 주문 · □ 설정했지만 수량 부족으로 전송되지 않는 주문")

    def _request_order(self, mode: str):
        code = self._order_target_code
        if not self.order_enable_check.isChecked() or not code:
            return
        count = self.split_group.checkedId()
        available_qty = self._current_orderable_qty()
        if mode == "fixed":
            total_qty = self._fixed_total(available_qty, count)
            count = len(fixed_quantities(total_qty))
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

    def _request_trim(self):
        """미체결 매수에서 100주씩만 남기고 나머지 취소를 요청한다."""
        code = self._order_target_code
        if not self.order_enable_check.isChecked() or not code:
            return
        if self._trim_qty <= 0:
            return
        # 취소가 장부에 반영되기 전 연타를 막는다. 새 잔량이 내려오면
        # set_pending_orders가 다시 켠다.
        self._trim_qty = 0
        self._refresh_order_actions()
        self.trim_requested.emit(code)

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
            self._available_base = int(detail.get("reserved_base") or 0)
        else:
            self._account_available = (
                self._misu_orderable if checked else self._cash_orderable)
            self._available_base = self._summary_base
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
        self._summary_base = int(summary.get("reserved_base") or 0)
        self._available_base = self._summary_base
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
        is_dark = self.palette().window().color().lightness() < 128
        number_color = "#72D7FF" if is_dark else "#0057A8"
        self.count_label.setText(
            "종목수: "
            f"<span style='color:{number_color}; font-weight:700;'>"
            f"{self.model.rowCount()}</span>")

    def changeEvent(self, event):
        super().changeEvent(event)
        if hasattr(self, "count_label") and event.type() in (
                QEvent.Type.PaletteChange,
                QEvent.Type.ApplicationPaletteChange):
            self._update_count()

    def _layout_key(self, name: str, expected: bool | None = None) -> str:
        """컬럼 배치 저장 키. '예상' 체크에 따라 두 벌을 따로 남긴다.

        나머지 화면 설정(재조회·소리·정렬 체크 등)은 프로필과 무관하게 하나만
        쓴다. 표 배치만 갈아 끼워야 전환했을 때 놀랄 일이 없다.
        """
        if expected is None:
            expected = self.expected_layout_check.isChecked()
        return self.prefix + ("exp_" if expected else "") + name

    def _restore_layout(self, expected: bool | None = None):
        """저장된 컬럼 순서·너비·정렬을 화면에 되돌린다."""
        header = self.table.horizontalHeader()
        state = self._settings.value(self._layout_key("header", expected))
        # 컬럼 수가 바뀐 옛 저장분은 restoreState가 False -> 기본 레이아웃/정렬 유지
        if state is not None and header.restoreState(state):
            # restoreState가 옛 정렬값(가운데)까지 되살림 -> 왼쪽 재적용
            header.setDefaultAlignment(Qt.AlignLeft | Qt.AlignVCenter)
            sec = header.sortIndicatorSection()
            if sec >= 0:  # 마지막 정렬 컬럼/방향 복원
                self._sort_col = sec
                self._sort_order = header.sortIndicatorOrder()
        # saveState는 컬럼 수가 달라지면 통째로 복원에 실패한다. 이름별 너비를 다시
        # 덮어써 새 컬럼이 추가돼도 기존 컬럼 크기는 그대로 유지한다.
        removed_bad_width = False
        for col, field in enumerate(FIELDS):
            key = self._layout_key("colwidth_" + field, expected)
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

    def _on_expected_layout(self, checked: bool):
        """예상 체크를 바꾸면 지금 배치를 이전 프로필에 남기고 새 배치를 연다."""
        self._save_layout(expected=not checked)
        self._settings.setValue(
            self.prefix + "expected_layout", "true" if checked else "false")
        self._settings.sync()  # _save_layout의 sync보다 뒤라 따로 내려야 한다
        self._restore_layout(checked)
        self._restore_window_width(checked)
        self._apply_sort()

    def _restore_window_width(self, expected: bool | None = None):
        """이 프로필에 저장된 창 가로폭으로 되돌린다. 높이·위치는 그대로 둔다."""
        window = self.window()
        if window is None or window.isMaximized():
            return
        try:
            width = int(self._settings.value(
                self._layout_key("window_width", expected)) or 0)
        except (TypeError, ValueError):
            return
        if width > 0 and width != window.width():
            window.resize(width, window.height())

    def resizeEvent(self, event):
        """창 가로폭도 배치 프로필에 함께 남긴다. 헤더 저장 타이머를 같이 쓴다."""
        super().resizeEvent(event)
        self._save_timer.start(400)

    def _save_layout(self, expected: bool | None = None):
        header = self.table.horizontalHeader()
        self._settings.setValue(
            self._layout_key("header", expected), header.saveState())
        window = self.window()
        # 최대화 중에는 화면 폭이 잡히므로 저장하지 않는다. 복원할 때 그 값으로
        # 창을 줄이면 사용자가 최대화해 둔 상태가 깨진다.
        if window is not None and not window.isMaximized():
            self._settings.setValue(
                self._layout_key("window_width", expected), window.width())
        for col, field in enumerate(FIELDS):
            # 숨김 컬럼은 sectionSize=0이다. 이를 저장하면 순위 화면에서 다시
            # 표시해도 폭 0으로 남으므로 마지막 정상 너비를 보존한다.
            if not header.isSectionHidden(col) and header.sectionSize(col) > 0:
                self._settings.setValue(
                    self._layout_key("colwidth_" + field, expected),
                    header.sectionSize(col))
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

    # 저장된 청산키를 복원할 때 main이 호출한다. 종목이 아직 편입 전이면
    # 셀이 없으므로 조용히 넘어가고, 편입될 때 모델 값으로 그려진다.
    refresh_exit_hotkey_cell = _refresh_exit_hotkey_cell

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
            if cancel_area and index.data(ORDER_CANCEL_ROLE):
                self.model.set_order_status(
                    code, self.model.order_status.get(code, ""), False)
                self.cancel_requested.emit(code)
                return
            # 취소가 끝난 종목도 다시 주문할 수 있어야 한다. '자 취소',
            # '취소전송', '취소없음'이 모두 해당한다. 취소 확인이 아직 안 온
            # 주문(order_cancellable)이 남았으면 그대로 둔다.
            if (order_status in ("장종료", "오류", "수량부족", "분할부족",
                                 "대상없음")
                    or order_status.endswith("완료")
                    or ("취소" in order_status
                        and code not in self.model.order_cancellable)):
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
        elif index.column() == BID_QTY_COL:
            self._open_bid_popup(code)
        elif index.column() == NAME_COL:
            QApplication.clipboard().setText(code)
            QToolTip.showText(QCursor.pos(), f"{code} 복사됨")

    def _open_bid_popup(self, code: str):
        """매수잔량 셀 클릭 -> 종목별 확대 창. 이미 떠 있으면 앞으로 올린다.

        갱신은 타이머가 아니라 0D 호가 푸시(on_tick)가 직접 한다.
        """
        popup = self._bid_popups.get(code)
        if popup is not None:
            popup.raise_()
            return
        stored = self.model.rows.get(code, {})
        popup = BidQtyPopup(
            self, code, str(stored.get("name") or code))
        popup.set_value(int(stored.get("bid_qty") or 0))
        self._bid_popups[code] = popup
        popup.show()

    def _close_orphan_bid_popups(self, *_):
        """조건 이탈로 행이 사라진 종목은 확대 창도 닫는다."""
        for code, popup in list(self._bid_popups.items()):
            if code not in self.model.rows:
                popup.close()

    def _on_context_menu(self, pos):
        """종목명은 네이버 종목토론실, 그 밖의 자리는 컬럼 표시 메뉴."""
        index = self.table.indexAt(pos)
        if index.isValid() and index.column() == NAME_COL:
            code = self.model.codes[self.proxy.mapToSource(index).row()]
            QDesktopServices.openUrl(QUrl(
                f"https://finance.naver.com/item/board.naver?code={code}"))
            return
        self._show_column_menu(self.table.viewport().mapToGlobal(pos))

    def _show_column_menu(self, at):
        """보이는 열을 체크로 고른다. 숨긴 열도 목록에 남아 되살릴 수 있다.

        헤더가 아니라 표에서 여는 이유는 열을 다 숨겨도 메뉴에 닿기 위해서다.
        순위·변동은 화면 모드(일반/순위/보유)가 소유하므로 목록에서 뺀다.
        """
        columns = [col for col in range(len(FIELDS)) if col not in RANK_COLS]
        shown = [col for col in columns if not self.table.isColumnHidden(col)]
        menu = QMenu(self)
        for col in columns:
            action = menu.addAction(COLUMNS[col])
            action.setCheckable(True)
            visible = col in shown
            action.setChecked(visible)
            # 마지막 한 열까지 숨기면 이 메뉴를 띄울 자리가 사라진다.
            action.setEnabled(not visible or len(shown) > 1)
            action.triggered.connect(
                lambda checked, target=col:
                self._set_column_visible(target, checked))
        menu.exec(at)

    def _set_column_visible(self, col: int, visible: bool):
        """열을 감추거나 되살리고 현재 배치 프로필에 남긴다."""
        self.table.setColumnHidden(col, not visible)
        if visible and self.table.columnWidth(col) <= 0:
            # 숨김 상태의 폭은 0이다. 마지막 정상 너비를 되살린다.
            try:
                width = int(self._settings.value(
                    self._layout_key("colwidth_" + FIELDS[col])) or 0)
            except (TypeError, ValueError):
                width = 0
            self.table.setColumnWidth(col, width if width > 0 else 80)
        self._save_layout()

    def _sync_dynamic_sort_mode(self):
        """고빈도·복합 정렬은 타이머가 맡고 일반 컬럼만 Qt 자동정렬을 쓴다."""
        throttled = (
            self._sort_col == MINUTE_VALUE_COL
            or self.limit_sort.isChecked()
            or self.theme_sort.isChecked()
        )
        self.proxy.setDynamicSortFilter(not throttled)

    def _toggle_pin(self, row: int):
        """순위 칸을 누르면 그 종목을 맨 위에 고정하거나 해제한다."""
        code = self.proxy.row_code(row)
        if not code:
            return
        if code in self.proxy.pinned:
            self.proxy.pinned.discard(code)
        else:
            self.proxy.pinned.add(code)
        self._settings.setValue(
            self.prefix + "pinned", ",".join(sorted(self.proxy.pinned)))
        self._settings.sync()
        self._resort_proxy()
        if self.proxy.rowCount():  # 번호 <-> 표식 교체를 즉시 반영
            self.proxy.headerDataChanged.emit(
                Qt.Vertical, 0, self.proxy.rowCount() - 1)

    def _resort_proxy(self):
        """스로틀 시간이 끝나면 현재 열과 방향으로 정렬을 확실히 다시 적용한다."""
        self.proxy.invalidate()
        self._apply_sort()
        # 점상알림은 여기서 판정한다. paintEvent에 두면 창이 가려지거나
        # 최소화됐을 때 리페인트가 안 와서 정작 필요한 순간에 안 울린다.
        self._jumsang_alert(
            self.table.waiting_group()[1] if self.limit_sort.isChecked() else None)

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
                codes_with_individual_news, dart_relation_evidence_labels,
                news_theme_labels,
            )
            labels = active_theme_labels()
            relation_groups = active_relation_groups()
            relation_evidence = dart_relation_evidence_labels()
            news_themes = news_theme_labels()
            news_codes = codes_with_individual_news()
        except Exception as error:  # noqa: BLE001
            log.warning("theme labels unavailable: %s", error)
            labels = {}
            relation_groups = {}
            relation_evidence = {}
            news_themes = {}
            news_codes = set()
        self.model.set_news_marks(news_themes, news_codes)
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

    def _refresh_bidqty_probe(self):
        """ponytail: 계측 구간(09:00~09:03)을 켜고 끄고, 끝나면 파일로 내린다."""
        on = (self.limit_sort.isChecked()
              and "0900" <= time.strftime("%H%M") < "0903")
        if on == self._bidqty_probe_on:
            return
        self._bidqty_probe_on = on
        if not on and self._bidqty_probe:
            rows, self._bidqty_probe = self._bidqty_probe, []
            try:
                with open("bidqty_probe.csv", "a", encoding="utf-8") as file:
                    file.write("epoch,code,bid_qty,ask_qty\n")
                    for stamp, stock, bid, ask in rows:
                        file.write(f"{stamp:.3f},{stock},{bid},{ask}\n")
            except OSError as error:
                log.warning("bidqty probe write failed: %s", error)
                return
            log.warning("bidqty probe rows=%s -> bidqty_probe.csv", len(rows))

    def on_tick(self, code: str, fields: dict):
        """실시간 시세 (0B 체결 / 0D 호가)"""
        previous_upper = (
            int(self.model.rows.get(code, {}).get("upper") or 0)
            if code == self._order_target_code else 0)
        self.model.update_stock(code, fields)
        # ponytail: 계측. 켜져 있는 3분 동안 점상 대기 종목만 메모리에 모은다.
        if self._bidqty_probe_on and "bid_qty" in fields:
            stored = self.model.rows.get(code)
            if stored is not None and _limit_tier(stored) == TIER_WAIT_CLEAN:
                self._bidqty_probe.append(
                    (time.time(), code, stored["bid_qty"], stored["ask_qty"]))
        # 0D는 매도쪽만 바뀌어도 오므로 매수잔량이 실린 틱만 확대 창에 넘긴다.
        if self._bid_popups and "bid_qty" in fields:
            popup = self._bid_popups.get(code)
            if popup is not None:
                popup.set_value(int(fields.get("bid_qty") or 0))
        if code == self._order_target_code:
            self._refresh_order_target_display()
            upper = int(self.model.rows.get(code, {}).get("upper") or 0)
            if upper and upper != previous_upper:
                # 상한가가 바뀌면 그 값으로 받아 둔 주문가능수량은 무효다.
                # 상장 당일 종목은 시초가가 정해질 때 상한가가 바뀌므로,
                # 다시 조회하지 않으면 주문 버튼이 잠긴 채로 남는다.
                self._orderable_detail = None
                self.order_target_selected.emit(code, upper)

    def _jumsang_alert(self, waiting: set[str] | None):
        """구분선 위 그룹에 새로 들어온 종목만 알린다.
        직전 집합을 모르는 첫 호출은 기준선이라 소리 없음.
        None(상한↑ 꺼짐)은 기준선을 지워 다시 켤 때 도배되지 않게 한다."""
        if waiting is None:
            self._jumsang_group = None
            return
        before, self._jumsang_group = self._jumsang_group, waiting
        fresh = waiting - before if before is not None else set()
        if (fresh and self.jumsang_check.isChecked()
                and "0901" <= time.strftime("%H%M") < "0903"):
            self.table.blink_rows(fresh)
            self.jumsang_entered.emit(", ".join(sorted(fresh)))


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
