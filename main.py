# -*- coding: utf-8 -*-
"""진입점: qasync로 Qt 이벤트 루프 안에서 asyncio 실행 (단일 스레드).

구조: App(공유: 웹소켓/REST/등록큐/순위창) + View(조건검색 창 하나 = 화면+조건seq).
'창+' 버튼으로 독립 조건검색 창 추가(조건별 동시 감시, 시세 REG는 참조수 공유)."""
import asyncio
import logging
import sys
import time
from collections import Counter

import qasync
from PySide6.QtCore import QSettings, Qt, QTimer
from PySide6.QtGui import QColor, QFont
from PySide6.QtWidgets import QApplication, QMainWindow

from api import RestClient
from gui import ConditionScreen
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


def _start_title_clock(win: QMainWindow, title: str):
    """영웅문에서 동기화한 PC 시각을 창 제목에 1초 단위로 표시."""
    def update():
        win.setWindowTitle(f"{win._title_clock_base} | {time.strftime('%H:%M:%S')}")

    win._title_clock_base = title
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
            self._market = await self.rest.market_info()
            for v in self.views:
                self._inject_market(v)
            if self._rank is not None:
                self._rank.set_market(self._market)
            m = self._market
            log.info("kosdaq %d, single %d, nxt %d, misu %d, admin %d",
                     len(m.kosdaq), len(m.single), len(m.nxt), len(m.misu), len(m.admin))
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
        m.kosdaq, m.single, m.nxt, m.misu, m.admin = (
            self._market.kosdaq, self._market.single, self._market.nxt,
            self._market.misu, self._market.admin)
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
        screen.unified_check.setVisible(False)  # 통합 시세는 전 창 공통 -> 메인창에서만 전환
        screen.theme_btn.setVisible(False)  # 테마는 앱 전체 공통 -> 메인창에서만 전환
        win = ConditionWindow(prefix, on_close=self._on_window_closed)
        _start_title_clock(win, f"[0156-{n}] 조건검색실시간")
        win.setCentralWidget(screen)
        view = View(self, screen)
        self._inject_market(view)
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

    def _on_window_closed(self, win):
        if _SHUTDOWN[0]:  # 앱 종료 동반 닫힘: 창 개수 보존 (재시작 때 복원용)
            return
        for v in list(self.views[1:]):
            if v.screen.window() is win:
                asyncio.ensure_future(v.stop())
                self.views.remove(v)
                self.force_real_sync()
        if win in self._extra_windows:
            self._extra_windows.remove(win)
        self._save_window_count()

    def _save_window_count(self):
        self._settings.setValue("cond_windows", len(self.views) - 1)
        self._settings.sync()


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
    _start_title_clock(win, "[0156] 조건검색실시간")
    screen = ConditionScreen()
    win.setCentralWidget(screen)
    win.show()

    with loop:
        loop.create_task(_amain(screen))
        loop.run_forever()


if __name__ == "__main__":
    main()
