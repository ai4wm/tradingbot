# -*- coding: utf-8 -*-
"""진입점: qasync로 Qt 이벤트 루프 안에서 asyncio 실행 (단일 스레드).

구조: App(공유: 웹소켓/REST/등록큐/순위창) + View(조건검색 창 하나 = 화면+조건seq).
'창+' 버튼으로 독립 조건검색 창 추가(조건별 동시 감시, 시세 REG는 참조수 공유)."""
import asyncio
import ctypes
import logging
import math
import os
import sys
import time
from collections import Counter, deque
from ctypes import wintypes
from datetime import date, datetime, timedelta
from logging.handlers import RotatingFileHandler

import holidays
import qasync
import config
from PySide6.QtCore import (
    QAbstractNativeEventFilter, QDate, QPoint, QRect, QSettings, QSize, Qt,
    QTimer, QUrl, Signal,
)
from PySide6.QtGui import (
    QColor, QDesktopServices, QFont, QPainter, QPalette, QPen,
)
from PySide6.QtWidgets import (
    QApplication, QCheckBox, QComboBox, QDialog, QHBoxLayout, QLabel,
    QMainWindow, QLineEdit, QMessageBox, QProgressBar, QHeaderView,
    QPushButton, QSplitter, QTableWidget, QTableWidgetItem, QTabWidget,
    QVBoxLayout, QWidget,
)

from analysis_db import (
    DB_PATH, database_stats, save_stock_history, start_collection,
    update_collection, save_dart_corp_codes, save_dart_parent_relations,
    sync_stock_catalog, save_krx_market_day, krx_collected_dates,
    stock_history_dates, pending_intraday_events, save_last_entry_time,
    save_disclosures, disclosure_rows, pending_disclosure_stocks,
    mark_disclosure_range_collected, save_theme_snapshot,
    theme_source_codes, theme_source_member_counts,
    save_source_classifications, limit_up_codes_without_sources,
    dart_inferred_classifications, save_investor_flows,
    pending_condition_investor_flow_stocks, market_dashboard,
    save_market_index_prices, save_market_investor_flows,
    save_external_market_quotes, resolve_analysis_stock,
    realtime_watch_codes, set_realtime_watch, realtime_watch_rows,
    save_news_items, reconcile_news_search_results, news_rows,
    log_content_request, news_request_count_today, save_condition_snapshot,
    save_condition_snapshot_quotes, save_condition_theme_stats,
    active_theme_labels, recent_condition_snapshots, condition_theme_stats,
    save_condition_theme_members, save_next_day_candidates,
)
from api import (
    KST, RestClient,
    active_kiwoom_maintenance, format_kiwoom_maintenance,
)
from classification_api import ClassificationClient
from dart_api import DartClient
from krx_api import KrxClient
from naver_news_api import NaverNewsClient
from global_market_api import GlobalMarketClient
from gui import (
    BALANCE_SELL_COL, ConditionScreen, NumericTableWidgetItem, PURPLE)
from ls_news_server_sync import LSNewsServerSync
from ui.limit_up_tab import LimitUpTabMixin
from ui.realtime_news_tab import (
    LatestLSNewsLabel, LSNewsDetailDialog, RealtimeNewsTabMixin)
from ui.telegram_news_tab import TelegramNewsTabMixin
from ui.theme_tab import ThemeTabMixin
from order import OrderEngine, split_quantity
from rank import RankScreen, _beep
from ws import WSClient


class _ProductionFileLogFilter(logging.Filter):
    """운영 경고와 검증 중인 거래 기능의 감사 로그만 파일에 남긴다."""

    def filter(self, record: logging.LogRecord) -> bool:
        return (
            record.levelno >= logging.WARNING
            or record.name == "trade.audit"
        )


file_log = RotatingFileHandler(
    "bot.log",
    maxBytes=20 * 1024 * 1024,
    backupCount=5,
    encoding="utf-8",
    delay=True,
)
file_log.setLevel(logging.INFO)
file_log.addFilter(_ProductionFileLogFilter())
console_log = logging.StreamHandler()
console_log.setLevel(logging.ERROR)
logging.basicConfig(
    level=logging.WARNING,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
    handlers=[file_log, console_log],
)
log = logging.getLogger("main")
audit_log = logging.getLogger("trade.audit")
audit_log.setLevel(logging.INFO)

def _is_process_admin() -> bool:
    if sys.platform != "win32":
        return False
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:  # noqa: BLE001
        return False


class WindowsGlobalHotkeys(QAbstractNativeEventFilter):
    """RegisterHotKey 기반 전역키. 키보드 후킹 없이 WM_HOTKEY만 처리한다."""

    WM_HOTKEY = 0x0312
    MOD_ALT, MOD_CONTROL, MOD_SHIFT, MOD_WIN = 0x1, 0x2, 0x4, 0x8
    MOD_NOREPEAT = 0x4000

    def __init__(self, callback):
        super().__init__()
        self._callback = callback
        self._next_id = 0xB000
        self._ids: dict[tuple[int, str], int] = {}
        self._payloads: dict[int, tuple] = {}
        QApplication.instance().installNativeEventFilter(self)

    @staticmethod
    def _vk(spec: dict) -> tuple[int, int]:
        key = int(spec["key"])
        modifiers = int(spec.get("modifiers", 0))
        qt = Qt.KeyboardModifier
        win_mod = WindowsGlobalHotkeys.MOD_NOREPEAT
        if modifiers & int(qt.AltModifier.value):
            win_mod |= WindowsGlobalHotkeys.MOD_ALT
        if modifiers & int(qt.ControlModifier.value):
            win_mod |= WindowsGlobalHotkeys.MOD_CONTROL
        if modifiers & int(qt.ShiftModifier.value):
            win_mod |= WindowsGlobalHotkeys.MOD_SHIFT
        if modifiers & int(qt.MetaModifier.value):
            win_mod |= WindowsGlobalHotkeys.MOD_WIN
        keypad = bool(modifiers & int(qt.KeypadModifier.value))
        if int(Qt.Key.Key_0) <= key <= int(Qt.Key.Key_9):
            return ((0x60 + key - int(Qt.Key.Key_0)) if keypad else key), win_mod
        if int(Qt.Key.Key_A) <= key <= int(Qt.Key.Key_Z):
            return key, win_mod
        if int(Qt.Key.Key_F1) <= key <= int(Qt.Key.Key_F24):
            return 0x70 + key - int(Qt.Key.Key_F1), win_mod
        special = {
            int(Qt.Key.Key_Backspace): 0x08, int(Qt.Key.Key_Tab): 0x09,
            int(Qt.Key.Key_Return): 0x0D, int(Qt.Key.Key_Enter): 0x0D,
            int(Qt.Key.Key_Pause): 0x13, int(Qt.Key.Key_CapsLock): 0x14,
            int(Qt.Key.Key_Escape): 0x1B, int(Qt.Key.Key_Space): 0x20,
            int(Qt.Key.Key_PageUp): 0x21, int(Qt.Key.Key_PageDown): 0x22,
            int(Qt.Key.Key_End): 0x23, int(Qt.Key.Key_Home): 0x24,
            int(Qt.Key.Key_Left): 0x25, int(Qt.Key.Key_Up): 0x26,
            int(Qt.Key.Key_Right): 0x27, int(Qt.Key.Key_Down): 0x28,
            int(Qt.Key.Key_Print): 0x2C, int(Qt.Key.Key_Insert): 0x2D,
            int(Qt.Key.Key_Delete): 0x2E, int(Qt.Key.Key_NumLock): 0x90,
            int(Qt.Key.Key_ScrollLock): 0x91,
        }
        if key in special:
            return special[key], win_mod
        text = str(spec.get("text") or "")
        if text and sys.platform == "win32":
            value = int(ctypes.windll.user32.VkKeyScanW(ord(text[0])))
            if value != -1:
                return value & 0xFF, win_mod
        return 0, win_mod

    def register(self, token: tuple[int, str], spec: dict, payload: tuple) -> bool:
        self.unregister(token)
        if sys.platform != "win32":
            return False
        vk, modifiers = self._vk(spec)
        if not vk:
            return False
        hotkey_id = self._next_id
        self._next_id += 1
        if not ctypes.windll.user32.RegisterHotKey(
                None, hotkey_id, modifiers, vk):
            return False
        self._ids[token] = hotkey_id
        self._payloads[hotkey_id] = payload
        return True

    def unregister(self, token: tuple[int, str]):
        hotkey_id = self._ids.pop(token, None)
        if hotkey_id is None:
            return
        self._payloads.pop(hotkey_id, None)
        if sys.platform == "win32":
            ctypes.windll.user32.UnregisterHotKey(None, hotkey_id)

    def nativeEventFilter(self, event_type, message):
        if sys.platform == "win32":
            msg = wintypes.MSG.from_address(int(message))
            if msg.message == self.WM_HOTKEY:
                payload = self._payloads.get(int(msg.wParam))
                if payload:
                    self._callback(payload)
                    return True, 0
        return False, 0

MAX_WINDOWS = 3  # 실시간 등록 ~100종목 한도 내 (조건당 20~30종목 기준)
RANK_SEQ = "RANK"      # [순위]조회순위 (ka00198 폴 -> on_snapshot)
HOLDINGS_SEQ = "HOLDINGS"  # [계좌]보유종목 (kt00018)
NXT_RATE_SEQ = "NXT_RATE"  # [NXT]등락률순위 (ka10027, NXT 전용)
VSURGE_SEQ = "VSURGE"  # [급증]거래량급증 (ka10023)
TVAL_SEQ = "TVAL"      # [대금]거래대금상위 (ka10032)
# 순위 계열: 서버 조건검색 대신 REST 폴, 순위 그리드 공유. seq -> 기준시간 콤보 서브모드
RANK_SUBMODE = {RANK_SEQ: "rank", NXT_RATE_SEQ: "nxt_rate",
                VSURGE_SEQ: "vsurge", TVAL_SEQ: "tval"}
RANK_SEQS = set(RANK_SUBMODE)
RANK_TOP = 20          # 순위 모드 실시간 슬롯 캡 (95한도 공유)
ORDERABLE_PREFETCH_TOP = 20  # 화면에 정렬된 상위 선조회 수
ROTATION_CONDITION_SEQ = "1"
ROTATION_CONDITION_NAME = "=====8% 이상종목 15%예상상한가==========="
ROTATION_BATCH_NAME = f"시장테마 브리핑 · {ROTATION_CONDITION_NAME}"
THEME_MODES = ("system", "dark", "light")
THEME_UI = {
    "system": ("🖥", "테마: 시스템 — Windows 설정을 따름"),
    "dark": ("🌙", "테마: 다크 — 클릭하면 라이트"),
    "light": ("☀", "테마: 라이트 — 클릭하면 시스템"),
}
APP_FONT_FAMILY = "굴림체"
APP_FONT_SIZES = (9, 10, 11, 12)
DEFAULT_APP_FONT_SIZE = 10
APP_FONT_SIZE_KEY = "ui_font_size"
NEWS_WEB_AUTO_RELOAD_PATHS = {
    "/item/board.naver",  # 종목토론 목록
    "/item/news.naver",   # 종목뉴스 목록
    "/item/dart.naver",   # 종목공시 목록
}


def _is_news_web_auto_reload_url(url: QUrl) -> bool:
    """자동 새로고침이 필요한 네이버 종목 목록 화면인지 판정한다."""
    return (
        url.host().lower() == "finance.naver.com"
        and url.path() in NEWS_WEB_AUTO_RELOAD_PATHS
    )


def _market_context(data: dict) -> dict:
    """저장된 국내 지수·시장수급·등락 종목수로 설명 가능한 국면을 판정한다."""
    indices = {row["market"]: row for row in data.get("indices", [])}
    flows = {row["market"]: row for row in data.get("market_flows", [])}
    index_dates = {
        str(row.get("trade_date") or "") for row in data.get("indices", [])
        if row.get("trade_date")
    }
    use_breadth = bool(data.get("live_market")) or (
        len(index_dates) == 1
        and str(data.get("trade_date") or "") in index_dates
    )
    breadth = (
        {row["market"]: row for row in data.get("markets", [])}
        if use_breadth else {}
    )
    score = 0
    rates = {}
    for market in ("KOSPI", "KOSDAQ"):
        rate = indices.get(market, {}).get("change_rate")
        rates[market] = float(rate) if rate is not None else None
        if rate is not None:
            score += 1 if rate >= 0.5 else -1 if rate <= -0.5 else 0
    foreign_total = sum(
        int(row.get("foreign_net") or 0) for row in flows.values())
    if flows:
        score += 1 if foreign_total > 0 else -1 if foreign_total < 0 else 0
    ratios = []
    for row in breadth.values():
        total = int(row.get("stock_count") or 0)
        if total:
            ratios.append(int(row.get("rising") or 0) / total)
    if ratios:
        average_ratio = sum(ratios) / len(ratios)
        score += (
            1 if average_ratio >= 0.55
            else -1 if average_ratio <= 0.45 else 0
        )
    regime = "위험선호" if score >= 2 else "위험회피" if score <= -2 else "중립"
    kospi_rate = rates["KOSPI"]
    kosdaq_rate = rates["KOSDAQ"]
    limit_count = sum(
        int(row.get("limit_up_count") or 0)
        for row in data.get("markets", []))
    if kospi_rate is not None and kosdaq_rate is not None:
        if kosdaq_rate - kospi_rate >= 0.6:
            leadership = "코스닥 장세"
        elif kospi_rate - kosdaq_rate >= 0.6:
            leadership = "대형주 장세"
        elif limit_count >= 5 and max(kospi_rate, kosdaq_rate) < 0:
            leadership = "개별 테마주 장세"
        else:
            leadership = "혼합장"
    else:
        leadership = "판정 대기"
    flow_state = (
        "외국인 매수 우위" if foreign_total > 0
        else "외국인 매도 우위" if foreign_total < 0
        else "외국인 중립"
    )
    return {
        "regime": regime,
        "leadership": leadership,
        "flow_state": flow_state,
        "score": score,
        "kospi_rate": kospi_rate,
        "kosdaq_rate": kosdaq_rate,
        "average_rise_ratio": (
            sum(ratios) / len(ratios) if ratios else None),
    }


def _format_flow_million(value) -> str:
    """시장수급 DB의 백만원 값을 억원/조원 단위로 표시한다."""
    amount = int(value or 0)
    won = amount * 1_000_000
    if abs(won) >= 1_000_000_000_000:
        return f"{won / 1_000_000_000_000:+.2f}조"
    return f"{won / 100_000_000:+,.0f}억"


_KR_HOLIDAY_CACHE = {}


def _krx_holiday_reason(day: date) -> str:
    """KRX 휴장 규칙에 해당하면 사유를, 거래 예정일이면 빈 문자열을 반환한다."""
    if day.weekday() >= 5:
        return "주말"
    calendar = _KR_HOLIDAY_CACHE.setdefault(
        day.year, holidays.KR(years=[day.year], language="ko"))
    if day in calendar:
        return str(calendar.get(day) or "공휴일")
    if day.month == 5 and day.day == 1:
        return "근로자의 날"
    year_end = date(day.year, 12, 31)
    while (
        year_end.weekday() >= 5
        or year_end in calendar
    ):
        year_end -= timedelta(days=1)
    if day == year_end:
        return "연말 휴장"
    return ""


def _market_session_states(now: datetime) -> tuple[str, str, str]:
    """현재 KRX·NXT 세션 표시와 휴장 사유를 반환한다."""
    reason = _krx_holiday_reason(now.date())
    if reason:
        return "휴장", "휴장", reason
    seconds = now.hour * 3600 + now.minute * 60 + now.second

    def at(hour, minute=0, second=0):
        return hour * 3600 + minute * 60 + second

    if seconds < at(8, 30):
        krx = "개장 전"
    elif seconds < at(9):
        krx = "시가 동시호가"
    elif seconds < at(15, 20):
        krx = "정규장"
    elif seconds < at(15, 30):
        krx = "종가 동시호가"
    elif seconds < at(15, 40):
        krx = "정규장 종료"
    elif seconds < at(18):
        krx = "시간외"
    else:
        krx = "종료"

    if seconds < at(8):
        nxt = "개장 전"
    elif seconds < at(8, 50):
        nxt = "프리마켓"
    elif seconds < at(9, 0, 30):
        nxt = "일시휴장"
    elif seconds < at(15, 20):
        nxt = "메인마켓"
    elif seconds < at(15, 30):
        nxt = "일시휴장"
    elif seconds < at(20):
        nxt = "애프터마켓"
    else:
        nxt = "종료"
    return krx, nxt, ""


def _is_krx_market_open(now: datetime | None = None) -> bool:
    """KRX 정규장 시간(09:00~15:30)인지 반환한다."""
    now = now or datetime.now()
    if _krx_holiday_reason(now.date()):
        return False
    seconds = now.hour * 3600 + now.minute * 60 + now.second
    return 9 * 3600 <= seconds < 15 * 3600 + 30 * 60


def _previous_krx_quote_price(price: int) -> int:
    """일반 주권의 바로 아래 유효 호가가격을 반환한다."""
    price = max(1, int(price))
    if price <= 2_000:
        tick = 1
    elif price <= 5_000:
        tick = 5
    elif price <= 20_000:
        tick = 10
    elif price <= 50_000:
        tick = 50
    elif price <= 200_000:
        tick = 100
    elif price <= 500_000:
        tick = 500
    else:
        tick = 1_000
    return max(1, price - tick)


def _balance_stage3_limit_price(row: dict) -> int:
    """매수 4호가가 비어도 최우선 매수가 기준 3틱 아래 가격을 만든다."""
    quoted = int(row.get("bid_price4") or 0)
    if quoted > 0:
        return quoted
    price = int(row.get("bid_price") or row.get("upper") or 0)
    if price <= 0:
        return 0
    for _ in range(3):
        price = _previous_krx_quote_price(price)
    lower = int(row.get("lower") or 0)
    return max(lower, price) if lower > 0 else price


def _largest_shareholder_evidence(
    shareholders: list[dict], business_year: str,
) -> dict[str, str]:
    """DART 최대주주 현황에서 관계 저장·표시에 쓸 원문 근거를 추린다."""
    row = next((
        item for item in shareholders
        if str(item.get("relate") or "").strip() == "최대주주"
        and str(item.get("nm") or "").strip()
    ), None)
    if row is None:
        return {}
    return {
        "name": str(row.get("nm") or "").strip(),
        "share_ratio": str(
            row.get("trmend_posesn_stock_qota_rt")
            or row.get("bsis_posesn_stock_qota_rt") or "").strip(),
        "share_count": str(
            row.get("trmend_posesn_stock_co")
            or row.get("bsis_posesn_stock_co") or "").strip(),
        "business_year": str(business_year),
        "receipt_no": str(row.get("rcept_no") or "").strip(),
    }


def _apply_theme(app: QApplication, mode: str):
    scheme = {"dark": Qt.ColorScheme.Dark, "light": Qt.ColorScheme.Light}.get(
        mode, Qt.ColorScheme.Unknown)
    app.styleHints().setColorScheme(scheme)


def _normalize_app_font_size(value) -> int:
    try:
        size = int(float(value))
    except (TypeError, ValueError):
        return DEFAULT_APP_FONT_SIZE
    return size if size in APP_FONT_SIZES else DEFAULT_APP_FONT_SIZE


def _apply_app_font(app: QApplication, size: int):
    font = QFont(APP_FONT_FAMILY, _normalize_app_font_size(size))
    font.setStyleStrategy(QFont.PreferAntialias)
    app.setFont(font)


def _start_title_clock(win: QMainWindow, title: str, suffix: str = ""):
    """영웅문에서 동기화한 PC 시각을 창 제목에 1초 단위로 표시."""
    def update():
        win.setWindowTitle(
            f"{win._title_clock_base} | {time.strftime('%H:%M:%S')}{win._title_clock_suffix}")

    win._title_clock_base = title
    win._title_clock_suffix = suffix
    win._update_title_clock = update
    timer = QTimer(win)
    timer.timeout.connect(update)
    timer.start(1000)
    win._title_clock = timer  # 부모가 보관하지만 명시적으로 수명 유지
    update()


def _set_title_clock_base(win: QMainWindow, title: str):
    """IP 상태처럼 변하는 제목을 시계 갱신에 보존."""
    win._title_clock_base = title
    win._update_title_clock()
_SHUTDOWN = [False]  # 메인 창 닫는 중: 추가 창 동반 종료를 '사용자 닫기'로 오인 방지


class View:
    """조건검색 창 하나: 화면 + 조건 seq + 재조회/자동재조회 + 진입시각 채우기."""

    def __init__(self, app: "App", screen: ConditionScreen):
        self.app = app
        self.screen = screen
        self.prefix = screen.prefix
        self.seq = None
        self._refresh_task = None
        self._entry_cache: dict[str, str] = {}
        self._entry_pending: set[str] = set()
        self._settings = QSettings("layout.ini", QSettings.IniFormat)
        self._auto_timer = QTimer(screen)
        self._auto_timer.timeout.connect(self.on_refresh)
        self._auto_refresh_cutoff_timer = None

        screen.condition_combo.activated.connect(self._on_condition_selected)
        screen.rank_period.activated.connect(self.on_refresh)  # 기준시간 변경 -> 즉시 재폴
        screen.refresh_btn.clicked.connect(self.on_refresh)
        screen.refresh_interval.setValue(int(self._settings.value(self.prefix + "refresh_interval", 3)))
        screen.auto_refresh.setChecked(self._settings.value(self.prefix + "auto_refresh", "false") == "true")
        if screen.auto_refresh.isChecked():
            self._auto_timer.start(screen.refresh_interval.value() * 1000)
        screen.auto_refresh.toggled.connect(self._on_auto_refresh)
        screen.refresh_interval.valueChanged.connect(self._on_interval_changed)
        if not self.prefix:
            self._auto_refresh_cutoff_timer = QTimer(screen)
            self._auto_refresh_cutoff_timer.setSingleShot(True)
            self._auto_refresh_cutoff_timer.timeout.connect(
                self._on_main_auto_refresh_cutoff)
            self._schedule_main_auto_refresh_cutoff(clear_stale=True)
        self._beep_t = 0.0  # 편입소리 스로틀 (개장 이벤트 폭주 때 소리 도배 방지)
        screen.sound_check.setChecked(self._settings.value(self.prefix + "sound", "false") == "true")
        screen.sound_check.toggled.connect(self._on_sound)

    # --- 조건 목록/선택 ---------------------------------------------------
    def on_condition_list(self, items):
        combo = self.screen.condition_combo
        selected_seq = self.seq
        combo.clear()
        combo.addItem("[순위]조회순위", RANK_SEQ)   # 맨 위 고정: REST 순위 계열
        combo.addItem("[계좌]보유종목", HOLDINGS_SEQ)
        combo.addItem("[NXT]등락률순위", NXT_RATE_SEQ)
        combo.addItem("[급증]거래량급증", VSURGE_SEQ)
        combo.addItem("[대금]거래대금상위", TVAL_SEQ)
        f = QFont(combo.font())
        f.setBold(True)
        for i, color in ((0, "#FFDD00"), (1, "#D6A5FF"), (2, "#33C24D"),
                         (3, "#FF8C00"), (4, "#38B8FF")):  # 볼드+색으로 조건식과 구분
            combo.setItemData(i, f, Qt.FontRole)
            combo.setItemData(i, QColor(color), Qt.ForegroundRole)
        combo.insertSeparator(5)  # 진짜 조건식과 구분선
        for seq, name in items:
            combo.addItem(name, seq)
        if self.seq is None:
            last = self._settings.value(self.prefix + "last_condition")
            idx = combo.findData(last) if last is not None else -1
            if idx < 0:  # 저장 없음: 첫 진짜 조건식 (0~4=내장메뉴,5=구분선)
                idx = 6 if combo.count() > 6 else 0
            combo.setCurrentIndex(idx)  # setCurrentIndex는 activated 안 터짐 -> 수동 등록
            asyncio.ensure_future(self._switch_condition(combo.itemData(idx)))
        else:  # 재조회/재접속: 현재 조건 선택 복원
            idx = combo.findData(selected_seq)
            if idx >= 0:
                combo.setCurrentIndex(idx)
            else:
                # 영웅문에서 현재 조건식을 삭제한 뒤 목록을 재조회한 경우,
                # 콤보는 자동으로 0번을 표시하지만 실제 구독은 예전 조건에 남는 문제가 있다.
                idx = 6 if combo.count() > 6 else 0
                combo.setCurrentIndex(idx)
                asyncio.ensure_future(self._switch_condition(combo.itemData(idx)))

    def _on_condition_selected(self, index: int):
        seq = self.screen.condition_combo.itemData(index)
        if seq is not None:
            self._settings.setValue(self.prefix + "last_condition", seq)
            self._settings.sync()
            asyncio.ensure_future(self._switch_condition(seq))

    async def _switch_condition(self, seq: str):
        changed = seq != self.seq
        if changed:  # 조건 변경: 이전 조건 해제 + 이 창 행 전량 정리
            await self.stop()
            # 창 닫기와 조건 전환이 겹쳐도 이전 참조수가 새 REG를 삼키지 않게
            # 다음 묶음에서 서버 등록을 현재 화면 기준으로 전량 재확인한다.
            self.app.force_real_sync()
        elif seq not in RANK_SEQS and seq != HOLDINGS_SEQ:  # 같은 조건식 재조회
            await self.app.clear_condition_if_sole(self.seq, self)
        mode = "rank" if seq in RANK_SEQS else "holdings" if seq == HOLDINGS_SEQ else "normal"
        switched = self.screen.set_view_mode(mode)
        if seq in RANK_SEQS:  # 기준시간 콤보 내용을 서브모드에 맞게 교체 (계열 간 직접 전환 포함)
            self.screen.set_rank_period(RANK_SUBMODE[seq])
        self.seq = str(seq)
        # 본창에서 선택한 일반 조건식은 창을 닫거나 다른 화면을 보더라도
        # 백그라운드 수집 대상으로 기억한다. 순위/보유종목 메뉴는 제외한다.
        if (self.app.views and self is self.app.views[0]
                and self.seq not in RANK_SEQS and self.seq != HOLDINGS_SEQ):
            self._settings.setValue("background_condition_seq", self.seq)
            condition_name = next(
                (str(name) for item_seq, name in self.app._cond_items
                 if str(item_seq) == self.seq), "")
            if condition_name:
                self._settings.setValue("background_condition_name", condition_name)
            self._settings.sync()
        if switched:  # 재조회/간격도 모드별 저장 -> 새 모드 값 로드 (시그널이 타이머까지 정리)
            self.screen.refresh_interval.setValue(
                int(self._settings.value(self._mkey("refresh_interval"), 3)))
            self.screen.auto_refresh.setChecked(
                self._settings.value(self._mkey("auto_refresh"), "false") == "true")
        if seq in RANK_SEQS:  # 순위 계열: 서버 조건검색 대신 REST 폴 -> 같은 snapshot 경로
            await self._poll_rank()
            return
        if seq == HOLDINGS_SEQ:
            await self._poll_holdings()
            return
        # 조건식 실시간 등록은 서버가 조건번호별로 하나만 유지한다. 같은 조건을
        # 두 번째 창에서 다시 CNSRREQ하면 일부 응답이 빈 스냅샷으로 와서, seq가
        # 같은 모든 창의 목록을 지우는 문제가 있다. 이미 등록된 조건은 현재 창의
        # 목록을 즉시 복제하고 이후 편입/이탈 이벤트를 함께 받는다.
        peer = next(
            (view for view in self.app.views
             if view is not self and view.seq == self.seq),
            None,
        )
        if peer is not None and self.seq in self.app.ws._active_seqs:
            self.on_snapshot(list(peer.screen.model.codes))
            log.info("condition shared%s: seq=%s copied=%d", self.prefix or " ",
                     self.seq, len(peer.screen.model.codes))
            return
        await self.app.ws.register_condition(seq)

    async def stop(self):
        """이 창의 조건/시세 구독 정리 (조건 변경·창 닫기)."""
        # 이전 조건의 지연 백필이 살아 있으면 새 조건의 _schedule_refresh가 이를 보고
        # 예약을 생략할 수 있다. 전환 전에 끝내 보유종목 등 새 목록이 반드시 백필되게 한다.
        if self._refresh_task and not self._refresh_task.done():
            self._refresh_task.cancel()
            try:
                await self._refresh_task
            except asyncio.CancelledError:
                pass
        self._refresh_task = None
        suffix = self._real_suffix()
        if self.seq is not None and self.seq != HOLDINGS_SEQ:
            await self.app.clear_condition_if_sole(self.seq, self)
        self.seq = None
        codes = list(self.screen.model.codes)
        for code in codes:
            self.app.queue_real(code, add=False, suffix=suffix)
            self.screen.model.remove_stock(code)

    def _real_suffix(self):
        """None=전역 KRX/통합 설정, _NX=NXT 등락률 메뉴 전용."""
        return "_NX" if self.seq == NXT_RATE_SEQ else None

    async def _poll_rank(self):
        """순위 계열: REST 상위 RANK_TOP개 -> 조건검색과 동일한 snapshot 경로.
        조회순위=ka00198, NXT등락률=ka10027(stex_tp=2), 거래량급증=ka10023."""
        try:
            if self.seq == NXT_RATE_SEQ:
                rows = (await self.app.rest.change_rate_rank("2"))[:RANK_TOP]
            elif self.seq == VSURGE_SEQ:
                rows = (await self.app.rest.volume_surge(
                    self.screen.rank_period.currentData()))[:RANK_TOP]
            elif self.seq == TVAL_SEQ:
                rows = (await self.app.rest.trade_value_rank())[:RANK_TOP]
            else:
                rows = (await self.app.rest.inquiry_rank(
                    self.screen.rank_period.currentData()))[:RANK_TOP]
        except Exception as e:  # noqa: BLE001
            log.warning("rank poll%s: %s", self.prefix or "", e)
            return
        rows = [r for r in rows if r.get("code")]
        if len(rows) < RANK_TOP:
            log.warning("rank poll%s: incomplete %d/%d", self.prefix or "", len(rows), RANK_TOP)
            return
        self.on_snapshot([r["code"] for r in rows])
        for r in rows:  # 순위/변동/이름 바로 채움 (시세는 실시간+백필)
            self.screen.on_tick(r["code"], {"qrank": r["rank"], "qrank_chg": r["rank_chg"],
                                            "name": r["name"]})

    async def _poll_holdings(self):
        """계좌 보유종목을 조회해 조건검색 그리드와 실시간 시세에 연결."""
        try:
            rows = await self.app.rest.holdings()
        except Exception as e:  # noqa: BLE001
            log.warning("holdings poll%s: %s", self.prefix or "", e)
            return
        self.on_snapshot([r["code"] for r in rows])
        for r in rows:
            self.screen.on_tick(r["code"], {"name": r["name"]})
        # 행 추가 시 예약되는 백필과 별개로 이름 반영 뒤 한 번 더 보장한다.
        self._schedule_refresh()

    # --- 재조회 -----------------------------------------------------------
    def on_refresh(self):
        seq = self.screen.condition_combo.currentData()
        if seq is not None:
            asyncio.ensure_future(self._switch_condition(seq))

    def _mkey(self, name: str) -> str:
        """화면별 재조회 설정 키 (gui._mkey와 동일 규칙)."""
        mode_prefix = ("rankmode_" if self.seq in RANK_SEQS else
                       "holdingsmode_" if self.seq == HOLDINGS_SEQ else "")
        return self.prefix + mode_prefix + name

    def _on_sound(self, on: bool):
        self._settings.setValue(self.prefix + "sound", "true" if on else "false")
        self._settings.sync()

    def _maybe_beep(self):
        if self.screen.sound_check.isChecked() and time.monotonic() - self._beep_t >= 1.0:
            self._beep_t = time.monotonic()
            _beep("in")

    def _on_auto_refresh(self, on: bool):
        self._settings.setValue(self._mkey("auto_refresh"), "true" if on else "false")
        self._settings.sync()
        if on:
            self._auto_timer.start(self.screen.refresh_interval.value() * 1000)
            log.info("auto-requery ON (%ds) %s", self.screen.refresh_interval.value(), self.prefix)
        else:
            self._auto_timer.stop()
            log.info("auto-requery OFF %s", self.prefix)

    def _on_interval_changed(self, sec: int):
        self._settings.setValue(self._mkey("refresh_interval"), sec)
        self._settings.sync()
        if self._auto_timer.isActive():
            self._auto_timer.start(sec * 1000)

    def _schedule_main_auto_refresh_cutoff(self, clear_stale: bool = False):
        """본창 재조회 체크를 매일 09:02:20에 해제하도록 예약한다."""
        if self.prefix or self._auto_refresh_cutoff_timer is None:
            return
        now = datetime.now(KST)
        cutoff = now.replace(
            hour=9, minute=2, second=20, microsecond=0)
        if clear_stale and now >= cutoff:
            self.screen.auto_refresh.setChecked(False)
        if now >= cutoff:
            cutoff += timedelta(days=1)
        delay_ms = max(
            1, int((cutoff - now).total_seconds() * 1000))
        self._auto_refresh_cutoff_timer.start(delay_ms)

    def _on_main_auto_refresh_cutoff(self):
        """추가 창에는 영향을 주지 않고 본창의 자동 재조회만 끈다."""
        was_checked = self.screen.auto_refresh.isChecked()
        self.screen.auto_refresh.setChecked(False)
        if was_checked:
            log.info("main auto-requery OFF at 09:02:20")
        self._schedule_main_auto_refresh_cutoff()

    # --- 편입/이탈 ---------------------------------------------------------
    def on_snapshot(self, codes: list[str]):
        cur = set(self.screen.model.codes)
        new = set(codes)
        for code in cur - new:
            self.screen.on_excluded(code)
            if self.seq in RANK_SEQS and code in self.screen.model.rows:
                # 자동삭제 OFF면 행과 실시간 추적은 남긴다. 다만 직전 순위를
                # 그대로 두면 현재 상위 20종목보다 실시간 등록 우선순위가
                # 높아질 수 있으므로 순위 표식만 비워 이탈 상태를 구분한다.
                self.screen.on_tick(code, {"qrank": 0, "qrank_chg": 0})
            self.app.queue_real(code, add=False, suffix=self._real_suffix())
        added = list(dict.fromkeys(code for code in codes if code not in cur))
        if added:
            self.screen.on_included_many(added)
        for code in added:
            if code in self.app._account_auto_cancel_armed:
                self.screen.model.set_account_auto_cancel_armed(code, True)
        if added:
            # 실시간 종목 등록은 현재 모든 창의 모델을 기준으로 한 번만 동기화한다.
            self.app.queue_real("", add=True, suffix=self._real_suffix())
            self._schedule_refresh()
            self._maybe_beep()
        log.info("snapshot%s: %d codes (+%d/-%d) %s", self.prefix or " ",
                 len(new), len(added), len(cur - new), ",".join(sorted(new)))

    def on_event(self, code: str, is_insert: bool):
        if is_insert:
            self.screen.on_included(code, {"name": code})
            if code in self.app._account_auto_cancel_armed:
                self.screen.model.set_account_auto_cancel_armed(code, True)
            self.app.queue_real(code, add=True, suffix=self._real_suffix())
            self._schedule_refresh()
            self._maybe_beep()
        else:
            self.screen.on_excluded(code)
            self.app.queue_real(code, add=False, suffix=self._real_suffix())

    # --- 시세 채우기/진입시각 ----------------------------------------------
    def _schedule_refresh(self):
        if self._refresh_task and not self._refresh_task.done():
            return
        self._refresh_task = asyncio.ensure_future(self._refresh_quotes())

    async def _refresh_quotes(self):
        await asyncio.sleep(0.4)  # 편입 버스트 모으기
        codes = list(self.screen.model.codes)
        for i in range(0, len(codes), 100):
            try:
                for row in await self.app.rest.watch_info(
                        codes[i:i + 100], suffix=self._real_suffix()):
                    self.screen.on_tick(row["code"], row)
            except Exception as e:  # noqa: BLE001
                log.warning("watch_info failed: %s", e)
        self.app.ensure_prev_vol(self.screen.model)  # 역산 0인 종목 ka10081 백필
        self._fill_entry_times()

    def _fill_entry_times(self):
        m = self.screen.model
        todo = []
        for code in list(m.codes):
            d = m.rows[code]
            at_limit = d["upper"] > 0 and d["price"] == d["upper"]
            if at_limit:
                if code in self._entry_cache:
                    self.screen.on_tick(code, {"time": self._entry_cache[code]})
                elif code not in self._entry_pending:
                    self._entry_pending.add(code)
                    todo.append((d["vol"], code, d["upper"]))
            elif code in self._entry_cache:
                del self._entry_cache[code]
                self.screen.on_tick(code, {"time": ""})
        if todo:  # 거래량 적은 순(점상 먼저) 순차 조회
            todo.sort()
            asyncio.ensure_future(self._drain_entries(todo))

    async def _drain_entries(self, todo):
        for _, code, upper in todo:
            try:
                t = await self.app.rest.last_limit_entry(code, upper)
            except Exception as e:  # noqa: BLE001
                log.warning("last_limit_entry %s: %s", code, e)
                t = ""
            self._entry_pending.discard(code)
            self._entry_cache[code] = t
            self.screen.on_tick(code, {"time": t})
            if t:
                trade_date = QDate.currentDate().toString("yyyyMMdd")
                if save_last_entry_time(trade_date, code, t):
                    log.info(
                        "live limit entry saved: %s %s %s",
                        trade_date, code, t)
                    if self.app._analysis is not None:
                        self.app._analysis._refresh_limit_up_table()


class App:
    def __init__(self, screen: ConditionScreen):
        self.rest = RestClient()
        self.ws = WSClient()
        self.orders = OrderEngine(self.rest, self._on_order_update)
        self._orderable_cache: dict[tuple[str, int], dict] = {}
        self._orderable_tasks: dict[ConditionScreen, asyncio.Task] = {}
        self._orderable_prefetch_task = None
        self._orderable_prefetch_failed: dict[tuple[str, int], float] = {}
        self._orderable_prefetch_blocked_date = ""
        # 순환매 분석은 경량 분석창에서 사용하지 않으므로 예약 수집을
        # 시작하지 않는다. 수동 호출용 상태만 남겨 기존 저장 데이터와의
        # 호환성을 유지한다.
        self._background_condition_task = None
        self._background_condition_slots: set[tuple[str, str]] = set()
        self._orderable_prefetch_timer = QTimer()
        self._orderable_prefetch_timer.timeout.connect(
            self._queue_orderable_prefetch)
        self._orderable_prefetch_timer.start(400)
        self._settings = QSettings("layout.ini", QSettings.IniFormat)
        self._theme_mode = str(self._settings.value("theme_mode", "system"))
        if self._theme_mode not in THEME_MODES:
            self._theme_mode = "system"
        self.views: list[View] = [View(self, screen)]
        # 주문허용은 창마다 따로 보이면 실제 키 입력 창과 사용자가 확인한 창이
        # 달라질 수 있으므로 앱 전체에서 하나의 상태로 동기화한다.
        self._order_enabled = bool(screen.order_enable_check.isChecked())
        self._global_hotkeys = WindowsGlobalHotkeys(
            self._on_global_exit_hotkey)
        screen.global_hotkeys = True
        self._extra_windows: list = []  # 추가 창(ConditionWindow) 목록
        self._cond_items = []           # CNSRLST 결과 (새 창 콤보 채우기용)
        self._condition_reload_id = 0   # 재조회 타임아웃과 실제 응답의 경합 방지
        self._market = None             # MarketInfo (새 창 모델 주입용)
        self._limit_cnt = None          # 어제까지 연속상한 일수 (연상 컬럼, 시작 시 1회, 일봉 계산)
        self._account_summary = None     # 주문 툴바 공통 실계좌 요약
        self._balance_sell_settings: dict[str, dict] = {}
        self._balance_sell_stage: dict[str, int] = {}
        self._balance_sell_date: dict[str, str] = {}
        self._balance_sell_tasks: dict[str, asyncio.Task] = {}
        self._emergency_locked: set[str] = set()
        self._emergency_tasks: dict[str, asyncio.Task] = {}
        self._emergency_prices: dict[str, int] = {}
        self._emergency_recheck: set[str] = set()
        # 주문셀에서 종료 상태를 확인해 지운 뒤에는 이미 진행 중이던 계좌조회가
        # 같은 '대상없음'을 뒤늦게 다시 덮어쓰지 못하게 한다.
        self._emergency_status_dismissed: set[str] = set()
        # 종목별 계좌 자동취소는 행에서 사용자가 직접 켠 경우에만 무장된다.
        # 웹소켓 체결 ID로 중복 이벤트를 제거하고, 무장 이후 각 매수 주문의
        # 체결량이 100주에 도달하면 그 주문번호의 잔량 전부를 취소한다.
        # 종목 내 다른 분할 주문 체결량은 합산하지 않는다.
        self._account_auto_cancel_armed: set[str] = set()
        self._account_auto_cancel_filled: dict[tuple[str, str], int] = {}
        self._account_auto_cancel_fill_ids: set[tuple[str, str, str]] = set()
        self._account_auto_cancel_tasks: dict[tuple[str, str], asyncio.Task] = {}
        # 화면 변경을 0.3초 모은 뒤, 현재 보이는 행 전체와 WS 등록 상태를 동기화한다.
        # 편입/이탈 이벤트 횟수로 참조수를 증감하면 중복 이벤트나 창 전환 경합 때
        # 실제 화면과 참조수가 어긋날 수 있으므로 화면 모델을 단일 진실로 삼는다.
        self._real_dirty = False
        self._real_force_pending = False
        self._reg_task = None
        # 단일가 종목은 WS 무송신(실측 0건) -> REST 3초 폴이 유일한 채널
        self._single_task = None
        # 전일거래량: 동시호가 역산실패(0) 종목만 ka10081로 1회 백필 (정적값 캐시)
        self._prevvol_pending: set[str] = set()
        self._prevvol_done: set[str] = set()
        self._prevvol_queue: deque[str] = deque()
        self._prevvol_workers: set[asyncio.Task] = set()
        self._single_timer = QTimer()
        self._single_timer.timeout.connect(self._on_single_poll)
        self._single_timer.start(3000)
        self._rank = None
        self._analysis = None
        # 네이버 뉴스 API 자동수집은 저장된 체크 상태와 주기에만 따른다.
        self._news_auto_timer = QTimer()
        news_interval = int(self._settings.value("analysis_news_interval", 5))
        self._news_auto_timer.setInterval(news_interval * 60 * 1000)
        self._news_auto_timer.timeout.connect(self._auto_news_collection)
        if self._settings.value("analysis_news_auto", "false") == "true":
            self._news_auto_timer.start()
            QTimer.singleShot(10000, self._auto_news_collection)
        # 상한가 원천은 분석창의 KRX·키움 수집 버튼으로만 저장한다.
        # 장중 조건검색값이나 시작 직후 자동 보완으로 종가를 확정하지 않는다.
        # 공인 IP 감시: 바뀌면 키움 화이트리스트에서 벗어나 API 차단 -> 상단바 경보
        self._public_ip = None
        self._ls_news_startup_sync_task = None
        self.ws_task = None
        self._kiwoom_started = False
        self._kiwoom_wait_task = None
        self._title_suffix_before_maintenance = None
        self._ip_task = None
        self._ip_timer = QTimer()
        self._ip_timer.timeout.connect(
            lambda: setattr(self, "_ip_task", asyncio.ensure_future(self._check_ip()))
            if not (self._ip_task and not self._ip_task.done()) else None)
        self._ip_timer.start(60000)

        self.ws.on_condition_list = self._on_condition_list
        self.ws.on_condition_event = self._on_condition_event
        self.ws.on_condition_snapshot = self._on_condition_snapshot
        self.ws.on_real = self._on_real
        self.ws.on_vi = self._on_vi
        self.ws.on_order = self._on_account_order_event
        # 통합 시세·조건검색: 전 창 공통 설정. 첫 REG/CNSRREQ 전에 확정한다.
        if self._settings.value("unified_real", "false") == "true":
            self.ws.real_suffix = self.rest.suffix = "_AL"
            self.ws.condition_stex_tp = "A"
            screen.unified_check.setChecked(True)  # toggled 연결 전 = 시각 상태만
        screen.unified_check.toggled.connect(self._on_unified)
        self._font_size = _normalize_app_font_size(
            self._settings.value(APP_FONT_SIZE_KEY, DEFAULT_APP_FONT_SIZE))
        font_index = screen.font_size_combo.findData(self._font_size)
        screen.font_size_combo.setCurrentIndex(max(0, font_index))
        screen.font_size_combo.activated.connect(
            self._on_font_size_selected)
        screen.theme_btn.clicked.connect(self._cycle_theme)
        self._sync_theme_button()
        self._wire_common(screen)
        self._sync_realtime_watch_models()

    def _sync_theme_button(self):
        icon, tip = THEME_UI[self._theme_mode]
        btn = self.views[0].screen.theme_btn
        btn.setText(icon)
        btn.setToolTip(tip)

    def _cycle_theme(self):
        i = (THEME_MODES.index(self._theme_mode) + 1) % len(THEME_MODES)
        self._theme_mode = THEME_MODES[i]
        _apply_theme(QApplication.instance(), self._theme_mode)
        self._settings.setValue("theme_mode", self._theme_mode)
        self._settings.sync()
        self._sync_theme_button()

    def _on_font_size_selected(self, index: int):
        size = _normalize_app_font_size(
            self.views[0].screen.font_size_combo.itemData(index))
        if size == self._font_size:
            return
        self._font_size = size
        _apply_app_font(QApplication.instance(), size)
        self._settings.setValue(APP_FONT_SIZE_KEY, size)
        self._settings.sync()

    def _wire_common(self, screen: ConditionScreen):
        screen.reload_btn.clicked.connect(self._reload_conditions)
        screen.rank_btn.clicked.connect(self._on_rank)
        screen.realtime_news_requested.connect(self._open_realtime_news)
        screen.newwin_btn.clicked.connect(self._on_newwin)
        screen.order_target_selected.connect(
            lambda code, price, target=screen:
            self._queue_orderable_quantity(target, code, price))
        screen.account_auto_cancel_changed.connect(
            self._set_account_auto_cancel)
        screen.order_requested.connect(
            lambda code, mode, count, auto, total, price, target=screen:
            self._submit_order(target, code, mode, count, auto, total, price))
        screen.cancel_requested.connect(self._cancel_order)
        screen.order_enable_check.toggled.connect(
            lambda enabled, source=screen:
            self._sync_order_enabled(enabled, source))
        screen.exit_hotkey_changed.connect(
            lambda code, spec, source=screen:
            self._set_global_exit_hotkey(source, code, spec))
        screen.emergency_exit_requested.connect(self._emergency_exit)
        screen.order_status_acknowledged.connect(
            self._acknowledge_order_status)
        screen.balance_sell_changed.connect(self._set_balance_sell)
        screen.watch_toggled.connect(
            lambda code, enabled, target=screen:
            self._toggle_realtime_watch(target, code, enabled))
        screen.analysis_stock_requested.connect(
            lambda code, target=screen:
            self._open_condition_analysis_stock(target, code))

    def _sync_order_enabled(
            self, enabled: bool, source: ConditionScreen | None = None):
        self._order_enabled = bool(enabled)
        for view in self.views:
            checkbox = view.screen.order_enable_check
            if view.screen is source or checkbox.isChecked() == self._order_enabled:
                continue
            checkbox.blockSignals(True)
            checkbox.setChecked(self._order_enabled)
            checkbox.blockSignals(False)
            view.screen._refresh_order_actions()
        log.warning(
            "global order permission changed enabled=%s source=%s",
            self._order_enabled, source.prefix if source else "-")

    def _set_global_exit_hotkey(
            self, screen: ConditionScreen, code: str, spec):
        token = (id(screen), code)
        if spec is None:
            self._global_hotkeys.unregister(token)
            audit_log.info(
                "exit hotkey cleared code=%s screen=%s",
                code, screen.prefix)
            return
        ok = self._global_hotkeys.register(
            token, spec, (screen, code, str(spec.get("label") or "")))
        if ok:
            log.warning(
                "global exit hotkey registered code=%s key=%s screen=%s "
                "admin=%s pid=%s",
                code, spec.get("label"), screen.prefix,
                _is_process_admin(), os.getpid())
            return
        screen.model.set_order_status(code, "키충돌")
        log.error(
            "global exit hotkey registration failed code=%s key=%s screen=%s",
            code, spec.get("label"), screen.prefix)

    def _on_global_exit_hotkey(self, payload: tuple):
        screen, code, label = payload
        row = screen.model.rows.get(code, {})
        price = int(row.get("bid_price4") or 0)
        # 등록한 조건검색창이 아니라 분석창/다른 조건검색창을 보고 있어도
        # 같은 Qt 앱 안에 활성 창이 하나라도 있으면 '앱 활성'으로 본다.
        active = QApplication.activeWindow() is not None
        log.warning(
            "global exit hotkey triggered code=%s key=%s price=%s "
            "enabled=%s app_active=%s admin=%s pid=%s",
            code, label, price, self._order_enabled, active,
            _is_process_admin(), os.getpid())
        # 앱이 뒤에 있어 셀 상태 변경을 볼 수 없을 때만 전역키 수신을
        # 작업표시줄로만 알린다. 청산키에는 3단매도 경고음을 재사용하지 않는다.
        if not active:
            QApplication.alert(screen.window(), 4000)
        self._emergency_exit(code, price, self._order_enabled)

    def _open_market_status(self):
        analysis = self._ensure_analysis_window()
        analysis.open_market_status()
        analysis.show()
        analysis.raise_()
        analysis.activateWindow()

    def _open_realtime_news(self):
        """본창 뉴스 버튼에서 분석창의 LS 실시간 뉴스 탭을 바로 연다."""
        analysis = self._ensure_analysis_window()
        analysis.open_ls_realtime_news()
        analysis.show()
        analysis.raise_()
        analysis.activateWindow()
        QTimer.singleShot(0, analysis._ensure_titlebar_visible)

    def _refresh_market_overview(self):
        """DB 최신 국내지수·외국인 수급을 모든 조건검색창에 표시한다."""
        try:
            data = market_dashboard()
        except Exception as error:  # noqa: BLE001
            log.warning("market overview refresh failed: %s", error)
            return
        if not data.get("trade_date"):
            text, regime = "시황  저장된 국내 시장 데이터가 없습니다.", "중립"
        else:
            indices = {
                row["market"]: row for row in data.get("indices", [])}
            flows = {
                row["market"]: row for row in data.get("market_flows", [])}
            external = {
                row["indicator_code"]: row
                for row in data.get("external", [])}

            def index_text(market):
                row = indices.get(market)
                if not row:
                    return f"{market} 미수집"
                rate = row.get("change_rate")
                rate_text = (
                    f"{float(rate):+.2f}%" if rate is not None else "-")
                return (
                    f"{market} {float(row.get('close_value') or 0):,.2f} "
                    f"{rate_text}")

            foreign_parts = []
            for market in ("KOSPI", "KOSDAQ"):
                if market in flows:
                    foreign_parts.append(
                        f"{market} "
                        f"{_format_flow_million(flows[market].get('foreign_net'))}"
                    )
            foreign = " / ".join(foreign_parts) or "미수집"

            def external_text(code, title, value_only=False):
                row = external.get(code)
                if not row:
                    return f"{title} 미수집"
                rate = row.get("change_rate")
                rate_text = (
                    f"{float(rate):+.2f}%" if rate is not None else "-")
                if value_only:
                    return (
                        f"{title} {float(row.get('value') or 0):,.2f} "
                        f"{rate_text}")
                return f"{title} {rate_text}"

            context = _market_context(data)
            trade_date = str(data["trade_date"])
            basis = (
                f"{trade_date[4:6]}/{trade_date[6:8]}"
                if len(trade_date) == 8 else trade_date)
            text = (
                f"시황  {external_text('NASDAQ_FUT', 'NQ선물')}  |  "
                f"{external_text('SOX', 'SOX')}  |  "
                f"{external_text('USDKRW', '환율', True)}  |  "
                f"{basis} {index_text('KOSPI')}  |  "
                f"{index_text('KOSDAQ')}  |  외국인 {foreign}  |  "
                f"{context['regime']} · {context['leadership']}")
            regime = context["regime"]
        for view in self.views:
            view.screen.set_market_overview(text, regime)

    def _queue_external_market_collection(self):
        if (
            self._external_market_task is None
            or self._external_market_task.done()
        ):
            self._external_market_task = asyncio.ensure_future(
                self._collect_external_market())

    async def _collect_external_market(self):
        """해외 선행지표를 1분마다 조회해 원본시각과 함께 SQLite에 저장한다."""
        try:
            rows, errors = await GlobalMarketClient().fetch_all()
            saved = save_external_market_quotes(rows)
            if errors:
                log.warning(
                    "external market partial failure: %s", " | ".join(errors))
            log.info(
                "external market collected: saved=%d errors=%d",
                saved, len(errors))
            self._refresh_market_overview()
            if self._analysis is not None and self._analysis.isVisible():
                current = self._analysis._tabs.currentIndex()
                if self._analysis._tabs.tabText(current) == "시장 현황":
                    self._analysis._refresh_market_page()
        except Exception as error:  # noqa: BLE001
            log.warning("external market collection failed: %s", error)

    def _sync_realtime_watch_models(self):
        """DB 감시목록을 모든 조건검색 창의 연상 셀에 반영한다."""
        try:
            watched = realtime_watch_codes()
        except Exception as error:  # noqa: BLE001
            log.warning("realtime watch sync failed: %s", error)
            return
        for view in self.views:
            view.screen.model.set_watched_codes(watched)
        if self._analysis is not None:
            self._analysis._sync_ls_news_watched_codes(watched)

    def _toggle_realtime_watch(
            self, screen: ConditionScreen, code: str, enabled: bool):
        """조건검색 연상 셀 클릭을 영구 뉴스 감시목록과 연결한다."""
        try:
            set_realtime_watch(
                code, enabled, "CONDITION_STREAK",
                screen.model.rows.get(code, {}).get("name", ""))
        except ValueError as error:
            QMessageBox.warning(screen, "실시간 감시", str(error))
            return
        self._sync_realtime_watch_models()
        if enabled:
            analysis = self._ensure_analysis_window()
            analysis.open_realtime_watch(code, fetch_news=True)
        elif self._analysis is not None:
            analysis = self._analysis
            if analysis._selected_watch_code == code:
                analysis._selected_watch_code = ""
            analysis._refresh_realtime_watch_table()
            analysis._refresh_realtime_news_table()
            analysis._refresh_limit_up_table()
        state = "등록" if enabled else "해제"
        log.info("realtime news watch %s: code=%s source=condition streak",
                 state, code)

    def _open_condition_analysis_stock(
            self, screen: ConditionScreen, code: str):
        """등락률 셀 클릭 종목을 감시목록에 넣고 뉴스 분석을 바로 연다."""
        try:
            if code not in realtime_watch_codes():
                set_realtime_watch(
                    code, True, "CONDITION_RATE",
                    screen.model.rows.get(code, {}).get("name", ""))
        except ValueError as error:
            QMessageBox.warning(screen, "실시간 감시", str(error))
            return
        self._sync_realtime_watch_models()
        analysis = self._ensure_analysis_window()
        analysis.open_realtime_watch(code, fetch_news=True)
        log.info(
            "realtime news watch open: code=%s source=condition rate", code)

    def _queue_orderable_quantity(
            self, screen: ConditionScreen, code: str, price: int):
        key = (code, price)
        cached = self._orderable_cache.get(key)
        if cached is not None:
            screen.set_orderable_quantity(code, price, cached)
            return
        # 사용자가 직접 고른 종목은 백그라운드 선조회보다 항상 우선한다.
        if self._orderable_prefetch_task and not self._orderable_prefetch_task.done():
            self._orderable_prefetch_task.cancel()
        previous = self._orderable_tasks.get(screen)
        if previous and not previous.done():
            previous.cancel()
        task = asyncio.ensure_future(
            self._load_orderable_quantity(screen, code, price))
        self._orderable_tasks[screen] = task

    async def _load_orderable_quantity(
            self, screen: ConditionScreen, code: str, price: int):
        try:
            detail = await self.rest.orderable_quantity(code, price)
        except asyncio.CancelledError:
            return
        except Exception as e:  # noqa: BLE001
            log.warning("orderable quantity %s@%s: %s", code, price, e)
            screen.set_orderable_quantity_error(code, price, str(e))
            return
        else:
            self._orderable_cache[(code, price)] = detail
            screen.set_orderable_quantity(code, price, detail)
        finally:
            if self._orderable_tasks.get(screen) is asyncio.current_task():
                self._orderable_tasks.pop(screen, None)

    def _prefetch_candidates(self) -> list[tuple[str, int]]:
        """각 표에 현재 보이는 정렬 순서 그대로 상위 종목을 모은다."""
        result = []
        seen = set()
        for view in self.views:  # 본창 우선, 이후 추가 창 순서
            screen = view.screen
            # 편입 직후 시세가 행마다 순차 도착하는 동안 조회를 시작하면 아래 행의
            # upper가 먼저 채워져 화면 재정렬 전 순서로 선조회될 수 있다.
            # 이 창의 상한가 기준값이 전부 준비된 다음 실제 프록시 순위를 읽는다.
            if screen.model.codes and any(
                    int(screen.model.rows.get(code, {}).get("upper") or 0) <= 0
                    for code in screen.model.codes):
                continue
            for proxy_row in range(screen.proxy.rowCount()):
                source = screen.proxy.mapToSource(
                    screen.proxy.index(proxy_row, 0))
                if not source.isValid():
                    continue
                code = screen.model.codes[source.row()]
                data = screen.model.rows.get(code, {})
                upper = int(data.get("upper") or 0)
                key = (code, upper)
                if upper <= 0 or key in seen:
                    continue
                seen.add(key)
                result.append(key)
                if len(result) >= ORDERABLE_PREFETCH_TOP:
                    return result
        return result

    def _queue_orderable_prefetch(self):
        if self._orderable_prefetch_task and not self._orderable_prefetch_task.done():
            return
        now_local = datetime.now()
        today = now_local.strftime("%Y%m%d")
        # 휴장일·장외 시간에는 모든 후보가 같은 업무 오류를 내므로 선조회를 하지 않는다.
        if (now_local.weekday() >= 5
                or not (8 * 60 + 20 <= now_local.hour * 60 + now_local.minute
                        <= 15 * 60 + 30)
                or self._orderable_prefetch_blocked_date == today):
            return
        # 직접 선택한 종목 조회가 진행 중이면 끝날 때까지 양보한다.
        if any(task and not task.done() for task in self._orderable_tasks.values()):
            return
        now = time.monotonic()
        for key in self._prefetch_candidates():
            if key in self._orderable_cache:
                continue
            if now - self._orderable_prefetch_failed.get(key, 0) < 10:
                continue
            code, price = key
            rank = next((
                row + 1
                for view in self.views
                for row in range(view.screen.proxy.rowCount())
                if view.screen.model.codes[
                    view.screen.proxy.mapToSource(
                        view.screen.proxy.index(row, 0)).row()] == code
            ), 0)
            log.info("orderable prefetch start: rank=%s code=%s price=%s",
                     rank or "-", code, price)
            self._orderable_prefetch_task = asyncio.ensure_future(
                self._load_orderable_prefetch(*key))
            return

    async def _load_orderable_prefetch(self, code: str, price: int):
        try:
            detail = await self.rest.orderable_quantity(code, price)
        except asyncio.CancelledError:
            return
        except Exception as e:  # noqa: BLE001
            self._orderable_prefetch_failed[(code, price)] = time.monotonic()
            if "장이 열리지 않는 날" in str(e):
                self._orderable_prefetch_blocked_date = datetime.now().strftime(
                    "%Y%m%d")
                log.info("orderable prefetch disabled today: %s", e)
            else:
                log.warning("orderable prefetch %s@%s: %s", code, price, e)
        else:
            self._orderable_cache[(code, price)] = detail
            self._orderable_prefetch_failed.pop((code, price), None)
            # 선조회 도중 사용자가 이 종목을 골랐다면 즉시 화면에도 반영한다.
            for view in self.views:
                view.screen.set_orderable_quantity(code, price, detail)

    def _submit_order(self, screen: ConditionScreen, code: str, mode: str,
                      count: int, auto_cancel: bool, total_qty: int, price: int):
        try:
            quantities = (
                [100] * count if mode == "fixed"
                else split_quantity(total_qty, count))
            name = screen.model.rows.get(code, {}).get("name") or code
            if auto_cancel:
                # 자동취소 방식으로 실제 앱 주문을 전송한 경우도 명시적 무장으로
                # 간주하고 행에 동일하게 표시한다.
                self._set_account_auto_cancel(code, True)
            self.orders.submit(code, name, price, quantities, auto_cancel)
            audit_log.info(
                "order batch queued code=%s name=%s mode=%s "
                "cancel_mode=%s price=%s quantities=%s total=%s",
                code, name, mode,
                "auto" if auto_cancel else "manual",
                price, quantities, sum(quantities))
        except Exception as e:  # noqa: BLE001
            log.exception(
                "order batch rejected code=%s mode=%s count=%s "
                "auto_cancel=%s total=%s price=%s",
                code, mode, count, auto_cancel, total_qty, price)
            screen.set_order_state(code, "오류", f"상태 오류 · {e}", False)

    def _cancel_order(self, code: str):
        # 주문 셀의 취소 버튼은 앱이 보낸 분할 주문에만 노출된다. 따라서 계좌
        # 미체결 조회가 일부만 돌아와도, 이 앱이 받은 모든 주문번호를 직접
        # 취소해야 9분할 주문이 빠짐없이 취소된다.
        audit_log.info("manual cancel requested code=%s", code)
        count, qty = self.orders.cancel_submitted_children(code)
        if count:
            log.warning(
                "local split cancel queued code=%s orders=%s qty=%s",
                code, count, qty)
            return
        asyncio.ensure_future(self._cancel_account_orders(code))

    async def _cancel_account_orders(self, code: str):
        try:
            count, qty = await self.rest.cancel_open_buy_orders(code)
        except Exception as e:  # noqa: BLE001
            log.exception("account open buys cancel failed code=%s", code)
            for view in self.views:
                if code in view.screen.model.rows:
                    view.screen.set_order_state(
                        code, "오류", f"상태 취소오류 · {e}", False)
            return
        status = "취소전송" if count else "취소없음"
        for view in self.views:
            if code in view.screen.model.rows:
                view.screen.set_order_state(
                    code, status,
                    (f"상태 계좌 미체결 매수 {count:,}건 "
                     f"{qty:,}주 취소전송"
                     if count else "상태 계좌 미체결 매수주문 없음"),
                    False)
        log.warning(
            "account open buys cancel code=%s orders=%s qty=%s",
            code, count, qty)

    def _on_order_update(self, batch, state: str):
        count = len(batch.children)
        mode = "자" if batch.auto_cancel else "수"
        if state == "긴급정리":
            compact = "긴급정리"
        elif batch.error:
            compact = "장종료" if "장종료" in batch.error else "오류"
        elif batch.remaining_qty == 0 and batch.sent_count == count:
            compact = f"{mode} 완료"
        elif state.startswith("취소") or batch.stop_requested:
            compact = f"{mode} 취소"
        else:
            compact = f"{mode} {batch.sent_count}/{count}"
        detail = (
            f"상태 {state} · {'자동취소' if batch.auto_cancel else '수동취소'}"
            f" · 상한가 지정가 {batch.price:,}원"
            f" · 전송 {batch.sent_count}/{count}"
            f" · 체결 {batch.total_filled:,}/{batch.total_requested:,}주"
            f" · 잔량 {batch.remaining_qty:,}주"
        )
        if batch.auto_cancel:
            detail += f" · 취소 {batch.cancel_count}/{count}"
        if batch.error:
            detail += f" · {batch.error}"
        has_remaining = any(
            child.order_no and child.remaining_qty > 0 and not child.cancel_sent
            for child in batch.children)
        reserved = self.orders.committed_notional()
        for view in self.views:
            view.screen.set_order_reserved(reserved)
            if batch.code in view.screen.model.rows:
                view.screen.set_order_state(
                    batch.code, compact, detail, has_remaining)

    def _reload_conditions(self):
        """조건 목록 재조회 요청. 응답 전후가 화면에 보이도록 버튼 상태도 갱신한다."""
        self._condition_reload_id += 1
        request_id = self._condition_reload_id
        for v in self.views:
            v.screen.reload_btn.setEnabled(False)
            v.screen.reload_btn.setText("…")
            v.screen.reload_btn.setToolTip("조건목록 조회 중")
        asyncio.ensure_future(self.ws.list_conditions())
        # 연결 이상 등으로 응답이 없더라도 버튼이 영구 비활성화되지 않게 한다.
        QTimer.singleShot(5000, lambda: self._finish_condition_reload(None, request_id))

    def _finish_condition_reload(self, count, request_id=None):
        if request_id is not None and request_id != self._condition_reload_id:
            return
        if count is not None:
            self._condition_reload_id += 1  # 예약된 타임아웃 무효화
        for v in self.views:
            btn = v.screen.reload_btn
            btn.setEnabled(True)
            btn.setText("")
            if count is None:
                btn.setToolTip("조건목록 응답 없음 — 다시 시도하세요")
            else:
                btn.setToolTip(f"조건목록 재조회 완료 — {count}개")

    def _on_unified(self, on: bool):
        self._settings.setValue("unified_real", "true" if on else "false")
        self._settings.sync()
        self.rest.suffix = "_AL" if on else ""  # watch_info 백필도 같은 소스로
        # WS가 기존 조건을 해제한 뒤 A(통합)/K(KRX)로 즉시 재등록한다.
        asyncio.ensure_future(self.ws.set_real_suffix("_AL" if on else ""))
        for v in self.views:  # 전 종목 시세 강제 재백필: 편입 diff 없어도 KRX<->통합 값 교체
            v._schedule_refresh()

    async def _check_ip(self):
        maintenance = active_kiwoom_maintenance()
        if maintenance is not None:
            self._pause_for_kiwoom_maintenance(maintenance)
            return
        try:
            ip = await self.rest.public_ip()
        except Exception as e:  # noqa: BLE001 - 외부 서비스 실패는 무시(다음 주기 재시도)
            log.warning("public_ip: %s", e)
            return
        if not ip or ip == self._public_ip:
            return
        screen = self.views[0].screen  # 메인창에만 표시
        changed = self._public_ip is not None  # None=최초 확인(정상), 값 있으면 실제 변경
        self._public_ip = ip
        screen.set_ip(ip, changed)
        _set_title_clock_base(
            screen.window(),
            (f"⚠ IP변경 {ip} — " if changed else "") + "[0156] 조건검색실시간" +
            ("" if changed else f" — {ip}"))
        if changed:
            log.warning("public IP changed -> %s (키움 화이트리스트 재등록 필요)", ip)
            _beep("jump")  # 초고음 3연타 경보

    def _start_ls_news_gap_sync(self):
        """분석창을 열지 않은 시작에서도 서버 누락 뉴스를 백그라운드 저장한다."""
        if (
            self._ls_news_startup_sync_task is not None
            and not self._ls_news_startup_sync_task.done()
        ):
            return
        self._ls_news_startup_sync_task = asyncio.create_task(
            self._sync_ls_news_gap_on_startup())

    async def _sync_ls_news_gap_on_startup(self):
        try:
            result = await LSNewsServerSync().sync()
            if result.get("status") != "disabled":
                log.info(
                    "LS server startup sync: checked=%s inserted=%s "
                    "duplicates=%s cursor=%s/%s",
                    result["processed"], result["inserted"],
                    result["updated"], result["cursor"],
                    result["upper_id"],
                )
        except asyncio.CancelledError:
            raise
        except Exception as error:  # noqa: BLE001 - 실시간 앱 시작은 계속한다.
            log.warning("LS server startup sync failed: %s", error)
        finally:
            self._ls_news_startup_sync_task = None

    def _pause_for_kiwoom_maintenance(self, window):
        """공지 점검 중에는 키움 연결을 끊고 종료 시각까지 한 번만 기다린다."""
        win = self.views[0].screen.window()
        if self._title_suffix_before_maintenance is None:
            self._title_suffix_before_maintenance = getattr(
                win, "_title_clock_suffix", "")
        win._title_clock_suffix = ""
        _set_title_clock_base(
            win, f"⚠ {format_kiwoom_maintenance(window)}")

        if self.ws_task is not None and not self.ws_task.done():
            self.ws_task.cancel()
        self.ws_task = None
        self._kiwoom_started = False

        if self._kiwoom_wait_task is None or self._kiwoom_wait_task.done():
            self._kiwoom_wait_task = asyncio.create_task(
                self._resume_kiwoom_after_maintenance(window[1]))
            log.warning(
                "%s; no token/REST/WebSocket request until announced end",
                format_kiwoom_maintenance(window))

    async def _resume_kiwoom_after_maintenance(self, end: datetime):
        """공지 종료 시각까지 1회 대기한 뒤 키움 연결을 시작한다."""
        delay = max(0.0, (end - datetime.now(KST)).total_seconds())
        await asyncio.sleep(delay)
        self._kiwoom_wait_task = None

        # 타이틀을 정상 상태로 되돌린 뒤 딱 한 번 연결한다.
        win = self.views[0].screen.window()
        win._title_clock_suffix = self._title_suffix_before_maintenance or ""
        self._title_suffix_before_maintenance = None
        _set_title_clock_base(win, "[0156] 조건검색실시간")
        await self._start_kiwoom_services()

    async def _start_kiwoom_services(self):
        if self._kiwoom_started:
            return
        maintenance = active_kiwoom_maintenance()
        if maintenance is not None:
            self._pause_for_kiwoom_maintenance(maintenance)
            return
        self._kiwoom_started = True
        asyncio.ensure_future(self._check_ip())  # 시작 즉시 IP 표시
        self.ws_task = asyncio.create_task(self.ws.run(self.rest.tokens.token))
        try:
            self._account_summary = await self.rest.account_summary()
            for v in self.views:
                v.screen.set_account_summary(self._account_summary)
            log.info("account summary loaded: estimated=%s orderable=%s",
                     self._account_summary["estimated_assets"],
                     self._account_summary["cash_orderable"])
        except Exception as e:  # noqa: BLE001
            log.warning("account summary failed: %s", e)
        try:
            self._market = await self.rest.market_info()
            for v in self.views:
                self._inject_market(v)
            if self._rank is not None:
                self._rank.set_market(self._market)
            m = self._market
            log.info("kosdaq %d, single %d, short_overheat %d, liquidation %d, "
                     "nxt %d, misu %d, admin %d",
                     len(m.kosdaq), len(m.single), len(m.short_overheat), len(m.liquidation),
                     len(m.nxt), len(m.misu), len(m.admin))
        except Exception as e:  # noqa: BLE001
            log.warning("market_info failed: %s", e)
        try:
            # ponytail: 시작 시 1회. 자정 넘겨 켜두면 옛 목록 -> 날짜 가드는 필요해지면
            self._limit_cnt = await self.rest.yesterday_limit_counts()
            for v in self.views:
                self._inject_market(v)
            log.info("yesterday limit: %s",
                     ",".join(f"{c}={n}" for c, (n, _) in self._limit_cnt.items()))
        except Exception as e:  # noqa: BLE001
            log.warning("limit_counts failed: %s", e)

    async def start(self):
        for _ in range(int(self._settings.value("cond_windows", 0))):
            self._open_window()  # 지난 세션의 추가 창 복원
        self._restore_session_windows()
        # 분석창이 복원된 경우에는 LS 웹소켓 연결 완료 콜백이 먼저 동기화한다.
        # 분석창이 닫혀 있으면 서버 DB 보완만 즉시 백그라운드에서 시작한다.
        if self._analysis is None:
            self._start_ls_news_gap_sync()
        maintenance = active_kiwoom_maintenance()
        if maintenance is not None:
            self._pause_for_kiwoom_maintenance(maintenance)
            return
        await self._start_kiwoom_services()

    def _restore_session_windows(self):
        """정상 종료 직전에 보이던 분석·순위창을 다시 연다."""
        if str(self._settings.value(
                "session_analysis_visible", "false")).lower() == "true":
            self._on_analysis()
        if str(self._settings.value(
                "session_rank_visible", "false")).lower() == "true":
            self._on_rank()

    def save_session_window_state(self):
        """동반 종료로 창이 숨기기 전에 현재 표시 상태를 저장한다."""
        self._settings.setValue(
            "session_analysis_visible",
            "true" if self._analysis is not None
            and self._analysis.isVisible() else "false",
        )
        self._settings.setValue(
            "session_rank_visible",
            "true" if self._rank is not None
            and self._rank.isVisible() else "false",
        )
        self._settings.setValue("cond_windows", len(self.views) - 1)
        self._settings.sync()

    def _inject_market(self, view: View):
        m = view.screen.model
        if self._limit_cnt is not None:
            m.limit_cnt = self._limit_cnt
            m.refresh_streaks()
        if self._market is None:
            return
        m.kosdaq, m.single, m.short_overheat, m.liquidation, m.nxt, m.misu, m.admin = (
            self._market.kosdaq, self._market.single, self._market.short_overheat,
            self._market.liquidation, self._market.nxt, self._market.misu,
            self._market.admin)
        m.new_today, m.new15, m.new30 = (
            self._market.new_today, self._market.new15, self._market.new30)
        m.shares = self._market.shares
        m.refresh_market_markers()

    async def collect_condition_snapshot(self, seq: str, condition_name: str = "",
                                         market: str = "KRX") -> dict:
        """화면 없이 조건검색 일반 조회를 수행하고 결과만 DB에 저장한다.

        실시간 조건식 등록 목록이나 ConditionScreen 모델을 건드리지 않으므로
        본창의 편입·이탈·정렬·주문 상태와 독립적이다. 시간대 예약은 후속
        시장테마 브리핑 단계에서 이 메서드를 호출해 구성한다.
        """
        key = str(seq)
        started = time.monotonic()
        codes = await self.ws.request_condition_once(key)
        snapshot_id = save_condition_snapshot(
            key, condition_name, codes, market=market, truncated=len(codes) >= 100)
        quote_rows = []
        theme_stats = []
        theme_members = []
        try:
            quote_rows = await self.rest.watch_info(codes)
            save_condition_snapshot_quotes(snapshot_id, quote_rows)
            labels = active_theme_labels()
            quote_by_code = {
                str(row.get("code") or "").removeprefix("A"): row
                for row in quote_rows
            }
            theme_codes: dict[str, list[str]] = {}
            for code in codes:
                for theme in labels.get(code, ()):
                    theme_codes.setdefault(theme, []).append(code)
            stats = []
            for theme, members in theme_codes.items():
                rows = [quote_by_code.get(code, {}) for code in members]
                rates = [float(row.get("rate") or 0) for row in rows]
                leaders = sorted(
                    zip(members, rows),
                    key=lambda item: (float(item[1].get("rate") or 0), item[0]),
                    reverse=True,
                )
                leader_code, leader = leaders[0]
                for rank, (member_code, member_row) in enumerate(leaders[:3], 1):
                    theme_members.append({
                        "theme_name": theme,
                        "theme_rank": rank,
                        "stock_code": member_code,
                        "stock_name": str(member_row.get("name") or ""),
                        "change_rate": float(member_row.get("rate") or 0),
                    })
                upper_count = sum(
                    int(row.get("upper") or 0) > 0
                    and int(row.get("price") or 0) >= int(row.get("upper") or 0)
                    for row in rows
                )
                stats.append({
                    "theme_name": theme,
                    "member_count": len(members),
                    "upper_count": upper_count,
                    "average_rate": sum(rates) / len(rates) if rates else 0,
                    "top_rate": max(rates) if rates else 0,
                    "trading_value": sum(
                        int(row.get("trading_value") or 0) for row in rows),
                    "leader_stock_code": leader_code,
                    "leader_stock_name": str(leader.get("name") or ""),
                })
            theme_stats = stats
            save_condition_theme_stats(snapshot_id, stats)
            save_condition_theme_members(snapshot_id, theme_members)
            log.info("condition theme stats saved: snapshot=%d themes=%d quotes=%d",
                     snapshot_id, len(stats), len(quote_rows))
        except Exception as error:  # noqa: BLE001
            # 조건검색 결과 자체는 보존하고, 시세/테마 보강 실패만 경고한다.
            log.warning("condition snapshot enrichment failed: snapshot=%d error=%s",
                        snapshot_id, error)
        result = {
            "snapshot_id": snapshot_id,
            "condition_seq": key,
            "condition_name": condition_name,
            "market": market,
            "codes": codes,
            "stock_count": len(codes),
            "quote_count": len(quote_rows),
            "quotes": quote_rows,
            "theme_stats": theme_stats,
            "theme_members": theme_members,
            "truncated": len(codes) >= 100,
        }
        log.info("background condition snapshot: seq=%s name=%s market=%s "
                 "codes=%d truncated=%s elapsed=%.2fs",
                 key, condition_name, market, len(codes), result["truncated"],
                 time.monotonic() - started)
        return result

    def _background_condition_targets(self) -> list[tuple[str, str]]:
        """예약 수집 대상 조건식을 반환한다.

        ``background_condition_seqs``가 비어 있으면 현재 열려 있는 일반
        조건검색을 사용한다. 순위/보유종목 메뉴는 조건식 스냅샷 대상에서
        제외한다.
        """
        raw = str(self._settings.value("background_condition_seqs", "") or "")
        configured = [item.strip() for item in raw.split(",") if item.strip()]
        names = {str(seq): str(name) for seq, name in self._cond_items}
        remembered_name = str(
            self._settings.value("background_condition_name", "") or ""
        ).strip()
        remembered = str(
            self._settings.value("background_condition_seq", "")
            or self._settings.value("last_condition", "")
            or ""
        ).strip()
        # 조건식 순서가 바뀌어 seq가 달라져도 저장된 이름으로 현재 seq를
        # 다시 찾는다. 이름이 변경된 경우에는 기존 seq를 보조로 사용한다.
        if not configured and remembered_name:
            renamed_seq = next(
                (seq for seq, name in names.items() if name == remembered_name), "")
            if renamed_seq:
                remembered = renamed_seq
        if not configured and remembered:
            configured = [remembered]
        if configured:
            return [(seq, names.get(seq, seq)) for seq in dict.fromkeys(configured)]
        # 기본은 첫 번째 본창의 현재 조건식 하나만 사용한다. 추가 창까지
        # 자동으로 합치면 사용자가 의도하지 않은 조건식이 함께 수집될 수
        # 있으므로, 여러 조건식은 설정값으로 명시한 경우에만 허용한다.
        view = self.views[0] if self.views else None
        seq = str(view.seq or "") if view else ""
        if not seq or seq in RANK_SEQS or seq == HOLDINGS_SEQ:
            return []
        return [(seq, names.get(seq, seq))]

    def _rotation_condition_target(self) -> tuple[str, str]:
        """순환매 전용 조건식을 본창 선택과 독립적으로 정확히 찾는다."""
        saved_seq = str(self._settings.value(
            "rotation_condition_seq", ROTATION_CONDITION_SEQ
        ) or ROTATION_CONDITION_SEQ).strip()
        saved_name = str(self._settings.value(
            "rotation_condition_name", ROTATION_CONDITION_NAME
        ) or ROTATION_CONDITION_NAME).strip()
        condition_by_seq = {
            str(seq): str(name) for seq, name in self._cond_items
        }
        if not condition_by_seq:
            return saved_seq, saved_name
        if condition_by_seq.get(saved_seq) == saved_name:
            return saved_seq, saved_name

        # 조건식 순서가 바뀐 경우에만 전체 이름이 정확히 같은 항목으로
        # seq를 보정한다. '8%' 포함 여부 같은 부분 문자열 판정은 하지 않는다.
        exact_matches = [
            (seq, name) for seq, name in condition_by_seq.items()
            if name == saved_name
        ]
        if len(exact_matches) != 1:
            if not exact_matches:
                raise RuntimeError(
                    f"순환매 전용 조건식을 찾을 수 없습니다: {saved_name}")
            raise RuntimeError(
                f"이름이 같은 순환매 조건식이 {len(exact_matches)}개입니다: "
                f"{saved_name}")
        resolved_seq, resolved_name = exact_matches[0]
        self._settings.setValue("rotation_condition_seq", resolved_seq)
        self._settings.setValue("rotation_condition_name", resolved_name)
        self._settings.sync()
        log.info("rotation condition seq remapped: %s -> %s name=%s",
                 saved_seq, resolved_seq, resolved_name)
        return resolved_seq, resolved_name

    def _recent_rotation_batch_result(
            self, max_age_seconds: float) -> dict | None:
        """방금 정상 저장된 8% 배치가 있으면 버튼 결과 형식으로 반환한다."""
        rows = recent_condition_snapshots(
            limit=1, condition_name=ROTATION_BATCH_NAME)
        if not rows:
            return None
        snapshot = rows[0]
        try:
            captured = datetime.fromisoformat(str(snapshot["captured_at"]))
            now = datetime.now(captured.tzinfo) if captured.tzinfo else datetime.now()
            age = (now - captured).total_seconds()
        except (TypeError, ValueError):
            return None
        if age < -5 or age > float(max_age_seconds):
            return None
        snapshot_id = int(snapshot["snapshot_id"])
        return {
            "snapshot_id": snapshot_id,
            "codes": int(snapshot["stock_count"] or 0),
            "themes": len(condition_theme_stats(snapshot_id)),
            "captured_at": str(snapshot["captured_at"]),
            "reused": True,
            "age_seconds": max(0.0, age),
        }

    @staticmethod
    def _background_condition_slot(now: datetime) -> str:
        """현재 시각이 예약 조회 구간이면 슬롯명을, 아니면 빈 값을 반환한다."""
        if _krx_holiday_reason(now.date()):
            return ""
        minute = now.hour * 60 + now.minute
        # 30초 타이머가 한 번 놓쳐도 2분 창 안에서 한 번만 실행한다.
        for label, target in (("0930", 9 * 60 + 30),
                              ("1100", 11 * 60),
                              ("1520", 15 * 60 + 20)):
            if target <= minute <= target + 1:
                return label
        return ""

    def _run_background_condition_schedule(self, startup: bool = False):
        """예약 시각에 순환매 전용 조건식을 백그라운드 수집한다."""
        if self._background_condition_task and not self._background_condition_task.done():
            return
        now = datetime.now()
        slot = self._background_condition_slot(now)
        if startup and not slot:
            # 앱을 예약 시각 이후에 켜도 오늘 결과가 완전히 비지 않도록
            # 정규장 시간에는 현재 시각 기준 보완 스냅샷을 한 번 만든다.
            if _is_krx_market_open(now):
                slot = "START"
            else:
                # 15:30 이후에도 KRX 조건검색 일반조회는 최종 편입목록을
                # 반환할 수 있으므로, NXT 종료 전까지 마감 보완을 허용한다.
                seconds = now.hour * 3600 + now.minute * 60 + now.second
                if 15 * 3600 + 30 * 60 <= seconds < 20 * 3600:
                    slot = "START_AFTER"
        if not slot:
            return
        day_key = now.strftime("%Y%m%d")
        marker = (day_key, slot)
        if marker in self._background_condition_slots:
            return
        try:
            targets = [self._rotation_condition_target()]
        except RuntimeError as error:
            log.warning("background rotation snapshot skipped: %s", error)
            self._background_condition_slots.add(marker)
            return
        if not targets:
            log.info("background condition snapshot skipped: no target condition")
            self._background_condition_slots.add(marker)
            return
        self._background_condition_slots.add(marker)
        self._background_condition_task = asyncio.ensure_future(
            self._collect_scheduled_rotation_snapshot(targets, slot))

    async def _collect_scheduled_rotation_snapshot(
            self, targets: list[tuple[str, str]], slot: str) -> dict | None:
        """자동수집 0건을 최근 정상값 또는 60초 후 재조회로 보완한다."""
        result = await self._collect_background_condition_targets(targets, slot)
        if result:
            result["reused"] = False
            return result

        fallback = self._recent_rotation_batch_result(5 * 60)
        if fallback:
            fallback["reuse_reason"] = "자동수집 0건 · 5분 이내 정상 수집값"
            log.warning(
                "scheduled rotation empty response fallback: "
                "slot=%s snapshot=%d age=%.1fs codes=%d",
                slot, fallback["snapshot_id"], fallback["age_seconds"],
                fallback["codes"])
            return fallback

        log.warning(
            "scheduled rotation empty response: slot=%s retry in 60s", slot)
        await asyncio.sleep(60)
        retry_slot = slot + "_RETRY"
        result = await self._collect_background_condition_targets(
            targets, retry_slot)
        if result:
            result["reused"] = False
            log.info("scheduled rotation retry succeeded: slot=%s codes=%d",
                     retry_slot, result["codes"])
            return result

        fallback = self._recent_rotation_batch_result(5 * 60)
        if fallback:
            fallback["reuse_reason"] = "자동 재조회 0건 · 최근 정상 수집값"
            log.warning(
                "scheduled rotation retry fallback: "
                "slot=%s snapshot=%d age=%.1fs codes=%d",
                retry_slot, fallback["snapshot_id"],
                fallback["age_seconds"], fallback["codes"])
            return fallback
        log.warning(
            "scheduled rotation snapshot unavailable after retry: slot=%s",
            retry_slot)
        return None

    async def _collect_background_condition_targets(
            self, targets: list[tuple[str, str]], slot: str):
        started = time.monotonic()
        combined_codes = []
        combined_seen = set()
        captured_names = []
        combined_truncated = False
        combined_quotes = []
        try:
            for seq, name in targets:
                try:
                    result = await self.collect_condition_snapshot(
                        seq, name, "KRX")
                    combined_truncated = combined_truncated or result["truncated"]
                    captured_names.append(name)
                    combined_quotes.extend(result.get("quotes", ()))
                    for code in result["codes"]:
                        if code not in combined_seen:
                            combined_seen.add(code)
                            combined_codes.append(code)
                except asyncio.CancelledError:
                    raise
                except Exception as error:  # noqa: BLE001
                    log.warning("background condition snapshot failed: seq=%s error=%s",
                                seq, error)
            if combined_codes:
                today = datetime.now().strftime("%Y%m%d")
                batch_seq = f"BATCH:{today}:{slot}"
                batch_name = "시장테마 브리핑 · " + ", ".join(captured_names)
                save_condition_snapshot(
                    batch_seq, batch_name, combined_codes, market="KRX",
                    truncated=combined_truncated)
                batch_quote_by_code = {
                    str(row.get("code") or "").removeprefix("A"): row
                    for row in combined_quotes
                }
                batch_theme_codes: dict[str, list[str]] = {}
                labels = active_theme_labels()
                for code in combined_codes:
                    for theme in labels.get(code, ()):
                        batch_theme_codes.setdefault(theme, []).append(code)
                batch_rows = []
                batch_members = []
                for theme, members in batch_theme_codes.items():
                    rows = [batch_quote_by_code.get(code, {}) for code in members]
                    rates = [float(row.get("rate") or 0) for row in rows]
                    leaders = sorted(
                        zip(members, rows),
                        key=lambda item: (float(item[1].get("rate") or 0), item[0]),
                        reverse=True,
                    )
                    leader_code, leader = leaders[0]
                    for rank, (member_code, member_row) in enumerate(leaders[:3], 1):
                        batch_members.append({
                            "theme_name": theme,
                            "theme_rank": rank,
                            "stock_code": member_code,
                            "stock_name": str(member_row.get("name") or ""),
                            "change_rate": float(member_row.get("rate") or 0),
                        })
                    batch_rows.append({
                        "theme_name": theme,
                        "member_count": len(members),
                        "upper_count": sum(
                            int(row.get("upper") or 0) > 0
                            and int(row.get("price") or 0) >= int(row.get("upper") or 0)
                            for row in rows),
                        "average_rate": sum(rates) / len(rates) if rates else 0,
                        "top_rate": max(rates) if rates else 0,
                        "trading_value": sum(
                            int(row.get("trading_value") or 0)
                            for row in rows),
                        "leader_stock_code": leader_code,
                        "leader_stock_name": str(leader.get("name") or ""),
                    })
                # 통합 스냅샷은 방금 삽입된 마지막 배치 행을 찾아 연결한다.
                batch_rows_saved = recent_condition_snapshots(batch_seq, limit=1)
                if batch_rows_saved:
                    batch_id = batch_rows_saved[0]["snapshot_id"]
                    save_condition_snapshot_quotes(batch_id, combined_quotes)
                    save_condition_theme_stats(batch_id, batch_rows)
                    save_condition_theme_members(batch_id, batch_members)
                    candidate_count = save_next_day_candidates(batch_id)
                    log.info("next-day candidates saved: snapshot=%d count=%d",
                             batch_id, candidate_count)
                log.info("background condition batch saved: slot=%s codes=%d "
                         "sources=%d", slot, len(combined_codes), len(captured_names))
                return {
                    "snapshot_id": int(batch_id) if batch_rows_saved else 0,
                    "codes": len(combined_codes),
                    "themes": len(batch_rows),
                }
            return None
        finally:
            log.info("background condition slot finished: slot=%s targets=%d elapsed=%.2fs",
                     slot, len(targets), time.monotonic() - started)

    async def collect_rotation_snapshot_now(self):
        """8% 이상 전용 조건식을 일반조회해 순환매 스냅샷을 만든다."""
        # 같은 조건식의 정상 수집이 1분 안에 끝났으면 다시 API를 호출하지
        # 않는다. 현재가·테마 보강까지 끝난 배치이므로 그대로 분석에 반영한다.
        recent = self._recent_rotation_batch_result(60)
        if recent:
            recent["reuse_reason"] = "1분 이내 정상 수집값"
            log.info("rotation recent batch reused: snapshot=%d age=%.1fs codes=%d",
                     recent["snapshot_id"], recent["age_seconds"],
                     recent["codes"])
            return recent

        # 앱 시작 직후 예약 수집과 버튼 클릭이 겹치면 같은 조건식 일반조회가
        # 연달아 전송되어 두 번째 응답이 0건으로 올 수 있다. 진행 중인 예약
        # 수집이 있으면 그 결과를 먼저 공유한다.
        background_task = self._background_condition_task
        if background_task is not None and not background_task.done():
            scheduled_result = await asyncio.shield(background_task)
            if scheduled_result:
                result = dict(scheduled_result)
                result["reused"] = True
                result["reuse_reason"] = "진행 중이던 예약 수집 완료값"
                return result

        target = self._rotation_condition_target()
        slot = "ROTATION_" + datetime.now().strftime("%H%M%S")
        result = await self._collect_background_condition_targets(
            [target], slot)
        if result:
            result["reused"] = False
            return result

        # 키움이 짧은 간격의 중복 일반조회에 0건을 반환한 경우, 오류로
        # 끝내지 않고 5분 이내 마지막 정상 배치를 사용한다.
        fallback = self._recent_rotation_batch_result(5 * 60)
        if fallback:
            fallback["reuse_reason"] = "중복조회 0건 · 최근 정상 수집값"
            log.warning(
                "rotation empty response fallback: snapshot=%d age=%.1fs codes=%d",
                fallback["snapshot_id"], fallback["age_seconds"],
                fallback["codes"])
            return fallback
        raise RuntimeError(
            f"순환매 조건검색 결과가 없습니다: {target[1]} (seq={target[0]})")

    # --- 웹소켓 콜백 라우팅 -------------------------------------------------
    def _on_condition_list(self, items):
        self._cond_items = items
        log.info("condition list: %d", len(items))
        for v in self.views:
            v.on_condition_list(items)
        self._finish_condition_reload(len(items))

    def _on_condition_event(self, seq: str, code: str, is_insert: bool):
        for v in self.views:
            if v.seq == str(seq):
                v.on_event(code, is_insert)

    def _on_condition_snapshot(self, seq: str, codes: list[str]):
        for v in self.views:
            if v.seq == str(seq):
                v.on_snapshot(codes)

    def _on_real(self, code: str, fields: dict):
        # 모델도 누적거래량의 KRX/NXT/통합 출처를 알아야 늦은 REST 값과
        # 사용자가 실제로 시장을 전환한 경우를 구분할 수 있다.
        source = fields.get("_real_suffix", None)
        for v in self.views:
            expected = "_NX" if v.seq == NXT_RATE_SEQ else self.ws.real_suffix
            # REST/내부 갱신(source=None)은 기존처럼 전달하고, 웹소켓은 시장 출처가 맞는 창에만 전달.
            if code in v.screen.model.rows and (source is None or source == expected):
                v.screen.on_tick(code, fields)
        if "bid_qty" in fields:
            self._check_balance_sell(code, int(fields.get("bid_qty") or 0))

    def _on_account_order_event(self, event: dict):
        """앱/영웅문 어느 쪽 주문이든 계좌 체결 이벤트를 자동취소에 반영한다."""
        code = str(event.get("code") or "")
        if code:
            audit_log.info(
                "account order event code=%s side=%s status=%s order=%s "
                "original=%s order_qty=%s fill_qty=%s remaining=%s "
                "fill_id=%s exchange=%s",
                code, event.get("side"), event.get("status"),
                event.get("order_no"), event.get("original_order_no"),
                event.get("order_qty"), event.get("fill_qty"),
                event.get("remaining_qty"), event.get("fill_id"),
                event.get("exchange"))
        self.orders.on_order_event(event)
        if not code:
            return
        if (
            code in self._emergency_locked
            and event.get("side") == "buy"
            and int(event.get("fill_qty") or 0) > 0
        ):
            latest_price = next((
                int(view.screen.model.rows[code].get("bid_price4") or 0)
                for view in self.views if code in view.screen.model.rows
            ), 0)
            self._queue_emergency_reconcile(code, latest_price or None)
        if event.get("side") != "buy":
            return
        order_no = str(event.get("order_no") or "")
        if not order_no or not code:
            return
        if code not in self._account_auto_cancel_armed:
            return
        fill_qty = max(0, int(event.get("fill_qty") or 0))
        if fill_qty <= 0:
            return
        fill_id = str(event.get("fill_id") or "").strip()
        if fill_id:
            token = (code, order_no, fill_id)
            if token in self._account_auto_cancel_fill_ids:
                return
            self._account_auto_cancel_fill_ids.add(token)
        order_key = (code, order_no)
        previous_filled = self._account_auto_cancel_filled.get(order_key, 0)
        order_qty = max(0, int(event.get("order_qty") or 0))
        remaining_qty = event.get("remaining_qty")
        if order_qty and remaining_qty is not None:
            # 주문을 앱에서 냈는지 영웅문에서 냈는지와 무관하게, 이벤트의
            # 주문수량-잔량을 누적체결량의 권위값으로 사용한다. 셀을 늦게
            # 켰거나 체결 이벤트 일부를 놓쳐도 주문별 100주를 판정할 수 있다.
            filled = max(
                previous_filled,
                order_qty - max(0, int(remaining_qty)),
            )
        else:
            filled = previous_filled + fill_qty
        self._account_auto_cancel_filled[order_key] = filled
        log.warning(
            "account auto-cancel fill code=%s order=%s fill=%s order_total=%s/100",
            code, order_no, fill_qty, filled)
        if filled < 100 or order_key in self._account_auto_cancel_tasks:
            return
        task = asyncio.ensure_future(
            self._auto_cancel_account(
                code, order_no, remaining_qty, event.get("exchange")))
        self._account_auto_cancel_tasks[order_key] = task
        task.add_done_callback(
            lambda _task, key=order_key:
            self._account_auto_cancel_tasks.pop(key, None))

    async def _auto_cancel_account(
            self, code: str, order_no: str, remaining_qty: int | None,
            exchange: str | None = None):
        try:
            # 체결 이벤트의 주문번호·잔량으로 직접 취소한다. 계좌 미체결 조회가
            # 지연되거나 일부만 반환되어도 100주를 체결한 해당 주문을 놓치지 않는다.
            if remaining_qty is not None and exchange:
                qty = max(0, int(remaining_qty))
                if qty:
                    # 키움 취소수량 0은 이 원주문번호의 잔량 전부를 뜻한다.
                    await self.rest.cancel_order(
                        code, order_no, 0, str(exchange))
                    count = 1
                else:
                    count = 0
            else:
                # 잔량 또는 거래소 FID가 없는 경우 계좌 조회로 보완한다.
                count, qty = await self.rest.cancel_open_buy_order(code, order_no)
            log.warning(
                "account auto-cancel code=%s event_order=%s "
                "orders=%s qty=%s",
                code, order_no, count, qty)
        except Exception:  # noqa: BLE001
            log.exception(
                "account auto-cancel failed code=%s event_order=%s",
                code, order_no)
            return
        # 자동취소 감시는 유지한다. 다른 분할 주문도 각자 100주가 체결되면
        # 그 주문번호의 잔량만 같은 방식으로 취소한다.

    def _set_account_auto_cancel(self, code: str, armed: bool):
        code = str(code or "").strip().split("_")[0].removeprefix("A")
        if not code:
            return
        if armed:
            self._account_auto_cancel_armed.add(code)
            self._account_auto_cancel_filled = {
                key: qty for key, qty in self._account_auto_cancel_filled.items()
                if key[0] != code}
            self._account_auto_cancel_fill_ids = {
                token for token in self._account_auto_cancel_fill_ids
                if token[0] != code}
        else:
            self._account_auto_cancel_armed.discard(code)
            self._account_auto_cancel_filled = {
                key: qty for key, qty in self._account_auto_cancel_filled.items()
                if key[0] != code}
            self._account_auto_cancel_fill_ids = {
                token for token in self._account_auto_cancel_fill_ids
                if token[0] != code}
        for view in self.views:
            view.screen.model.set_account_auto_cancel_armed(code, armed)
        log.warning(
            "account auto-cancel %s code=%s",
            "armed" if armed else "disarmed", code)

    def _set_balance_sell(self, code: str, setting):
        if setting is None:
            self._balance_sell_settings.pop(code, None)
            self._balance_sell_stage.pop(code, None)
            self._balance_sell_date.pop(code, None)
        else:
            self._balance_sell_settings[code] = dict(setting)
            self._balance_sell_stage[code] = 0
            self._balance_sell_date[code] = datetime.now().strftime("%Y%m%d")
        for view in self.views:
            if code not in view.screen.model.rows:
                continue
            view.screen.model.balance_sell_settings.pop(code, None)
            view.screen.model.balance_sell_stage.pop(code, None)
            view.screen.model.balance_alert_stage.pop(code, None)
            view.screen.model.balance_alert_ticks.pop(code, None)
            if setting is not None:
                view.screen.model.balance_sell_settings[code] = dict(setting)
                view.screen.model.balance_sell_stage[code] = 0
            row = view.screen.model.codes.index(code)
            cell = view.screen.model.index(row, BALANCE_SELL_COL)
            view.screen.model.dataChanged.emit(cell, cell)
        audit_log.info(
            "balance sell setting code=%s setting=%s", code, setting)

    def _check_balance_sell(self, code: str, bid_qty: int):
        setting = self._balance_sell_settings.get(code)
        if not setting:
            return
        today = datetime.now().strftime("%Y%m%d")
        if self._balance_sell_date.get(code) != today:
            self._set_balance_sell(code, None)
            audit_log.info(
                "expired balance sell setting cleared code=%s", code)
            return
        krx_state, _, reason = _market_session_states(datetime.now())
        if reason or krx_state != "정규장":
            return
        stage = self._balance_sell_stage.get(code, 0)
        target = max((
            number for number, key in enumerate(
                ("first", "second", "third"), 1)
            if int(setting.get(key, 0)) > 0
            and bid_qty <= int(setting[key])
        ), default=0)
        if target <= stage:
            return
        ratio = float(setting.get(
            ("first_ratio", "second_ratio", "third_ratio")[target - 1],
            (0.0, 0.5, 1.0)[target - 1]))
        if ratio <= 0:
            audit_log.info(
                "balance sell stage skipped code=%s stage=%s "
                "bid_qty=%s threshold=%s ratio=%s",
                code, target, bid_qty,
                setting.get(("first", "second", "third")[target - 1]),
                ratio)
            self._complete_balance_stage(code, target)
            return
        market_sell = bool(setting.get("market_sell", False))
        row = next((
            view.screen.model.rows[code]
            for view in self.views if code in view.screen.model.rows), {})
        if market_sell:
            price = 0
        elif target == 3:
            # 매수 4호가 잔량/존재 여부와 무관하게 3틱 아래 가격으로
            # 지정가를 즉시 낸다. 실제 4호가가 비면 가격을 직접 계산한다.
            price = _balance_stage3_limit_price(row)
        else:
            price = int(row.get("upper") or 0)
        if not market_sell and price <= 0:
            log.warning(
                "balance sell reference price unavailable "
                "code=%s stage=%s bid_qty=%s",
                code, target, bid_qty)
            return
        running = self._balance_sell_tasks.get(code)
        if running and not running.done():
            return
        audit_log.info(
            "balance sell stage triggered code=%s stage=%s "
            "bid_qty=%s threshold=%s ratio=%s order_type=%s price=%s",
            code, target, bid_qty,
            setting.get(("first", "second", "third")[target - 1]),
            ratio, "market" if market_sell else "limit", price)
        task = asyncio.ensure_future(
            self._execute_balance_stage(
                code, target, ratio, price, bid_qty, market_sell))
        self._balance_sell_tasks[code] = task
        task.add_done_callback(
            lambda _task, stock_code=code:
            self._balance_sell_tasks.pop(stock_code, None))

    def _complete_balance_stage(self, code: str, target: int):
        if target <= self._balance_sell_stage.get(code, 0):
            return
        self._balance_sell_stage[code] = target
        for view in self.views:
            view.screen.set_balance_sell_stage(code, target)
        _beep(f"balance{target}")

    async def _execute_balance_stage(
            self, code: str, target: int, ratio: float,
            price: int, bid_qty: int, market_sell: bool):
        try:
            sold = await self._sell_account_position(
                code, ratio, price, f"잔량 {target}단계", market_sell)
        except Exception:  # noqa: BLE001
            log.exception(
                "balance sell failed; stage remains pending "
                "code=%s stage=%s",
                code, target)
            return
        if sold <= 0:
            # 보유가 아직 잔고에 반영되지 않은 경우 다음 호가에서 재조회한다.
            return
        self._complete_balance_stage(code, target)
        log.warning(
            "balance sell completed code=%s stage=%s bid_qty=%s sold=%s",
            code, target, bid_qty, sold)

    async def _sell_account_position(
            self, code: str, ratio: float, price: int, reason: str,
            market_sell: bool = False) -> int:
        """실제 계좌 매매가능수량을 조회해 지정 비율만큼 매도한다."""
        position = await self.rest.holding_position(code)
        held = max(0, int(position.get("held_qty") or 0))
        sellable = max(0, int(position.get("sellable_qty") or 0))
        if sellable <= 0:
            log.warning(
                "%s account sell ignored code=%s held=%s sellable=0",
                reason, code, held)
            return 0
        ratio = min(1.0, max(0.0, float(ratio)))
        qty = sellable if ratio >= 1.0 else max(1, int(sellable * ratio))
        result = await self.rest.sell_order(
            code, qty, int(price), market=market_sell)
        log.warning(
            "%s account sell sent code=%s held=%s sellable=%s qty=%s "
            "order_type=%s price=%s order_no=%s",
            reason, code, held, sellable, qty,
            "시장가" if market_sell else "지정가",
            "" if market_sell else price, result["order_no"])
        return qty

    def _emergency_exit(self, code: str, price: int, order_enabled: bool):
        # 새 청산키 입력은 새 결과이므로 이전 사용자의 상태 확인 기록을 해제한다.
        self._emergency_status_dismissed.discard(code)
        if price > 0:
            self._emergency_prices[code] = int(price)
        if order_enabled:
            self._emergency_locked.add(code)
            self.orders.stop_local_submissions(code)
        self._queue_emergency_reconcile(code, price, order_enabled)

    def _acknowledge_order_status(self, code: str):
        """종료 상태를 모든 창에서 지우고 진행 중인 이전 결과의 재표시를 막는다."""
        self._emergency_status_dismissed.add(code)
        for view in self.views:
            if (
                code in view.screen.model.rows
                and view.screen.model.order_status.get(code) == "대상없음"
            ):
                view.screen.set_order_state(code, "", "", False)

    def _queue_emergency_reconcile(
            self, code: str, price: int | None = None,
            order_enabled: bool | None = None):
        if price and price > 0:
            self._emergency_prices[code] = int(price)
        running = self._emergency_tasks.get(code)
        if running and not running.done():
            self._emergency_recheck.add(code)
            return
        enabled = (
            self._order_enabled if order_enabled is None else order_enabled)
        task = asyncio.ensure_future(self._emergency_exit_async(
            code, self._emergency_prices.get(code, 0), enabled))
        self._emergency_tasks[code] = task
        task.add_done_callback(
            lambda _task, stock_code=code:
            self._finish_emergency_reconcile(stock_code))

    def _finish_emergency_reconcile(self, code: str):
        self._emergency_tasks.pop(code, None)
        if code in self._emergency_recheck:
            self._emergency_recheck.discard(code)
            self._queue_emergency_reconcile(code)

    async def _emergency_exit_async(
            self, code: str, price: int, order_enabled: bool):
        try:
            position = await self.rest.holding_position(code)
            open_buys = await self.rest.open_buy_orders(code)
        except Exception as error:  # noqa: BLE001
            log.exception("emergency account query failed code=%s", code)
            for view in self.views:
                if code in view.screen.model.rows:
                    view.screen.set_order_state(
                        code, "잔고오류",
                        f"상태 청산 보류 · 계좌잔고 조회 오류 · {error}", False)
            return
        batch = self.orders.batches.get(code)
        tracked_filled = max(0, int(batch.total_filled)) if batch else 0
        local_pending_qty = sum((
            max(0, int(child.remaining_qty))
            for child in batch.children
            if not child.done), 0) if batch else 0
        pending_qty = sum(
            max(0, int(order.get("remaining_qty") or 0))
            for order in open_buys)
        held_qty = max(0, int(position.get("held_qty") or 0))
        sellable_qty = max(0, int(position.get("sellable_qty") or 0))
        if held_qty <= 0 and pending_qty <= 0:
            if code not in self._emergency_status_dismissed:
                for view in self.views:
                    if code in view.screen.model.rows:
                        view.screen.set_order_state(
                            code, "대상없음",
                            "상태 청산 대상 없음 · 계좌 보유수량 0주",
                            False)
            else:
                log.info(
                    "emergency no-position status suppressed after "
                    "acknowledgement code=%s", code)
            log.warning("emergency exit ignored no-position code=%s", code)
            return
        if not order_enabled:
            for view in self.views:
                if code in view.screen.model.rows:
                    view.screen.set_order_state(
                        code, "허용꺼짐",
                        (f"상태 청산 차단 · 주문허용 꺼짐 · "
                         f"미체결 {pending_qty:,}주 / "
                         f"보유 {held_qty:,}주"),
                        pending_qty > 0)
            log.warning(
                "emergency exit blocked order-disabled code=%s "
                "pending=%s held=%s sellable=%s",
                code, pending_qty, held_qty, sellable_qty)
            return
        if int(price) <= 0:
            for view in self.views:
                if code in view.screen.model.rows:
                    view.screen.set_order_state(
                        code, "호가대기",
                        "상태 청산 보류 · 최신 매수 4호가 없음",
                        pending_qty > 0)
            log.warning(
                "emergency exit blocked no-bid4 code=%s pending=%s held=%s",
                code, pending_qty, held_qty)
            return
        if sellable_qty <= 0 and pending_qty <= 0:
            for view in self.views:
                if code in view.screen.model.rows:
                    view.screen.set_order_state(
                        code, "매도가능0",
                        (f"상태 보유 {held_qty:,}주 · 매도가능수량 0주 "
                         "(기존 매도주문 확인)"),
                        False)
            log.warning(
                "emergency exit ignored sellable-zero code=%s held=%s",
                code, held_qty)
            return
        sold_qty = 0
        cancelled_count = 0
        cancelled_qty = 0
        try:
            if sellable_qty > 0:
                result = await self.rest.sell_order(
                    code, sellable_qty, int(price))
                sold_qty = sellable_qty
                log.warning(
                    "emergency account sell sent code=%s held=%s "
                    "sellable=%s price=%s order_no=%s",
                    code, held_qty, sellable_qty, price, result["order_no"])
            cancelled_count, cancelled_qty = (
                await self.rest.cancel_open_buy_orders(code))
        except Exception as error:  # noqa: BLE001
            log.exception(
                "emergency cancel/sell failed code=%s price=%s "
                "pending=%s held=%s sellable=%s",
                code, price, pending_qty, held_qty, sellable_qty)
            for view in self.views:
                if code in view.screen.model.rows:
                    view.screen.set_order_state(
                        code, "오류", f"상태 긴급정리 오류 · {error}", False)
            return
        for view in self.views:
            if code in view.screen.model.rows:
                view.screen.set_order_state(
                    code,
                    "긴급정리" if sold_qty else "매수취소",
                    (f"상태 계좌 미체결 {cancelled_count:,}건 "
                     f"{cancelled_qty:,}주 취소 · "
                     f"계좌 {sold_qty:,}주 매도 추적 중"),
                    False)
        log.warning(
            "emergency cancel+sell requested code=%s price=%s "
            "pending=%s held=%s sellable=%s tracked_filled=%s "
            "local_pending=%s "
            "cancelled_orders=%s cancelled_qty=%s",
            code, price, pending_qty, held_qty, sellable_qty, tracked_filled,
            local_pending_qty, cancelled_count, cancelled_qty)

    def _on_vi(self, code: str, active: bool, price: int):
        hit = False
        for v in self.views:
            if code in v.screen.model.rows:
                v.screen.model.set_vi(code, active, price)
                hit = True
        if active and hit:
            asyncio.ensure_future(self._vi_fetch(code))

    async def _vi_fetch(self, code: str):
        try:
            for row in await self.rest.watch_info([code], exp=True):
                self._on_real(row["code"], row)
        except Exception as e:  # noqa: BLE001
            log.warning("vi_fetch %s: %s", code, e)

    # --- 공유 자원 ----------------------------------------------------------
    async def clear_condition_if_sole(self, seq: str, me: View):
        """다른 창이 같은 조건을 안 쓰면 CNSRCLR. 쓰면 등록 유지(실시간 공유)."""
        if not any(v is not me and v.seq == str(seq) for v in self.views):
            await self.ws.clear_condition(seq)

    def queue_real(self, code: str, add: bool, suffix: str = None):
        # code/add/suffix는 호출부 의미를 드러내기 위해 유지한다. 실제 목표 상태는
        # 이벤트 횟수가 아니라 활성 창의 model.codes에서 다시 계산한다.
        self._real_dirty = True
        if not (self._reg_task and not self._reg_task.done()):
            self._reg_task = asyncio.ensure_future(self._flush_real())

    def force_real_sync(self):
        """조건 전환/창 닫기 뒤 서버 등록도 전량 재확인한다."""
        self._real_force_pending = True
        self._real_dirty = True
        if not (self._reg_task and not self._reg_task.done()):
            self._reg_task = asyncio.ensure_future(self._flush_real())

    def _desired_real_refs(self) -> Counter:
        """현재 활성 창의 보이는 행을 실시간 등록 우선순위대로 모은다.

        서버의 95종목 상한에 닿으면 뒤에서 잘리므로 보유종목을 가장 먼저,
        최근 60초 체결이 꼭 필요한 거래대금 상위 화면을 그다음에 둔다.
        """
        refs = Counter()
        priority = {HOLDINGS_SEQ: 0, TVAL_SEQ: 1}
        views = sorted(
            self.views, key=lambda view: priority.get(view.seq, 2))
        for view in views:
            suffix = view._real_suffix()
            codes = view.screen.model.codes
            if view.seq == TVAL_SEQ:
                # 상한이 보유종목만으로 거의 찬 경우에도 거래대금 상위 순번부터 살린다.
                rows = view.screen.model.rows
                codes = sorted(
                    codes,
                    key=lambda code: int(
                        rows.get(code, {}).get("qrank") or 1_000_000))
            for code in codes:
                refs[(code, suffix)] += 1
        return refs

    async def _flush_real(self):
        while True:
            await asyncio.sleep(0.3)
            self._real_dirty = False
            force = self._real_force_pending
            self._real_force_pending = False
            await self.ws.sync_real_refs(self._desired_real_refs(), force=force)
            # 위 await 중 화면이 다시 바뀌면 dirty가 켜진다. 후속 상태도 빠짐없이 반영한다.
            if not self._real_dirty and not self._real_force_pending:
                return

    def _on_single_poll(self):
        codes = sorted({c for v in self.views if v.seq != NXT_RATE_SEQ
                        for c in v.screen.model.codes if c in v.screen.model.single})
        if codes and not (self._single_task and not self._single_task.done()):
            self._single_task = asyncio.ensure_future(self._poll_single(codes))

    def ensure_prev_vol(self, model):
        """전일거래량이 0인 종목을 제한된 저우선순위 백필 큐에 넣는다."""
        for code in list(model.codes):
            if (model.rows[code].get("prev_vol", 0) == 0
                    and code not in self._prevvol_pending
                    and code not in self._prevvol_done):
                self._prevvol_pending.add(code)
                self._prevvol_queue.append(code)
        self._start_prevvol_workers()

    def _start_prevvol_workers(self):
        """초기 대량 편입 때 ka10081 요청이 동시에 폭주하지 않게 최대 3개만 실행한다."""
        while self._prevvol_queue and len(self._prevvol_workers) < 3:
            task = asyncio.ensure_future(self._drain_prevvol_queue())
            self._prevvol_workers.add(task)
            task.add_done_callback(self._on_prevvol_worker_done)

    def _on_prevvol_worker_done(self, task: asyncio.Task):
        self._prevvol_workers.discard(task)
        # 마지막 pop 직후 새 종목이 들어오는 경합에서도 큐가 멈추지 않게 한다.
        self._start_prevvol_workers()

    async def _drain_prevvol_queue(self):
        while self._prevvol_queue:
            await self._fetch_prev_vol(self._prevvol_queue.popleft())

    async def _fetch_prev_vol(self, code: str):
        try:
            vol = await self.rest.prev_volume(code)
            self._prevvol_done.add(code)  # 응답 받았으면(0이라도) 재조회 안 함
            if vol:
                for v in self.views:
                    if code in v.screen.model.rows:
                        v.screen.on_tick(code, {"prev_vol": vol})
        except Exception as e:  # noqa: BLE001
            log.warning("prev_vol %s: %s", code, e)  # 실패는 done 안 찍어 다음 refresh 재시도
        finally:
            self._prevvol_pending.discard(code)

    async def _poll_single(self, codes: list[str]):
        try:
            for row in await self.rest.watch_info(codes):
                self._on_real(row["code"], row)
        except Exception as e:  # noqa: BLE001
            log.warning("single poll: %s", e)

    # --- [0198] 순위창 / 추가 조건검색 창 ------------------------------------
    def _on_rank(self):
        if self._rank is None:
            self._rank = RankScreen(self.rest)
            if self._market is not None:
                self._rank.set_market(self._market)
        if self._rank.isVisible():
            self._rank.close()
        else:
            self._rank.show()
            self._rank.raise_()

    def _on_analysis(self):
        analysis = self._ensure_analysis_window()
        analysis.show()
        analysis.raise_()
        analysis.activateWindow()
        # 이미 만들어진 창도 분석 버튼을 다시 누르면 즉시 화면 안으로 복구한다.
        QTimer.singleShot(0, analysis._ensure_titlebar_visible)

    def _ensure_analysis_window(self):
        if self._analysis is None:
            self._analysis = AnalysisWindow(self.rest, self)
            self._analysis.watchlist_changed.connect(
                self._sync_realtime_watch_models)
            self._analysis.news_auto_changed.connect(
                self._set_news_auto_collection)
            self._analysis.news_auto_interval_changed.connect(
                self._set_news_auto_interval)
            self._analysis.new_news_found.connect(self._notify_new_news)
            self._analysis.limit_count_collect_requested.connect(
                self._manual_limit_count_refresh)
        return self._analysis

    def _set_news_auto_collection(self, enabled: bool):
        """저장된 자동수집 체크 상태를 백그라운드 타이머에 반영한다."""
        if enabled:
            self._news_auto_timer.start()
        else:
            self._news_auto_timer.stop()

    def _set_news_auto_interval(self, minutes: int):
        """사용자가 선택한 뉴스 자동수집 주기를 즉시 반영한다."""
        self._news_auto_timer.setInterval(int(minutes) * 60 * 1000)
        if self._settings.value("analysis_news_auto", "false") == "true":
            self._news_auto_timer.start()

    def _auto_news_collection(self):
        """자동수집이 체크된 경우에만 등록 종목의 뉴스를 수집한다."""
        if self._settings.value("analysis_news_auto", "false") != "true":
            self._news_auto_timer.stop()
            return
        if not realtime_watch_codes():
            return
        analysis = self._ensure_analysis_window()
        analysis._start_realtime_news_collection(False, False)

    def _notify_new_news(self, count: int):
        """DB에 처음 들어온 완전 신규 뉴스만 소리와 창 강조로 알린다."""
        if count <= 0:
            return
        analysis = self._analysis
        if analysis is not None and analysis.isVisible():
            analysis.flash_realtime_news_tab()
            target = analysis
        else:
            target = self.views[0].screen.window()
        QApplication.alert(target, 5000)
        log.info("new realtime news alert: %d", count)

    def _auto_intraday_collection(self, catch_up: bool = False):
        """시작 시 최근 거래일을 보완하고 평일 15:40 이후 하루 한 번 실행한다."""
        now = time.localtime()
        today = time.strftime("%Y%m%d", now)
        after_close = now.tm_wday < 5 and (now.tm_hour, now.tm_min) >= (15, 40)
        if not catch_up and not after_close:
            return
        key = "auto_limit_entry_attempt_date"
        if str(self._settings.value(key, "")) == today:
            return
        analysis = self._ensure_analysis_window()
        if analysis._collection_task and not analysis._collection_task.done():
            return
        self._settings.setValue(key, today)
        self._settings.sync()
        asyncio.ensure_future(self._refresh_limit_counts_after_close())
        analysis._start_intraday_enrichment(silent=True)

    def _manual_limit_count_refresh(self):
        log.info("manual limit count refresh requested")
        asyncio.ensure_future(self._refresh_limit_counts_after_close())

    async def _refresh_limit_counts_after_close(self):
        """장 마감 후 틱차트 기반 연상 보완을 기다린 뒤 다시 조회한다."""
        log.info("limit count refresh waiting: 20s")
        await asyncio.sleep(20)
        try:
            counts = await self.rest.yesterday_limit_counts()
            self._limit_cnt = counts
            for view in self.views:
                self._inject_market(view)
            if self._analysis is not None:
                self._analysis._refresh_limit_up_table()
            log.info("post-close limit counts refreshed: %d", len(counts))
        except Exception as error:  # noqa: BLE001
            log.warning("post-close limit count refresh failed: %s", error)

    def _on_newwin(self):
        if len(self.views) >= MAX_WINDOWS:
            log.warning("창 최대 %d개 (실시간 등록 한도)", MAX_WINDOWS)
            return
        self._open_window()
        self._save_window_count()

    def _open_window(self):
        used = {v.prefix for v in self.views}
        n = next(i for i in range(2, MAX_WINDOWS + 1) if f"w{i}_" not in used)
        prefix = f"w{n}_"
        # 최초 생성 때만 본창의 컬럼폭/정렬을 복사. 이후에는 해당 창의 마지막 상태를 유지.
        main = self.views[0].screen
        if self._settings.value(prefix + "header") is None:
            self._settings.setValue(prefix + "header", main.table.horizontalHeader().saveState())
        seeded = False
        if self._settings.value(prefix + "geometry") is None:  # 첫 오픈: 위치도 본창에서
            self._settings.setValue(prefix + "geometry", main.window().saveGeometry())
            seeded = True
        screen = ConditionScreen(prefix=prefix)
        screen.order_enable_check.setChecked(self._order_enabled)
        screen.global_hotkeys = True
        screen.newwin_btn.setVisible(False)  # 추가 창에선 창+/순위/통합 숨김 (메인창에서만)
        screen.rank_btn.setVisible(False)
        screen.news_btn.setVisible(False)
        screen.unified_check.setVisible(False)  # 통합 시세는 전 창 공통 -> 메인창에서만 전환
        screen.font_size_combo.setVisible(False)  # 글자 크기는 앱 전체 공통 -> 메인창에서만 전환
        screen.theme_btn.setVisible(False)  # 테마는 앱 전체 공통 -> 메인창에서만 전환
        win = ConditionWindow(prefix, on_close=self._on_window_closed)
        _start_title_clock(win, f"[0156-{n}] 조건검색실시간")
        win.setCentralWidget(screen)
        view = View(self, screen)
        self._inject_market(view)
        screen.model.set_watched_codes(realtime_watch_codes())
        if self._account_summary is not None:
            screen.set_account_summary(self._account_summary)
        self.views.append(view)
        self._wire_extra(screen)
        self._extra_windows.append(win)
        win.show()
        if seeded:
            # 최초 생성한 추가 창만 본창 크기로 시작한다.
            # 이후에는 ConditionWindow가 prefix별로 저장한 크기와 위치를 그대로 복원한다.
            win.resize(self.views[0].screen.window().size())
        if seeded:  # 본창과 완전히 겹치지 않게 살짝 비껴 배치
            win.move(win.x() + 40, win.y() + 40)
        if self._cond_items:  # 이미 목록 받아놨으면 즉시 콤보 채움 + 자동 등록
            view.on_condition_list(self._cond_items)

    def _wire_extra(self, screen: ConditionScreen):
        screen.reload_btn.clicked.connect(self._reload_conditions)
        screen.order_target_selected.connect(
            lambda code, price, target=screen:
            self._queue_orderable_quantity(target, code, price))
        screen.account_auto_cancel_changed.connect(
            self._set_account_auto_cancel)
        screen.order_requested.connect(
            lambda code, mode, count, auto, total, price, target=screen:
            self._submit_order(target, code, mode, count, auto, total, price))
        screen.cancel_requested.connect(self._cancel_order)
        screen.order_enable_check.toggled.connect(
            lambda enabled, source=screen:
            self._sync_order_enabled(enabled, source))
        screen.exit_hotkey_changed.connect(
            lambda code, spec, source=screen:
            self._set_global_exit_hotkey(source, code, spec))
        screen.emergency_exit_requested.connect(self._emergency_exit)
        screen.order_status_acknowledged.connect(
            self._acknowledge_order_status)
        screen.balance_sell_changed.connect(self._set_balance_sell)
        screen.watch_toggled.connect(
            lambda code, enabled, target=screen:
            self._toggle_realtime_watch(target, code, enabled))
        screen.analysis_stock_requested.connect(
            lambda code, target=screen:
            self._open_condition_analysis_stock(target, code))

    def _on_window_closed(self, win):
        if _SHUTDOWN[0]:  # 앱 종료 동반 닫힘: 창 개수 보존 (재시작 때 복원용)
            return
        for v in list(self.views[1:]):
            if v.screen.window() is win:
                for code in tuple(v.screen.model.exit_hotkeys):
                    self._global_hotkeys.unregister((id(v.screen), code))
                task = self._orderable_tasks.pop(v.screen, None)
                if task and not task.done():
                    task.cancel()
                asyncio.ensure_future(v.stop())
                self.views.remove(v)
                self.force_real_sync()
        if win in self._extra_windows:
            self._extra_windows.remove(win)
        self._save_window_count()

    def _save_window_count(self):
        self._settings.setValue("cond_windows", len(self.views) - 1)
        self._settings.sync()


class GroupedTableWidgetItem(QTableWidgetItem):
    """그룹키를 먼저 비교해 표 정렬 중에도 날짜/종목 묶음을 유지한다."""

    def __init__(self, text: str, group_value, sort_value):
        super().__init__(text)
        self.setData(
            Qt.ItemDataRole.UserRole, (group_value, sort_value))

    def __lt__(self, other):
        left = self.data(Qt.ItemDataRole.UserRole)
        right = other.data(Qt.ItemDataRole.UserRole)
        if left is not None and right is not None:
            if left[0] != right[0]:
                table = self.tableWidget()
                order = (
                    table.horizontalHeader().sortIndicatorOrder()
                    if table is not None else Qt.SortOrder.DescendingOrder
                )
                # Qt가 내림차순에서 비교 결과를 뒤집는 점을 고려해,
                # 어느 방향을 눌러도 거래일 그룹은 최신순으로 유지한다.
                if order == Qt.SortOrder.AscendingOrder:
                    return left[0] > right[0]
                return left[0] < right[0]
            return left < right
        return super().__lt__(other)


class DisclosureDialog(QDialog):
    """선택 종목의 저장된 DART 공시 목록."""

    URL_ROLE = Qt.ItemDataRole.UserRole + 1

    def __init__(self, stock_code: str, stock_name: str, rows: list[dict],
                 parent=None):
        super().__init__(parent)
        self.setWindowTitle(f"{stock_name} ({stock_code}) · DART 공시")
        self.resize(920, 560)
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel(
            f"{stock_name} ({stock_code}) · 저장된 공시 {len(rows):,}건 "
            "· 공시명을 클릭하면 DART 원문을 엽니다."))
        columns = ("접수일", "공시명", "제출인", "유형", "정정", "접수번호")
        table = QTableWidget(len(rows), len(columns))
        table.setHorizontalHeaderLabels(columns)
        table.setSortingEnabled(False)
        table.setAlternatingRowColors(True)
        table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        table.verticalHeader().setVisible(False)
        for row_index, row in enumerate(rows):
            values = (
                row["receipt_date"], row["report_name"], row["submitter"],
                row["disclosure_type"], "정정" if row["correction"] else "",
                row["receipt_no"],
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem(str(value or ""))
                item.setData(self.URL_ROLE, row["source_url"])
                table.setItem(row_index, column, item)
        table.setSortingEnabled(True)
        table.resizeColumnsToContents()
        table.horizontalHeader().setStretchLastSection(True)
        table.cellClicked.connect(self._open_dart)
        self._table = table
        layout.addWidget(table)
        close_btn = QPushButton("닫기")
        close_btn.clicked.connect(self.accept)
        layout.addWidget(close_btn)

    def _open_dart(self, row: int, column: int):
        if column != 1:
            return
        item = self._table.item(row, 0)
        url = item.data(self.URL_ROLE) if item else ""
        if url:
            QDesktopServices.openUrl(QUrl(url))


class RotationCycleWidget(QWidget):
    """선택 테마의 순환 단계와 과거 상한가 발생일을 원형·시간축으로 표시한다."""

    STAGES = ("초기", "확산", "과열", "소멸", "재점화")

    def __init__(self, parent=None):
        super().__init__(parent)
        self._theme = "선택 테마 없음"
        self._phase = ""
        self._next_text = ""
        self._events = []
        self._stage_counts = {}
        self.setMinimumHeight(245)

    def set_cycle(
            self, theme: str, phase: str, next_text: str, events,
            stage_counts=None):
        phase_map = {"신규": "초기", "상한가": "확산", "관찰": "초기"}
        self._theme = str(theme or "선택 테마 없음")
        self._phase = phase_map.get(str(phase or ""), str(phase or ""))
        self._next_text = str(next_text or "")
        self._events = list(events or [])[-12:]
        self._stage_counts = dict(stage_counts or {})
        self.update()

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        dark = self.palette().color(QPalette.ColorRole.Base).lightness() < 128
        foreground = QColor("#ECEFF1" if dark else "#263238")
        muted = QColor("#78909C")
        painter.fillRect(self.rect(), QColor("#182126" if dark else "#F6F8FA"))
        painter.setPen(foreground)
        title_font = painter.font()
        title_font.setBold(True)
        title_font.setPointSize(max(10, title_font.pointSize() + 1))
        painter.setFont(title_font)
        painter.drawText(12, 24, f"{self._theme} · 현재 {self._phase or '-'}"
                         + (f" · 다음 {self._next_text}" if self._next_text else ""))
        painter.setPen(QColor("#FFB300"))
        painter.drawText(self.width() - 125, 24, "순환 방향 ↻ 시계방향")

        center_x = self.width() * 0.5
        center_y = 105
        radius_x = max(150, min(310, self.width() * 0.34))
        radius_y = 62
        centers = []
        for index, stage in enumerate(self.STAGES):
            angle = -math.pi / 2 + index * math.tau / len(self.STAGES)
            centers.append((center_x + math.cos(angle) * radius_x,
                            center_y + math.sin(angle) * radius_y, stage))
        for index, (x1, y1, stage) in enumerate(centers):
            x2, y2, _ = centers[(index + 1) % len(centers)]
            dx, dy = x2 - x1, y2 - y1
            length = max(1.0, math.hypot(dx, dy))
            ux, uy = dx / length, dy / length
            start_x, start_y = x1 + ux * 38, y1 + uy * 21
            end_x, end_y = x2 - ux * 38, y2 - uy * 21
            active_edge = stage == self._phase
            edge_color = QColor("#FFB300" if active_edge else "#78909C")
            painter.setPen(QPen(edge_color, 4 if active_edge else 2))
            painter.drawLine(int(start_x), int(start_y), int(end_x), int(end_y))
            # 목표 노드 앞에 화살촉을 그려 시계방향 진행을 명확히 표시한다.
            arrow_size = 9
            side_x, side_y = -uy, ux
            base_x, base_y = end_x - ux * arrow_size, end_y - uy * arrow_size
            painter.drawLine(int(end_x), int(end_y),
                             int(base_x + side_x * 5), int(base_y + side_y * 5))
            painter.drawLine(int(end_x), int(end_y),
                             int(base_x - side_x * 5), int(base_y - side_y * 5))
        normal_font = painter.font()
        normal_font.setPointSize(max(9, normal_font.pointSize() - 1))
        painter.setFont(normal_font)
        for x, y, stage in centers:
            active = stage == self._phase
            painter.setBrush(QColor("#FFB300" if active else "#37474F"))
            painter.setPen(QPen(QColor("#FFF59D" if active else "#90A4AE"),
                                3 if active else 1))
            painter.drawEllipse(int(x - 34), int(y - 18), 68, 36)
            painter.setPen(QColor("#1B1B1B" if active else "#ECEFF1"))
            count = int(self._stage_counts.get(stage, 0))
            label = f"{stage}\n{count}개" if self._stage_counts else stage
            painter.drawText(int(x - 34), int(y - 18), 68, 36,
                             Qt.AlignmentFlag.AlignCenter, label)

        line_y = self.height() - 34
        left, right = 55, max(56, self.width() - 30)
        painter.setPen(QPen(muted, 2))
        painter.drawLine(left, line_y, right, line_y)
        events = sorted(self._events, key=lambda row: str(row.get("trade_date") or ""))
        for index, row in enumerate(events):
            x = left if len(events) == 1 else int(
                left + (right - left) * index / (len(events) - 1))
            count = int(row.get("limit_up_count") or 0)
            radius = 4 + min(7, count * 2)
            painter.setBrush(QColor("#EF5350" if count else "#42A5F5"))
            painter.setPen(Qt.PenStyle.NoPen)
            painter.drawEllipse(x - radius, line_y - radius, radius * 2, radius * 2)
            if index in (0, len(events) - 1):
                painter.setPen(foreground)
                painter.drawText(x - 35, line_y + 12, 70, 18,
                                 Qt.AlignmentFlag.AlignCenter,
                                 str(row.get("trade_date") or "")[-4:])


class DetachedClockWindow(QWidget):
    """분석창에서 떼어낸 시계 전용 창: 항상 위, 닫으면 분석창으로 복귀."""

    def __init__(self, owner: "AnalysisWindow"):
        super().__init__(None, Qt.WindowType.Tool)
        self.setWindowFlag(Qt.WindowStaysOnTopHint, True)
        self.setWindowTitle("시계")
        self._owner = owner
        layout = QVBoxLayout(self)
        layout.setContentsMargins(4, 4, 4, 4)

    def moveEvent(self, e):
        super().moveEvent(e)
        self._owner._settings.setValue("clock_window_pos", self.pos())

    def closeEvent(self, e):
        if self._owner._clock_window is self:
            self._owner._attach_clock()  # 닫기 = 합치기
        super().closeEvent(e)


class AnalysisWindow(
        LimitUpTabMixin, RealtimeNewsTabMixin, TelegramNewsTabMixin,
        ThemeTabMixin, QMainWindow):
    """실시간 뉴스·상한가·테마 전용 경량 분석창."""

    watchlist_changed = Signal()
    news_auto_changed = Signal(bool)
    news_auto_interval_changed = Signal(int)
    new_news_found = Signal(int)
    limit_count_collect_requested = Signal()

    TABS = (
        ("실시간 뉴스", "LS증권에서 수신한 전체 실시간 뉴스를 확인합니다."),
        ("종목뉴스·종토방", "직접 등록한 종목의 뉴스와 웹페이지를 확인합니다."),
        ("텔레그램 뉴스", "구독 중인 텔레그램 채널의 종목 글을 확인합니다."),
        ("상한가", "상한가 종목 수집·조회·성과 분석 화면입니다."),
        ("테마", "테마 강도와 종목 확산 흐름을 분석합니다."),
    )

    def __init__(self, rest=None, app: "App | None" = None):
        super().__init__()
        self._rest = rest
        self._app = app
        self._collection_task = None
        self._collection_cancelled = False
        self._market_refresh_task = None
        self._market_live_leaders: list[dict] = []
        self._market_live_markets: list[dict] = []
        self._market_live_indices: list[dict] = []
        self._market_query_time: datetime | None = None
        self._unified_refresh_task = None
        self._dart_task = None
        self._rotation_collect_task = None
        self._rotation_refresh_task = None
        self._ls_news_task = None
        self._ls_news_stream = None
        self._telegram_task = None
        self._telegram_stream = None
        self._ls_news_server_sync_task = None
        self._ls_news_server_sync_pending = False
        self._ls_news_search_task: asyncio.Task | None = None
        self._ls_news_search_serial = 0
        self._ls_news_db_search_active = False
        self._ls_news_db_search_query = ""
        self._ls_news_detail_windows: dict[str, LSNewsDetailDialog] = {}
        self._ls_news_detail_tasks: dict[str, asyncio.Task] = {}
        self._ls_news_url_tasks: dict[str, asyncio.Task] = {}
        self._news_web_auto_timer = QTimer(self)
        self._news_web_auto_timer.timeout.connect(self._auto_reload_news_web)
        self.setWindowTitle("분석")
        self._key = "analysis_geometry"
        self._settings = QSettings("layout.ini", QSettings.IniFormat)
        self._geo_timer = QTimer(self)
        self._geo_timer.setSingleShot(True)
        self._geo_timer.timeout.connect(self._save_geo)

        central = QWidget()
        central_layout = QVBoxLayout(central)
        central_layout.setContentsMargins(6, 6, 6, 6)
        central_layout.setSpacing(6)

        principle_bar = QHBoxLayout()
        principle_bar.setSpacing(6)
        self._analysis_clock_label = QLabel()
        self._analysis_clock_label.setAlignment(
            Qt.AlignmentFlag.AlignCenter)
        self._analysis_clock_label.setFixedWidth(275)
        self._analysis_clock_label.setMinimumHeight(86)
        self._analysis_clock_label.setToolTip(
            "KRX 정규장 09:00~15:30 · 시간외 15:40~18:00\n"
            "NXT 프리 08:00~08:50 · 메인 09:00:30~15:20 · "
            "애프터 15:30~20:00")
        self._analysis_clock_label.setStyleSheet(
            "QLabel {"
            " color: #F7FF8A;"
            " background-color: #16251C;"
            " border: 2px solid #4CAF50;"
            " border-radius: 7px;"
            " padding: 5px 8px;"
            "}")
        self._analysis_clock_timer = QTimer(self)
        self._analysis_clock_timer.timeout.connect(
            self._update_analysis_clock)
        self._analysis_clock_timer.start(1000)
        self._update_analysis_clock()
        principle_bar.addWidget(self._analysis_clock_label)
        self._principle_bar = principle_bar
        self._clock_window: DetachedClockWindow | None = None

        self._latest_ls_news_context: dict | None = None
        self._latest_ls_news_label = LatestLSNewsLabel()
        self._latest_ls_news_label.setMinimumHeight(86)
        self._latest_ls_news_label.clicked.connect(
            self._open_latest_ls_news)
        self._latest_ls_news_highlight_timer = QTimer(self)
        self._latest_ls_news_highlight_timer.setSingleShot(True)
        self._latest_ls_news_highlight_timer.timeout.connect(
            self._clear_latest_ls_news_highlight)
        self._set_latest_ls_news_highlight(False)
        self._latest_ls_news_label.set_headline("실시간 뉴스 대기 중")
        principle_bar.addWidget(self._latest_ls_news_label, 1)

        self._clock_detach_btn = QPushButton("🕒")
        self._clock_detach_btn.setCheckable(True)
        self._clock_detach_btn.setFixedSize(32, 32)
        self._clock_detach_btn.setToolTip(
            "시계 분리 — 시계만 항상 위 작은 창으로 띄우기")
        self._clock_detach_btn.toggled.connect(self._clock_detach_toggle)

        self._analysis_on_top_btn = QPushButton("📌")
        self._analysis_on_top_btn.setCheckable(True)
        self._analysis_on_top_btn.setFixedSize(32, 32)
        self._analysis_on_top_btn.setToolTip(
            "항상 맨 위 — 분석창을 다른 창들 위에 계속 고정")
        self._analysis_on_top_btn.toggled.connect(
            self._analysis_on_top_toggle)
        corner_buttons = QVBoxLayout()
        corner_buttons.setSpacing(4)
        corner_buttons.addWidget(self._analysis_on_top_btn)
        corner_buttons.addWidget(self._clock_detach_btn)
        corner_buttons.addStretch(1)
        principle_bar.addLayout(corner_buttons)
        central_layout.addLayout(principle_bar)

        collection_row = QHBoxLayout()
        self._collection_progress = QProgressBar()
        self._collection_progress.setRange(0, 1)
        self._collection_progress.setValue(0)
        self._collection_progress.setMaximumHeight(14)
        self._collection_status = QLabel(
            "상한가·테마 데이터 수동 수집 대기")
        self._cancel_btn = QPushButton("수집 중지")
        self._cancel_btn.setEnabled(False)
        self._cancel_btn.clicked.connect(self._cancel_history_collection)
        collection_row.addWidget(self._collection_progress, 1)
        collection_row.addWidget(self._collection_status, 2)
        collection_row.addWidget(self._cancel_btn)
        central_layout.addLayout(collection_row)

        tabs = QTabWidget()
        self._tabs = tabs
        for title, description in self.TABS:
            page = QWidget()
            layout = QVBoxLayout(page)
            if title == "실시간 뉴스":
                self._build_ls_realtime_news_page(layout)
            elif title == "종목뉴스·종토방":
                self._build_realtime_news_page(layout)
            elif title == "텔레그램 뉴스":
                self._build_telegram_news_page(layout)
            elif title == "상한가":
                self._build_limit_up_page(layout)
            elif title == "테마":
                self._build_theme_page(layout)
            else:
                label = QLabel(description)
                label.setAlignment(Qt.AlignmentFlag.AlignCenter)
                layout.addWidget(label)
            tabs.addTab(page, title)
        tabs.currentChanged.connect(self._analysis_tab_changed)
        central_layout.addWidget(tabs, 1)
        self.setCentralWidget(central)

        geo = self._settings.value(self._key)
        if geo is not None:
            restored = self.restoreGeometry(geo)
        else:
            restored = False
        if not restored:
            self.resize(1050, 680)
        saved_size = self._settings.value("analysis_size")
        if isinstance(saved_size, QSize) and saved_size.isValid():
            self.resize(saved_size)
        saved_pos = self._settings.value("analysis_pos")
        if isinstance(saved_pos, QPoint):
            self.move(saved_pos)
        # 모니터 분리·해상도 변경 뒤 저장 위치가 화면 밖이어도 타이틀바를
        # 잡을 수 있도록, 실제 창 프레임이 만들어진 다음 작업영역 안으로 보정한다.
        QTimer.singleShot(0, self._ensure_titlebar_visible)
        if self._settings.value("analysis_on_top", "false") == "true":
            self._analysis_on_top_btn.setChecked(True)
        if self._settings.value("clock_detached", "false") == "true":
            self._clock_detach_btn.setChecked(True)
        self.watchlist_changed.connect(self._sync_ls_news_watched_codes)
        self._refresh_realtime_watch_table()
        self._refresh_realtime_news_table()
        if self._selected_watch_code:
            self._open_selected_watch_board()
        self._load_saved_ls_news()
        self._load_saved_telegram_news()
        QTimer.singleShot(0, self._start_ls_news_stream)
        QTimer.singleShot(0, self._start_telegram_stream)

    def _ensure_titlebar_visible(self):
        screens = QApplication.screens()
        if not screens:
            return
        frame = self.frameGeometry()
        title = QRect(
            frame.left(), frame.top(),
            max(160, min(frame.width(), 320)), 40)
        areas = [screen.availableGeometry() for screen in screens]
        # 한쪽 끝만 걸친 상태는 타이틀바를 잡을 수 없으므로 전체 확인 영역이
        # 작업영역 안에 들어와야 정상 위치로 인정한다.
        if any(area.contains(title) for area in areas):
            return

        center = frame.center()
        intersecting = [
            area for area in areas if area.intersects(frame)]
        if intersecting:
            area = max(
                intersecting,
                key=lambda candidate: (
                    candidate.intersected(frame).width()
                    * candidate.intersected(frame).height()))
        else:
            area = min(
                areas,
                key=lambda candidate: (
                    candidate.center().x() - center.x()
                ) ** 2 + (
                    candidate.center().y() - center.y()
                ) ** 2,
            )
        width = min(max(500, self.width()), area.width())
        height = min(max(400, self.height()), area.height())
        if self.size() != QSize(width, height):
            self.resize(width, height)
        x = min(max(frame.left(), area.left()), area.right() - 160)
        y = min(max(frame.top(), area.top()), area.bottom() - 40)
        self.move(x, y)
        self._save_geo()
        log.info(
            "analysis geometry moved on-screen: x=%s y=%s size=%sx%s",
            x, y, width, height)

    def _update_analysis_clock(self):
        now = datetime.now().astimezone()
        weekdays = (
            "월요일", "화요일", "수요일", "목요일",
            "금요일", "토요일", "일요일",
        )
        weekday = weekdays[now.weekday()]
        krx_state, nxt_state, holiday_reason = _market_session_states(now)
        active_states = {
            "정규장", "시간외", "프리마켓", "메인마켓", "애프터마켓",
        }
        auction_states = {"시가 동시호가", "종가 동시호가"}

        def badge(market: str, state: str) -> str:
            if state in active_states:
                background, foreground = "#18733C", "#E2FFE9"
            elif state in auction_states:
                background, foreground = "#9A6500", "#FFF4C2"
            elif state in {"휴장", "일시휴장"}:
                background, foreground = "#8B2635", "#FFE1E5"
            else:
                background, foreground = "#424A55", "#E2E7ED"
            return (
                f"<span style='background-color:{background};"
                f" color:{foreground}; font-size:12px; font-weight:800;'>"
                f"&nbsp;{market} {state}&nbsp;</span>"
            )

        if holiday_reason:
            border, background = "#E05252", "#351D22"
            day_color = "#FF8C98"
            day_state = f" · {holiday_reason}"
        elif krx_state in active_states or nxt_state in active_states:
            border, background = "#3FC56B", "#16291D"
            day_color = "#F7FF8A"
            day_state = " · 거래일"
        elif krx_state in auction_states:
            border, background = "#E3A522", "#332A16"
            day_color = "#FFD66B"
            day_state = " · 개장 준비"
        else:
            border, background = "#718096", "#202832"
            day_color = "#D7E3F2"
            day_state = " · 거래 종료"

        # 개장 직후 체결이 활발한 구간과 09:30 이후의 속도 저하 구간을
        # 시계만 보아도 구분할 수 있게 한다. 거래 자체를 막지는 않고,
        # 느린 시간의 신규 매매를 다시 생각하게 하는 시각 경고다.
        seconds = now.hour * 3600 + now.minute * 60 + now.second
        if not holiday_reason and 9 * 3600 <= seconds < 9 * 3600 + 10 * 60:
            phase_text = "개장 급변 구간 · 추격 주의"
            phase_background, phase_color = "#9A6500", "#FFF4C2"
        elif (
            not holiday_reason
            and 9 * 3600 + 10 * 60 <= seconds < 9 * 3600 + 30 * 60
        ):
            phase_text = "핵심 매매시간 · 체결 활발"
            phase_background, phase_color = "#087F5B", "#E6FFF7"
            border = "#35D6A2"
        elif (
            not holiday_reason
            and 9 * 3600 + 30 * 60 <= seconds < 15 * 3600 + 30 * 60
        ):
            phase_text = "09:30 경과 · 체결속도 저하 · 신규매매 경고"
            phase_background, phase_color = "#A32632", "#FFF0F2"
            border, background = "#FF5868", "#351D22"
        else:
            phase_text = "매매 집중시간 아님"
            phase_background, phase_color = "#424A55", "#E2E7ED"

        phase_badge = (
            f"<div style='margin-top:3px; background-color:{phase_background};"
            f" color:{phase_color}; font-size:12px; font-weight:900;'>"
            f"{phase_text}</div>"
        )
        self._analysis_clock_label.setStyleSheet(
            "QLabel {"
            " color: #F4F7FA;"
            f" background-color: {background};"
            f" border: 2px solid {border};"
            " border-radius: 7px;"
            " padding: 4px 7px;"
            "}")
        self._analysis_clock_label.setText(
            "<div style='font-size:13px; font-weight:800;'>"
            f"{now:%Y-%m-%d} "
            f"<span style='color:{day_color};'>{weekday}{day_state}</span>"
            "</div>"
            "<div style='font-size:26px; font-weight:900;"
            " letter-spacing:1px;'>"
            f"{now:%H:%M:%S}</div>"
            f"<div>{badge('KRX', krx_state)}&nbsp;"
            f"{badge('NXT', nxt_state)}</div>"
            f"{phase_badge}"
        )

    def _clock_detach_toggle(self, on: bool):
        if on:
            self._detach_clock()
        else:
            self._attach_clock()

    def _detach_clock(self):
        if self._clock_window is not None:
            return
        window = DetachedClockWindow(self)
        self._principle_bar.removeWidget(self._analysis_clock_label)
        window.layout().addWidget(self._analysis_clock_label)
        self._clock_window = window
        pos = self._settings.value("clock_window_pos")
        if isinstance(pos, QPoint):
            window.move(pos)
        window.show()
        self._settings.setValue("clock_detached", "true")
        self._settings.sync()

    def _attach_clock(self):
        window = self._clock_window
        if window is None:
            return
        self._clock_window = None
        window.layout().removeWidget(self._analysis_clock_label)
        self._principle_bar.insertWidget(0, self._analysis_clock_label)
        self._analysis_clock_label.show()
        window.close()
        window.deleteLater()
        self._settings.setValue("clock_detached", "false")
        self._settings.sync()
        if self._clock_detach_btn.isChecked():
            self._clock_detach_btn.blockSignals(True)
            self._clock_detach_btn.setChecked(False)
            self._clock_detach_btn.blockSignals(False)

    def _analysis_on_top_toggle(self, on: bool):
        geo = self.geometry()
        was_visible = self.isVisible()
        self.setWindowFlag(Qt.WindowStaysOnTopHint, on)
        if was_visible:
            self.show()
        if not geo.isEmpty():
            self.setGeometry(geo)
        self._settings.setValue(
            "analysis_on_top", "true" if on else "false")
        self._settings.sync()

    def _show_async_error(self, title: str, text: str):
        """비동기 작업 안에서 Qt 중첩 이벤트 루프를 만들지 않는 오류 알림."""
        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Critical)
        box.setWindowTitle(title)
        box.setText(text)
        box.setStandardButtons(QMessageBox.StandardButton.Ok)
        box.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        box.open()

    def _analysis_tab_changed(self, index: int):
        title = self._tabs.tabText(index)
        if title == "종목뉴스·종토방":
            self._refresh_realtime_watch_table()
            self._refresh_realtime_news_table()
        elif title == "상한가":
            self._refresh_limit_up_table()
        elif title == "테마":
            self._refresh_theme_table()

    def _save_market_splitters(self, *_args):
        if not hasattr(self, "_market_vertical_splitter"):
            return
        self._settings.setValue(
            "analysis_market_vertical_v2",
            self._market_vertical_splitter.saveState())
        self._settings.setValue(
            "analysis_market_signal",
            self._market_signal_splitter.saveState())
        self._settings.setValue(
            "analysis_market_top", self._market_top_splitter.saveState())
        self._settings.setValue(
            "analysis_market_bottom", self._market_bottom_splitter.saveState())
        self._settings.sync()

    def _fill_dashboard_table(
        self, table, rows, sort_column=0,
        sort_order=Qt.SortOrder.DescendingOrder,
    ):
        table.setSortingEnabled(False)
        table.setRowCount(len(rows))
        for r, values in enumerate(rows):
            for c, value in enumerate(values):
                item = (
                    NumericTableWidgetItem(str(value[0]), value[1])
                    if isinstance(value, tuple) else
                    QTableWidgetItem(str(value or ""))
                )
                table.setItem(r, c, item)
        table.setSortingEnabled(True)
        table.resizeColumnsToContents()
        for column in range(table.columnCount()):
            table.setColumnWidth(
                column, min(260, max(80, table.columnWidth(column))))
        table.sortItems(sort_column, sort_order)

    def _set_market_card(
        self, key: str, title: str, value: str, detail: str = "",
        direction: float | None = None, unavailable: bool = False,
    ):
        label = self._market_cards[key]
        if unavailable:
            background, foreground, border = "#30343A", "#AEB5BE", "#626971"
        elif direction is not None and direction > 0:
            background, foreground, border = "#452323", "#FFD3D3", "#D65A5A"
        elif direction is not None and direction < 0:
            background, foreground, border = "#1E3150", "#D0E2FF", "#4F7CC0"
        else:
            background, foreground, border = "#26313C", "#E7EDF4", "#727A84"
        detail_html = (
            f"<br><span style='font-size:10px;'>{detail}</span>"
            if detail else "")
        label.setText(
            f"<b>{title}</b><br><span style='font-size:13px;'>"
            f"{value}</span>{detail_html}")
        label.setStyleSheet(
            "QLabel { padding:4px;"
            f" border:1px solid {border}; border-radius:4px;"
            f" background:{background}; color:{foreground}; }}")

    def _refresh_market_page(self):
        if not hasattr(self, "_market_summary"):
            return
        data = market_dashboard()
        if self._market_live_markets:
            data["markets"] = self._market_live_markets
            data["live_market"] = True
        if self._market_live_indices:
            data["indices"] = self._market_live_indices
        if self._market_live_leaders:
            data["leaders"] = self._market_live_leaders
        trade_date = (
            data.get("trade_date")
            or (
                QDate.currentDate().toString("yyyyMMdd")
                if data.get("live_market") else ""
            )
        )
        if not trade_date:
            self._market_basis.setText("저장된 시장 데이터가 없습니다.")
            return
        if self._market_query_time and data.get("live_market"):
            theme_time = str(data.get("theme_snapshot_at") or "")
            theme_detail = (
                f" · 테마 조건검색 {theme_time[11:19]}"
                if len(theme_time) >= 19 else "")
            self._market_basis.setText(
                "조회시각 기준 "
                + self._market_query_time.strftime("%Y-%m-%d %H:%M:%S")
                + " · 지수·등락 종목 수·거래대금·상하한가 실시간 조회"
                + theme_detail)
        else:
            self._market_basis.setText(
                f"저장 자료 {trade_date} · 전체 갱신을 누르면 조회시각 기준으로 전환")
        context = _market_context(data)
        regime_styles = {
            "위험선호": ("#173F2A", "#B9F6CA", "#38A169"),
            "위험회피": ("#4A2020", "#FFD0D0", "#E05252"),
            "중립": ("#263544", "#D6E8FF", "#6F91B5"),
        }
        background, foreground, border = regime_styles[context["regime"]]
        self._market_regime.setText(
            f"{context['regime']} · {context['leadership']} · "
            f"{context['flow_state']}  |  "
            + ("조회시각 데이터 기반 규칙 판정 · 매수 신호 아님"
               if data.get("live_market")
               else "저장 데이터 기반 규칙 판정 · 매수 신호 아님"))
        self._market_regime.setStyleSheet(
            "QLabel { padding:5px 10px;"
            f" background:{background}; color:{foreground};"
            f" border:1px solid {border}; border-radius:4px;"
            " font-size:14px; font-weight:700; }")

        indices = {
            row["market"]: row for row in data.get("indices", [])}
        market_flows = {
            row["market"]: row for row in data.get("market_flows", [])}
        for market, title in (("KOSPI", "코스피"), ("KOSDAQ", "코스닥")):
            row = indices.get(market)
            if row:
                rate = row.get("change_rate")
                rate_text = (
                    f"{float(rate):+.2f}%" if rate is not None else "-")
                self._set_market_card(
                    market, title,
                    f"{float(row.get('close_value') or 0):,.2f}  {rate_text}",
                    f"{row.get('trade_date') or trade_date} · 키움",
                    float(rate) if rate is not None else None,
                )
            else:
                self._set_market_card(
                    market, title, "미수집", "키움 시장지수 수집 필요",
                    unavailable=True)
        for market, key, title in (
            ("KOSPI", "FOREIGN_KOSPI", "외국인 코스피"),
            ("KOSDAQ", "FOREIGN_KOSDAQ", "외국인 코스닥"),
        ):
            row = market_flows.get(market)
            if row:
                value = int(row.get("foreign_net") or 0)
                self._set_market_card(
                    key, title, _format_flow_million(value),
                    f"5일 {_format_flow_million(row.get('foreign_5d'))}",
                    float(value),
                )
            else:
                self._set_market_card(
                    key, title, "미수집", "키움 시장수급 수집 필요",
                    unavailable=True)
        external = {
            row["indicator_code"]: row
            for row in data.get("external", [])}
        newest_collected = None
        for key, title in (
            ("SOX", "필라델피아 반도체"),
            ("NASDAQ_FUT", "나스닥100 선물"),
            ("USDKRW", "원/달러"),
        ):
            row = external.get(key)
            if not row:
                self._set_market_card(
                    key, title, "미수집", "첫 자동조회 대기",
                    unavailable=True)
                continue
            rate = row.get("change_rate")
            value = float(row.get("value") or 0)
            observed = str(row.get("observed_at") or "")
            try:
                observed_text = datetime.fromisoformat(
                    observed).strftime("%m/%d %H:%M")
            except ValueError:
                observed_text = observed
            value_text = (
                f"{value:,.2f}"
                if key == "USDKRW" else f"{value:,.2f}")
            rate_text = (
                f"  {float(rate):+.2f}%" if rate is not None else "")
            self._set_market_card(
                key, title, value_text + rate_text,
                f"원본 {observed_text} · Yahoo 공개 chart",
                float(rate) if rate is not None else None,
            )
            try:
                collected = datetime.fromisoformat(
                    str(row.get("collected_at") or ""))
                newest_collected = max(
                    filter(None, (newest_collected, collected)))
            except ValueError:
                pass
        if external and newest_collected is not None:
            age_seconds = (
                datetime.now().astimezone() - newest_collected
            ).total_seconds()
            status = "수집 정상" if age_seconds <= 180 else "수집 지연"
            self._set_market_card(
                "EXTERNAL_STATUS", "해외지표 수집",
                "NQ선물 · SOX · 원/달러",
                f"{status} · 마지막 {newest_collected.strftime('%m/%d %H:%M:%S')}"
                " · 시장 마감 시 원본값 고정",
                unavailable=age_seconds > 180,
            )
        else:
            self._set_market_card(
                "EXTERNAL_STATUS", "해외지표 수집", "첫 자동조회 대기",
                "앱 실행 약 3초 후 시작", unavailable=True)

        summaries = []
        for row in data["markets"]:
            summaries.append(
                f"{row['market']} 종목 {row['stock_count']:,} · "
                f"상승 {row['rising']:,} / 하락 {row['falling']:,} "
                f"/ 보합 {row['unchanged']:,} · "
                f"거래대금 {row['trading_value'] / 100_000_000:,.0f}억원 · "
                f"상한가 {row['limit_up_count']:,}")
        self._market_summary.setText("     ".join(summaries))
        self._fill_dashboard_table(self._market_index_table, [
            (
                row["market"],
                (f"{float(row.get('close_value') or 0):,.2f}",
                 row.get("close_value") or 0),
                (f"{float(row.get('change_rate') or 0):+.2f}%",
                 row.get("change_rate") or 0),
                (f"{int(row.get('trading_value') or 0)/100_000_000:,.0f}억",
                 row.get("trading_value") or 0),
                row.get("trade_date") or "",
            ) for row in data.get("indices", [])
        ], 0, Qt.SortOrder.AscendingOrder)
        self._fill_dashboard_table(self._market_investor_table, [
            (
                row["market"],
                (_format_flow_million(row.get("foreign_net")),
                 row.get("foreign_net") or 0),
                (_format_flow_million(row.get("institution_net")),
                 row.get("institution_net") or 0),
                (_format_flow_million(row.get("individual_net")),
                 row.get("individual_net") or 0),
                (_format_flow_million(row.get("foreign_5d")),
                 row.get("foreign_5d") or 0),
                (_format_flow_million(row.get("foreign_20d")),
                 row.get("foreign_20d") or 0),
            ) for row in data.get("market_flows", [])
        ], 0, Qt.SortOrder.AscendingOrder)

        self._fill_dashboard_table(self._market_theme_table, [
            (
                row["theme_name"],
                (f"{(row['average_rate'] or 0):.2f}%", row["average_rate"] or 0),
                (f"{(row['trading_value'] or 0)/100_000_000:,.0f}억",
                 row["trading_value"] or 0),
                (row["limit_up_count"], row["limit_up_count"]),
                (row["member_count"], row["member_count"]),
            ) for row in data["themes"]
        ], 1, Qt.SortOrder.DescendingOrder)
        self._fill_dashboard_table(self._market_leader_table, [
            (
                f"{row['stock_name']} ({row['stock_code']})",
                (f"{(row['change_rate'] or 0):.2f}%", row["change_rate"] or 0),
                (f"{(row['trading_value'] or 0)/100_000_000:,.0f}억",
                 row["trading_value"] or 0),
                row["themes"],
            ) for row in data["leaders"]
        ], 2, Qt.SortOrder.DescendingOrder)
        self._fill_dashboard_table(self._market_limit_table, [
            (
                f"{row['stock_name']} ({row['stock_code']})",
                row["last_entry_time"] or "-",
                (
                    (f"{row['trading_value']/100_000_000:,.0f}억",
                     row["trading_value"])
                    if row.get("trading_value") is not None
                    else "-"
                ),
            ) for row in data["limit_ups"]
        ], 1, Qt.SortOrder.AscendingOrder)
        self._fill_dashboard_table(self._market_flow_table, [
            (
                f"{row['stock_name']} ({row['stock_code']})",
                (f"{row['net']:+,}", row["net"]),
            ) for row in data["flows"]
        ], 1, Qt.SortOrder.DescendingOrder)

    def _request_market_page_refresh(self):
        """시장 현황의 원천 데이터를 다시 조회한 뒤 화면을 갱신한다."""
        if (
            self._market_refresh_task is not None
            and not self._market_refresh_task.done()
        ):
            return
        if self._rest is None:
            self._refresh_market_page()
            self._market_basis.setText(
                self._market_basis.text()
                + " · 키움 REST 연결이 없어 저장 자료만 표시")
            return
        self._market_refresh_btn.setEnabled(False)
        self._market_basis.setText(
            "국내 종목·지수·시장 수급을 조회 중입니다...")
        self._market_refresh_task = asyncio.ensure_future(
            self._collect_market_dashboard_now())

    async def _collect_market_dashboard_now(self):
        """버튼을 누른 조회시각의 국내 시장·수급·해외지표를 갱신한다."""
        trade_date = QDate.currentDate().toString("yyyyMMdd")
        saved_market = saved_index = saved_flow = saved_stock_flow = 0
        saved_external = 0
        errors = []
        self._market_live_markets = []
        self._market_live_indices = []
        self._market_query_time = None
        try:
            try:
                breadth = await self._rest.market_breadth()
                queried_at = datetime.now().astimezone()
                self._market_live_markets = [
                    dict(row) for row in breadth]
                self._market_live_indices = [{
                    "market": row["market"],
                    "index_code": row["index_code"],
                    "close_value": row["close_value"],
                    "change_rate": row["change_rate"],
                    "trading_value": row["trading_value"],
                    "trade_date": trade_date,
                    "collected_at": queried_at.isoformat(timespec="seconds"),
                } for row in breadth]
                self._market_query_time = queried_at
            except Exception as error:  # noqa: BLE001
                errors.append(f"조회시점 전체시장: {error}")
                log.warning(
                    "market dashboard live breadth refresh failed: %s", error)
            if config.KRX_API_KEY:
                client = KrxClient(config.KRX_API_KEY)
                try:
                    rows = await client.daily_market(trade_date)
                    if rows:
                        saved_market, _ = save_krx_market_day(rows)
                    else:
                        log.info(
                            "KRX daily archive not published yet: %s",
                            trade_date)
                except Exception as error:  # noqa: BLE001
                    errors.append(f"KRX 일별 보조자료: {error}")
                    log.warning(
                        "market dashboard KRX refresh failed: %s", error)
                finally:
                    await client.close()
            for index_code, market in (("001", "KOSPI"), ("101", "KOSDAQ")):
                try:
                    rows = await self._rest.market_index_daily(
                        index_code, trade_date, trade_date)
                    if not rows:
                        raise RuntimeError("오늘 지수 응답 없음")
                    saved_index += save_market_index_prices(rows)
                except Exception as error:  # noqa: BLE001
                    errors.append(f"{market} 지수: {error}")
                    log.warning(
                        "market dashboard index refresh %s failed: %s",
                        market, error)
            for market in ("KOSPI", "KOSDAQ"):
                try:
                    rows = await self._rest.market_investor_flows(
                        market, trade_date)
                    if not rows:
                        raise RuntimeError("오늘 시장수급 응답 없음")
                    saved_flow += save_market_investor_flows(rows)
                except Exception as error:  # noqa: BLE001
                    errors.append(f"{market} 수급: {error}")
                    log.warning(
                        "market dashboard flow refresh %s failed: %s",
                        market, error)
            try:
                rank_rows = await self._rest.trade_value_rank(stex_tp="1")
                labels = active_theme_labels()
                self._market_live_leaders = [{
                    "stock_code": row["code"],
                    "stock_name": row["name"],
                    "change_rate": row["rate"],
                    "trading_value": row["trading_value"],
                    "themes": ", ".join(labels.get(row["code"], ())),
                } for row in rank_rows[:20]]
                if not self._market_live_leaders:
                    raise RuntimeError("거래대금 순위 응답 없음")
            except Exception as error:  # noqa: BLE001
                errors.append(f"거래대금 TOP20: {error}")
                log.warning(
                    "market dashboard trade value rank failed: %s", error)
            stocks = pending_condition_investor_flow_stocks(trade_date)
            for index, stock in enumerate(stocks, 1):
                try:
                    self._market_basis.setText(
                        f"종목별 외인·기관 수급 조회 중 "
                        f"{index}/{len(stocks)} · {stock['stock_name']}")
                    rows = await self._rest.investor_flows(
                        stock["stock_code"], trade_date, trade_date)
                    saved_stock_flow += save_investor_flows(
                        stock["stock_code"], rows)
                except Exception as error:  # noqa: BLE001
                    errors.append(
                        f"{stock['stock_name']} 종목수급: {error}")
                    log.warning(
                        "market dashboard stock flow %s failed: %s",
                        stock["stock_code"], error)
            try:
                rows, external_errors = await GlobalMarketClient().fetch_all()
                saved_external = save_external_market_quotes(rows)
                errors.extend(external_errors)
            except Exception as error:  # noqa: BLE001
                errors.append(f"해외지표: {error}")
                log.warning(
                    "market dashboard external refresh failed: %s", error)
        finally:
            self._refresh_market_page()
            if self._app is not None:
                self._app._refresh_market_overview()
            if errors:
                self._market_basis.setText(
                    self._market_basis.text()
                    + f" · 갱신 일부 실패 {len(errors)}건")
            else:
                self._market_basis.setText(
                    self._market_basis.text()
                    + datetime.now().strftime(" · 갱신 완료 %H:%M:%S"))
            log.info(
                "market dashboard refreshed: market=%d index=%d flow=%d "
                "stock_flow=%d external=%d errors=%d",
                saved_market, saved_index, saved_flow, saved_stock_flow,
                saved_external, len(errors))
            self._market_refresh_btn.setEnabled(True)
            self._market_refresh_task = None

    def _show_latest_naver_news(self, row: dict):
        """네이버 API 신규 뉴스를 상단 공용 전광판에 표시한다."""
        title = " ".join(str(row.get("current_title") or "").split())
        if not title:
            return
        stock_code = str(row.get("stock_code") or "").strip()
        stock_name = str(row.get("stock_name") or stock_code).strip()
        self._latest_ls_news_context = {
            "provider": "NAVER",
            "title": title,
            "stock_code": stock_code,
            "stock_name": stock_name,
            "url": str(
                row.get("naver_url") or row.get("original_url") or ""
            ).strip(),
        }
        headline = f"[네이버] {stock_name} · {title}" if stock_name else title
        self._latest_ls_news_label.set_headline(headline)
        self._set_latest_ls_news_highlight(True)
        self._latest_ls_news_highlight_timer.start(3500)

    def _build_realtime_news_page(self, layout: QVBoxLayout):
        controls = QHBoxLayout()
        self._news_watch_search = QLineEdit()
        self._news_watch_search.setPlaceholderText("종목코드·종목명")
        self._news_watch_search.setClearButtonEnabled(True)
        self._news_watch_search.returnPressed.connect(self._add_news_watch)
        add_btn = QPushButton("감시 추가")
        add_btn.clicked.connect(self._add_news_watch)
        remove_btn = QPushButton("선택 감시 해제")
        remove_btn.clicked.connect(self._remove_selected_news_watch)
        fetch_btn = QPushButton("선택 종목 뉴스 조회")
        fetch_btn.clicked.connect(
            lambda: self._start_realtime_news_collection(True, True))
        self._news_auto = QCheckBox("자동수집")
        self._news_auto.setChecked(
            self._settings.value(
                "analysis_news_auto", "false") == "true")
        self._news_auto_interval = QComboBox()
        for minutes in (1, 3, 5, 10, 15, 30):
            self._news_auto_interval.addItem(f"{minutes}분", minutes)
        saved_news_interval = int(
            self._settings.value("analysis_news_interval", 5))
        saved_news_index = self._news_auto_interval.findData(
            saved_news_interval)
        self._news_auto_interval.setCurrentIndex(
            saved_news_index if saved_news_index >= 0 else 2)
        self._news_auto.toggled.connect(self._news_auto_toggled)
        self._news_auto_interval.currentIndexChanged.connect(
            self._news_auto_interval_changed)
        controls.addWidget(QLabel("감시종목"))
        controls.addWidget(self._news_watch_search, 1)
        controls.addWidget(add_btn)
        controls.addWidget(remove_btn)
        controls.addWidget(fetch_btn)
        controls.addWidget(self._news_auto)
        controls.addWidget(self._news_auto_interval)
        layout.addLayout(controls)

        self._news_status = QLabel(
            "감시 종목을 직접 추가해 주세요. 종목뉴스·종토방은 웹뷰 열람 전용입니다.")
        self._news_status.setWordWrap(True)
        layout.addWidget(self._news_status)

        watch_columns = (
            "번호", "종목명", "감시", "종목코드", "최근 뉴스", "마지막 조회",
        )
        self._news_watch_table = QTableWidget(0, len(watch_columns))
        self._news_watch_table.setHorizontalHeaderLabels(watch_columns)
        self._news_watch_table.setSortingEnabled(True)
        self._news_watch_table.setAlternatingRowColors(True)
        self._news_watch_table.setEditTriggers(
            QTableWidget.EditTrigger.NoEditTriggers)
        self._news_watch_table.verticalHeader().setVisible(False)
        watch_header = self._news_watch_table.horizontalHeader()
        watch_header.setSectionResizeMode(QHeaderView.ResizeMode.Interactive)
        watch_header.setSectionsMovable(False)
        watch_header.setStretchLastSection(False)
        saved_watch_header = self._settings.value(
            "analysis_news_watch_header_v4")
        self._news_watch_header_restored = bool(
            saved_watch_header is not None
            and watch_header.restoreState(saved_watch_header))
        if not self._news_watch_header_restored:
            for column, width in enumerate((42, 115, 38, 72, 145, 145)):
                self._news_watch_table.setColumnWidth(column, width)
        # 기본은 등록 순서(번호 오름차순)이며, 이후 모든 열 제목으로 정렬 가능하다.
        self._news_watch_table.sortItems(0, Qt.SortOrder.AscendingOrder)
        self._news_watch_header_initialized = False
        self._news_watch_header_timer = QTimer(self)
        self._news_watch_header_timer.setSingleShot(True)
        self._news_watch_header_timer.timeout.connect(
            self._save_news_watch_header)
        watch_header.sectionResized.connect(
            lambda *_: self._news_watch_header_timer.start(400))
        self._news_watch_table.cellClicked.connect(
            self._news_watch_table_clicked)
        self._news_watch_table.setContextMenuPolicy(
            Qt.ContextMenuPolicy.CustomContextMenu)
        self._news_watch_table.customContextMenuRequested.connect(
            self._news_watch_context_menu)

        news_columns = (
            "게시시각", "종목", "제목", "재료", "상태", "언론사",
        )
        self._realtime_news_table = QTableWidget(0, len(news_columns))
        self._realtime_news_table.setHorizontalHeaderLabels(news_columns)
        self._realtime_news_table.setSortingEnabled(True)
        self._realtime_news_table.setAlternatingRowColors(True)
        self._realtime_news_table.setStyleSheet(
            "QTableWidget::item:selected {"
            " background-color: #5B4FC4;"
            " color: #FFFFFF;"
            "}")
        self._realtime_news_table.setEditTriggers(
            QTableWidget.EditTrigger.NoEditTriggers)
        self._realtime_news_table.verticalHeader().setVisible(False)
        self._realtime_news_table.cellClicked.connect(
            self._realtime_news_table_clicked)

        watch_pane = QWidget()
        watch_layout = QVBoxLayout(watch_pane)
        watch_layout.setContentsMargins(0, 0, 0, 0)
        watch_layout.addWidget(QLabel("직접 등록한 감시 종목"))
        watch_layout.addWidget(self._news_watch_table)

        news_pane = QWidget()
        news_layout = QVBoxLayout(news_pane)
        news_layout.setContentsMargins(0, 0, 0, 0)
        news_header = QHBoxLayout()
        news_header.addWidget(QLabel("네이버 공식 검색 API 뉴스"))
        self._news_scope = QComboBox()
        self._news_scope.addItem("관심종목 전체뉴스", "all")
        self._news_scope.addItem("선택한 종목뉴스만", "selected")
        saved_news_scope = str(self._settings.value(
            "analysis_news_scope", "selected") or "selected")
        saved_scope_index = self._news_scope.findData(saved_news_scope)
        self._news_scope.setCurrentIndex(
            saved_scope_index if saved_scope_index >= 0 else 1)
        self._news_scope.currentIndexChanged.connect(
            self._news_scope_changed)
        news_header.addWidget(self._news_scope)
        news_header.addStretch(1)
        news_layout.addLayout(news_header)
        news_layout.addWidget(self._realtime_news_table)

        web_pane = QWidget()
        web_layout = QVBoxLayout(web_pane)
        web_layout.setContentsMargins(0, 0, 0, 0)
        web_controls = QHBoxLayout()
        self._news_web_back = QPushButton("←")
        self._news_web_forward = QPushButton("→")
        self._news_web_reload = QPushButton("새로고침")
        self._news_web_auto = QCheckBox("자동 새로고침")
        self._news_web_auto.setToolTip(
            "종목토론·뉴스·공시 목록 화면에서만 선택한 간격으로 새로고침")
        self._news_web_auto.setChecked(
            self._settings.value("analysis_web_auto", "false") == "true")
        self._news_web_auto_interval = QComboBox()
        for minutes in (1, 3, 5, 10, 15, 30):
            self._news_web_auto_interval.addItem(f"{minutes}분", minutes)
        saved_web_interval = int(
            self._settings.value("analysis_web_auto_interval", 5))
        saved_web_interval_index = self._news_web_auto_interval.findData(
            saved_web_interval)
        self._news_web_auto_interval.setCurrentIndex(
            saved_web_interval_index
            if saved_web_interval_index >= 0 else 2)
        self._news_web_auto_interval.setFixedWidth(68)
        self._news_web_auto_interval.setToolTip(
            "웹뷰 자동 새로고침 간격")
        external_btn = QPushButton("외부 브라우저")
        self._news_web_back.clicked.connect(
            lambda: self._news_web_action("back"))
        self._news_web_forward.clicked.connect(
            lambda: self._news_web_action("forward"))
        self._news_web_reload.clicked.connect(
            lambda: self._news_web_action("reload"))
        external_btn.clicked.connect(self._open_news_web_external)
        self._news_web_auto.toggled.connect(self._news_web_auto_toggled)
        self._news_web_auto_interval.currentIndexChanged.connect(
            self._news_web_auto_interval_changed)
        web_controls.addWidget(self._news_web_back)
        web_controls.addWidget(self._news_web_forward)
        web_controls.addWidget(self._news_web_reload)
        web_controls.addWidget(self._news_web_auto)
        web_controls.addWidget(self._news_web_auto_interval)
        web_controls.addStretch(1)
        web_controls.addWidget(external_btn)
        web_layout.addLayout(web_controls)

        item_menu = QHBoxLayout()
        item_menu.setSpacing(2)
        naver_item_pages = (
            ("종합정보", "main.naver"),
            ("시세", "sise.naver"),
            ("차트", "fchart.naver"),
            ("투자자별 매매동향", "frgn.naver"),
            ("뉴스·공시", "news.naver"),
            ("종목분석", "coinfo.naver"),
            ("종목토론", "board.naver"),
            ("전자공시", "dart.naver"),
            ("공매도현황", "short_trade.naver"),
        )
        self._news_item_menu_buttons = []
        for title, path in naver_item_pages:
            button = QPushButton(title)
            button.setToolTip(
                f"선택한 감시 종목의 네이버 금융 {title}를 엽니다.")
            button.clicked.connect(
                lambda _checked=False, page=path:
                self._open_selected_watch_page(page))
            item_menu.addWidget(button)
            self._news_item_menu_buttons.append(button)
        web_layout.addLayout(item_menu)

        self._news_web_host = QWidget()
        self._news_web_host_layout = QVBoxLayout(self._news_web_host)
        self._news_web_host_layout.setContentsMargins(0, 0, 0, 0)
        self._news_web_placeholder = QLabel(
            "뉴스 제목이나 종토방을 누르면 이곳에 웹페이지가 열립니다.\n"
            "웹페이지 내용은 자동 추출하거나 DB에 저장하지 않습니다.")
        self._news_web_placeholder.setAlignment(
            Qt.AlignmentFlag.AlignCenter)
        self._news_web_placeholder.setWordWrap(True)
        self._news_web_host_layout.addWidget(self._news_web_placeholder)
        # 첫 링크 클릭 때 Chromium 프로세스 생성과 레이아웃 삽입이 겹치면
        # 분석창이 잠깐 사라졌다 다시 나타나는 것처럼 보일 수 있다. 창이
        # 표시되기 전에 숨김 웹뷰를 준비하고 첫 클릭에는 URL만 교체한다.
        self._news_webview = None
        self._news_web_profile = None
        try:
            from PySide6.QtWebEngineCore import (
                QWebEnginePage, QWebEngineProfile,
            )
            from PySide6.QtWebEngineWidgets import QWebEngineView

            analysis_window = self

            class AnalysisWebPage(QWebEnginePage):
                """새 창 주소를 현재 웹뷰의 방문기록에 포함해 연다."""

                def createWindow(page_self, _window_type):
                    popup_page = QWebEnginePage(
                        page_self.profile(), page_self)

                    def open_in_current(url):
                        if (
                            analysis_window._news_webview is not None
                            and url.isValid()
                            and url.toString() not in ("", "about:blank")
                        ):
                            # 현재 페이지의 setUrl 이동이어야 뒤로가기 기록이
                            # 기존 종목 페이지 뒤에 정상적으로 쌓인다.
                            analysis_window._news_webview.setUrl(url)
                            QTimer.singleShot(0, popup_page.deleteLater)

                    popup_page.urlChanged.connect(open_in_current)
                    return popup_page

            profile_root = DB_PATH.parent / "web_profile" / "naver"
            storage_path = profile_root / "storage"
            cache_path = profile_root / "cache"
            storage_path.mkdir(parents=True, exist_ok=True)
            cache_path.mkdir(parents=True, exist_ok=True)
            self._news_web_profile = QWebEngineProfile(
                "analysis_naver", self)
            self._news_web_profile.setPersistentStoragePath(
                os.fspath(storage_path))
            self._news_web_profile.setCachePath(
                os.fspath(cache_path))
            self._news_web_profile.setHttpCacheType(
                QWebEngineProfile.HttpCacheType.DiskHttpCache)
            self._news_web_profile.setPersistentCookiesPolicy(
                QWebEngineProfile.PersistentCookiesPolicy
                .ForcePersistentCookies)
            self._news_webview = QWebEngineView(self._news_web_host)
            web_page = AnalysisWebPage(
                self._news_web_profile, self._news_webview)
            # 비활성 창의 Chromium 표면이 다시 합성되는 순간 기본 검정색이
            # 드러나지 않도록 앱의 웹뷰 바탕색을 명시한다.
            web_page.setBackgroundColor(QColor("#202124"))
            self._news_webview.setPage(web_page)
            self._news_webview.setVisible(False)
            self._news_web_host_layout.addWidget(self._news_webview)
            self._news_webview.urlChanged.connect(
                lambda current: setattr(
                    self, "_news_current_url", current.toString()))
            self._news_webview.loadFinished.connect(
                self._news_web_load_finished)
            self._news_webview.setUrl(QUrl("about:blank"))
        except ImportError:
            pass
        web_layout.addWidget(self._news_web_host, 1)
        if self._news_web_auto.isChecked():
            self._news_web_auto_timer.start(
                int(self._news_web_auto_interval.currentData())
                * 60 * 1000)

        self._news_right_splitter = QSplitter(Qt.Orientation.Vertical)
        self._news_right_splitter.addWidget(news_pane)
        self._news_right_splitter.addWidget(web_pane)
        self._news_right_splitter.setStretchFactor(0, 2)
        self._news_right_splitter.setStretchFactor(1, 3)
        self._news_main_splitter = QSplitter(Qt.Orientation.Horizontal)
        self._news_main_splitter.addWidget(watch_pane)
        self._news_main_splitter.addWidget(self._news_right_splitter)
        self._news_main_splitter.setStretchFactor(0, 1)
        self._news_main_splitter.setStretchFactor(1, 3)
        layout.addWidget(self._news_main_splitter, 1)

        for key, splitter in (
            ("analysis_news_main_splitter", self._news_main_splitter),
            ("analysis_news_right_splitter", self._news_right_splitter),
        ):
            state = self._settings.value(key)
            if state is not None:
                splitter.restoreState(state)
            splitter.splitterMoved.connect(self._save_news_splitters)

        self._selected_watch_code = ""
        self._news_current_url = ""
        self._news_scroll_mode = ""
        self._news_new_ids_by_code: dict[str, set[int]] = {}
        self._news_task = None
        self._news_pending_codes: set[str] = set()
        self._news_flash_serial = 0

    def _save_news_splitters(self, *_args):
        if not hasattr(self, "_news_main_splitter"):
            return
        self._settings.setValue(
            "analysis_news_main_splitter",
            self._news_main_splitter.saveState())
        self._settings.setValue(
            "analysis_news_right_splitter",
            self._news_right_splitter.saveState())
        self._settings.sync()

    def _save_news_watch_header(self):
        if not hasattr(self, "_news_watch_table"):
            return
        self._settings.setValue(
            "analysis_news_watch_header_v4",
            self._news_watch_table.horizontalHeader().saveState())
        self._settings.sync()

    @staticmethod
    def _display_timestamp(value: str) -> str:
        value = str(value or "")
        return value[:19].replace("T", " ") if value else "-"

    @staticmethod
    def _is_today_news(published: str) -> bool:
        """원본 게시시각을 로컬 시간대로 바꿔 오늘 기사인지 판별한다."""
        try:
            value = datetime.fromisoformat(str(published or ""))
            if value.tzinfo is not None:
                value = value.astimezone()
            return value.date() == datetime.now().astimezone().date()
        except (TypeError, ValueError):
            return False

    def _refresh_realtime_watch_table(self):
        if not hasattr(self, "_news_watch_table"):
            return
        rows = realtime_watch_rows()
        table = self._news_watch_table
        selected_code = self._selected_watch_code
        table.setSortingEnabled(False)
        table.setRowCount(len(rows))
        selected_row = -1
        for row_index, row in enumerate(rows):
            values = (
                row_index + 1, row["stock_name"], "★", row["stock_code"],
                self._display_timestamp(row["latest_news_at"]),
                self._display_timestamp(row["last_news_checked_at"]),
            )
            for column, value in enumerate(values):
                item = (
                    NumericTableWidgetItem(str(value), int(value))
                    if column == 0 else QTableWidgetItem(str(value or ""))
                )
                item.setData(
                    Qt.ItemDataRole.UserRole + 2, row["stock_code"])
                item.setData(
                    Qt.ItemDataRole.UserRole + 3, row["stock_name"])
                if column == 0:
                    item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                elif column == 2:
                    item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                    item.setForeground(QColor("#f4b400"))
                elif column == 1 and row["market"] == "KOSDAQ":
                    item.setForeground(PURPLE)
                    item.setToolTip("KOSDAQ 종목")
                table.setItem(row_index, column, item)
            if row["stock_code"] == selected_code:
                selected_row = row_index
        table.setSortingEnabled(True)
        if selected_code:
            for row_index in range(table.rowCount()):
                item = table.item(row_index, 0)
                if item and str(
                        item.data(Qt.ItemDataRole.UserRole + 2) or "") == selected_code:
                    selected_row = row_index
                    break
        if not self._news_watch_header_initialized:
            if not self._news_watch_header_restored:
                for column, width in enumerate((42, 115, 38, 72, 145, 145)):
                    table.setColumnWidth(column, width)
            self._news_watch_header_initialized = True
        if rows and selected_row < 0:
            selected_row = 0
            self._selected_watch_code = str(rows[0]["stock_code"])
        if selected_row >= 0:
            table.setCurrentCell(selected_row, 0)
        used = news_request_count_today()
        key_state = (
            "API 키 준비" if config.NAVER_CLIENT_ID
            and config.NAVER_CLIENT_SECRET else "API 키 없음")
        self._news_status.setText(
            f"감시 {len(rows):,}/80종목 · 오늘 뉴스 API {used:,}/25,000회 "
            f"· {key_state} · 종목뉴스·종토방은 웹뷰 열람 전용")

    def _refresh_realtime_news_table(self):
        if not hasattr(self, "_realtime_news_table"):
            return
        show_all_watched = (
            hasattr(self, "_news_scope")
            and self._news_scope.currentData() == "all"
        )
        if show_all_watched:
            rows = news_rows("", 300, watched_only=True)
            watched_codes = realtime_watch_codes()
            new_ids = set().union(*(
                self._news_new_ids_by_code.get(code, set())
                for code in watched_codes
            )) if watched_codes else set()
        else:
            rows = (
                news_rows(self._selected_watch_code, 300)
                if self._selected_watch_code else []
            )
            new_ids = self._news_new_ids_by_code.get(
                self._selected_watch_code, set())
        table = self._realtime_news_table
        is_dark = (
            table.palette().color(QPalette.ColorRole.Base).lightness() < 128)
        link_color = QColor("#64B5F6" if is_dark else "#1565C0")
        table.setSortingEnabled(False)
        table.setRowCount(len(rows))
        for row_index, row in enumerate(rows):
            is_today = self._is_today_news(row["published_at_source"])
            removal_status = str(row["removal_status"] or "ACTIVE").upper()
            is_removed = removal_status in ("REMOVED", "DELETED")
            is_missing = removal_status == "MISSING"
            is_new = int(row["news_id"]) in new_ids
            if is_removed:
                status = "삭제"
            elif is_missing:
                status = "검색에서 사라짐"
            elif row["modified_count"]:
                status = f"수정 {row['modified_count']}회"
            else:
                status = "정상"
            values = (
                self._display_timestamp(row["published_at_source"]),
                row["stock_name"], row["current_title"],
                row["material_type"], status, row["publisher"],
            )
            url = row["naver_url"] or row["original_url"]
            for column, value in enumerate(values):
                item = QTableWidgetItem(str(value or ""))
                item.setData(Qt.ItemDataRole.UserRole + 20, url)
                item.setData(
                    Qt.ItemDataRole.UserRole + 2, row["stock_code"])
                if is_removed:
                    item.setBackground(QColor("#E0E0E0"))
                    item.setForeground(QColor("#777777"))
                elif is_missing:
                    item.setBackground(QColor("#FFE0B2"))
                    item.setForeground(QColor("#7A3E00"))
                elif is_new:
                    item.setBackground(QColor("#C8F7D4"))
                    item.setForeground(QColor("#14532D"))
                elif is_today:
                    item.setBackground(QColor("#FFF0A6"))
                    item.setForeground(QColor("#5D3A00"))
                if column == 2:
                    item.setToolTip(row["current_summary"] or "")
                    if is_new and not is_removed and not is_missing:
                        title_font = item.font()
                        title_font.setBold(True)
                        item.setFont(title_font)
                    elif not is_today and not is_removed and not is_missing:
                        item.setForeground(link_color)
                    elif is_today and not is_removed and not is_missing:
                        title_font = item.font()
                        title_font.setBold(True)
                        item.setFont(title_font)
                elif column == 4:
                    item.setTextAlignment(Qt.AlignmentFlag.AlignCenter)
                    if is_removed:
                        item.setForeground(QColor("#C62828"))
                        status_font = item.font()
                        status_font.setBold(True)
                        item.setFont(status_font)
                    elif is_missing:
                        item.setForeground(QColor("#E65100"))
                        status_font = item.font()
                        status_font.setBold(True)
                        item.setFont(status_font)
                    elif row["modified_count"]:
                        item.setForeground(QColor("#E65100"))
                table.setItem(row_index, column, item)
        table.setSortingEnabled(True)
        table.resizeColumnsToContents()
        if table.columnCount() > 2:
            table.setColumnWidth(
                2, min(620, max(300, table.columnWidth(2))))

    def _news_scope_changed(self, _index: int):
        """네이버 공식 뉴스 표의 관심종목 전체/선택종목 범위를 전환한다."""
        scope = str(self._news_scope.currentData() or "selected")
        self._settings.setValue("analysis_news_scope", scope)
        self._settings.sync()
        self._refresh_realtime_news_table()

    def _add_news_watch(self):
        query = self._news_watch_search.text().strip()
        stock = resolve_analysis_stock(query)
        if stock is None:
            QMessageBox.information(
                self, "실시간 감시", "종목코드 또는 종목명을 확인해 주세요.")
            return
        try:
            set_realtime_watch(
                stock["stock_code"], True, "MANUAL_NEWS_TAB")
        except ValueError as error:
            QMessageBox.warning(self, "실시간 감시", str(error))
            return
        self._selected_watch_code = stock["stock_code"]
        self._news_watch_search.clear()
        self._refresh_realtime_watch_table()
        self._refresh_realtime_news_table()
        self._refresh_limit_up_table()
        self.watchlist_changed.emit()
        if self._news_auto.isChecked():
            self._start_realtime_news_collection(True, False)

    def _remove_selected_news_watch(self):
        code = self._selected_watch_code
        if not code:
            return
        set_realtime_watch(code, False)
        self._selected_watch_code = ""
        self._refresh_realtime_watch_table()
        self._refresh_realtime_news_table()
        self._refresh_limit_up_table()
        self.watchlist_changed.emit()

    def _news_watch_table_clicked(self, row: int, column: int):
        item = self._news_watch_table.item(row, 0)
        if item is None:
            return
        code = str(
            item.data(Qt.ItemDataRole.UserRole + 2) or "")
        if column == 2:
            set_realtime_watch(code, False)
            if self._selected_watch_code == code:
                self._selected_watch_code = ""
            self._refresh_realtime_watch_table()
            self._refresh_limit_up_table()
            self.watchlist_changed.emit()
        else:
            self._selected_watch_code = code
            self._open_selected_watch_board()
        self._refresh_realtime_news_table()

    def _news_watch_context_menu(self, position):
        """감시목록 종목명 우클릭은 종목코드만 복사한다."""
        item = self._news_watch_table.itemAt(position)
        if item is None or item.column() != 1:
            return
        code = str(
            item.data(Qt.ItemDataRole.UserRole + 2) or "")
        if not code:
            return
        QApplication.clipboard().setText(code)
        self.statusBar().showMessage(f"{code} 복사됨", 2000)

    def _realtime_news_table_clicked(self, row: int, column: int):
        if column != 2:
            return
        item = self._realtime_news_table.item(row, 0)
        url = (
            item.data(Qt.ItemDataRole.UserRole + 20) if item else "")
        if url:
            self._show_news_web_url(str(url), "article")

    def _news_auto_toggled(self, enabled: bool):
        self._settings.setValue(
            "analysis_news_auto", "true" if enabled else "false")
        self._settings.sync()
        self.news_auto_changed.emit(enabled)
        if enabled:
            self._start_realtime_news_collection(False, False)

    def _news_auto_interval_changed(self, _index: int):
        minutes = int(self._news_auto_interval.currentData())
        self._settings.setValue("analysis_news_interval", minutes)
        self._settings.sync()
        self.news_auto_interval_changed.emit(minutes)

    def _start_realtime_news_collection(
            self, selected_only: bool, show_warning: bool):
        if not config.NAVER_CLIENT_ID or not config.NAVER_CLIENT_SECRET:
            self._news_status.setText(
                ".env에 NAVER_CLIENT_ID와 NAVER_CLIENT_SECRET이 필요합니다. "
                "감시목록과 웹뷰는 API 키 없이도 사용할 수 있습니다.")
            if show_warning:
                QMessageBox.information(
                    self, "네이버 뉴스 API",
                    ".env에 NAVER_CLIENT_ID와 "
                    "NAVER_CLIENT_SECRET을 입력해 주세요.")
            return
        if news_request_count_today() >= 20000:
            self._news_status.setText(
                "오늘 뉴스 API 안전 한도 20,000회에 도달해 자동수집을 멈췄습니다.")
            self._news_auto.setChecked(False)
            return
        codes = (
            [self._selected_watch_code]
            if selected_only and self._selected_watch_code
            else [row["stock_code"] for row in realtime_watch_rows()]
        )
        if not codes:
            if show_warning:
                QMessageBox.information(
                    self, "네이버 뉴스 API", "감시 종목을 먼저 추가해 주세요.")
            return
        self._news_pending_codes.update(code for code in codes if code)
        if self._news_task and not self._news_task.done():
            self._news_status.setText(
                f"뉴스 조회 중 · 추가 대기 "
                f"{len(self._news_pending_codes):,}종목")
            return
        self._news_task = asyncio.ensure_future(
            self._drain_realtime_news_queue())

    async def _drain_realtime_news_queue(self):
        try:
            while self._news_pending_codes:
                codes = sorted(self._news_pending_codes)
                self._news_pending_codes.clear()
                await self._collect_realtime_news(codes)
        finally:
            self._news_task = None

    async def _collect_realtime_news(self, codes: list[str]):
        client = NaverNewsClient(
            config.NAVER_CLIENT_ID, config.NAVER_CLIENT_SECRET)
        total_new = total_updated = errors = 0
        latest_new_news = None
        try:
            for index, code in enumerate(codes, 1):
                stock = resolve_analysis_stock(code)
                if stock is None:
                    continue
                stock_name = stock["stock_name"]
                query = f'"{stock_name}"'
                started = time.monotonic()
                try:
                    items = await client.search(query)
                    matched_items = [
                        item for item in items
                        if stock_name.lower() in (
                            f"{item.get('title') or ''} "
                            f"{item.get('summary') or ''}"
                        ).lower()
                    ]
                    saved = save_news_items(
                        code, stock_name, matched_items)
                    code_new_ids = {
                        int(news_id) for news_id in saved["new_ids"]
                    }
                    self._news_new_ids_by_code[code] = code_new_ids
                    if code_new_ids:
                        candidate = next((
                            row for row in news_rows(code, 300)
                            if int(row["news_id"]) in code_new_ids
                        ), None)
                        if candidate is not None and (
                            latest_new_news is None
                            or (
                                str(candidate["published_at_source"] or ""),
                                int(candidate["news_id"]),
                            ) > (
                                str(latest_new_news["published_at_source"] or ""),
                                int(latest_new_news["news_id"]),
                            )
                        ):
                            latest_new_news = candidate
                    reconcile_news_search_results(code, items)
                    elapsed = int((time.monotonic() - started) * 1000)
                    log_content_request(
                        code, query, 200, len(items),
                        saved["new"], elapsed)
                    total_new += saved["new"]
                    total_updated += saved["updated"]
                except Exception as error:  # noqa: BLE001
                    elapsed = int((time.monotonic() - started) * 1000)
                    log_content_request(
                        code, query, 0, 0, 0, elapsed, str(error))
                    errors += 1
                    log.warning(
                        "naver news collection failed: %s %s", code, error)
                self._news_status.setText(
                    f"뉴스 조회 {index:,}/{len(codes):,} · 신규 "
                    f"{total_new:,} · 수정 {total_updated:,} · 오류 {errors:,}")
                await asyncio.sleep(0)
        finally:
            self._refresh_realtime_watch_table()
            self._refresh_realtime_news_table()
            self._news_status.setText(
                f"뉴스 조회 완료 · 신규 {total_new:,} · 수정 "
                f"{total_updated:,} · 오류 {errors:,} · 오늘 API "
                f"{news_request_count_today():,}/25,000회")
            if total_new:
                if latest_new_news is not None:
                    self._show_latest_naver_news(latest_new_news)
                if (
                    hasattr(self, "_ls_news_sound")
                    and self._ls_news_sound.isChecked()
                ):
                    _beep("naver_news")
                self.new_news_found.emit(total_new)

    def flash_realtime_news_tab(self):
        """분석창이 보일 때 실시간 뉴스 탭을 짧게 점멸한다."""
        index = next((
            i for i in range(self._tabs.count())
            if self._tabs.tabText(i) == "종목뉴스·종토방"
        ), -1)
        if index < 0:
            return
        self._news_flash_serial += 1
        serial = self._news_flash_serial
        tab_bar = self._tabs.tabBar()

        def set_color(color: QColor):
            if serial == self._news_flash_serial:
                tab_bar.setTabTextColor(index, color)

        for delay, color in (
            (0, QColor("#FFB300")),
            (300, QColor()),
            (600, QColor("#FFB300")),
            (900, QColor()),
            (1200, QColor("#FFB300")),
            (1800, QColor()),
        ):
            QTimer.singleShot(delay, lambda c=color: set_color(c))

    def open_market_status(self):
        """시장 현황 탭을 선택하고 조회시점 원천을 즉시 갱신한다."""
        for index in range(self._tabs.count()):
            if self._tabs.tabText(index) == "시장 현황":
                self._tabs.setCurrentIndex(index)
                break
        self._request_market_page_refresh()

    def open_ls_realtime_news(self):
        """LS 실시간 뉴스 탭을 선택한다."""
        for index in range(self._tabs.count()):
            if self._tabs.tabText(index) == "실시간 뉴스":
                self._tabs.setCurrentIndex(index)
                break

    def open_realtime_watch(self, code: str, fetch_news: bool = True):
        """뉴스 탭에서 종목을 선택하고 저장 뉴스와 종토방을 함께 연다."""
        self._selected_watch_code = str(code or "")
        for index in range(self._tabs.count()):
            if self._tabs.tabText(index) == "종목뉴스·종토방":
                self._tabs.setCurrentIndex(index)
                break
        self._refresh_realtime_watch_table()
        self._refresh_realtime_news_table()
        self._refresh_limit_up_table()
        self._open_selected_watch_board()
        self.show()
        self.raise_()
        self.activateWindow()
        if fetch_news:
            self._start_realtime_news_collection(True, False)

    def _open_selected_watch_page(self, page: str):
        if not self._selected_watch_code:
            self._news_status.setText(
                "왼쪽 감시목록에서 종목을 먼저 선택해 주세요.")
            return
        self._show_news_web_url(
            f"https://finance.naver.com/item/{page}?code="
            f"{self._selected_watch_code}",
            "item",
        )

    def _open_selected_watch_board(self):
        self._open_selected_watch_page("board.naver")

    def _show_news_web_url(self, url: str, scroll_mode: str = ""):
        self._news_current_url = str(url or "")
        self._news_scroll_mode = str(scroll_mode or "")
        if not self._news_current_url:
            return
        if self._news_webview is None:
            QDesktopServices.openUrl(QUrl(self._news_current_url))
            self._news_status.setText(
                "Qt 웹뷰를 사용할 수 없어 외부 브라우저로 열었습니다.")
            return
        self._news_web_placeholder.hide()
        self._news_webview.show()
        self._news_webview.setUrl(QUrl(self._news_current_url))

    def _news_web_load_finished(self, succeeded: bool):
        """종목 페이지는 중간 메뉴, 뉴스 기사는 실제 제목부터 보이게 한다."""
        if not succeeded or self._news_webview is None:
            return
        current_url = self._news_webview.url()
        loaded_url = current_url.toString()
        item_menu_paths = {
            "/item/main.naver", "/item/sise.naver", "/item/fchart.naver",
            "/item/frgn.naver", "/item/news.naver", "/item/coinfo.naver",
            "/item/board.naver", "/item/dart.naver",
            "/item/short_trade.naver",
        }
        is_item_page = (
            current_url.host().lower() == "finance.naver.com"
            and (
                current_url.path() in item_menu_paths
                or current_url.path() == "/item/board_read.naver"
            )
        )
        if is_item_page:
            today_highlight = ""
            today_page_labels = {
                "/item/board.naver": "게시글",
                "/item/news.naver": "뉴스",
                "/item/dart.naver": "공시",
            }
            today_page_label = today_page_labels.get(current_url.path())
            if today_page_label:
                today_short = datetime.now().strftime("%y.%m.%d")
                today_long_dot = datetime.now().strftime("%Y.%m.%d")
                today_long_dash = datetime.now().strftime("%Y-%m-%d")
                today_month_day = datetime.now().strftime("%m.%d")
                today_highlight = f"""
                    const todayLabels = [
                        {today_short!r}, {today_long_dot!r},
                        {today_long_dash!r}, {today_month_day!r}
                    ];
                    const applyTodayHighlight = () => {{
                    let todayCount = 0;
                    // 뉴스·공시 목록은 본문 iframe에 들어가므로 함께 검사한다.
                    const documents = [document];
                    for (const frame of document.querySelectorAll('iframe')) {{
                        try {{
                            if (frame.contentDocument) documents.push(frame.contentDocument);
                        }} catch (_error) {{}}
                    }}
                    for (const source of documents) {{
                        for (const row of source.querySelectorAll('table tr')) {{
                            const text = (row.innerText || '').replace(/\\s+/g, ' ');
                            const isToday = todayLabels.some(label => text.includes(label));
                            if (!isToday) continue;
                            todayCount += 1;
                            row.style.setProperty('background-color', '#FFF3B0', 'important');
                            row.style.setProperty('box-shadow', 'inset 4px 0 #F57C00', 'important');
                            row.style.setProperty('font-weight', '700', 'important');
                            for (const cell of row.querySelectorAll('td, th')) {{
                                cell.style.setProperty('background-color', '#FFF3B0', 'important');
                                cell.style.setProperty('font-weight', '700', 'important');
                            }}
                            row.title = '오늘 {today_page_label}';
                        }}
                    }}
                    const board = document.querySelector('#content, #container, body');
                    if (board) {{
                        let badge = document.getElementById('codex-today-item-badge');
                        if (!badge) {{
                            badge = document.createElement('div');
                            badge.id = 'codex-today-item-badge';
                            badge.style.cssText = [
                                'position:sticky', 'top:0', 'z-index:9999',
                                'display:inline-block', 'margin:4px 0',
                                'padding:3px 8px', 'border-radius:3px',
                                'background:#FFF3B0', 'color:#6D4300',
                                'font:700 12px sans-serif'
                            ].join(';');
                            board.prepend(badge);
                        }}
                        badge.textContent = `오늘 {today_page_label} 강조: ${{todayCount}}건`;
                    }}
                    }};
                    applyTodayHighlight();
                    // 상위 문서는 먼저 끝나고 목록 iframe이 뒤늦게 채워질 수 있다.
                    for (const frame of document.querySelectorAll('iframe')) {{
                        frame.addEventListener('load', applyTodayHighlight);
                    }}
                    window.setTimeout(applyTodayHighlight, 500);
                    window.setTimeout(applyTodayHighlight, 1500);
                """
            script = f"""
                (() => {{
                    const itemMenuPaths = new Set([
                        '/item/main.naver', '/item/sise.naver',
                        '/item/fchart.naver', '/item/frgn.naver',
                        '/item/news.naver', '/item/coinfo.naver',
                        '/item/board.naver', '/item/dart.naver',
                        '/item/short_trade.naver'
                    ]);
                    const findItemMenu = () => {{
                        // 목록과 본문에 공통으로 있는 정확한 종목 중간 메뉴를
                        // 먼저 사용한다. inner_sub는 게시글 본문 영역이므로
                        // 본문보기에서 스크롤 기준으로 사용하면 위치가 달라진다.
                        const exactMenu = document.querySelector(
                            'ul.tabs_submenu.tab_total_submenu'
                        );
                        if (exactMenu) return exactMenu;

                        const candidates = Array.from(
                            document.querySelectorAll(
                                'ul.tabs_submenu, [class*="tabs_submenu"]'
                            )
                        );
                        let bestTarget = null;
                        let bestScore = 0;
                        for (const candidate of candidates) {{
                            const matchedPaths = new Set();
                            for (const link of candidate.querySelectorAll(
                                'a[href]'
                            )) {{
                                try {{
                                    const path = new URL(
                                        link.getAttribute('href'), location.href
                                    ).pathname.replace(/\/$/, '');
                                    if (itemMenuPaths.has(path)) {{
                                        matchedPaths.add(path);
                                    }}
                                }} catch (_error) {{}}
                            }}
                            if (matchedPaths.size > bestScore) {{
                                bestScore = matchedPaths.size;
                                bestTarget = candidate;
                            }}
                        }}
                        return bestScore >= 3 ? bestTarget : null;
                    }};

                    const alignItemMenu = () => {{
                        const target = findItemMenu();
                        if (!target) return false;
                        const top = Math.max(
                            0,
                            target.getBoundingClientRect().top
                                + window.scrollY - 4
                        );
                        window.scrollTo(0, top);
                        return true;
                    }};

                    try {{
                        history.scrollRestoration = 'manual';
                    }} catch (_error) {{}}
                    const aligned = alignItemMenu();

                    if (location.pathname.replace(/\/$/, '') ===
                        '/item/board_read.naver') {{
                        // Chromium이 내용보기에서 목록의 이전 스크롤 위치를
                        // 뒤늦게 복원하거나 광고 영역 높이가 변해도 공통 메뉴가
                        // 목록과 같은 자리에 있도록 잠깐만 보정한다.
                        if (window.__analysisBoardMenuAlignTimer) {{
                            window.clearInterval(
                                window.__analysisBoardMenuAlignTimer
                            );
                        }}
                        let remainingAlignments = 24;
                        let userMovedPage = false;
                        const cancelAlignment = () => {{
                            userMovedPage = true;
                            if (window.__analysisBoardMenuAlignTimer) {{
                                window.clearInterval(
                                    window.__analysisBoardMenuAlignTimer
                                );
                                window.__analysisBoardMenuAlignTimer = null;
                            }}
                        }};
                        for (const eventName of [
                            'wheel', 'touchstart', 'pointerdown', 'keydown'
                        ]) {{
                            window.addEventListener(
                                eventName, cancelAlignment,
                                {{ once: true, capture: true }}
                            );
                        }}
                        window.__analysisBoardMenuAlignTimer =
                            window.setInterval(() => {{
                                if (userMovedPage ||
                                    --remainingAlignments <= 0) {{
                                    cancelAlignment();
                                    return;
                                }}
                                alignItemMenu();
                            }}, 100);
                    }}
                    {today_highlight}
                    return aligned;
                }})();
            """
        elif self._news_scroll_mode == "article":
            script = """
                (() => {
                    const selectors = [
                        '#title_area', '.media_end_head_headline',
                        'article h1', 'main h1', '.article_title',
                        '.news_title', '.headline', 'h1',
                        '[class*="article"] h2'
                    ];
                    for (const selector of selectors) {
                        const candidates = document.querySelectorAll(selector);
                        for (const target of candidates) {
                            const style = window.getComputedStyle(target);
                            const rect = target.getBoundingClientRect();
                            if (style.display === 'none' ||
                                style.visibility === 'hidden' ||
                                rect.width === 0 || rect.height === 0 ||
                                !target.textContent.trim()) continue;
                            const top = rect.top + window.scrollY;
                            window.scrollTo(0, Math.max(0, top - 6));
                            return true;
                        }
                    }
                    return false;
                })();
            """
        else:
            return

        def scroll_loaded_page():
            if self._news_webview is None:
                return
            if self._news_webview.url().toString() == loaded_url:
                self._news_webview.page().runJavaScript(script)

        # 종목토론 내용보기는 자바스크립트가 약 2.4초 동안 레이아웃 변화를
        # 추적한다. 바깥 타이머를 다시 걸면 사용자의 직접 스크롤 이후에도
        # 위치를 되돌릴 수 있으므로 내용보기에는 한 번만 실행한다.
        delays = (
            (0,)
            if current_url.path() == "/item/board_read.naver"
            else (0, 500)
        )
        for delay in delays:
            QTimer.singleShot(delay, scroll_loaded_page)

    def _news_web_action(self, action: str):
        if self._news_webview is None:
            return
        getattr(self._news_webview, action)()

    def _open_news_web_external(self):
        if self._news_current_url:
            QDesktopServices.openUrl(QUrl(self._news_current_url))

    def _news_web_auto_toggled(self, enabled: bool):
        self._settings.setValue("analysis_web_auto", "true" if enabled else "false")
        self._settings.sync()
        if enabled:
            self._news_web_auto_timer.start(
                int(self._news_web_auto_interval.currentData())
                * 60 * 1000)
        else:
            self._news_web_auto_timer.stop()

    def _news_web_auto_interval_changed(self, _index: int):
        minutes = int(self._news_web_auto_interval.currentData())
        self._settings.setValue("analysis_web_auto_interval", minutes)
        self._settings.sync()
        if self._news_web_auto.isChecked():
            self._news_web_auto_timer.start(minutes * 60 * 1000)

    def _auto_reload_news_web(self):
        if (self._news_webview is not None and self._news_webview.isVisible()
                and _is_news_web_auto_reload_url(self._news_webview.url())):
            self._news_webview.reload()

    def _open_selected_disclosures(self):
        row = self._limit_table.currentRow()
        if row < 0:
            QMessageBox.information(
                self, "DART 공시", "상한가 표에서 종목을 먼저 선택해 주세요.")
            return
        self._open_disclosures_for_row(row)

    def _open_disclosures_for_row(self, row: int):
        item = self._limit_table.item(row, 0)
        if item is None:
            return
        stock_code = item.data(Qt.ItemDataRole.UserRole + 2) or ""
        stock_name = item.data(Qt.ItemDataRole.UserRole + 3) or stock_code
        date_from, date_to = self._limit_dates()
        rows = disclosure_rows(stock_code, date_from, date_to)
        if not rows:
            QMessageBox.information(
                self, "DART 공시",
                f"{stock_name} ({stock_code})의 저장된 공시가 없습니다.")
            return
        DisclosureDialog(
            stock_code, stock_name, rows, self).exec()

    def _start_dart_collection(self):
        if not config.DART_API_KEY:
            QMessageBox.warning(
                self, "DART 공시", ".env에 DART_API_KEY가 설정되지 않았습니다.")
            return
        if self._dart_task and not self._dart_task.done():
            return
        self._dart_btn.setEnabled(False)
        if hasattr(self, "_data_dart_btn"):
            self._data_dart_btn.setEnabled(False)
        self._dart_task = asyncio.ensure_future(self._collect_dart())

    async def _collect_dart(
            self, date_from: str | None = None, date_to: str | None = None):
        if date_from is None or date_to is None:
            date_from, date_to = self._limit_dates()
        client = DartClient(config.DART_API_KEY)
        saved = errors = 0
        parent_evidence_by_child: dict[str, dict[str, str]] = {}
        relation_year = str(datetime.now().year - 1)
        try:
            self._limit_summary.setText("DART 기업코드 목록을 불러오는 중…")
            mapping = await client.corp_codes()
            save_dart_corp_codes(mapping)
            stocks, skipped = pending_disclosure_stocks(date_from, date_to)
            collectable = []
            missing_corp = 0
            for stock in stocks:
                stock["dart_corp_code"] = (
                    stock.get("dart_corp_code")
                    or mapping.get(stock["stock_code"], "")
                )
                if stock["dart_corp_code"]:
                    collectable.append(stock)
                else:
                    missing_corp += 1
            stocks = collectable
            self._limit_summary.setText(
                f"DART 수집 대상 {len(stocks):,}종목 "
                f"· 기존 수집 제외 {skipped:,}종목 "
                f"· 기업코드 없음 {missing_corp:,}종목")
            for index, stock in enumerate(stocks, 1):
                corp_code = stock["dart_corp_code"]
                try:
                    rows = await client.disclosures(
                        corp_code, date_from, date_to)
                    saved += save_disclosures(
                        stock["stock_code"], corp_code, rows)
                    mark_disclosure_range_collected(
                        stock["stock_code"], date_from, date_to)
                    shareholders = await client.largest_shareholders(
                        corp_code, relation_year)
                    evidence = _largest_shareholder_evidence(
                        shareholders, relation_year)
                    if evidence:
                        parent_evidence_by_child[stock["stock_code"]] = evidence
                except Exception as error:  # noqa: BLE001
                    errors += 1
                    log.warning("DART %s: %s", stock["stock_code"], error)
                self._limit_summary.setText(
                    f"DART {index:,}/{len(stocks):,} · 공시 {saved:,}건 "
                    f"· 기존 제외 {skipped:,}종목 "
                    f"· 기업코드 없음 {missing_corp:,}종목 "
                    f"· 오류 {errors:,}건")
                await asyncio.sleep(0)
            relation_groups, relation_members = save_dart_parent_relations(
                parent_evidence_by_child)
            self._limit_summary.setText(
                f"DART 완료 · 공시 {saved:,}건 · 관계 {relation_groups:,}묶음/"
                f"{relation_members:,}종목 · 오류 {errors:,}건")
        except Exception as error:  # noqa: BLE001
            log.exception("DART collection failed")
            self._show_async_error(
                "DART 공시", f"수집에 실패했습니다.\n{error}")
        finally:
            await client.close()
            self._dart_btn.setEnabled(True)
            if hasattr(self, "_data_dart_btn"):
                self._data_dart_btn.setEnabled(True)
            self._dart_task = None
            self._refresh_limit_up_table()
            self._refresh_db_status()

    def _refresh_db_status(self):
        if not hasattr(self, "_db_state_label"):
            return
        try:
            stats = database_stats()
        except Exception as error:  # noqa: BLE001
            self._db_state_label.setText(f"오류: {error}")
            return
        self._db_state_label.setText("사용 가능" if stats["exists"] else "아직 생성되지 않음")
        self._db_size_label.setText(self._format_bytes(stats["size"]))
        self._db_rows_label.setText(
            f"종목 {stats['stocks']:,} / 일봉 {stats['daily_prices']:,} / "
            f"상한가 {stats['limit_up_events']:,} / 공시 {stats['disclosures']:,} / "
            f"테마 {stats['themes']:,} / 연결 {stats['stock_themes']:,} / "
            f"시장지수 {stats['market_index_prices']:,} / "
            f"시장수급 {stats['market_investor_flows']:,} / "
            f"해외지표 {stats['external_market_ticks']:,} / "
            f"LS뉴스 {stats['ls_realtime_news']:,}")
        self._db_date_label.setText(stats["last_trade_date"] or "-")
        self._db_run_label.setText(stats["last_run"] or "-")

    @staticmethod
    def _format_bytes(size: int) -> str:
        value = float(size)
        for unit in ("B", "KB", "MB", "GB"):
            if value < 1024 or unit == "GB":
                return f"{value:,.1f} {unit}"
            value /= 1024
        return f"{size:,} B"

    def _set_core_collection_running(self, running: bool):
        """남겨 둔 상한가·테마 수집 버튼을 한 번에 잠그거나 해제한다."""
        for name in (
            "_krx_btn",
            "_kiwoom_limit_btn",
            "_collect_btn",
            "_theme_btn",
            "_naver_theme_btn",
            "_dart_btn",
            "_delete_limit_btn",
        ):
            button = getattr(self, name, None)
            if button is not None:
                button.setEnabled(not running)
        self._cancel_btn.setEnabled(running)
        self._date_from.setEnabled(not running)
        self._date_to.setEnabled(not running)

    def _start_theme_collection(self):
        if self._rest is None:
            QMessageBox.warning(
                self, "키움 테마", "키움 REST 연결이 준비되지 않았습니다.")
            return
        if self._collection_task and not self._collection_task.done():
            return
        self._collection_cancelled = False
        self._set_core_collection_running(True)
        self._collection_task = asyncio.ensure_future(self._collect_themes())

    async def _collect_themes(self, new_only: bool = False):
        snapshot_date = QDate.currentDate().toString("yyyyMMdd")
        run_id = start_collection(
            "KIWOOM_THEME", snapshot_date, snapshot_date)
        processed = saved = errors = 0
        status, message = "COMPLETED", ""
        try:
            groups = await self._rest.theme_groups()
            if new_only:
                known_counts = theme_source_member_counts("KIWOOM")
                groups = [
                    group for group in groups
                    if (
                        str(group.get("thema_grp_cd") or "").strip()
                        not in known_counts
                        or int(group.get("stk_num") or 0)
                        != known_counts.get(
                            str(group.get("thema_grp_cd") or "").strip(), -1)
                    )
                ]
                log.info(
                    "Kiwoom changed theme scan: known=%d changed=%d",
                    len(known_counts), len(groups))
            self._collection_progress.setRange(0, max(1, len(groups)))
            self._collection_progress.setValue(0)
            snapshots = []
            for index, group in enumerate(groups, 1):
                if self._collection_cancelled:
                    status, message = "CANCELLED", "사용자가 중지함"
                    break
                theme_code = str(group.get("thema_grp_cd") or "").strip()
                theme_name = str(group.get("thema_nm") or "").strip()
                if not theme_code or not theme_name:
                    continue
                try:
                    members = await self._rest.theme_members(theme_code)
                except Exception:  # noqa: BLE001
                    errors += 1
                    raise
                snapshots.append({
                    "code": theme_code,
                    "name": theme_name,
                    "members": [
                        row.get("stk_cd") for row in members
                        if row.get("stk_cd")
                    ],
                })
                processed += 1
                self._collection_progress.setValue(index)
                self._collection_status.setText(
                    f"키움 테마 {index:,}/{len(groups):,} · {theme_name} "
                    f"· 연결 {sum(len(row['members']) for row in snapshots):,}건")
                update_collection(
                    run_id, "RUNNING", processed, 0, errors, theme_name)
                await asyncio.sleep(0)
            if status == "COMPLETED":
                theme_count, kiwoom_links = save_theme_snapshot(
                    snapshots, snapshot_date, "KIWOOM", 0.8,
                    replace_source=not new_only)
                saved = kiwoom_links

                classifier = ClassificationClient()
                try:
                    missing = limit_up_codes_without_sources(("KIWOOM",))
                    self._collection_status.setText(
                        f"WICS 보완 조회 · 대상 {len(missing):,}종목")
                    wics = await classifier.wics_for_stocks(missing)
                    _, wics_links = save_source_classifications(
                        wics, snapshot_date, "WICS", 0.9)
                    saved += wics_links

                    missing = limit_up_codes_without_sources(
                        ("KIWOOM", "WICS"))
                    self._collection_status.setText(
                        f"KRX 보완 조회 · 대상 {len(missing):,}종목")
                    krx_all = await classifier.krx_classifications()
                    krx = {
                        code: krx_all[code] for code in missing
                        if code in krx_all
                    }
                    _, krx_links = save_source_classifications(
                        krx, snapshot_date, "KRX", 0.65)
                    saved += krx_links
                finally:
                    await classifier.close()

                missing = limit_up_codes_without_sources(
                    ("KIWOOM", "WICS", "KRX"))
                dart = dart_inferred_classifications(missing)
                _, dart_links = save_source_classifications(
                    dart, snapshot_date, "DART", 0.45)
                saved += dart_links
                # 테마 DB 갱신 후 실행 중인 화면이 이전 테마 연결표를
                # 계속 사용하지 않도록 현재 테마정렬을 즉시 재계산한다.
                if self._app:
                    for view in self._app.views:
                        view.screen.refresh_theme_sort()
                message = (
                    f"{'신규 ' if new_only else ''}키움 "
                    f"{theme_count:,}개/{kiwoom_links:,}건 · "
                    f"WICS {wics_links:,} · "
                    f"KRX {krx_links:,} · DART {dart_links:,}건")
        except asyncio.CancelledError:
            status, message = "CANCELLED", "사용자가 중지함"
            raise
        except Exception as error:  # noqa: BLE001
            status, message = "FAILED", str(error)
            log.exception("Kiwoom theme collection failed")
            self._show_async_error(
                "키움 테마", f"테마 수집에 실패했습니다.\n{error}")
        finally:
            update_collection(
                run_id, status, processed, saved, errors, message)
            self._collection_status.setText(
                f"{status} · {message or '저장하지 않음'}")
            self._set_core_collection_running(False)
            self._collection_task = None
            self._refresh_limit_up_table()
            self._refresh_theme_table()

    def _start_naver_theme_collection(self):
        if self._collection_task and not self._collection_task.done():
            return
        self._collection_cancelled = False
        self._set_core_collection_running(True)
        self._collection_task = asyncio.ensure_future(
            self._collect_naver_themes())

    async def _collect_naver_themes(self, new_only: bool = False):
        snapshot_date = QDate.currentDate().toString("yyyyMMdd")
        run_id = start_collection(
            "NAVER_THEME", snapshot_date, snapshot_date)
        processed = saved = errors = 0
        status, message = "COMPLETED", ""
        client = ClassificationClient()
        try:
            self._collection_progress.setRange(0, 0)
            self._collection_status.setText("네이버 테마 목록 조회 중")

            def progress(done, total, name, member_count):
                nonlocal processed
                processed = done
                self._collection_progress.setRange(0, max(1, total))
                self._collection_progress.setValue(done)
                self._collection_status.setText(
                    f"네이버 테마 {done:,}/{total:,} · {name} "
                    f"· 구성 {member_count:,}종목")
                update_collection(
                    run_id, "RUNNING", done, 0, errors, name)

            snapshots = await client.naver_themes(
                progress=progress,
                cancelled=lambda: self._collection_cancelled,
                known_codes=(
                    theme_source_codes("NAVER") if new_only else None),
            )
            if self._collection_cancelled:
                status, message = "CANCELLED", "사용자가 중지함"
            else:
                theme_count, saved = save_theme_snapshot(
                    snapshots, snapshot_date, "NAVER", 0.95,
                    replace_source=not new_only)
                # 네이버 테마 갱신 후에도 화면의 메모리 테마표를 즉시
                # DB 기준으로 교체해야 테마정렬이 오래된 분류를 쓰지 않는다.
                if self._app:
                    for view in self._app.views:
                        view.screen.refresh_theme_sort()
                message = (
                    f"{'신규 ' if new_only else ''}테마 "
                    f"{theme_count:,}개 · 연결 {saved:,}건")
        except asyncio.CancelledError:
            status, message = "CANCELLED", "사용자가 중지함"
            raise
        except Exception as error:  # noqa: BLE001
            errors += 1
            status, message = "FAILED", str(error)
            log.exception("Naver theme collection failed")
            self._show_async_error(
                "네이버 테마", f"테마 수집에 실패했습니다.\n{error}")
        finally:
            await client.close()
            update_collection(
                run_id, status, processed, saved, errors, message)
            self._collection_status.setText(
                f"{status} · {message or '저장하지 않음'}")
            self._set_core_collection_running(False)
            self._collection_task = None
            self._refresh_limit_up_table()
            self._refresh_theme_table()

    def _start_krx_collection(self):
        if not config.KRX_API_KEY:
            QMessageBox.warning(
                self, "KRX 수집", ".env에 KRX_API_KEY가 설정되지 않았습니다.")
            return
        if self._collection_task and not self._collection_task.done():
            return
        date_from = self._date_from.date()
        date_to = self._date_to.date()
        if date_from > date_to:
            QMessageBox.warning(self, "KRX 수집", "시작일이 종료일보다 늦습니다.")
            return
        self._collection_cancelled = False
        self._set_core_collection_running(True)
        self._collection_task = asyncio.ensure_future(
            self._collect_krx(
                date_from.toString("yyyyMMdd"), date_to.toString("yyyyMMdd")))

    async def _collect_krx(self, date_from: str, date_to: str):
        run_id = start_collection("KRX_DAILY_LIMIT_UP", date_from, date_to)
        processed = saved = events = errors = 0
        status, message = "COMPLETED", ""
        client = KrxClient(config.KRX_API_KEY)
        try:
            done_dates = krx_collected_dates(date_from, date_to)
            start = QDate.fromString(date_from, "yyyyMMdd")
            end = QDate.fromString(date_to, "yyyyMMdd")
            dates = []
            current = start
            while current <= end:
                date_text = current.toString("yyyyMMdd")
                if current.dayOfWeek() <= 5 and date_text not in done_dates:
                    dates.append(date_text)
                current = current.addDays(1)
            self._collection_progress.setRange(0, max(1, len(dates)))
            self._collection_progress.setValue(0)
            if not dates:
                message = "선택 기간의 KRX 데이터가 이미 수집됨"
            for index, trade_date in enumerate(dates, 1):
                if self._collection_cancelled:
                    status, message = "CANCELLED", "사용자가 중지함"
                    break
                try:
                    rows = await client.daily_market(trade_date)
                    day_saved, day_events = save_krx_market_day(rows)
                    saved += day_saved
                    events += day_events
                except RuntimeError as error:
                    if "401" in str(error):
                        raise
                    errors += 1
                    log.warning("KRX collection %s: %s", trade_date, error)
                except Exception as error:  # noqa: BLE001
                    errors += 1
                    log.warning("KRX collection %s: %s", trade_date, error)
                processed += 1
                self._collection_progress.setValue(index)
                self._collection_status.setText(
                    f"KRX {index:,}/{len(dates):,} · {trade_date} "
                    f"· 일봉 {saved:,}건 · 상한가 {events:,}건 · 오류 {errors:,}건")
                update_collection(
                    run_id, "RUNNING", processed, saved, errors,
                    f"상한가 {events:,}건")
                await asyncio.sleep(0.05)
        except Exception as error:  # noqa: BLE001
            status, message = "FAILED", str(error)
            log.exception("KRX collection failed")
            self._show_async_error("KRX 수집", message)
        finally:
            await client.close()
            update_collection(
                run_id, status, processed, saved, errors,
                message or f"상한가 {events:,}건")
            self._collection_status.setText(
                f"{status} · 거래일 {processed:,} · 일봉 {saved:,} "
                f"· 상한가 {events:,} · 오류 {errors:,}"
                + (f" · {message}" if message else ""))
            self._set_core_collection_running(False)
            self._collection_task = None
            self._refresh_limit_up_table()

    def _start_intraday_enrichment(self, silent: bool = False):
        if self._rest is None:
            QMessageBox.information(
                self, "키움 장중정보 보완",
                "프로그램을 정상 실행한 분석창에서 사용할 수 있습니다.")
            return
        maintenance = active_kiwoom_maintenance()
        if maintenance is not None:
            message = format_kiwoom_maintenance(maintenance)
            self._collection_status.setText(message)
            if not silent:
                QMessageBox.information(
                    self, "키움 장중정보 보완", message)
            return
        if self._collection_task and not self._collection_task.done():
            return
        date_from = self._date_from.date().toString("yyyyMMdd")
        date_to = self._date_to.date().toString("yyyyMMdd")
        latest, events = pending_intraday_events(date_from, date_to)
        if not latest:
            if not silent:
                QMessageBox.information(
                    self, "키움 장중정보 보완",
                    "선택 기간에 저장된 상한가 종목이 없습니다.")
            return
        if not events:
            if not silent:
                QMessageBox.information(
                    self, "키움 장중정보 보완",
                    f"{latest} 상한가 종목의 진입시간이 이미 모두 저장되어 있습니다.")
            return
        self._collection_cancelled = False
        self._set_core_collection_running(True)
        self._collection_task = asyncio.ensure_future(
            self._collect_intraday(latest, events))

    async def _collect_intraday(self, trade_date: str, events: list[dict]):
        run_id = start_collection(
            "KIWOOM_LAST_LIMIT_ENTRY", trade_date, trade_date)
        processed = saved = unavailable = errors = 0
        status, message = "COMPLETED", ""
        self._collection_progress.setRange(0, len(events))
        self._collection_progress.setValue(0)
        try:
            # 배치 시작 전에 토큰을 한 번 확인한다. 서비스 전체 장애라면 종목별로
            # 같은 토큰 발급을 반복하지 않고 이 지점에서 한 건의 오류로 끝낸다.
            await self._rest.tokens.token()
            for index, event in enumerate(events, 1):
                if self._collection_cancelled:
                    status, message = "CANCELLED", "사용자가 중지함"
                    break
                try:
                    entry_time = await self._rest.last_limit_entry_on_date(
                        event["stock_code"], event["upper_price"], trade_date)
                    if save_last_entry_time(
                            trade_date, event["stock_code"], entry_time):
                        saved += 1
                    else:
                        unavailable += 1
                except Exception as error:  # noqa: BLE001
                    errors += 1
                    log.warning(
                        "limit entry collection %s %s: %s",
                        trade_date, event["stock_code"], error)
                processed += 1
                self._collection_progress.setValue(index)
                self._collection_status.setText(
                    f"키움 진입시간 {index:,}/{len(events):,} · "
                    f"{event['stock_name']} ({event['stock_code']}) "
                    f"· 저장 {saved:,} · 조회불가 {unavailable:,} "
                    f"· 오류 {errors:,}")
                update_collection(
                    run_id, "RUNNING", processed, saved, errors,
                    f"조회불가 {unavailable:,}건")
                await asyncio.sleep(0)
        except Exception as error:  # noqa: BLE001
            status, message = "FAILED", str(error)
            errors += 1
            log.exception("limit entry collection failed")
        finally:
            update_collection(
                run_id, status, processed, saved, errors,
                message or f"조회불가 {unavailable:,}건")
            self._collection_status.setText(
                f"{status} · {trade_date} · 처리 {processed:,} "
                f"· 저장 {saved:,} · 조회불가 {unavailable:,} "
                f"· 오류 {errors:,}")
            self._set_core_collection_running(False)
            self._collection_task = None
            self._refresh_limit_up_table()

    def _start_history_collection(self):
        if self._rest is None:
            QMessageBox.information(
                self, "데이터 수집", "프로그램을 정상 실행한 분석창에서 사용할 수 있습니다.")
            return
        if self._collection_task and not self._collection_task.done():
            return
        date_from = self._date_from.date()
        date_to = self._date_to.date()
        if date_from > date_to:
            QMessageBox.warning(self, "데이터 수집", "시작일이 종료일보다 늦습니다.")
            return
        self._collection_cancelled = False
        self._set_core_collection_running(True)
        self._collection_task = asyncio.ensure_future(
            self._collect_history(
                date_from.toString("yyyyMMdd"), date_to.toString("yyyyMMdd")))

    def _cancel_history_collection(self):
        self._collection_cancelled = True
        self._cancel_btn.setEnabled(False)
        self._collection_status.setText("현재 종목 저장 후 중지합니다…")

    async def _collect_history(self, date_from: str, date_to: str):
        run_id = start_collection("DAILY_LIMIT_UP", date_from, date_to)
        processed = saved = unavailable = errors = 0
        status = "COMPLETED"
        message = ""
        try:
            self._collection_status.setText("종목 목록을 불러오는 중…")
            try:
                universe = await asyncio.wait_for(
                    self._rest.stock_universe(), timeout=60)
            except asyncio.TimeoutError as error:
                raise RuntimeError(
                    "키움 종목 목록 조회가 60초 안에 완료되지 않았습니다. "
                    "네트워크 또는 REST 연결 상태를 확인한 뒤 다시 실행해 주세요."
                ) from error
            sync_stock_catalog(universe)
            chunks = []
            first = QDate.fromString(date_from, "yyyyMMdd")
            chunk_to = min(
                QDate.fromString(date_to, "yyyyMMdd"),
                QDate.currentDate())
            while chunk_to >= first:
                chunk_from = chunk_to.addYears(-1).addDays(1)
                if chunk_from < first:
                    chunk_from = first
                chunks.append((
                    chunk_from.toString("yyyyMMdd"),
                    chunk_to.toString("yyyyMMdd"),
                ))
                chunk_to = chunk_from.addDays(-1)

            existing_dates = stock_history_dates(
                [stock["code"] for stock in universe],
                date_from, date_to)
            today = QDate.currentDate().toString("yyyyMMdd")
            now = datetime.now()
            refresh_today = (
                date_from <= today <= date_to
                and not _krx_holiday_reason(now.date())
                and (now.hour, now.minute) >= (9, 0)
            )
            history_requests = []
            missing_date_count = 0
            for chunk_index, (part_from, part_to) in enumerate(chunks, 1):
                expected_dates = set()
                current = QDate.fromString(part_from, "yyyyMMdd")
                end = QDate.fromString(part_to, "yyyyMMdd")
                while current <= end:
                    current_day = date(
                        current.year(), current.month(), current.day())
                    if not _krx_holiday_reason(current_day):
                        expected_dates.add(current.toString("yyyyMMdd"))
                    current = current.addDays(1)
                for stock in universe:
                    listed_date = "".join(
                        character for character in str(
                            stock.get("listed_date") or "")
                        if character.isdigit())[:8]
                    required_dates = {
                        trade_date for trade_date in expected_dates
                        if not listed_date or trade_date >= listed_date
                    }
                    pending_dates = required_dates - existing_dates.get(
                        stock["code"], set())
                    # 오늘은 과거 일봉 API가 아니라 100종목 묶음 현재시세로
                    # 별도 갱신한다. ka10081은 하루만 지정해도 과거 묶음을
                    # 반환하므로 당일 수집에 사용하지 않는다.
                    pending_dates.discard(today)
                    if not pending_dates:
                        continue
                    history_requests.append((
                        chunk_index, part_from, part_to, stock, pending_dates))
                    missing_date_count += len(pending_dates)

            today_stocks = universe if refresh_today else []
            total = len(today_stocks) + len(history_requests)
            self._collection_progress.setRange(0, max(1, total))
            self._collection_progress.setValue(0)
            if not total:
                message = (
                    "오늘 장 시작 전이라 수집할 당일 일봉이 없음"
                    if date_from == date_to == today
                    and not _krx_holiday_reason(now.date())
                    and (now.hour, now.minute) < (9, 0)
                    else "선택 기간의 기존 일봉이 모두 저장되어 있음"
                )
            else:
                self._collection_status.setText(
                    f"수집 대상 · 오늘 {len(today_stocks):,}종목 "
                    f"· 과거 누락 {missing_date_count:,}건")

            # 오늘 하루는 관심종목정보(ka10095)를 100종목씩 묶어 받아
            # 종목별 과거 일봉 묶음 조회를 피한다.
            for offset in range(0, len(today_stocks), 100):
                if self._collection_cancelled:
                    status = "CANCELLED"
                    message = "사용자가 중지함"
                    break
                stock_part = today_stocks[offset:offset + 100]
                codes = [stock["code"] for stock in stock_part]
                try:
                    quotes = await self._rest.watch_info(
                        codes, exp=False, suffix="")
                    quote_by_code = {
                        str(quote.get("code") or "").removeprefix("A"): quote
                        for quote in quotes
                    }
                    for stock in stock_part:
                        quote = quote_by_code.get(stock["code"])
                        if not quote or int(quote.get("price") or 0) <= 0:
                            unavailable += 1
                            continue
                        bars = [
                            {
                                "date": "00000000",
                                "close": int(quote.get("base") or 0),
                            },
                            {
                                "date": today,
                                "open": int(quote.get("open") or 0),
                                "high": int(quote.get("high") or 0),
                                "low": int(quote.get("low") or 0),
                                "close": int(quote.get("price") or 0),
                                "volume": int(quote.get("vol") or 0),
                                "trading_value": int(
                                    quote.get("trading_value") or 0),
                            },
                        ]
                        price_count, _ = save_stock_history(
                            stock, bars, today, today,
                            selected_dates={today})
                        saved += price_count
                except Exception as error:  # noqa: BLE001
                    errors += 1
                    unavailable += len(stock_part)
                    log.warning(
                        "today quote collection %s~%s: %s",
                        codes[0], codes[-1], error)
                processed += len(stock_part)
                self._collection_progress.setValue(processed)
                self._collection_status.setText(
                    f"{processed:,}/{total:,} · 오늘 {today} 묶음조회 "
                    f"· 일봉 {saved:,}건 · 조회불가 {unavailable:,}건 "
                    f"· 오류 {errors:,}건")
                update_collection(
                    run_id, "RUNNING", processed, saved, errors,
                    f"오늘 {today} · 조회불가 {unavailable:,}건")
                await asyncio.sleep(0)

            for chunk_index, part_from, part_to, stock, pending_dates in (
                    history_requests):
                if status == "CANCELLED":
                    break
                if self._collection_cancelled:
                    status = "CANCELLED"
                    message = "사용자가 중지함"
                    break
                try:
                    bars = await self._rest.daily_bars(
                        stock["code"], part_to)
                    price_count, _ = save_stock_history(
                        stock, bars, part_from, part_to,
                        selected_dates=pending_dates)
                    saved += price_count
                except Exception as error:  # noqa: BLE001
                    errors += 1
                    log.warning(
                        "history collection %s %s~%s: %s",
                        stock["code"], part_from, part_to, error)
                processed += 1
                self._collection_progress.setValue(processed)
                self._collection_status.setText(
                    f"{processed:,}/{total:,} · 구간 "
                    f"{chunk_index}/{len(chunks)} · "
                    f"과거 누락 {len(pending_dates):,}일 · "
                    f"{stock['name']} ({stock['code']}) · "
                    f"일봉 {saved:,}건 · 조회불가 {unavailable:,}건 "
                    f"· 오류 {errors:,}건")
                if processed % 10 == 0:
                    update_collection(
                        run_id, "RUNNING", processed, saved, errors,
                        f"{part_from}~{part_to}")
                    await asyncio.sleep(0)
        except Exception as error:  # noqa: BLE001
            status = "FAILED"
            message = str(error)
            log.exception("history collection failed")
        finally:
            update_collection(
                run_id, status, processed, saved, errors, message)
            self._collection_status.setText(
                f"{status} · 조회 {processed:,} · 일봉 {saved:,} "
                f"· 조회불가 {unavailable:,} · 오류 {errors:,}"
                + (f" · {message}" if message else ""))
            self._set_core_collection_running(False)
            self._collection_task = None
            self._refresh_limit_up_table()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._geo_timer.start(400)

    def moveEvent(self, event):
        super().moveEvent(event)
        self._geo_timer.start(400)

    def _save_geo(self):
        self._settings.setValue(self._key, self.saveGeometry())
        if not self.isMaximized() and not self.isFullScreen():
            self._settings.setValue("analysis_size", self.size())
            self._settings.setValue("analysis_pos", self.pos())
        self._settings.sync()

    def closeEvent(self, event):
        self._save_market_splitters()
        self._save_news_splitters()
        self._save_ls_news_header()
        self._save_news_watch_header()
        self._save_limit_header()
        self._save_geo()
        if self._clock_window is not None:
            # 시계 위젯을 되돌려 고아 창을 남기지 않되, 분리 상태 설정은
            # 남겨 다음 실행에서 분리된 시계 창을 그대로 복원한다.
            self._attach_clock()
            self._settings.setValue("clock_detached", "true")
            self._settings.sync()
        super().closeEvent(event)


class ConditionWindow(QMainWindow):
    """추가 조건검색 창: 위치/크기를 접두사 키로 기억, 닫으면 구독 정리 콜백."""

    def __init__(self, prefix: str, on_close=None):
        super().__init__()
        self._key = prefix + "geometry"
        self._on_close = on_close
        self._settings = QSettings("layout.ini", QSettings.IniFormat)
        self._geo_timer = QTimer(self)
        self._geo_timer.setSingleShot(True)
        self._geo_timer.timeout.connect(self._save_geo)
        geo = self._settings.value(self._key)
        if geo is not None:
            self.restoreGeometry(geo)
        else:
            self.resize(900, 560)

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._geo_timer.start(400)

    def moveEvent(self, e):
        super().moveEvent(e)
        self._geo_timer.start(400)

    def _save_geo(self):
        self._settings.setValue(self._key, self.saveGeometry())
        self._settings.sync()

    def closeEvent(self, e):
        self._save_geo()
        screen = self.centralWidget()
        if hasattr(screen, "_save_layout"):
            screen._save_layout()  # 400ms 저장 타이머 전에 닫혀도 마지막 정렬 보존
        if self._on_close:
            self._on_close(self)
        super().closeEvent(e)


async def _amain(screen):
    app = App(screen)
    window = screen.window()
    if hasattr(window, "set_app_controller"):
        window.set_app_controller(app)
    await app.start()


class MainWindow(QMainWindow):
    """메인 창: 크기/위치를 layout.ini에 기억 (컬럼 너비는 ConditionScreen이 담당)."""

    def __init__(self):
        super().__init__()
        self._app_controller = None
        self._key = "geometry"  # 화면 전환 시 set_view_mode가 화면별 키로 교체
        self._settings = QSettings("layout.ini", QSettings.IniFormat)
        self._geo_timer = QTimer(self)
        self._geo_timer.setSingleShot(True)
        self._geo_timer.timeout.connect(self._save_geo)
        geo = self._settings.value(self._key)
        if geo is not None:
            self.restoreGeometry(geo)
        else:
            self.resize(900, 560)

    def set_app_controller(self, controller):
        self._app_controller = controller

    def resizeEvent(self, e):
        super().resizeEvent(e)
        self._geo_timer.start(400)  # debounce

    def moveEvent(self, e):
        super().moveEvent(e)
        self._geo_timer.start(400)

    def _save_geo(self):
        self._settings.setValue(self._key, self.saveGeometry())
        self._settings.sync()  # 강제 종료돼도 남게

    def closeEvent(self, e):
        self._save_geo()
        screen = self.centralWidget()
        if hasattr(screen, "_save_layout"):
            screen._save_layout()  # 400ms 저장 타이머 전에 닫혀도 마지막 정렬 보존
        if not _SHUTDOWN[0]:
            if self._app_controller is not None:
                self._app_controller.save_session_window_state()
            _SHUTDOWN[0] = True  # 동반 닫힘을 사용자 닫기로 오인 방지 + 재귀 방지
            for w in QApplication.instance().topLevelWidgets():
                if w is not self and w.isVisible():
                    w.close()  # 메인 닫으면 추가 창/순위창도 같이 종료
        super().closeEvent(e)


def main():
    log.warning(
        "process start admin=%s pid=%s executable=%s",
        _is_process_admin(), os.getpid(), sys.executable)
    # Windows에서 다른 창에 가려진 Qt WebEngine을 Chromium이 절전시키면
    # 분석창을 다시 볼 때 검은 화면으로 남고 마우스 이동 뒤에야 재합성되는
    # 경우가 있다. 웹뷰 생성 전에 배경 렌더링 억제를 해제한다.
    webengine_flags = os.environ.get("QTWEBENGINE_CHROMIUM_FLAGS", "")
    required_webengine_flags = (
        "--disable-backgrounding-occluded-windows",
        "--disable-renderer-backgrounding",
        "--disable-features=CalculateNativeWinOcclusion",
    )
    for flag in required_webengine_flags:
        if flag not in webengine_flags:
            webengine_flags = f"{webengine_flags} {flag}".strip()
    os.environ["QTWEBENGINE_CHROMIUM_FLAGS"] = webengine_flags

    qapp = QApplication(sys.argv)
    settings = QSettings("layout.ini", QSettings.IniFormat)
    theme = str(settings.value("theme_mode", "system"))
    _apply_theme(qapp, theme if theme in THEME_MODES else "system")
    _apply_app_font(
        qapp,
        _normalize_app_font_size(settings.value(
            APP_FONT_SIZE_KEY, DEFAULT_APP_FONT_SIZE)),
    )
    loop = qasync.QEventLoop(qapp)
    asyncio.set_event_loop(loop)

    win = MainWindow()
    _start_title_clock(
        win,
        "[0156] 조건검색실시간",
        " 원칙: 절대 잃지 않는 매매 [급등주 기회줄 때 매도]",
    )
    screen = ConditionScreen()
    win.setCentralWidget(screen)
    win.show()

    with loop:
        loop.create_task(_amain(screen))
        loop.run_forever()


if __name__ == "__main__":
    main()
