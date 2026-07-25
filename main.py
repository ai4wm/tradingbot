# -*- coding: utf-8 -*-
"""진입점: qasync로 Qt 이벤트 루프 안에서 asyncio 실행 (단일 스레드).

구조: App(공유: 웹소켓/REST/등록큐/순위창) + View(조건검색 창 하나 = 화면+조건seq).
'창+' 버튼으로 독립 조건검색 창 추가(조건별 동시 감시, 시세 REG는 참조수 공유)."""
import asyncio
import logging
import os
import sys
import time
from collections import Counter

import qasync
import config
from PySide6.QtCore import (
    QDate, QPoint, QSettings, QSize, Qt, QTimer, QUrl, QUrlQuery,
)
from PySide6.QtGui import QColor, QDesktopServices, QFont
from PySide6.QtWidgets import (
    QApplication, QComboBox, QDateEdit, QDialog, QFormLayout,
    QHBoxLayout, QLabel, QMainWindow, QLineEdit, QMessageBox, QProgressBar,
    QPushButton, QSplitter, QTableWidget, QTableWidgetItem, QTabWidget,
    QVBoxLayout, QWidget,
)

from analysis_db import (
    DB_PATH, database_stats, initialize, save_stock_history, start_collection,
    update_collection, limit_up_rows, limit_up_stocks, save_dart_corp_codes,
    sync_stock_catalog, save_krx_market_day, krx_collected_dates,
    pending_intraday_events, save_last_entry_time,
    save_disclosures, disclosure_rows, disclosure_list_rows,
    pending_disclosure_stocks, mark_disclosure_range_collected,
    save_theme_snapshot, save_source_classifications,
    limit_up_codes_without_sources, dart_inferred_classifications,
    theme_summary_rows, save_investor_flows, investor_flow_rows,
    pending_investor_flow_stocks, market_dashboard,
    limit_up_backtest_rows,
)
from api import RestClient
from classification_api import ClassificationClient
from dart_api import DartClient
from krx_api import KrxClient
from gui import ConditionScreen
from order import OrderEngine, split_quantity
from rank import RankScreen, _beep
from ws import WSClient

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
    handlers=[logging.FileHandler("bot.log", encoding="utf-8"), logging.StreamHandler()],
)
log = logging.getLogger("main")

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
THEME_MODES = ("system", "dark", "light")
THEME_UI = {
    "system": ("🖥", "테마: 시스템 — Windows 설정을 따름"),
    "dark": ("🌙", "테마: 다크 — 클릭하면 라이트"),
    "light": ("☀", "테마: 라이트 — 클릭하면 시스템"),
}


def _apply_theme(app: QApplication, mode: str):
    scheme = {"dark": Qt.ColorScheme.Dark, "light": Qt.ColorScheme.Light}.get(
        mode, Qt.ColorScheme.Unknown)
    app.styleHints().setColorScheme(scheme)


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

        screen.condition_combo.activated.connect(self._on_condition_selected)
        screen.rank_period.activated.connect(self.on_refresh)  # 기준시간 변경 -> 즉시 재폴
        screen.refresh_btn.clicked.connect(self.on_refresh)
        screen.refresh_interval.setValue(int(self._settings.value(self.prefix + "refresh_interval", 3)))
        screen.auto_refresh.setChecked(self._settings.value(self.prefix + "auto_refresh", "false") == "true")
        if screen.auto_refresh.isChecked():
            self._auto_timer.start(screen.refresh_interval.value() * 1000)
        screen.auto_refresh.toggled.connect(self._on_auto_refresh)
        screen.refresh_interval.valueChanged.connect(self._on_interval_changed)
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

    # --- 편입/이탈 ---------------------------------------------------------
    def on_snapshot(self, codes: list[str]):
        cur = set(self.screen.model.codes)
        new = set(codes)
        for code in cur - new:
            self.screen.on_excluded(code)
            self.app.queue_real(code, add=False, suffix=self._real_suffix())
        for code in new - cur:
            self.screen.on_included(code, {"name": code})
            self.app.queue_real(code, add=True, suffix=self._real_suffix())
        if new - cur:
            self._schedule_refresh()
            self._maybe_beep()
        log.info("snapshot%s: %d codes (+%d/-%d) %s", self.prefix or " ",
                 len(new), len(new - cur), len(cur - new), ",".join(sorted(new)))

    def on_event(self, code: str, is_insert: bool):
        if is_insert:
            self.screen.on_included(code, {"name": code})
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


class App:
    def __init__(self, screen: ConditionScreen):
        self.rest = RestClient()
        self.ws = WSClient()
        self.orders = OrderEngine(self.rest, self._on_order_update)
        self._orderable_cache: dict[tuple[str, int], dict] = {}
        self._orderable_tasks: dict[ConditionScreen, asyncio.Task] = {}
        self._orderable_prefetch_task = None
        self._orderable_prefetch_failed: dict[tuple[str, int], float] = {}
        self._orderable_prefetch_timer = QTimer()
        self._orderable_prefetch_timer.timeout.connect(
            self._queue_orderable_prefetch)
        self._orderable_prefetch_timer.start(400)
        self._settings = QSettings("layout.ini", QSettings.IniFormat)
        self._theme_mode = str(self._settings.value("theme_mode", "system"))
        if self._theme_mode not in THEME_MODES:
            self._theme_mode = "system"
        self.views: list[View] = [View(self, screen)]
        self._extra_windows: list = []  # 추가 창(ConditionWindow) 목록
        self._cond_items = []           # CNSRLST 결과 (새 창 콤보 채우기용)
        self._condition_reload_id = 0   # 재조회 타임아웃과 실제 응답의 경합 방지
        self._market = None             # MarketInfo (새 창 모델 주입용)
        self._limit_cnt = None          # 어제까지 연속상한 일수 (연상 컬럼, 시작 시 1회, 일봉 계산)
        self._account_summary = None     # 주문 툴바 공통 실계좌 요약
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
        self._single_timer = QTimer()
        self._single_timer.timeout.connect(self._on_single_poll)
        self._single_timer.start(3000)
        self._rank = None
        self._analysis = None
        self._auto_intraday_timer = QTimer()
        self._auto_intraday_timer.timeout.connect(self._auto_intraday_collection)
        self._auto_intraday_timer.start(60000)
        QTimer.singleShot(10000, lambda: self._auto_intraday_collection(True))
        # 공인 IP 감시: 바뀌면 키움 화이트리스트에서 벗어나 API 차단 -> 상단바 경보
        self._public_ip = None
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
        self.ws.on_order = self.orders.on_order_event
        # 통합(_AL) 시세: 전 창 공통 설정. 첫 REG 전에 접미사 확정돼야 해서 여기서 복원
        if self._settings.value("unified_real", "false") == "true":
            self.ws.real_suffix = self.rest.suffix = "_AL"
            screen.unified_check.setChecked(True)  # toggled 연결 전 = 시각 상태만
        screen.unified_check.toggled.connect(self._on_unified)
        screen.theme_btn.clicked.connect(self._cycle_theme)
        self._sync_theme_button()
        self._wire_common(screen)

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

    def _wire_common(self, screen: ConditionScreen):
        screen.reload_btn.clicked.connect(self._reload_conditions)
        screen.rank_btn.clicked.connect(self._on_rank)
        screen.newwin_btn.clicked.connect(self._on_newwin)
        screen.analysis_btn.clicked.connect(self._on_analysis)
        screen.order_target_selected.connect(
            lambda code, price, target=screen:
            self._queue_orderable_quantity(target, code, price))
        screen.order_requested.connect(
            lambda code, mode, count, auto, total, price, target=screen:
            self._submit_order(target, code, mode, count, auto, total, price))
        screen.cancel_requested.connect(self._cancel_order)

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
            self.orders.submit(code, name, price, quantities, auto_cancel)
        except Exception as e:  # noqa: BLE001
            screen.set_order_state(code, "오류", f"상태 오류 · {e}", False)

    def _cancel_order(self, code: str):
        try:
            self.orders.manual_cancel(code)
        except Exception as e:  # noqa: BLE001
            for view in self.views:
                if code in view.screen.model.rows:
                    view.screen.set_order_state(
                        code, "오류", f"상태 취소오류 · {e}", False)

    def _on_order_update(self, batch, state: str):
        count = len(batch.children)
        mode = "자" if batch.auto_cancel else "수"
        if batch.error:
            compact = "장종료" if "장종료" in batch.error else "오류"
        elif batch.remaining_qty == 0 and batch.sent_count == count:
            compact = f"{mode} 완료"
        elif state.startswith("취소"):
            compact = f"{mode} 취소"
        else:
            compact = f"{mode} {batch.sent_count}/{count}"
        detail = (
            f"상태 {state} · {'자동취소' if batch.auto_cancel else '수동취소'}"
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
        asyncio.ensure_future(self.ws.set_real_suffix("_AL" if on else ""))
        for v in self.views:  # 전 종목 시세 강제 재백필: 편입 diff 없어도 KRX<->통합 값 교체
            v._schedule_refresh()

    async def _check_ip(self):
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

    async def start(self):
        asyncio.ensure_future(self._check_ip())  # 시작 즉시 IP 표시
        self.ws_task = asyncio.create_task(self.ws.run(self.rest.tokens.token))
        for _ in range(int(self._settings.value("cond_windows", 0))):
            self._open_window()  # 지난 세션의 추가 창 복원
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
            log.info("kosdaq %d, single %d, liquidation %d, nxt %d, misu %d, admin %d",
                     len(m.kosdaq), len(m.single), len(m.liquidation),
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

    def _inject_market(self, view: View):
        m = view.screen.model
        if self._limit_cnt is not None:
            m.limit_cnt = self._limit_cnt
        if self._market is None:
            return
        m.kosdaq, m.single, m.liquidation, m.nxt, m.misu, m.admin = (
            self._market.kosdaq, self._market.single, self._market.liquidation,
            self._market.nxt, self._market.misu, self._market.admin)
        m.new_today, m.new15, m.new30 = (
            self._market.new_today, self._market.new15, self._market.new30)
        m.shares = self._market.shares

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
        source = fields.pop("_real_suffix", None)
        for v in self.views:
            expected = "_NX" if v.seq == NXT_RATE_SEQ else self.ws.real_suffix
            # REST/내부 갱신(source=None)은 기존처럼 전달하고, 웹소켓은 시장 출처가 맞는 창에만 전달.
            if code in v.screen.model.rows and (source is None or source == expected):
                v.screen.on_tick(code, fields)

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
        """현재 활성 창의 보이는 행을 (코드, 시장접미사)별 참조수로 만든다."""
        refs = Counter()
        for view in self.views:
            suffix = view._real_suffix()
            for code in view.screen.model.codes:
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
        """전일거래량이 0인(동시호가 역산실패) 종목만 ka10081로 1회 백필."""
        for code in list(model.codes):
            if (model.rows[code].get("prev_vol", 0) == 0
                    and code not in self._prevvol_pending
                    and code not in self._prevvol_done):
                self._prevvol_pending.add(code)
                asyncio.ensure_future(self._fetch_prev_vol(code))

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
        if self._analysis is None:
            self._analysis = AnalysisWindow(self.rest)
        self._analysis.show()
        self._analysis.raise_()
        self._analysis.activateWindow()

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
        if self._analysis is None:
            self._analysis = AnalysisWindow(self.rest)
        if self._analysis._collection_task and not self._analysis._collection_task.done():
            return
        self._settings.setValue(key, today)
        self._settings.sync()
        self._analysis._start_intraday_enrichment(silent=True)

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
        screen.newwin_btn.setVisible(False)  # 추가 창에선 창+/순위/통합 숨김 (메인창에서만)
        screen.rank_btn.setVisible(False)
        screen.analysis_btn.setVisible(False)
        screen.unified_check.setVisible(False)  # 통합 시세는 전 창 공통 -> 메인창에서만 전환
        screen.theme_btn.setVisible(False)  # 테마는 앱 전체 공통 -> 메인창에서만 전환
        win = ConditionWindow(prefix, on_close=self._on_window_closed)
        _start_title_clock(win, f"[0156-{n}] 조건검색실시간")
        win.setCentralWidget(screen)
        view = View(self, screen)
        self._inject_market(view)
        if self._account_summary is not None:
            screen.set_account_summary(self._account_summary)
        self.views.append(view)
        self._wire_extra(screen)
        self._extra_windows.append(win)
        win.show()
        win.resize(self.views[0].screen.window().size())  # 크기는 항상 본창 따라감 (위치만 창별 기억)
        if seeded:  # 본창과 완전히 겹치지 않게 살짝 비껴 배치
            win.move(win.x() + 40, win.y() + 40)
        if self._cond_items:  # 이미 목록 받아놨으면 즉시 콤보 채움 + 자동 등록
            view.on_condition_list(self._cond_items)

    def _wire_extra(self, screen: ConditionScreen):
        screen.reload_btn.clicked.connect(self._reload_conditions)
        screen.order_target_selected.connect(
            lambda code, price, target=screen:
            self._queue_orderable_quantity(target, code, price))
        screen.order_requested.connect(
            lambda code, mode, count, auto, total, price, target=screen:
            self._submit_order(target, code, mode, count, auto, total, price))
        screen.cancel_requested.connect(self._cancel_order)

    def _on_window_closed(self, win):
        if _SHUTDOWN[0]:  # 앱 종료 동반 닫힘: 창 개수 보존 (재시작 때 복원용)
            return
        for v in list(self.views[1:]):
            if v.screen.window() is win:
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


class AnalysisWindow(QMainWindow):
    """시장 분석 전용 비모달 창. 기능은 탭 단위로 점진적으로 확장한다."""

    TABS = (
        ("시장 현황", "시장 요약과 주요 지표를 표시합니다."),
        ("상한가", "상한가 종목 수집·조회·성과 분석 화면입니다."),
        ("테마", "테마 강도와 종목 확산 흐름을 분석합니다."),
        ("수급", "투자자별 수급과 거래대금 흐름을 분석합니다."),
        ("공시", "OpenDART 공시와 종목 움직임을 연결합니다."),
        ("백테스트", "과거 신호의 이후 성과를 검증합니다."),
        ("데이터 관리", "SQLite 데이터 수집 상태와 갱신 작업을 관리합니다."),
    )

    def __init__(self, rest=None):
        super().__init__()
        self._rest = rest
        self._collection_task = None
        self._collection_cancelled = False
        self._dart_task = None
        self.setWindowTitle("분석")
        self._key = "analysis_geometry"
        self._settings = QSettings("layout.ini", QSettings.IniFormat)
        self._geo_timer = QTimer(self)
        self._geo_timer.setSingleShot(True)
        self._geo_timer.timeout.connect(self._save_geo)

        tabs = QTabWidget()
        self._tabs = tabs
        for title, description in self.TABS:
            page = QWidget()
            layout = QVBoxLayout(page)
            if title == "데이터 관리":
                self._build_data_page(layout)
            elif title == "시장 현황":
                self._build_market_page(layout)
            elif title == "상한가":
                self._build_limit_up_page(layout)
            elif title == "공시":
                self._build_disclosure_page(layout)
            elif title == "테마":
                self._build_theme_page(layout)
            elif title == "수급":
                self._build_flow_page(layout)
            elif title == "백테스트":
                self._build_backtest_page(layout)
            else:
                label = QLabel(description)
                label.setAlignment(Qt.AlignmentFlag.AlignCenter)
                layout.addWidget(label)
            tabs.addTab(page, title)
        tabs.currentChanged.connect(self._analysis_tab_changed)
        self.setCentralWidget(tabs)

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
        self._refresh_db_status()
        self._refresh_market_page()

    def _analysis_tab_changed(self, index: int):
        title = self._tabs.tabText(index)
        if title == "시장 현황":
            self._refresh_market_page()
        elif title == "상한가":
            self._refresh_limit_up_table()
        elif title == "테마":
            self._refresh_theme_table()
        elif title == "수급":
            self._refresh_flow_table()
        elif title == "공시":
            self._refresh_disclosure_table()
        elif title == "백테스트":
            self._refresh_backtest()
        elif title == "데이터 관리":
            self._refresh_db_status()

    def _build_backtest_page(self, layout: QVBoxLayout):
        controls = QHBoxLayout()
        self._backtest_from = QDateEdit(QDate.currentDate().addMonths(-6))
        self._backtest_from.setCalendarPopup(True)
        self._backtest_from.setDisplayFormat("yyyy-MM-dd")
        self._backtest_to = QDateEdit(QDate.currentDate())
        self._backtest_to.setCalendarPopup(True)
        self._backtest_to.setDisplayFormat("yyyy-MM-dd")
        run_btn = QPushButton("백테스트 실행")
        run_btn.clicked.connect(self._refresh_backtest)
        controls.addWidget(QLabel("상한가 발생기간"))
        controls.addWidget(self._backtest_from)
        controls.addWidget(QLabel("~"))
        controls.addWidget(self._backtest_to)
        controls.addWidget(run_btn)
        controls.addStretch(1)
        layout.addLayout(controls)
        description = QLabel(
            "기준: 상한가 다음 거래일 시가 매수 → 각 보유기간 종가 매도 "
            "· 수수료/세금/슬리피지 미반영")
        layout.addWidget(description)

        self._backtest_summary = self._dashboard_table(
            ("보유기간", "표본", "승률", "평균수익률", "중앙수익률"))
        self._backtest_summary.setMaximumHeight(190)
        layout.addWidget(self._backtest_summary)
        self._backtest_status = QLabel("백테스트 대기")
        layout.addWidget(self._backtest_status)
        columns = (
            "상한가일", "종목코드", "종목명", "시장", "진입일", "진입가",
            "1일", "3일", "5일", "10일", "20일", "20일최고", "20일최저",
        )
        self._backtest_table = QTableWidget(0, len(columns))
        self._backtest_table.setHorizontalHeaderLabels(columns)
        self._backtest_table.setSortingEnabled(True)
        self._backtest_table.setAlternatingRowColors(True)
        self._backtest_table.setEditTriggers(
            QTableWidget.EditTrigger.NoEditTriggers)
        self._backtest_table.verticalHeader().setVisible(False)
        layout.addWidget(self._backtest_table)

    def _refresh_backtest(self):
        if not hasattr(self, "_backtest_table"):
            return
        date_from = self._backtest_from.date().toString("yyyyMMdd")
        date_to = self._backtest_to.date().toString("yyyyMMdd")
        rows = limit_up_backtest_rows(date_from, date_to)
        horizons = (1, 3, 5, 10, 20)
        summary_rows = []
        for horizon in horizons:
            values = sorted(
                float(row[f"return_{horizon}"])
                for row in rows
                if row[f"return_{horizon}"] is not None
            )
            count = len(values)
            wins = sum(value > 0 for value in values)
            average = sum(values) / count if count else 0
            if count:
                middle = count // 2
                median = (
                    values[middle] if count % 2 else
                    (values[middle - 1] + values[middle]) / 2
                )
            else:
                median = 0
            summary_rows.append((
                f"{horizon}거래일", count,
                wins * 100.0 / count if count else 0,
                average, median,
            ))
        self._fill_dashboard_table(
            self._backtest_summary,
            [
                (
                    (row[0], int(row[0].removesuffix("거래일"))),
                    (f"{row[1]:,}", row[1]),
                    (f"{row[2]:.1f}%", row[2]),
                    (f"{row[3]:+.2f}%", row[3]),
                    (f"{row[4]:+.2f}%", row[4]),
                )
                for row in summary_rows
            ],
            0, Qt.SortOrder.AscendingOrder,
        )

        table = self._backtest_table
        table.setSortingEnabled(False)
        table.setRowCount(len(rows))
        for row_index, row in enumerate(rows):
            raw_values = (
                row["event_date"], row["stock_code"], row["stock_name"],
                row["market"], row["entry_date"], row["entry_price"],
                row["return_1"], row["return_3"], row["return_5"],
                row["return_10"], row["return_20"],
                row["max_return_20"], row["max_drawdown_20"],
            )
            for column, value in enumerate(raw_values):
                if column == 5:
                    item = NumericTableWidgetItem(
                        f"{int(value or 0):,}", value or 0)
                elif column >= 6:
                    text = "-" if value is None else f"{float(value):+.2f}%"
                    item = NumericTableWidgetItem(
                        text, float(value) if value is not None else -999999)
                    if value is not None:
                        item.setForeground(
                            QColor("#e53935") if value > 0 else
                            QColor("#1e88e5") if value < 0 else
                            QColor("#808080"))
                else:
                    item = QTableWidgetItem(str(value or ""))
                table.setItem(row_index, column, item)
        table.setSortingEnabled(True)
        table.resizeColumnsToContents()
        self._backtest_status.setText(
            f"상한가 사건 {len(rows):,}건 · 다음 거래일 시가 진입 가능 표본")

    @staticmethod
    def _dashboard_table(columns):
        table = QTableWidget(0, len(columns))
        table.setHorizontalHeaderLabels(columns)
        table.setSortingEnabled(True)
        table.setAlternatingRowColors(True)
        table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        table.setSelectionMode(QTableWidget.SelectionMode.NoSelection)
        table.verticalHeader().setVisible(False)
        table.horizontalHeader().setStretchLastSection(True)
        return table

    def _build_market_page(self, layout: QVBoxLayout):
        top = QHBoxLayout()
        self._market_basis = QLabel("최근 거래일 데이터 대기")
        refresh = QPushButton("새로고침")
        refresh.clicked.connect(self._refresh_market_page)
        top.addWidget(self._market_basis)
        top.addStretch(1)
        top.addWidget(refresh)
        layout.addLayout(top)
        self._market_summary = QLabel("")
        self._market_summary.setWordWrap(True)
        layout.addWidget(self._market_summary)

        self._market_theme_table = self._dashboard_table(
            ("강한 테마", "평균등락률", "거래대금", "상한가", "종목수"))
        self._market_leader_table = self._dashboard_table(
            ("주도주", "등락률", "거래대금", "테마"))
        self._market_limit_table = self._dashboard_table(
            ("오늘 상한가", "진입시각", "거래대금"))
        self._market_flow_table = self._dashboard_table(
            ("외인+기관 순매수", "순매수"))

        def pane(title, table):
            widget = QWidget()
            pane_layout = QVBoxLayout(widget)
            pane_layout.setContentsMargins(2, 2, 2, 2)
            pane_layout.addWidget(QLabel(title))
            pane_layout.addWidget(table)
            return widget

        self._market_top_splitter = QSplitter(Qt.Orientation.Horizontal)
        self._market_top_splitter.addWidget(
            pane("강한 테마 TOP 15", self._market_theme_table))
        self._market_top_splitter.addWidget(
            pane("거래대금 주도주 TOP 20", self._market_leader_table))
        self._market_bottom_splitter = QSplitter(Qt.Orientation.Horizontal)
        self._market_bottom_splitter.addWidget(
            pane("상한가", self._market_limit_table))
        self._market_bottom_splitter.addWidget(
            pane("외국인+기관 순매수 TOP 15", self._market_flow_table))
        self._market_vertical_splitter = QSplitter(Qt.Orientation.Vertical)
        self._market_vertical_splitter.addWidget(self._market_top_splitter)
        self._market_vertical_splitter.addWidget(self._market_bottom_splitter)
        self._market_vertical_splitter.setStretchFactor(0, 2)
        self._market_vertical_splitter.setStretchFactor(1, 1)
        layout.addWidget(self._market_vertical_splitter)

        splitter_settings = (
            ("analysis_market_vertical", self._market_vertical_splitter),
            ("analysis_market_top", self._market_top_splitter),
            ("analysis_market_bottom", self._market_bottom_splitter),
        )
        for key, splitter in splitter_settings:
            state = self._settings.value(key)
            if state is not None:
                splitter.restoreState(state)
            splitter.splitterMoved.connect(self._save_market_splitters)

    def _save_market_splitters(self, *_args):
        if not hasattr(self, "_market_vertical_splitter"):
            return
        self._settings.setValue(
            "analysis_market_vertical",
            self._market_vertical_splitter.saveState())
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

    def _refresh_market_page(self):
        if not hasattr(self, "_market_summary"):
            return
        data = market_dashboard()
        trade_date = data["trade_date"]
        if not trade_date:
            self._market_basis.setText("저장된 시장 데이터가 없습니다.")
            return
        self._market_basis.setText(
            f"기준일 {trade_date} 마감 데이터"
            + (" · 장전에는 전 거래일 시황으로 표시됩니다."
               if trade_date != QDate.currentDate().toString("yyyyMMdd") else ""))
        summaries = []
        for row in data["markets"]:
            summaries.append(
                f"{row['market']} 종목 {row['stock_count']:,} · "
                f"상승 {row['rising']:,} / 하락 {row['falling']:,} "
                f"/ 보합 {row['unchanged']:,} · "
                f"거래대금 {row['trading_value'] / 100_000_000:,.0f}억원 · "
                f"상한가 {row['limit_up_count']:,}")
        self._market_summary.setText("     ".join(summaries))
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
                (f"{(row['trading_value'] or 0)/100_000_000:,.0f}억",
                 row["trading_value"] or 0),
            ) for row in data["limit_ups"]
        ], 1, Qt.SortOrder.AscendingOrder)
        self._fill_dashboard_table(self._market_flow_table, [
            (
                f"{row['stock_name']} ({row['stock_code']})",
                (f"{row['net']:+,}", row["net"]),
            ) for row in data["flows"]
        ], 1, Qt.SortOrder.DescendingOrder)

    def _build_flow_page(self, layout: QVBoxLayout):
        controls = QHBoxLayout()
        self._flow_from = QDateEdit(QDate.currentDate().addMonths(-1))
        self._flow_from.setCalendarPopup(True)
        self._flow_from.setDisplayFormat("yyyy-MM-dd")
        self._flow_to = QDateEdit(QDate.currentDate())
        self._flow_to.setCalendarPopup(True)
        self._flow_to.setDisplayFormat("yyyy-MM-dd")
        self._flow_search = QLineEdit()
        self._flow_search.setPlaceholderText("종목코드·종목명 검색")
        self._flow_search.setClearButtonEnabled(True)
        self._flow_search.returnPressed.connect(self._refresh_flow_table)
        self._flow_top_n = QComboBox()
        for count in (50, 100, 200):
            self._flow_top_n.addItem(f"시장별 {count}종목", count)
        self._flow_top_n.setCurrentIndex(1)
        self._flow_view_mode = QComboBox()
        self._flow_view_mode.addItem("날짜별 보기", "date")
        self._flow_view_mode.addItem("종목별 보기", "stock")
        self._flow_view_mode.currentIndexChanged.connect(
            self._refresh_flow_table)
        refresh = QPushButton("조회")
        refresh.clicked.connect(self._refresh_flow_table)
        self._flow_collect_btn = QPushButton("키움 수급 수집")
        self._flow_collect_btn.clicked.connect(self._start_flow_collection)
        self._flow_cancel_btn = QPushButton("수집 중지")
        self._flow_cancel_btn.setEnabled(False)
        self._flow_cancel_btn.clicked.connect(self._cancel_history_collection)
        controls.addWidget(QLabel("기간"))
        controls.addWidget(self._flow_from)
        controls.addWidget(QLabel("~"))
        controls.addWidget(self._flow_to)
        controls.addWidget(self._flow_search, 1)
        controls.addWidget(self._flow_view_mode)
        controls.addWidget(self._flow_top_n)
        controls.addWidget(refresh)
        controls.addWidget(self._flow_collect_btn)
        controls.addWidget(self._flow_cancel_btn)
        layout.addLayout(controls)

        self._flow_status = QLabel(
            "KOSPI·KOSDAQ 거래대금 상위 100종목과 상한가 종목의 순매수 "
            "· 단위: 백만원")
        layout.addWidget(self._flow_status)
        columns = (
            "거래일", "종목코드", "종목명", "시장", "상한가", "개인", "외국인",
            "기관", "외인+기관", "5일누적", "20일누적", "연속순매수",
            "거래대금", "수급비율", "금융투자", "투신", "연기금",
        )
        table = QTableWidget(0, len(columns))
        table.setHorizontalHeaderLabels(columns)
        table.setSortingEnabled(True)
        table.setAlternatingRowColors(True)
        table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        table.verticalHeader().setVisible(False)
        table.horizontalHeader().setStretchLastSection(False)
        self._flow_sort_clicked: set[int] = set()
        table.horizontalHeader().sectionClicked.connect(
            self._flow_header_clicked)
        table.cellClicked.connect(self._flow_table_clicked)
        table.setContextMenuPolicy(
            Qt.ContextMenuPolicy.CustomContextMenu)
        table.customContextMenuRequested.connect(
            self._flow_table_right_clicked)
        self._flow_columns_sized = False
        self._flow_table = table
        layout.addWidget(table)

    def _flow_table_clicked(self, row: int, column: int):
        code_item = self._flow_table.item(row, 1)
        stock_code = code_item.text().strip() if code_item is not None else ""
        if not stock_code:
            return
        if column == 1:
            QApplication.clipboard().setText(stock_code)
            self.statusBar().showMessage(
                f"종목코드 {stock_code}를 복사했습니다.", 3000)
        elif column == 2:
            QDesktopServices.openUrl(QUrl(
                "https://finance.naver.com/item/coinfo.naver"
                f"?code={stock_code}"))

    def _flow_table_right_clicked(self, position: QPoint):
        item = self._flow_table.itemAt(position)
        if item is None or item.column() != 2:
            return
        code_item = self._flow_table.item(item.row(), 1)
        stock_code = (
            code_item.text().strip() if code_item is not None else "")
        if stock_code:
            QDesktopServices.openUrl(QUrl(
                "https://finance.naver.com/item/board.naver"
                f"?code={stock_code}"))

    def _flow_header_clicked(self, column: int):
        """각 컬럼을 처음 누를 때는 큰 값부터 보이도록 내림차순 적용."""
        if column in self._flow_sort_clicked:
            return
        self._flow_sort_clicked.add(column)
        # QTableWidget의 기본 클릭 정렬이 처리된 다음 강제해야 다시
        # 오름차순으로 뒤집히지 않는다.
        QTimer.singleShot(
            0,
            lambda selected_column=column: self._flow_table.sortItems(
                selected_column, Qt.SortOrder.DescendingOrder),
        )

    def _flow_dates(self):
        return (
            self._flow_from.date().toString("yyyyMMdd"),
            self._flow_to.date().toString("yyyyMMdd"),
        )

    def _refresh_flow_table(self):
        if not hasattr(self, "_flow_table"):
            return
        date_from, date_to = self._flow_dates()
        rows, total = investor_flow_rows(
            date_from, date_to, self._flow_search.text(),
            self._flow_view_mode.currentData())
        table = self._flow_table
        table.setSortingEnabled(False)
        table.setRowCount(len(rows))
        fields = (
            "trade_date", "stock_code", "stock_name", "market",
            "is_limit_up", "individual_net", "foreign_net", "institution_net",
            "foreign_inst_net", "foreign_inst_5d", "foreign_inst_20d",
            "consecutive_buy_days", "trading_value_million",
            "foreign_inst_ratio", "financial_investment_net",
            "investment_trust_net", "pension_net",
        )
        view_mode = self._flow_view_mode.currentData()
        for row_index, row in enumerate(rows):
            for column, field in enumerate(fields):
                value = row[field]
                if column == 0 and view_mode == "date":
                    item = NumericTableWidgetItem(
                        str(value or ""),
                        (
                            -int(row["trade_date"]),
                            -int(row["foreign_inst_net"] or 0),
                        ),
                    )
                elif column == 1 and view_mode == "stock":
                    item = NumericTableWidgetItem(
                        str(value or ""),
                        (str(row["stock_code"]), -int(row["trade_date"])),
                    )
                elif column == 4:
                    item = QTableWidgetItem("상한가" if value else "")
                elif column >= 5:
                    suffix = "%" if column == 13 else ""
                    text = (
                        f"{float(value or 0):,.2f}{suffix}"
                        if column == 13 else f"{int(value or 0):+,}"
                    )
                    if column == 11:
                        text = f"{int(value or 0):,}일"
                    elif column == 12:
                        text = f"{int(value or 0):,}"
                    item = NumericTableWidgetItem(text, value or 0)
                    item.setTextAlignment(
                        Qt.AlignmentFlag.AlignRight
                        | Qt.AlignmentFlag.AlignVCenter)
                    if column in (5, 6, 7, 8, 9, 10, 13, 14, 15, 16):
                        numeric_value = float(value or 0)
                        if numeric_value > 0:
                            item.setForeground(QColor("#e53935"))
                        elif numeric_value < 0:
                            item.setForeground(QColor("#1e88e5"))
                        else:
                            item.setForeground(QColor("#808080"))
                else:
                    item = QTableWidgetItem(str(value or ""))
                if view_mode == "date":
                    sort_value = (
                        float(value or 0)
                        if column >= 4 else str(value or "")
                    )
                    grouped_item = GroupedTableWidgetItem(
                        item.text(), int(row["trade_date"]), sort_value)
                    grouped_item.setTextAlignment(item.textAlignment())
                    grouped_item.setForeground(item.foreground())
                    item = grouped_item
                table.setItem(row_index, column, item)
        table.setSortingEnabled(True)
        if view_mode == "stock":
            table.sortItems(1, Qt.SortOrder.AscendingOrder)
        else:
            table.sortItems(0, Qt.SortOrder.DescendingOrder)
        if not self._flow_columns_sized:
            table.resizeColumnsToContents()
            self._flow_columns_sized = True
        suffix = (
            f" · 최신 {len(rows):,}건 표시" if total > len(rows) else "")
        self._flow_status.setText(
            f"주요 수급 {total:,}건{suffix} · 거래대금 상위+상한가 "
            "· 단위: 백만원 "
            "· 수급비율=(외국인+기관)/거래대금")

    def _start_flow_collection(self):
        if self._rest is None:
            QMessageBox.warning(
                self, "키움 수급", "키움 REST 연결이 준비되지 않았습니다.")
            return
        if self._collection_task and not self._collection_task.done():
            return
        date_from, date_to = self._flow_dates()
        if date_from > date_to:
            QMessageBox.warning(self, "키움 수급", "시작일이 종료일보다 늦습니다.")
            return
        top_n = int(self._flow_top_n.currentData())
        stocks = pending_investor_flow_stocks(date_from, date_to, top_n)
        if not stocks:
            QMessageBox.information(
                self, "키움 수급", "이 기간의 수급은 이미 저장됐거나 "
                "거래대금 상위 종목 데이터가 없습니다.")
            return
        self._collection_cancelled = False
        self._flow_collect_btn.setEnabled(False)
        self._flow_cancel_btn.setEnabled(True)
        self._collection_task = asyncio.ensure_future(
            self._collect_investor_flows(date_from, date_to, stocks))

    async def _collect_investor_flows(
        self, date_from: str, date_to: str, stocks: list[dict]
    ):
        run_id = start_collection(
            "KIWOOM_INVESTOR_FLOW", date_from, date_to)
        processed = saved = errors = 0
        status, message = "COMPLETED", ""
        try:
            for index, stock in enumerate(stocks, 1):
                if self._collection_cancelled:
                    status, message = "CANCELLED", "사용자가 중지함"
                    break
                try:
                    rows = await self._rest.investor_flows(
                        stock["stock_code"], date_from, date_to)
                    saved += save_investor_flows(
                        stock["stock_code"], rows)
                except Exception as error:  # noqa: BLE001
                    errors += 1
                    log.warning(
                        "investor flow %s failed: %s",
                        stock["stock_code"], error)
                processed += 1
                self._flow_status.setText(
                    f"수급 수집 {index:,}/{len(stocks):,} · "
                    f"{stock['stock_name']} ({stock['stock_code']}) "
                    f"· 저장 {saved:,} · 오류 {errors:,}")
                update_collection(
                    run_id, "RUNNING", processed, saved, errors,
                    stock["stock_name"])
                await asyncio.sleep(0)
        except Exception as error:  # noqa: BLE001
            status, message = "FAILED", str(error)
            log.exception("investor flow collection failed")
        finally:
            update_collection(
                run_id, status, processed, saved, errors, message)
            self._flow_status.setText(
                f"{status} · 종목 {processed:,} · 저장 {saved:,} "
                f"· 오류 {errors:,}")
            self._flow_collect_btn.setEnabled(True)
            self._flow_cancel_btn.setEnabled(False)
            self._collection_task = None
            self._refresh_flow_table()

    def _build_theme_page(self, layout: QVBoxLayout):
        controls = QHBoxLayout()
        self._theme_search = QLineEdit()
        self._theme_search.setPlaceholderText("테마명·종목코드·종목명 검색")
        self._theme_search.setClearButtonEnabled(True)
        self._theme_search.returnPressed.connect(self._refresh_theme_table)
        refresh = QPushButton("조회")
        refresh.clicked.connect(self._refresh_theme_table)
        controls.addWidget(QLabel("검색"))
        controls.addWidget(self._theme_search, 1)
        controls.addWidget(refresh)
        layout.addLayout(controls)

        self._theme_summary = QLabel("테마 0개")
        layout.addWidget(self._theme_summary)
        columns = ("출처", "테마", "구성종목", "상한가종목", "종목 목록")
        table = QTableWidget(0, len(columns))
        table.setHorizontalHeaderLabels(columns)
        table.setSortingEnabled(True)
        table.setAlternatingRowColors(True)
        table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        table.verticalHeader().setVisible(False)
        table.horizontalHeader().setStretchLastSection(True)
        table.cellClicked.connect(self._theme_table_clicked)
        self._theme_table = table
        layout.addWidget(table)

    def _refresh_theme_table(self):
        if not hasattr(self, "_theme_table"):
            return
        rows = theme_summary_rows(self._theme_search.text())
        table = self._theme_table
        table.setSortingEnabled(False)
        table.setRowCount(len(rows))
        source_names = {
            "NAVER": "네이버", "KIWOOM": "키움", "WICS": "WICS",
            "KRX": "KRX", "DART": "DART",
        }
        source_priority = {
            "NAVER": 1, "KIWOOM": 2, "WICS": 3, "KRX": 4, "DART": 5,
        }
        total_members = 0
        for row_index, row in enumerate(rows):
            total_members += int(row["member_count"] or 0)
            members = str(row["members"] or "").replace(",", ", ")
            values = (
                source_names.get(row["source"], row["source"]),
                row["theme_name"],
                int(row["member_count"] or 0),
                int(row["limit_up_count"] or 0),
                members,
            )
            for column, value in enumerate(values):
                if column == 0:
                    item = NumericTableWidgetItem(
                        str(value or ""),
                        source_priority.get(row["source"], 99),
                    )
                elif column in (2, 3):
                    item = NumericTableWidgetItem(f"{value:,}", value)
                else:
                    item = QTableWidgetItem(str(value or ""))
                if column == 1 and row["source"] == "NAVER":
                    source_code = str(row["source_code"] or "").strip()
                    if source_code:
                        item.setData(
                            Qt.ItemDataRole.UserRole + 4, source_code,
                        )
                        item.setToolTip(
                            "클릭하면 네이버 금융 테마 상세 페이지를 엽니다.")
                if column == 4:
                    item.setToolTip(members)
                table.setItem(row_index, column, item)
        table.setSortingEnabled(True)
        table.resizeColumnsToContents()
        table.setColumnWidth(1, min(260, max(140, table.columnWidth(1))))
        table.setColumnWidth(4, 500)
        table.sortItems(0, Qt.SortOrder.AscendingOrder)
        self._theme_summary.setText(
            f"현재 테마 {len(rows):,}개 · 종목 연결 {total_members:,}건")

    def _theme_table_clicked(self, row: int, column: int):
        if column != 1:
            return
        item = self._theme_table.item(row, column)
        source_code = (
            item.data(Qt.ItemDataRole.UserRole + 4)
            if item is not None else ""
        )
        if source_code:
            url = QUrl(
                "https://finance.naver.com/sise/sise_group_detail.naver")
            query = QUrlQuery()
            query.addQueryItem("type", "theme")
            query.addQueryItem("no", str(source_code))
            url.setQuery(query)
            self.statusBar().showMessage(
                f"네이버 테마 열기: {item.text()} (no={source_code})", 5000)
            QDesktopServices.openUrl(url)

    def _build_disclosure_page(self, layout: QVBoxLayout):
        controls = QHBoxLayout()
        self._disclosure_from = QDateEdit(QDate.currentDate().addMonths(-6))
        self._disclosure_from.setCalendarPopup(True)
        self._disclosure_from.setDisplayFormat("yyyy-MM-dd")
        self._disclosure_to = QDateEdit(QDate.currentDate())
        self._disclosure_to.setCalendarPopup(True)
        self._disclosure_to.setDisplayFormat("yyyy-MM-dd")
        self._disclosure_search = QLineEdit()
        self._disclosure_search.setPlaceholderText(
            "종목코드·종목명·공시명·제출인 검색")
        self._disclosure_search.returnPressed.connect(
            self._refresh_disclosure_table)
        refresh = QPushButton("조회")
        refresh.clicked.connect(self._refresh_disclosure_table)
        controls.addWidget(QLabel("기간"))
        controls.addWidget(self._disclosure_from)
        controls.addWidget(QLabel("~"))
        controls.addWidget(self._disclosure_to)
        controls.addWidget(self._disclosure_search, 1)
        controls.addWidget(refresh)
        layout.addLayout(controls)

        self._disclosure_summary = QLabel("공시 0건")
        layout.addWidget(self._disclosure_summary)
        columns = (
            "접수일", "종목코드", "종목명", "공시명",
            "제출인", "유형", "정정", "접수번호",
        )
        table = QTableWidget(0, len(columns))
        table.setHorizontalHeaderLabels(columns)
        table.setSortingEnabled(True)
        table.setAlternatingRowColors(True)
        table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        table.verticalHeader().setVisible(False)
        table.horizontalHeader().setStretchLastSection(True)
        table.cellClicked.connect(self._open_disclosure_from_tab)
        self._disclosure_table = table
        layout.addWidget(table)

    def _refresh_disclosure_table(self):
        if not hasattr(self, "_disclosure_table"):
            return
        date_from = self._disclosure_from.date().toString("yyyyMMdd")
        date_to = self._disclosure_to.date().toString("yyyyMMdd")
        rows, total = disclosure_list_rows(
            date_from, date_to, self._disclosure_search.text())
        table = self._disclosure_table
        table.setSortingEnabled(False)
        table.setRowCount(len(rows))
        for row_index, row in enumerate(rows):
            values = (
                row["receipt_date"], row["stock_code"], row["stock_name"],
                row["report_name"], row["submitter"], row["disclosure_type"],
                "정정" if row["correction"] else "", row["receipt_no"],
            )
            for column, value in enumerate(values):
                item = QTableWidgetItem(str(value or ""))
                item.setData(
                    DisclosureDialog.URL_ROLE, row["source_url"])
                table.setItem(row_index, column, item)
        table.setSortingEnabled(True)
        table.resizeColumnsToContents()
        suffix = (
            f" · 최신 {len(rows):,}건 표시" if total > len(rows) else "")
        self._disclosure_summary.setText(f"검색 공시 {total:,}건{suffix}")

    def _open_disclosure_from_tab(self, row: int, column: int):
        if column != 3:
            return
        item = self._disclosure_table.item(row, 0)
        url = item.data(DisclosureDialog.URL_ROLE) if item else ""
        if url:
            QDesktopServices.openUrl(QUrl(url))

    def _build_limit_up_page(self, layout: QVBoxLayout):
        controls = QHBoxLayout()
        self._limit_from = QDateEdit(QDate.currentDate().addMonths(-6))
        self._limit_from.setCalendarPopup(True)
        self._limit_from.setDisplayFormat("yyyy-MM-dd")
        self._limit_to = QDateEdit(QDate.currentDate())
        self._limit_to.setCalendarPopup(True)
        self._limit_to.setDisplayFormat("yyyy-MM-dd")
        refresh = QPushButton("조회")
        refresh.clicked.connect(self._refresh_limit_up_table)
        disclosure_btn = QPushButton("선택 종목 공시 보기")
        disclosure_btn.clicked.connect(self._open_selected_disclosures)
        self._dart_btn = QPushButton("DART 공시 수집")
        self._dart_btn.clicked.connect(self._start_dart_collection)
        controls.addWidget(QLabel("기간"))
        controls.addWidget(self._limit_from)
        controls.addWidget(QLabel("~"))
        controls.addWidget(self._limit_to)
        controls.addWidget(refresh)
        controls.addWidget(disclosure_btn)
        controls.addWidget(self._dart_btn)
        controls.addStretch(1)
        layout.addLayout(controls)

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
        columns = ("거래일", "종목코드", "종목명", "시장", "상한가진입", "종가",
                   "등락률", "거래량", "거래대금", "연속", "공시", "테마")
        self._limit_table = QTableWidget(0, len(columns))
        self._limit_table.setHorizontalHeaderLabels(columns)
        self._limit_table.horizontalHeaderItem(4).setToolTip(
            "거래일별로 묶어 같은 날 먼저 상한가에 진입한 순서로 정렬")
        self._limit_table.setSortingEnabled(True)
        self._limit_table.setAlternatingRowColors(True)
        self._limit_table.setEditTriggers(QTableWidget.EditTrigger.NoEditTriggers)
        self._limit_table.verticalHeader().setVisible(False)
        # 마지막 컬럼도 자동 확장하지 않고 사용자가 경계선을 끌어 폭을 조절한다.
        self._limit_table.horizontalHeader().setStretchLastSection(False)
        saved_header = self._settings.value("analysis_limit_header")
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
            entry_item = table.item(row, 4)
            missing = not entry_item or entry_item.text().strip() in ("", "-")
            table.setRowHidden(row, column == 4 and missing)

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
        table = self._limit_table
        table.setSortingEnabled(False)
        table.setRowCount(len(rows))
        for row_index, row in enumerate(rows):
            values = (
                row["trade_date"], row["stock_code"], row["stock_name"],
                row["market"], row["last_entry_time"] or "-",
                f"{row['close_price'] or 0:,}",
                f"{row['change_rate'] or 0:.2f}%",
                f"{row['volume'] or 0:,}", f"{row['trading_value'] or 0:,}",
                str(row["consecutive_days"]), str(row["disclosure_count"]),
                row["theme_names"] or "-",
            )
            for column, value in enumerate(values):
                number = {
                    5: row["close_price"] or 0,
                    6: row["change_rate"] or 0,
                    7: row["volume"] or 0,
                    8: row["trading_value"] or 0,
                    9: row["consecutive_days"] or 0,
                    10: row["disclosure_count"] or 0,
                }.get(column)
                if column == 4:
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
                    item = NumericTableWidgetItem(value, number)
                else:
                    item = QTableWidgetItem(value)
                item.setData(
                    Qt.ItemDataRole.UserRole + 2, row["stock_code"])
                item.setData(
                    Qt.ItemDataRole.UserRole + 3, row["stock_name"])
                if column == 11 and row["theme_names"]:
                    item.setToolTip(row["theme_names"])
                if column in (5, 6, 7, 8, 9, 10):
                    item.setTextAlignment(
                        Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
                table.setItem(row_index, column, item)
        table.setSortingEnabled(True)
        if not self._limit_header_restored:
            table.resizeColumnsToContents()
            table.setColumnWidth(
                11, min(280, max(140, table.columnWidth(11))))
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
            "analysis_limit_header",
            self._limit_table.horizontalHeader().saveState(),
        )
        self._settings.sync()

    def _limit_table_clicked(self, row: int, column: int):
        """종목코드 복사, 기업정보 및 공시 열기를 컬럼별로 처리한다."""
        stock_code = self._limit_stock_code(row)
        if column == 1 and stock_code:
            QApplication.clipboard().setText(stock_code)
            self.statusBar().showMessage(
                f"종목코드 {stock_code}를 복사했습니다.", 3000)
        elif column == 2 and stock_code:
            QDesktopServices.openUrl(QUrl(
                f"https://finance.naver.com/item/coinfo.naver?code={stock_code}"))
        elif column == 10:
            self._open_disclosures_for_row(row)

    def _limit_table_right_clicked(self, position: QPoint):
        """종목명을 오른쪽 클릭하면 네이버 종목 토론실을 연다."""
        item = self._limit_table.itemAt(position)
        if item is None or item.column() != 2:
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

    async def _collect_dart(self):
        date_from, date_to = self._limit_dates()
        client = DartClient(config.DART_API_KEY)
        saved = errors = 0
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
                except Exception as error:  # noqa: BLE001
                    errors += 1
                    log.warning("DART %s: %s", stock["stock_code"], error)
                self._limit_summary.setText(
                    f"DART {index:,}/{len(stocks):,} · 공시 {saved:,}건 "
                    f"· 기존 제외 {skipped:,}종목 "
                    f"· 기업코드 없음 {missing_corp:,}종목 "
                    f"· 오류 {errors:,}건")
                await asyncio.sleep(0)
        except Exception as error:  # noqa: BLE001
            log.exception("DART collection failed")
            QMessageBox.critical(self, "DART 공시", f"수집에 실패했습니다.\n{error}")
        finally:
            await client.close()
            self._dart_btn.setEnabled(True)
            if hasattr(self, "_data_dart_btn"):
                self._data_dart_btn.setEnabled(True)
            self._dart_task = None
            self._refresh_limit_up_table()
            self._refresh_db_status()

    def _build_data_page(self, layout: QVBoxLayout):
        heading = QLabel("분석 데이터베이스")
        heading_font = QFont(heading.font())
        heading_font.setBold(True)
        heading.setFont(heading_font)
        layout.addWidget(heading)

        form = QFormLayout()
        self._db_path_label = QLabel(str(DB_PATH))
        self._db_path_label.setTextInteractionFlags(
            Qt.TextInteractionFlag.TextSelectableByMouse)
        self._db_state_label = QLabel()
        self._db_size_label = QLabel()
        self._db_rows_label = QLabel()
        self._db_date_label = QLabel()
        self._db_run_label = QLabel()
        self._dart_state_label = QLabel(
            "설정됨" if config.DART_API_KEY else "미설정 (.env의 DART_API_KEY)")
        self._krx_state_label = QLabel(
            "설정됨" if config.KRX_API_KEY else "미설정 (.env의 KRX_API_KEY)")
        form.addRow("파일", self._db_path_label)
        form.addRow("상태", self._db_state_label)
        form.addRow("크기", self._db_size_label)
        form.addRow("저장 건수", self._db_rows_label)
        form.addRow("최근 거래일", self._db_date_label)
        form.addRow("최근 수집", self._db_run_label)
        form.addRow("KRX API", self._krx_state_label)
        form.addRow("DART API", self._dart_state_label)
        layout.addLayout(form)

        buttons = QHBoxLayout()
        create_btn = QPushButton("DB 생성/확인")
        create_btn.clicked.connect(self._initialize_db)
        refresh_btn = QPushButton("상태 새로고침")
        refresh_btn.clicked.connect(self._refresh_db_status)
        folder_btn = QPushButton("DB 폴더 열기")
        folder_btn.clicked.connect(self._open_db_folder)
        buttons.addWidget(create_btn)
        buttons.addWidget(refresh_btn)
        buttons.addWidget(folder_btn)
        buttons.addStretch(1)
        layout.addLayout(buttons)

        range_row = QHBoxLayout()
        self._date_from = QDateEdit(QDate.currentDate().addMonths(-6))
        self._date_from.setCalendarPopup(True)
        self._date_from.setDisplayFormat("yyyy-MM-dd")
        self._date_to = QDateEdit(QDate.currentDate())
        self._date_to.setCalendarPopup(True)
        self._date_to.setDisplayFormat("yyyy-MM-dd")
        self._krx_btn = QPushButton("KRX 상한가 수집")
        self._krx_btn.clicked.connect(self._start_krx_collection)
        self._collect_btn = QPushButton("키움 장중정보 보완")
        self._collect_btn.clicked.connect(self._start_intraday_enrichment)
        self._data_dart_btn = QPushButton("DART 공시 수집")
        self._data_dart_btn.clicked.connect(self._start_dart_collection)
        self._theme_btn = QPushButton("키움 테마 수집")
        self._theme_btn.clicked.connect(self._start_theme_collection)
        self._naver_theme_btn = QPushButton("네이버 테마 수집")
        self._naver_theme_btn.clicked.connect(
            self._start_naver_theme_collection)
        self._cancel_btn = QPushButton("수집 중지")
        self._cancel_btn.setEnabled(False)
        self._cancel_btn.clicked.connect(self._cancel_history_collection)
        range_row.addWidget(QLabel("기간"))
        range_row.addWidget(self._date_from)
        range_row.addWidget(QLabel("~"))
        range_row.addWidget(self._date_to)
        range_row.addWidget(self._krx_btn)
        range_row.addWidget(self._collect_btn)
        range_row.addWidget(self._data_dart_btn)
        range_row.addWidget(self._theme_btn)
        range_row.addWidget(self._naver_theme_btn)
        range_row.addWidget(self._cancel_btn)
        range_row.addStretch(1)
        layout.addLayout(range_row)

        self._collection_progress = QProgressBar()
        self._collection_progress.setRange(0, 1)
        self._collection_progress.setValue(0)
        self._collection_status = QLabel("수집 대기")
        layout.addWidget(self._collection_progress)
        layout.addWidget(self._collection_status)

        note = QLabel(
            "KRX는 날짜별 KOSPI·KOSDAQ 일봉과 상한가를 수집하고 이미 저장된 "
            "KRX 거래일은 제외합니다. 키움 장중정보는 상한가 진입시각 보완용이며, "
            "키움·네이버 테마는 현재 구성 종목을 날짜별 변경 이력으로 저장합니다.")
        note.setWordWrap(True)
        layout.addWidget(note)
        layout.addStretch(1)

    def _initialize_db(self):
        try:
            initialize()
            self._refresh_db_status()
        except Exception as error:  # noqa: BLE001
            log.exception("analysis DB initialize failed")
            QMessageBox.critical(self, "분석 DB", f"DB 생성에 실패했습니다.\n{error}")

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
            f"테마 {stats['themes']:,} / 연결 {stats['stock_themes']:,}")
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

    def _open_db_folder(self):
        DB_PATH.parent.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(os.fspath(DB_PATH.parent)))

    def _start_theme_collection(self):
        if self._rest is None:
            QMessageBox.warning(
                self, "키움 테마", "키움 REST 연결이 준비되지 않았습니다.")
            return
        if self._collection_task and not self._collection_task.done():
            return
        self._collection_cancelled = False
        self._krx_btn.setEnabled(False)
        self._collect_btn.setEnabled(False)
        self._data_dart_btn.setEnabled(False)
        self._theme_btn.setEnabled(False)
        self._naver_theme_btn.setEnabled(False)
        self._cancel_btn.setEnabled(True)
        self._collection_task = asyncio.ensure_future(self._collect_themes())

    async def _collect_themes(self):
        snapshot_date = QDate.currentDate().toString("yyyyMMdd")
        run_id = start_collection(
            "KIWOOM_THEME", snapshot_date, snapshot_date)
        processed = saved = errors = 0
        status, message = "COMPLETED", ""
        try:
            groups = await self._rest.theme_groups()
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
                    snapshots, snapshot_date)
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
                message = (
                    f"키움 {kiwoom_links:,} · WICS {wics_links:,} · "
                    f"KRX {krx_links:,} · DART {dart_links:,}건")
        except Exception as error:  # noqa: BLE001
            status, message = "FAILED", str(error)
            log.exception("Kiwoom theme collection failed")
            QMessageBox.critical(
                self, "키움 테마", f"테마 수집에 실패했습니다.\n{error}")
        finally:
            update_collection(
                run_id, status, processed, saved, errors, message)
            self._collection_status.setText(
                f"{status} · {message or '저장하지 않음'}")
            self._krx_btn.setEnabled(True)
            self._collect_btn.setEnabled(True)
            self._data_dart_btn.setEnabled(True)
            self._theme_btn.setEnabled(True)
            self._naver_theme_btn.setEnabled(True)
            self._cancel_btn.setEnabled(False)
            self._collection_task = None
            self._refresh_limit_up_table()
            self._refresh_theme_table()
            self._refresh_db_status()

    def _start_naver_theme_collection(self):
        if self._collection_task and not self._collection_task.done():
            return
        self._collection_cancelled = False
        self._krx_btn.setEnabled(False)
        self._collect_btn.setEnabled(False)
        self._data_dart_btn.setEnabled(False)
        self._theme_btn.setEnabled(False)
        self._naver_theme_btn.setEnabled(False)
        self._cancel_btn.setEnabled(True)
        self._collection_task = asyncio.ensure_future(
            self._collect_naver_themes())

    async def _collect_naver_themes(self):
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
            )
            if self._collection_cancelled:
                status, message = "CANCELLED", "사용자가 중지함"
            else:
                theme_count, saved = save_theme_snapshot(
                    snapshots, snapshot_date, "NAVER", 0.95)
                message = f"테마 {theme_count:,}개 · 연결 {saved:,}건"
        except Exception as error:  # noqa: BLE001
            errors += 1
            status, message = "FAILED", str(error)
            log.exception("Naver theme collection failed")
            QMessageBox.critical(
                self, "네이버 테마", f"테마 수집에 실패했습니다.\n{error}")
        finally:
            await client.close()
            update_collection(
                run_id, status, processed, saved, errors, message)
            self._collection_status.setText(
                f"{status} · {message or '저장하지 않음'}")
            self._krx_btn.setEnabled(True)
            self._collect_btn.setEnabled(True)
            self._data_dart_btn.setEnabled(True)
            self._theme_btn.setEnabled(True)
            self._naver_theme_btn.setEnabled(True)
            self._cancel_btn.setEnabled(False)
            self._collection_task = None
            self._refresh_limit_up_table()
            self._refresh_theme_table()
            self._refresh_db_status()

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
        self._krx_btn.setEnabled(False)
        self._collect_btn.setEnabled(False)
        self._data_dart_btn.setEnabled(False)
        self._theme_btn.setEnabled(False)
        self._naver_theme_btn.setEnabled(False)
        self._cancel_btn.setEnabled(True)
        self._date_from.setEnabled(False)
        self._date_to.setEnabled(False)
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
            QMessageBox.critical(self, "KRX 수집", message)
        finally:
            await client.close()
            update_collection(
                run_id, status, processed, saved, errors,
                message or f"상한가 {events:,}건")
            self._collection_status.setText(
                f"{status} · 거래일 {processed:,} · 일봉 {saved:,} "
                f"· 상한가 {events:,} · 오류 {errors:,}"
                + (f" · {message}" if message else ""))
            self._krx_btn.setEnabled(True)
            self._collect_btn.setEnabled(True)
            self._data_dart_btn.setEnabled(True)
            self._theme_btn.setEnabled(True)
            self._naver_theme_btn.setEnabled(True)
            self._cancel_btn.setEnabled(False)
            self._date_from.setEnabled(True)
            self._date_to.setEnabled(True)
            self._collection_task = None
            self._refresh_db_status()
            self._refresh_limit_up_table()

    def _start_intraday_enrichment(self, silent: bool = False):
        if self._rest is None:
            QMessageBox.information(
                self, "키움 장중정보 보완",
                "프로그램을 정상 실행한 분석창에서 사용할 수 있습니다.")
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
        self._krx_btn.setEnabled(False)
        self._collect_btn.setEnabled(False)
        self._theme_btn.setEnabled(False)
        self._naver_theme_btn.setEnabled(False)
        self._data_dart_btn.setEnabled(False)
        self._cancel_btn.setEnabled(True)
        self._date_from.setEnabled(False)
        self._date_to.setEnabled(False)
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
            log.exception("limit entry collection failed")
        finally:
            update_collection(
                run_id, status, processed, saved, errors,
                message or f"조회불가 {unavailable:,}건")
            self._collection_status.setText(
                f"{status} · {trade_date} · 처리 {processed:,} "
                f"· 저장 {saved:,} · 조회불가 {unavailable:,} "
                f"· 오류 {errors:,}")
            self._krx_btn.setEnabled(True)
            self._collect_btn.setEnabled(True)
            self._data_dart_btn.setEnabled(True)
            self._theme_btn.setEnabled(True)
            self._naver_theme_btn.setEnabled(True)
            self._cancel_btn.setEnabled(False)
            self._date_from.setEnabled(True)
            self._date_to.setEnabled(True)
            self._collection_task = None
            self._refresh_db_status()
            self._refresh_limit_up_table()

    def _start_history_collection(self):
        if self._rest is None:
            QMessageBox.information(
                self, "데이터 수집", "프로그램을 정상 실행한 분석창에서 사용할 수 있습니다.")
            return
        date_from = self._date_from.date()
        date_to = self._date_to.date()
        if date_from > date_to:
            QMessageBox.warning(self, "데이터 수집", "시작일이 종료일보다 늦습니다.")
            return
        if date_from.daysTo(date_to) > 370:
            QMessageBox.warning(
                self, "데이터 수집", "현재 수집 범위는 한 번에 최대 1년입니다.")
            return
        self._collection_cancelled = False
        self._collect_btn.setEnabled(False)
        self._cancel_btn.setEnabled(True)
        self._date_from.setEnabled(False)
        self._date_to.setEnabled(False)
        self._collection_task = asyncio.ensure_future(
            self._collect_history(
                date_from.toString("yyyyMMdd"), date_to.toString("yyyyMMdd")))

    def _cancel_history_collection(self):
        self._collection_cancelled = True
        self._cancel_btn.setEnabled(False)
        self._collection_status.setText("현재 종목 저장 후 중지합니다…")

    async def _collect_history(self, date_from: str, date_to: str):
        run_id = start_collection("DAILY_LIMIT_UP", date_from, date_to)
        processed = saved = errors = 0
        status = "COMPLETED"
        message = ""
        try:
            self._collection_status.setText("종목 목록을 불러오는 중…")
            universe = await self._rest.stock_universe()
            sync_stock_catalog(universe)
            self._collection_progress.setRange(0, len(universe))
            for index, stock in enumerate(universe, 1):
                if self._collection_cancelled:
                    status = "CANCELLED"
                    message = "사용자가 중지함"
                    break
                try:
                    bars = await self._rest.daily_bars(stock["code"], date_to)
                    price_count, _ = save_stock_history(
                        stock, bars, date_from, date_to)
                    saved += price_count
                except Exception as error:  # noqa: BLE001
                    errors += 1
                    log.warning("history collection %s: %s", stock["code"], error)
                processed += 1
                self._collection_progress.setValue(index)
                self._collection_status.setText(
                    f"{index:,}/{len(universe):,}  {stock['name']} "
                    f"({stock['code']}) · 일봉 {saved:,}건 · 오류 {errors:,}건")
                if index % 10 == 0:
                    update_collection(
                        run_id, "RUNNING", processed, saved, errors)
                    await asyncio.sleep(0)
        except Exception as error:  # noqa: BLE001
            status = "FAILED"
            message = str(error)
            log.exception("history collection failed")
        finally:
            update_collection(
                run_id, status, processed, saved, errors, message)
            self._collection_status.setText(
                f"{status} · 종목 {processed:,} · 일봉 {saved:,} · 오류 {errors:,}")
            self._krx_btn.setEnabled(True)
            self._collect_btn.setEnabled(True)
            self._data_dart_btn.setEnabled(True)
            self._cancel_btn.setEnabled(False)
            self._date_from.setEnabled(True)
            self._date_to.setEnabled(True)
            self._collection_task = None
            self._refresh_db_status()

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
        self._save_limit_header()
        self._save_geo()
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
    await app.start()


class MainWindow(QMainWindow):
    """메인 창: 크기/위치를 layout.ini에 기억 (컬럼 너비는 ConditionScreen이 담당)."""

    def __init__(self):
        super().__init__()
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
            _SHUTDOWN[0] = True  # 동반 닫힘을 사용자 닫기로 오인 방지 + 재귀 방지
            for w in QApplication.instance().topLevelWidgets():
                if w is not self and w.isVisible():
                    w.close()  # 메인 닫으면 추가 창/순위창도 같이 종료
        super().closeEvent(e)


def main():
    qapp = QApplication(sys.argv)
    theme = str(QSettings("layout.ini", QSettings.IniFormat).value("theme_mode", "system"))
    _apply_theme(qapp, theme if theme in THEME_MODES else "system")
    f = QFont("굴림체", 9)
    f.setStyleStrategy(QFont.NoAntialias)  # 영웅문식 비트맵 렌더링, 전 위젯 통일
    qapp.setFont(f)  # 그리드/툴바/헤더/툴팁 전부. 타이틀바는 OS 소관(변경 불가)
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
