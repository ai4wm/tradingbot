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
    QAbstractTableModel, QModelIndex, QPoint, QRect, QSettings, QSortFilterProxyModel, Qt, QTimer, QUrl,
)
from PySide6.QtGui import QColor, QCursor, QDesktopServices, QFont, QIcon, QPainter, QPen, QPixmap, QPolygon
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QHBoxLayout, QHeaderView, QLabel,
    QMainWindow, QProxyStyle, QPushButton, QSpinBox, QStyle, QStyledItemDelegate, QStyleOptionViewItem, QTableView, QToolTip,
    QVBoxLayout, QWidget,
)

log = logging.getLogger("gui")

# 순위/변동: ★조회순위(ka00198) 모드 전용 -> 다른 화면에선 숨김 (set_view_mode)
COLUMNS = ["순위",  "변동",      "등락률", "연상", "종목명", "현재가", "예상체결가", "L일봉H", "예상등락률", "전일거래량", "거래량", "매도잔량", "매수잔량", "예상체결량", "체결/분", "매수%",   "프로그램", "예측", "시총(억)", "상한가진입시간"]
FIELDS  = ["qrank", "qrank_chg", "rate",   "streak", "name",  "price", "exp_price", "bar",    "exp_rate",   "prev_vol", "vol",   "ask_qty",  "bid_qty",  "exp_qty",  "tpm",    "buy_pct", "program", "predict", "mcap",   "time"]
# 컬럼은 아니지만 L일봉H 그리기에 필요한 저장 필드 (시/저/고/전일종가/상한/하한)
# streak(연상)/mcap(시가총액)/tpm(체결/분)은 저장 안 함: 매번 계산
BOOK_FIELDS = {f"{side}_{kind}{level if level > 1 else ''}"
               for side in ("ask", "bid") for kind in ("price", "qty")
               for level in range(1, 6)}
STORED = (set(FIELDS) - {"streak", "mcap", "tpm", "buy_pct", "program", "predict"}) | {
    "open", "low", "high", "base", "upper", "lower"} | BOOK_FIELDS
BAR_COL = FIELDS.index("bar")
NAME_COL = FIELDS.index("name")
TIME_COL = FIELDS.index("time")
BID_QTY_COL = FIELDS.index("bid_qty")
NON_LIMIT_IGNORED_SORT_COLS = {TIME_COL, BID_QTY_COL}
STREAK_COL = FIELDS.index("streak")
MCAP_COL = FIELDS.index("mcap")
TPM_COL = FIELDS.index("tpm")
BUY_PCT_COL = FIELDS.index("buy_pct")
PROGRAM_COL = FIELDS.index("program")
PREDICT_COL = FIELDS.index("predict")
RANK_COLS = (FIELDS.index("qrank"), FIELDS.index("qrank_chg"))
RANK_DEFAULT_WIDTHS = {RANK_COLS[0]: 42, RANK_COLS[1]: 48}
RANK_PERIODS = {  # 순위 계열 기준시간 콤보: (표시, data). 모드 따라 교체
    "rank":   [("30초", "5"), ("1분", "1"), ("10분", "2"), ("1시간", "3"), ("당일", "4")],  # ka00198 qry_tp
    "vsurge": [("1분", "1"), ("3분", "3"), ("5분", "5"), ("10분", "10"), ("30분", "30"), ("60분", "60")],  # ka10023 집계분(tm)
}
BAR_ROLE = Qt.UserRole + 1  # 델리게이트에 (open, high, low, close, base, upper, lower) 전달
NXT_ROLE = Qt.UserRole + 2  # NameDelegate에 NXT 종목 여부 전달
MISU_ROLE = Qt.UserRole + 3  # NameDelegate에 미수가능 여부 전달
NEW_ROLE = Qt.UserRole + 4  # NameDelegate에 신규상장 단계 전달 (3=당일 2=15일이내 1=30일이내 0=아님)
TPM_TREND_ROLE = Qt.UserRole + 5  # 체결/분 추세: 최근 10초 속도 vs 이전 50초 (-1/0/+1)
BUY_TREND_ROLE = Qt.UserRole + 6  # 매수% 추세: 최근 20초 비중 vs 이전 40초 (-1/0/+1)
TPM_PERSIST_ROLE = Qt.UserRole + 7  # 3개 1분 구간 체결 지속력: 둔화(-1)/판단중(0)/유지(+1)
PROGRAM_DIRECTION_ROLE = Qt.UserRole + 8  # 프로그램 수량 방향: 매도(-1)/중립(0)/매수(+1)
PROGRAM_PERSIST_ROLE = Qt.UserRole + 9  # 프로그램 3분 방향 지속: 둔화(-1)/판단중(0)/유지(+1)

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
DESC_FIRST = {"bid_qty", "rate", "price", "exp_price", "exp_rate", "streak", "tpm", "buy_pct", "program", "predict", "qrank_chg"}  # 첫 클릭 내림차순 컬럼
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
    """종목명 셀: 기본 렌더(글자색=코스닥 보라/관리 주황) 후 모서리 삼각형.
    좌상단 노랑=NXT, 우상단 녹색=미수가능(증거금100%는 무표시),
    좌하단=신규상장(마젠타=당일, 하늘=15일이내, 청회=30일이내)."""

    def paint(self, painter, option, index):
        # 일부 Windows 스타일은 textElideMode=ElideNone도 무시한다. 배경/선택은
        # 스타일에 맡기고 글자는 직접 그려 `…` 변환 경로 자체를 타지 않게 한다.
        selected = _is_current_row(option, index)
        opt = QStyleOptionViewItem(option)
        opt.state &= ~(QStyle.State_Selected | QStyle.State_HasFocus)
        self.initStyleOption(opt, index)
        text = opt.text
        opt.text = ""
        style = opt.widget.style() if opt.widget else QApplication.style()
        style.drawControl(QStyle.CE_ItemViewItem, opt, painter, opt.widget)
        text_rect = style.subElementRect(QStyle.SE_ItemViewItemText, opt, opt.widget).adjusted(3, 0, -2, 0)
        painter.save()
        painter.setClipRect(option.rect)
        painter.setFont(opt.font)
        painter.setPen(opt.palette.text().color())
        painter.drawText(text_rect, Qt.AlignLeft | Qt.AlignVCenter | Qt.TextSingleLine, text)
        painter.restore()
        nxt, misu = index.data(NXT_ROLE), index.data(MISU_ROLE)
        new = index.data(NEW_ROLE)
        if not (nxt or misu or new):
            if selected:
                _draw_selection_lines(painter, option.rect, option.palette)
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
        if selected:
            _draw_selection_lines(painter, option.rect, option.palette)


class TpmDelegate(QStyledItemDelegate):
    """체결/분 숫자 오른쪽에 단기 방향과 장기 지속력 기호를 별도 색으로 표시."""

    def __init__(self, trend_role=TPM_TREND_ROLE, parent=None, persistence_role=None):
        super().__init__(parent)
        self.trend_role = trend_role
        self.persistence_role = persistence_role

    def paint(self, painter, option, index):
        trend = index.data(self.trend_role) or 0
        persistence = ((index.data(self.persistence_role) or 0)
                       if self.persistence_role else 0)
        selected = _is_current_row(option, index)
        if not index.data(Qt.DisplayRole):
            opt = QStyleOptionViewItem(option)
            opt.state &= ~(QStyle.State_Selected | QStyle.State_HasFocus)
            super().paint(painter, opt, index)
            if selected:
                _draw_selection_lines(painter, option.rect, option.palette)
            return

        opt = QStyleOptionViewItem(option)
        opt.state &= ~(QStyle.State_Selected | QStyle.State_HasFocus)
        self.initStyleOption(opt, index)
        text = opt.text

        painter.save()
        if opt.backgroundBrush.style() != Qt.NoBrush:
            painter.fillRect(option.rect, opt.backgroundBrush)
        else:
            painter.fillRect(option.rect, option.palette.base())
        painter.setFont(opt.font)
        r = option.rect.adjusted(3, 0, -3, 0)
        arrow = "▲" if trend > 0 else "▼"
        # 추세가 없어도 삼각형 한 칸을 항상 확보해 숫자가 좌우로 움직이지 않게 한다.
        arrow_w = max(painter.fontMetrics().horizontalAdvance("▲"),
                      painter.fontMetrics().horizontalAdvance("▼")) + 3
        # 지속력 표식은 왼쪽의 작은 도형으로 그려 기존 55px 안팎의 저장된 열 폭에서도
        # 숫자와 추세 화살표가 잘리거나 좌우로 흔들리지 않게 한다.
        persistence_w = 8 if self.persistence_role else 0
        number_rect = QRect(
            r.left() + persistence_w, r.top(),
            max(0, r.width() - arrow_w - persistence_w), r.height())
        arrow_rect = QRect(number_rect.right() + 1, r.top(), arrow_w, r.height())
        persistence_rect = QRect(r.left(), r.top(), persistence_w, r.height())
        painter.setPen(opt.palette.text().color())
        painter.drawText(number_rect, Qt.AlignRight | Qt.AlignVCenter, text)
        if trend:
            # 체결/분 1500 초과는 셀 배경도 빨강이라 빨강 ▲가 묻힌다.
            # 이 구간의 증가 ▲만 흰색으로 바꿔 방향과 가독성을 함께 유지한다.
            hot_tpm = self.trend_role == TPM_TREND_ROLE and (index.data(Qt.UserRole) or 0) > 1500
            painter.setPen(WHITE if trend > 0 and hot_tpm else RED if trend > 0 else BLUE)
            painter.drawText(arrow_rect, Qt.AlignRight | Qt.AlignVCenter, arrow)
        if persistence:
            hot_tpm = (index.data(Qt.UserRole) or 0) > 1500
            dark = option.palette.base().color().lightness() < 128
            color = (QColor("#087F23") if persistence > 0
                     else QColor("#A0A7B4") if dark else QColor("#5E6470"))
            painter.setPen(WHITE if hot_tpm else color)
            center = persistence_rect.center()
            if persistence > 0:
                painter.setBrush(WHITE if hot_tpm else color)
                painter.drawEllipse(QRect(center.x() - 2, center.y() - 2, 5, 5))
            else:
                painter.setBrush(Qt.NoBrush)
                painter.drawPolygon(QPolygon([
                    QPoint(center.x() - 3, center.y() - 2),
                    QPoint(center.x() + 3, center.y() - 2),
                    QPoint(center.x(), center.y() + 3),
                ]))
        if selected:
            _draw_selection_lines(painter, option.rect, option.palette)
        painter.restore()


class ProgramDelegate(QStyledItemDelegate):
    """프로그램 우세방향·강도와 3분 지속 표식을 고정된 위치에 그린다."""

    def paint(self, painter, option, index):
        direction = index.data(PROGRAM_DIRECTION_ROLE) or 0
        persistence = index.data(PROGRAM_PERSIST_ROLE) or 0
        selected = _is_current_row(option, index)
        text = index.data(Qt.DisplayRole)
        if not text:
            opt = QStyleOptionViewItem(option)
            opt.state &= ~(QStyle.State_Selected | QStyle.State_HasFocus)
            super().paint(painter, opt, index)
            if selected:
                _draw_selection_lines(painter, option.rect, option.palette)
            return

        opt = QStyleOptionViewItem(option)
        opt.state &= ~(QStyle.State_Selected | QStyle.State_HasFocus)
        self.initStyleOption(opt, index)
        painter.save()
        if opt.backgroundBrush.style() != Qt.NoBrush:
            painter.fillRect(option.rect, opt.backgroundBrush)
        else:
            painter.fillRect(option.rect, option.palette.base())
        painter.setFont(opt.font)
        r = option.rect.adjusted(3, 0, -3, 0)
        mark_w = 8
        arrow_w = max(painter.fontMetrics().horizontalAdvance(c)
                      for c in ("▲", "▼", "－")) + 2
        mark_rect = QRect(r.left(), r.top(), mark_w, r.height())
        arrow_rect = QRect(mark_rect.right() + 1, r.top(), arrow_w, r.height())
        value_rect = QRect(arrow_rect.right() + 1, r.top(),
                           max(0, r.right() - arrow_rect.right()), r.height())
        arrow = "▲" if direction > 0 else "▼" if direction < 0 else "－"
        color = RED if direction > 0 else BLUE if direction < 0 else opt.palette.text().color()
        painter.setPen(color)
        painter.drawText(arrow_rect, Qt.AlignCenter, arrow)
        painter.drawText(value_rect, Qt.AlignRight | Qt.AlignVCenter, str(text))
        if persistence:
            dark = option.palette.base().color().lightness() < 128
            mark_color = (QColor("#087F23") if persistence > 0
                          else QColor("#A0A7B4") if dark else QColor("#5E6470"))
            painter.setPen(mark_color)
            center = mark_rect.center()
            if persistence > 0:
                painter.setBrush(mark_color)
                painter.drawEllipse(QRect(center.x() - 2, center.y() - 2, 5, 5))
            else:
                painter.setBrush(Qt.NoBrush)
                painter.drawPolygon(QPolygon([
                    QPoint(center.x() - 3, center.y() - 2),
                    QPoint(center.x() + 3, center.y() - 2),
                    QPoint(center.x(), center.y() + 3),
                ]))
        if selected:
            _draw_selection_lines(painter, option.rect, option.palette)
        painter.restore()


def _limit_tier(d: dict) -> int:
    """상한가정렬 우선순위. 당일 거래량 0은 아직 첫 체결 전인 종목이다."""
    actual_limit = d["upper"] > 0 and d["price"] == d["upper"]
    expected_limit = d["exp_price"] > 0 and d["exp_rate"] >= LIMIT
    if d["vol"] == 0 and expected_limit:
        return 0 if d["ask_qty"] == 0 else 1
    if d["vol"] > 0 and d["ask_qty"] == 0:
        if actual_limit:
            return 2
        if expected_limit:
            return 3
    return 4


class TieredProxy(QSortFilterProxyModel):
    """상한가정렬 모드(limit_mode):
    첫 체결 전 예상상한(매도0 -> 매도있음), 실제 상한, 장중 예상상한 순으로
    각 그룹을 분리해 위에 고정하고 그룹 안은 현재 정렬컬럼과 정렬방향을 따른다.
    모드 off면 전 컬럼 일반 정렬."""

    def __init__(self):
        super().__init__()
        self.limit_mode = False
        # 상한가진입시간 정렬 중 비상한 그룹이 유지할 마지막 일반 정렬 기준.
        self._non_limit_sort_col = FIELDS.index("rate")
        self._non_limit_sort_order = Qt.DescendingOrder

    def sort(self, column, order=Qt.AscendingOrder):
        if column not in NON_LIMIT_IGNORED_SORT_COLS:
            self._non_limit_sort_col = column
            self._non_limit_sort_order = order
        super().sort(column, order)

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
            ta, tb = _limit_tier(a), _limit_tier(b)
            desc = self.sortOrder() == Qt.DescendingOrder
            if ta != tb:  # 우선순위 그룹 순서는 현재 정렬방향과 무관하게 고정
                return ta > tb if desc else ta < tb
            if ta == 2 and left.column() == TIME_COL:
                # 실제 상한가 그룹에서는 진입시간 미수신 종목을 항상 뒤로 보낸다.
                a_has_time, b_has_time = bool(a["time"]), bool(b["time"])
                if a_has_time != b_has_time:
                    return not a_has_time if desc else a_has_time
            if ta == 4 and left.column() in NON_LIMIT_IGNORED_SORT_COLS:
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
        self.limit_cnt: dict[str, tuple[int, int]] = {}  # (어제까지 연속상한 일수, 어제 종가) (main 주입, 연상 컬럼)
        self.new_today: set[str] = set()     # 상장 당일 (main 주입, 좌하단 마젠타)
        self.new15: set[str] = set()         # 상장 15일 이내 (좌하단 하늘)
        self.new30: set[str] = set()         # 상장 16~30일 (좌하단 청회)
        self.shares: dict[str, int] = {}     # 상장주식수 ka10099 (main 주입, 시가총액 컬럼)
        self.ticks: dict[str, deque] = {}    # (체결시각, 부호있는 개별체결량, 체결가) 최근 60초
        self.quotes: dict[str, deque] = {}   # (시각, 1~5호가 (매도/매수 가격·잔량)) 최근 15초
        self.prediction_history: dict[str, deque] = {}  # 최근 5분 1초 체결 요약
        self.program_history: dict[str, deque] = {}  # 최근 5분 0w 매수/매도수량 차분
        self._program_cumulative: dict[str, tuple] = {}  # 마지막 (매수수량누적, 매도수량누적, 출처)
        self._program_since: dict[str, float] = {}  # 현재 출처 누적값을 관찰하기 시작한 시각
        self._activity_since: dict[str, float] = {}     # 3분 지속력의 완전한 관찰 여부
        self._prediction_cache: dict[str, tuple] = {}   # 같은 초의 반복 data() 계산 방지

    # --- 웹소켓/전략 계층이 부르는 API ---------------------------------
    def add_stock(self, code: str, data: dict):
        if code in self.rows:
            self.update_stock(code, data)
            return
        row = len(self.codes)
        self.beginInsertRows(QModelIndex(), row, row)
        self.codes.append(code)
        self.rows[code] = {f: "" if f in ("name", "time") else 0 for f in STORED}
        self._activity_since[code] = time.monotonic()
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
        self.quotes.pop(code, None)
        self.prediction_history.pop(code, None)
        self.program_history.pop(code, None)
        self._program_cumulative.pop(code, None)
        self._program_since.pop(code, None)
        self._activity_since.pop(code, None)
        self._prediction_cache.pop(code, None)
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
        tick_qty = fields.get("tick_qty")  # STORED 필터 전에 보존: +매수체결 / -매도체결
        program_changed = False
        if ("program_buy_qty" in fields and "program_sell_qty" in fields):
            program_changed = self._ingest_program(
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
            if qty and price:
                bucket.traded_value += abs(qty) * price
                bucket.traded_qty += abs(qty)
            while history and history[0].sec <= sec - 300:
                history.popleft()
        cols = {TPM_COL, BUY_PCT_COL, PREDICT_COL} if ticked else set()
        if program_changed:
            cols.update((PROGRAM_COL, PREDICT_COL))
        for f, v in fields.items():
            if stored.get(f) == v:
                continue
            if f in FIELDS:
                cols.add(FIELDS.index(f))
            if f == "vol":  # 체결/분 의존
                cols.update((TPM_COL, BUY_PCT_COL, PREDICT_COL))
            if f in BOOK_FIELDS:
                cols.add(PREDICT_COL)
            if f in ("price", "open", "low", "high", "base", "upper", "lower"):  # L일봉H 의존
                cols.update((BAR_COL, PREDICT_COL))
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

    def refresh_tpm(self):
        """최근 체결 지표 감쇠 갱신: 틱이 끊겨도 1초마다 재계산."""
        if self.codes:
            self.dataChanged.emit(self.index(0, TPM_COL), self.index(len(self.codes) - 1, PREDICT_COL))

    @staticmethod
    def _tpm_persistence(history, now, observed_since):
        """최근/이전/2분 전 체결 건수로 3분 활동 유지 또는 둔화를 판정한다."""
        sec = int(now)
        counts = tuple(
            sum(b.tick_count for b in history
                if sec - 60 * (offset + 1) < b.sec <= sec - 60 * offset)
            for offset in range(3)
        )
        if now - observed_since < 180:
            return 0, counts, None

        current, previous, older = counts
        prior_peak = max(previous, older)
        retention = current / prior_peak * 100 if prior_peak else None
        peak = max(counts)
        continuity = min(counts) / peak if peak else 0
        if min(counts) >= 100 and continuity >= 0.60:
            return 1, counts, retention
        if prior_peak >= 300 and current < previous and current < prior_peak * 0.60:
            return -1, counts, retention
        return 0, counts, retention

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

    @classmethod
    def _program_persistence(cls, program_history, market_history, now, observed_since):
        """최근 3개 1분 구간의 프로그램 방향 유지 또는 활동 둔화를 판정한다."""
        stats = tuple(
            cls._program_interval(
                program_history, market_history,
                now - 60 * (offset + 1), now - 60 * offset)
            for offset in range(3)
        )
        if now - observed_since < 180:
            return 0, stats, None

        current, previous, older = stats
        directions = tuple(s[3] / s[2] if s[2] else 0 for s in stats)
        prior_peak = max(previous[2], older[2])
        retention = current[2] / prior_peak * 100 if prior_peak else None
        relevant = all(s[5] is not None and s[5] >= 3 for s in stats)
        same_direction = (all(d >= 0.10 for d in directions)
                          or all(d <= -0.10 for d in directions))
        if relevant and same_direction and retention is not None and retention >= 60:
            return 1, stats, retention
        prior_participation = max(previous[5] or 0, older[5] or 0)
        if (prior_peak and prior_participation >= 5 and current[2] < prior_peak * 0.60):
            return -1, stats, retention
        return 0, stats, retention

    @staticmethod
    def _format_program_qty(value):
        sign = "+" if value > 0 else "-" if value < 0 else ""
        return f"{sign}{abs(value):,}주"

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

    def headerData(self, section, orientation, role=Qt.DisplayRole):
        if orientation == Qt.Horizontal and role == Qt.ToolTipRole:
            if FIELDS[section] == "tpm":
                return "최근 60초 체결 건수 (▲/▼ 단기 속도, ● 3분 유지, ▽ 고활성 둔화)"
            if FIELDS[section] == "program":
                return "최근 1분 프로그램매매 수량 우세 (▲ 매수/▼ 매도, ● 3분 유지, ▽ 둔화)"
            if FIELDS[section] == "predict":
                return "3·5·10분 단타 상승압력 종합점수 (실제 확률 아님)"
            return COLUMNS[section]  # 칸 좁혀 헤더 글자 잘려도 오버로 확인
        if orientation == Qt.Horizontal and role == Qt.DisplayRole:
            return COLUMNS[section]
        return None

    def data(self, index, role=Qt.DisplayRole):
        field = FIELDS[index.column()]
        stored = self.rows[self.codes[index.row()]]
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
            if role == Qt.ForegroundRole and n:
                return RED
            return None
        if field == "tpm":  # 체결/분 = 최근 60초 체결 틱수, 매번 계산 (저장 안 함)
            now = time.monotonic()
            code = self.codes[index.row()]
            dq = self.ticks.get(code, ())
            n = sum(1 for t, _, _ in dq if t >= now - 60)
            recent = sum(1 for t, _, _ in dq if t >= now - 10)
            previous = n - recent
            # 최근 10초와 이전 50초를 각각 분당 속도로 환산. 20% 완충 + 최소 표본으로 깜빡임 억제.
            recent_rate, previous_rate = recent * 6, previous * 1.2
            trend = (1 if recent >= 3 and recent_rate > previous_rate * 1.2
                     else -1 if previous >= 5 and recent_rate < previous_rate * 0.8
                     else 0)
            if role == Qt.DisplayRole:
                return str(n) if n else ""
            if role == Qt.UserRole:
                return n
            if role == TPM_TREND_ROLE:
                return trend
            if role in (TPM_PERSIST_ROLE, Qt.ToolTipRole):
                persistence, counts, retention = self._tpm_persistence(
                    self.prediction_history.get(code, ()), now,
                    self._activity_since.get(code, now))
                if role == TPM_PERSIST_ROLE:
                    return persistence
                current, previous, older = counts
                if retention is None:
                    status = "지속력 준비중 (편입 후 3분 필요)"
                else:
                    label = "유지 ●" if persistence > 0 else "둔화 ▽" if persistence < 0 else "보통"
                    status = f"유지율 {retention:.0f}% · {label}"
                return (f"최근 1분 {current}건 | 이전 1분 {previous}건 | "
                        f"2분 전 {older}건\n{status}")
            if role == Qt.TextAlignmentRole:
                return Qt.AlignRight | Qt.AlignVCenter
            if n >= 100:  # 활동 단계: 보통 회색 / 활발 초록 / 이후 노랑·주황·빨강
                if role == Qt.BackgroundRole:
                    if n < 300:
                        return QColor("#E8ECEF")
                    if n <= 500:
                        return QColor("#CDECCF")
                    return QColor("#FFE000") if n <= 1000 else QColor("#FF8C00") if n <= 1500 else RED
                if role == Qt.ForegroundRole:
                    return WHITE if n > 1500 else QColor("black")
                if role == Qt.FontRole and n > 500:
                    f = QFont(); f.setBold(True); return f
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
        if field == "program":  # 0w 누적 프로그램매매 수량의 최근 1분 차분
            now = time.monotonic()
            code = self.codes[index.row()]
            program_history = self.program_history.get(code, ())
            market_history = self.prediction_history.get(code, ())
            metrics = self._program_metrics(
                program_history, market_history, now, 60)
            pct = metrics[4]
            direction = (1 if pct is not None and pct >= 55
                         else -1 if pct is not None and pct <= 45 else 0)
            persistence, _, retention = self._program_persistence(
                program_history, market_history, now,
                self._program_since.get(code, now))
            if role == Qt.DisplayRole:
                return f"{max(pct, 100 - pct):.0f}" if pct is not None else ""
            if role == Qt.UserRole:
                return pct if pct is not None else -1
            if role == PROGRAM_DIRECTION_ROLE:
                return direction
            if role == PROGRAM_PERSIST_ROLE:
                return persistence
            if role == Qt.ToolTipRole:
                lines = ["프로그램매매 주식 수 기준 (0w 누적값 차분)"]
                for label, lookback in (("1분", 60), ("3분", 180), ("5분", 300)):
                    stat = self._program_metrics(
                        program_history, market_history, now, lookback)
                    if stat[2]:
                        participation = (f"참여 {stat[5]:.1f}%"
                                         if stat[5] is not None else "참여율 준비중")
                        lines.append(
                            f"{label} 순매수 {self._format_program_qty(stat[3])} | "
                            f"매수 {stat[4]:.0f}% | {participation}")
                    else:
                        lines.append(f"{label} 준비중")
                since = self._program_since.get(code)
                if since is None:
                    status = "지속력 데이터 대기중"
                elif now - since < 180:
                    status = f"지속력 준비중 ({max(0, 180 - int(now - since))}초 남음)"
                else:
                    label = "방향 유지 ●" if persistence > 0 else "활동 둔화 ▽" if persistence < 0 else "보통"
                    status = (f"3분 유지율 {retention:.0f}% · {label}"
                              if retention is not None else label)
                lines.append(status)
                return "\n".join(lines)
            if role == Qt.TextAlignmentRole:
                return Qt.AlignRight | Qt.AlignVCenter
            if role == Qt.ForegroundRole:
                return RED if direction > 0 else BLUE if direction < 0 else None
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
        self.auto_remove.setToolTip("이탈한 종목을 그리드에서 자동 제거")
        self.sound_check = QCheckBox("소리")
        self.sound_check.setToolTip("새 종목이 편입되면 소리 알림 (실시간/재조회 모두)")
        self.limit_sort = QCheckBox("상한↑")
        self.limit_sort.setToolTip(
            "상한가 우선순위를 위에 고정하고 각 그룹은 선택한 컬럼으로 정렬"
            " (진입시간·매수잔량은 비상한 종목 제외)")
        self._checkbox_style = VisibleCheckStyle()
        self._checkbox_style.setParent(self)
        for checkbox in (self.auto_refresh, self.auto_remove, self.sound_check, self.limit_sort):
            checkbox.setStyle(self._checkbox_style)
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
        top.addWidget(self.unified_check)
        top.addWidget(self.rank_btn)
        top.addWidget(self.newwin_btn)
        top.addStretch(1)  # 남는 공간은 오른쪽으로
        top.addWidget(self.ip_label)
        top.addWidget(self.count_label)
        top.addWidget(self.theme_btn)
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
        self._sort_col, self._sort_order = FIELDS.index("rate"), Qt.DescendingOrder  # 기본 등락률 내림차순
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
        # StretchLastSection 금지: 마지막 컬럼 경계선이 사라지고 폭 조절이 잠김
        # 헤더 글자 왼쪽 정렬: 가운데면 칸 좁힐 때 앞자리부터 잘림 (시가총액->총액)
        self.table.horizontalHeader().setDefaultAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        self.table.setColumnWidth(NAME_COL, 110)
        self.table.setColumnWidth(STREAK_COL, 34)
        self.table.setColumnWidth(BAR_COL, 70)
        self.table.setColumnWidth(BUY_PCT_COL, 58)
        self.table.setColumnWidth(PROGRAM_COL, 62)
        self.table.setColumnWidth(PREDICT_COL, 58)
        for col, width in RANK_DEFAULT_WIDTHS.items():
            self.table.setColumnWidth(col, width)
        self.table.setItemDelegate(PreserveTextColorDelegate(self.table))
        self.table.setItemDelegateForColumn(BAR_COL, BarDelegate(self.table))
        self.table.setItemDelegateForColumn(NAME_COL, NameDelegate(self.table))
        self.table.setItemDelegateForColumn(
            TPM_COL, TpmDelegate(
                TPM_TREND_ROLE, self.table, persistence_role=TPM_PERSIST_ROLE))
        self.table.setItemDelegateForColumn(BUY_PCT_COL, TpmDelegate(BUY_TREND_ROLE, self.table))
        self.table.setItemDelegateForColumn(PROGRAM_COL, ProgramDelegate(self.table))
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
        layout.addWidget(self.table)

        self.model.rowsInserted.connect(self._update_count)
        self.model.rowsRemoved.connect(self._update_count)

        # 컬럼 너비/순서 기억: 저장된 상태 복원 후, 변경 시 debounce 저장
        self._settings = QSettings("layout.ini", QSettings.IniFormat)
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
        self._apply_sort()
        self._view_mode = None  # normal / rank / holdings (None=초기)
        self.set_view_mode("normal")  # 순위/변동 기본 숨김
        self.rank_period.activated.connect(self._save_rank_period)
        self.set_rank_period("rank")  # 기본: 조회순위 기준시간 (급증 선택 시 main이 교체)
        if self._settings.value(self.prefix + "limit_sort", "false") == "true":  # 상한가정렬 복원
            self.limit_sort.setChecked(True)
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
        header = self.table.horizontalHeader()
        self._settings.setValue(self.prefix + "header", header.saveState())
        for col, field in enumerate(FIELDS):
            # 숨김 컬럼은 sectionSize=0이다. 이를 저장하면 순위 화면에서 다시
            # 표시해도 폭 0으로 남으므로 마지막 정상 너비를 보존한다.
            if not header.isSectionHidden(col) and header.sectionSize(col) > 0:
                self._settings.setValue(
                    self.prefix + "colwidth_" + field, header.sectionSize(col))
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
            self.table.setColumnHidden(c, mode != "rank")
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
        self.proxy.limit_mode = on
        self.proxy.invalidate()  # 모드 전환 즉시 재정렬 (정렬컬럼/방향은 그대로)
        self._settings.setValue(self._mkey("limit_sort"), "true" if on else "false")
        self._settings.sync()

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
