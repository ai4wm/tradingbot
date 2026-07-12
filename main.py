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
VSURGE_SEQ = "VSURGE"  # [급증]거래량급증 (ka10023)
TVAL_SEQ = "TVAL"      # [대금]거래대금상위 (ka10032)
# 순위 계열: 서버 조건검색 대신 REST 폴, 순위 그리드 공유. seq -> 기준시간 콤보 서브모드
RANK_SUBMODE = {RANK_SEQ: "rank", VSURGE_SEQ: "vsurge", TVAL_SEQ: "tval"}
RANK_SEQS = set(RANK_SUBMODE)
RANK_TOP = 20          # 순위 모드 실시간 슬롯 캡 (95한도 공유)
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
        combo.clear()
        combo.addItem("[순위]조회순위", RANK_SEQ)   # 맨 위 고정: REST 순위 계열
        combo.addItem("[급증]거래량급증", VSURGE_SEQ)
        combo.addItem("[대금]거래대금상위", TVAL_SEQ)
        f = QFont(combo.font())
        f.setBold(True)
        for i, color in ((0, "#FFDD00"), (1, "#FF8C00"), (2, "#38B8FF")):  # 볼드+색으로 조건식과 구분
            combo.setItemData(i, f, Qt.FontRole)
            combo.setItemData(i, QColor(color), Qt.ForegroundRole)
        combo.insertSeparator(3)  # 진짜 조건식과 구분선
        for seq, name in items:
            combo.addItem(name, seq)
        if self.seq is None:
            last = self._settings.value(self.prefix + "last_condition")
            idx = combo.findData(last) if last is not None else -1
            if idx < 0:  # 저장 없음: 첫 진짜 조건식 (0=순위,1=급증,2=대금,3=구분선,4=첫 조건식)
                idx = 4 if combo.count() > 4 else 0
            combo.setCurrentIndex(idx)  # setCurrentIndex는 activated 안 터짐 -> 수동 등록
            asyncio.ensure_future(self._switch_condition(combo.itemData(idx)))
        else:  # 재접속: ws가 재등록하므로 콤보 선택만 복원
            idx = combo.findData(self.seq)
            if idx >= 0:
                combo.setCurrentIndex(idx)

    def _on_condition_selected(self, index: int):
        seq = self.screen.condition_combo.itemData(index)
        if seq is not None:
            self._settings.setValue(self.prefix + "last_condition", seq)
            self._settings.sync()
            asyncio.ensure_future(self._switch_condition(seq))

    async def _switch_condition(self, seq: str):
        if seq != self.seq:  # 조건 변경: 이전 조건 해제 + 이 창 행 전량 정리
            await self.stop()
        elif seq not in RANK_SEQS:  # 같은 조건 재조회: 행 유지, 스냅샷 diff로만 반영
            await self.app.clear_condition_if_sole(self.seq, self)
        switched = self.screen.set_rank_mode(seq in RANK_SEQS)
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
        await self.app.ws.register_condition(seq)

    async def stop(self):
        """이 창의 조건/시세 구독 정리 (조건 변경·창 닫기)."""
        if self.seq is not None:
            await self.app.clear_condition_if_sole(self.seq, self)
            self.seq = None
        codes = list(self.screen.model.codes)
        for code in codes:
            self.app.queue_real(code, add=False)
            self.screen.model.remove_stock(code)

    async def _poll_rank(self):
        """순위 계열: REST 상위 RANK_TOP개 -> 조건검색과 동일한 snapshot 경로.
        조회순위=ka00198(기준시간 콤보), 거래량급증=ka10023(통합 stex_tp=3)."""
        try:
            if self.seq == VSURGE_SEQ:
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
        self.on_snapshot([r["code"] for r in rows])
        for r in rows:  # 순위/변동/이름 바로 채움 (시세는 실시간+백필)
            self.screen.on_tick(r["code"], {"qrank": r["rank"], "qrank_chg": r["rank_chg"],
                                            "name": r["name"]})

    # --- 재조회 -----------------------------------------------------------
    def on_refresh(self):
        seq = self.screen.condition_combo.currentData()
        if seq is not None:
            asyncio.ensure_future(self._switch_condition(seq))

    def _mkey(self, name: str) -> str:
        """모드별 설정 키 (재조회/간격): 순위 계열이면 rankmode_ 접두 (gui._mkey와 동일 규칙)."""
        return self.prefix + ("rankmode_" if self.seq in RANK_SEQS else "") + name

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
            self.app.queue_real(code, add=False)
        for code in new - cur:
            self.screen.on_included(code, {"name": code})
            self.app.queue_real(code, add=True)
        if new - cur:
            self._schedule_refresh()
            self._maybe_beep()
        log.info("snapshot%s: %d codes (+%d/-%d) %s", self.prefix or " ",
                 len(new), len(new - cur), len(cur - new), ",".join(sorted(new)))

    def on_event(self, code: str, is_insert: bool):
        if is_insert:
            self.screen.on_included(code, {"name": code})
            self.app.queue_real(code, add=True)
            self._schedule_refresh()
            self._maybe_beep()
        else:
            self.screen.on_excluded(code)
            self.app.queue_real(code, add=False)

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
                for row in await self.app.rest.watch_info(codes[i:i + 100]):
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
        self.views: list[View] = [View(self, screen)]
        self._extra_windows: list = []  # 추가 창(ConditionWindow) 목록
        self._cond_items = []           # CNSRLST 결과 (새 창 콤보 채우기용)
        self._market = None             # MarketInfo (새 창 모델 주입용)
        self._limit_cnt = None          # 어제까지 연속상한 일수 (연상 컬럼, 시작 시 1회, 일봉 계산)
        # REG/REMOVE는 0.3초 모아 각 1건으로 (서버 105110 유량거부 방지).
        # Counter: 두 창이 같은 종목을 등록하면 참조수 2가 되도록 발생 횟수 유지.
        self._reg_pending = Counter()
        self._rm_pending = Counter()
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
        self._wire_common(screen)

    def _wire_common(self, screen: ConditionScreen):
        screen.reload_btn.clicked.connect(
            lambda: asyncio.ensure_future(self.ws.list_conditions()))
        screen.rank_btn.clicked.connect(self._on_rank)
        screen.newwin_btn.clicked.connect(self._on_newwin)

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
        screen.window().setWindowTitle(
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

    def _on_condition_event(self, seq: str, code: str, is_insert: bool):
        for v in self.views:
            if v.seq == str(seq):
                v.on_event(code, is_insert)

    def _on_condition_snapshot(self, seq: str, codes: list[str]):
        for v in self.views:
            if v.seq == str(seq):
                v.on_snapshot(codes)

    def _on_real(self, code: str, fields: dict):
        for v in self.views:
            if code in v.screen.model.rows:
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

    def queue_real(self, code: str, add: bool):
        tgt, opp = ((self._reg_pending, self._rm_pending) if add
                    else (self._rm_pending, self._reg_pending))
        if opp[code] > 0:  # 같은 창에서 편입<->이탈이 겹치면 상쇄
            opp[code] -= 1
            if opp[code] == 0:
                del opp[code]
        else:
            tgt[code] += 1
        if not (self._reg_task and not self._reg_task.done()):
            self._reg_task = asyncio.ensure_future(self._flush_real())

    async def _flush_real(self):
        await asyncio.sleep(0.3)
        reg = sorted(self._reg_pending.elements())  # 발생 횟수 유지 (참조수 = 창 수)
        rm = sorted(self._rm_pending.elements())
        self._reg_pending.clear()
        self._rm_pending.clear()
        if rm:
            await self.ws.remove_real_many(rm)
        if reg:
            await self.ws.register_real_many(reg)

    def _on_single_poll(self):
        codes = sorted({c for v in self.views for c in v.screen.model.codes
                        if c in v.screen.model.single})
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
        # 컬럼폭/정렬은 열 때마다 본창 복사 (크기와 함께 통일). 위치만 창별 기억.
        main = self.views[0].screen
        self._settings.setValue(prefix + "header", main.table.horizontalHeader().saveState())
        seeded = False
        if self._settings.value(prefix + "geometry") is None:  # 첫 오픈: 위치도 본창에서
            self._settings.setValue(prefix + "geometry", main.window().saveGeometry())
            seeded = True
        screen = ConditionScreen(prefix=prefix)
        screen.newwin_btn.setVisible(False)  # 추가 창에선 창+/순위/통합 숨김 (메인창에서만)
        screen.rank_btn.setVisible(False)
        screen.unified_check.setVisible(False)  # 통합 시세는 전 창 공통 -> 메인창에서만 전환
        win = ConditionWindow(prefix, on_close=self._on_window_closed)
        win.setWindowTitle(f"[0156-{n}] 조건검색실시간")
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
        screen.reload_btn.clicked.connect(
            lambda: asyncio.ensure_future(self.ws.list_conditions()))

    def _on_window_closed(self, win):
        if _SHUTDOWN[0]:  # 앱 종료 동반 닫힘: 창 개수 보존 (재시작 때 복원용)
            return
        for v in list(self.views[1:]):
            if v.screen.window() is win:
                asyncio.ensure_future(v.stop())
                self.views.remove(v)
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
        self._key = "geometry"  # 순위 모드 전환 시 set_rank_mode가 rank_geometry로 교체
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
        if not _SHUTDOWN[0]:
            _SHUTDOWN[0] = True  # 동반 닫힘을 사용자 닫기로 오인 방지 + 재귀 방지
            for w in QApplication.instance().topLevelWidgets():
                if w is not self and w.isVisible():
                    w.close()  # 메인 닫으면 추가 창/순위창도 같이 종료
        super().closeEvent(e)


def main():
    qapp = QApplication(sys.argv)
    f = QFont("굴림체", 9)
    f.setStyleStrategy(QFont.NoAntialias)  # 영웅문식 비트맵 렌더링, 전 위젯 통일
    qapp.setFont(f)  # 그리드/툴바/헤더/툴팁 전부. 타이틀바는 OS 소관(변경 불가)
    loop = qasync.QEventLoop(qapp)
    asyncio.set_event_loop(loop)

    win = MainWindow()
    win.setWindowTitle("[0156] 조건검색실시간")
    screen = ConditionScreen()
    win.setCentralWidget(screen)
    win.show()

    with loop:
        loop.create_task(_amain(screen))
        loop.run_forever()


if __name__ == "__main__":
    main()
