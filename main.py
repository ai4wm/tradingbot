# -*- coding: utf-8 -*-
"""진입점: qasync로 Qt 이벤트 루프 안에서 asyncio 실행 (단일 스레드).

배선:
  토큰 -> 웹소켓 접속/LOGIN -> CNSRLST로 콤보박스 채움
  등록 토글 ON: CNSRREQ, 편입 -> ka10001 조회 -> on_included -> 시세 REG
  이탈: on_excluded -> 시세 REMOVE
  시세 REAL: on_tick
"""
import asyncio
import logging
import sys

import qasync
from PySide6.QtCore import QSettings, QTimer
from PySide6.QtWidgets import QApplication, QMainWindow

import config
from api import RestClient
from gui import ConditionScreen
from rank import RankScreen
from ws import WSClient

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(name)s %(levelname)s %(message)s",
    handlers=[logging.FileHandler("bot.log", encoding="utf-8"), logging.StreamHandler()],
)
log = logging.getLogger("main")


class App:
    def __init__(self, screen: ConditionScreen):
        self.screen = screen
        self.rest = RestClient()
        self.ws = WSClient()
        self._refresh_task = None  # 디바운스된 벌크 조회 태스크
        # REG/REMOVE는 어떤 경로로든 0.3초 모아 1건으로 전송 (서버 요청빈도 제한 105110 방지)
        self._reg_pending: set[str] = set()
        self._rm_pending: set[str] = set()
        self._reg_task = None
        self._entry_cache: dict[str, str] = {}   # code -> 상한가 진입시각 (상한 유지중 캐시)
        self._entry_pending: set[str] = set()
        self._active_seq = None    # 현재 등록된 조건 (최초 자동등록/재접속 판별)
        self._settings = QSettings("layout.ini", QSettings.IniFormat)  # 마지막 사용 조건 기억
        self._auto_timer = QTimer()  # 자동재조회 (동시호가 편입/이탈 갱신)
        self._auto_timer.timeout.connect(self._on_refresh)
        # 단일가 종목은 웹소켓 실시간이 안 옴(0D/0H 무송신, 2026-07-07 실측 0건)
        # -> REST가 유일한 채널. 그리드에 해당 종목 있을 때만 3초 주기 조회.
        self._single_task = None
        self._single_timer = QTimer()
        self._single_timer.timeout.connect(self._on_single_poll)
        self._single_timer.start(3000)
        self._loop = asyncio.get_event_loop()

        self.ws.on_condition_list = self._on_condition_list
        self.ws.on_condition_event = self._on_condition_event
        self.ws.on_condition_snapshot = self._on_condition_snapshot
        self.ws.on_real = self.screen.on_tick
        self.ws.on_vi = self._on_vi

        screen.condition_combo.activated.connect(self._on_condition_selected)
        screen.refresh_btn.clicked.connect(self._on_refresh)
        screen.reload_btn.clicked.connect(self._on_reload_conditions)
        self._rank = None  # [0198] 조회순위 창 (버튼 첫 클릭 때 생성)
        screen.rank_btn.clicked.connect(self._on_rank)
        # 자동재조회 간격/체크 상태 복원 (시그널 연결 전에 값부터 세팅)
        screen.refresh_interval.setValue(int(self._settings.value("refresh_interval", 3)))
        screen.auto_refresh.setChecked(self._settings.value("auto_refresh", "false") == "true")
        if screen.auto_refresh.isChecked():
            self._auto_timer.start(screen.refresh_interval.value() * 1000)
        screen.auto_refresh.toggled.connect(self._on_auto_refresh)
        screen.refresh_interval.valueChanged.connect(self._on_interval_changed)

    async def start(self):
        self.ws_task = asyncio.create_task(self.ws.run(self.rest.tokens.token))
        try:  # 코스닥(종목명 색) + 단일가 종목(예상값 상시 표시) 마킹
            m = self.screen.model
            info = await self.rest.market_info()
            m.kosdaq, m.single, m.nxt, m.misu, m.admin = (
                info.kosdaq, info.single, info.nxt, info.misu, info.admin)
            log.info("kosdaq %d, single %d, nxt %d, misu %d, admin %d",
                     len(m.kosdaq), len(m.single), len(m.nxt), len(m.misu), len(m.admin))
        except Exception as e:  # noqa: BLE001
            log.warning("market_info failed: %s", e)

    # --- 조건검색 목록 -> 콤보박스 ---------------------------------------
    def _on_condition_list(self, items):
        combo = self.screen.condition_combo
        combo.clear()
        for seq, name in items:
            combo.addItem(name, seq)
        log.info("condition list: %d", len(items))
        if combo.count() == 0:
            return
        if self._active_seq is None:
            # 최초 로드: 마지막 사용 조건(없으면 첫 항목)을 선택하고 자동 등록
            last = self._settings.value("last_condition")
            idx = combo.findData(last) if last is not None else -1
            idx = idx if idx >= 0 else 0
            combo.setCurrentIndex(idx)  # setCurrentIndex는 activated 안 터짐 -> 수동 등록
            asyncio.ensure_future(self._switch_condition(combo.itemData(idx)))
        else:
            # 재접속(ws가 _resubscribe로 재등록함): 콤보 선택만 현재 조건에 맞춤
            idx = combo.findData(self._active_seq)
            if idx >= 0:
                combo.setCurrentIndex(idx)

    # --- 조건 선택 = 즉시 등록(이전 조건 자동 해제) -----------------------
    def _on_condition_selected(self, index: int):
        seq = self.screen.condition_combo.itemData(index)
        if seq is not None:
            self._settings.setValue("last_condition", seq)  # 마지막 사용 조건 기억
            self._settings.sync()
            asyncio.ensure_future(self._switch_condition(seq))

    async def _switch_condition(self, seq: str):
        if seq != self._active_seq:  # 조건 변경: 이전 조건 해제 + 그리드 전량 정리
            await self._stop()
        else:                        # 같은 조건 재조회: 행 유지, 스냅샷 diff로만 반영
            await self.ws.clear_condition()
        await self.ws.register_condition(seq)
        self._active_seq = seq

    async def _stop(self):
        await self.ws.clear_condition()
        self._active_seq = None
        codes = list(self.screen.model.codes)
        await self.ws.remove_real_many(codes)
        for code in codes:
            self.screen.model.remove_stock(code)

    # --- [0198] 조회순위 창 토글 -----------------------------------------
    def _on_rank(self):
        if self._rank is None:
            self._rank = RankScreen(self.rest)
        if self._rank.isVisible():
            self._rank.close()  # 지오메트리 저장 + 숨김 + 폴링 중지
        else:
            self._rank.show()
            self._rank.raise_()

    # --- 조건목록 재조회: 영웅문서 조건 추가/수정한 걸 재시작 없이 반영 ----
    def _on_reload_conditions(self):
        asyncio.ensure_future(self.ws.list_conditions())  # CNSRLST -> 콤보 다시 채움(현재 조건 유지)

    # --- 재조회: 현재 조건을 해제->재등록 (서버가 중복 CNSRREQ 무시) ------
    def _on_refresh(self):
        seq = self.screen.condition_combo.currentData()
        if seq is not None:
            asyncio.ensure_future(self._switch_condition(seq))

    # --- 자동재조회: 동시호가 때 편입/이탈을 주기적으로 갱신 --------------
    def _on_auto_refresh(self, on: bool):
        self._settings.setValue("auto_refresh", "true" if on else "false")
        self._settings.sync()
        if on:
            self._auto_timer.start(self.screen.refresh_interval.value() * 1000)
            log.info("auto-requery ON (%ds)", self.screen.refresh_interval.value())
        else:
            self._auto_timer.stop()
            log.info("auto-requery OFF")

    def _on_interval_changed(self, sec: int):
        self._settings.setValue("refresh_interval", sec)
        self._settings.sync()
        if self._auto_timer.isActive():  # 켜진 상태에서 간격 바꾸면 즉시 반영
            self._auto_timer.start(sec * 1000)

    # --- 편입/이탈 -------------------------------------------------------
    def _on_condition_snapshot(self, codes: list[str]):
        """CNSRREQ 응답(현재 편입 전체). 기존 행은 건드리지 않고 차집합만 반영
        -> 재조회해도 예상값/진입시간 등 실시간 상태가 안 날아간다."""
        cur = set(self.screen.model.codes)
        new = set(codes)
        for code in cur - new:
            self.screen.on_excluded(code)
            self._queue_real(code, add=False)
        for code in new - cur:
            self.screen.on_included(code, {"name": code})
            self._queue_real(code, add=True)
        if new - cur:
            self._schedule_refresh()
        log.info("condition snapshot: %d codes (+%d/-%d) %s",
                 len(new), len(new - cur), len(cur - new), ",".join(sorted(new)))

    def _on_condition_event(self, code: str, is_insert: bool, time_str: str):
        if is_insert:
            # 행은 즉시 표시(종목코드 자리표시), 시세는 벌크 조회로 한꺼번에 채움.
            self.screen.on_included(code, {"name": code, "time": time_str})
            self._queue_real(code, add=True)
            self._schedule_refresh()
        else:
            self.screen.on_excluded(code)
            self._queue_real(code, add=False)

    def _queue_real(self, code: str, add: bool):
        (self._reg_pending if add else self._rm_pending).add(code)
        (self._rm_pending if add else self._reg_pending).discard(code)
        if not (self._reg_task and not self._reg_task.done()):
            self._reg_task = asyncio.ensure_future(self._flush_real())

    async def _flush_real(self):
        await asyncio.sleep(0.3)  # 이벤트 버스트를 모아 REG/REMOVE 각 1건으로
        reg, rm = sorted(self._reg_pending), sorted(self._rm_pending)
        self._reg_pending.clear()
        self._rm_pending.clear()
        if rm:
            await self.ws.remove_real_many(rm)
        if reg:
            await self.ws.register_real_many(reg)

    def _on_single_poll(self):
        m = self.screen.model
        codes = [c for c in m.codes if c in m.single]
        if codes and not (self._single_task and not self._single_task.done()):
            self._single_task = asyncio.ensure_future(self._poll_single(codes))

    async def _poll_single(self, codes: list[str]):
        try:
            for row in await self.rest.watch_info(codes):
                self.screen.on_tick(row["code"], row)
        except Exception as e:  # noqa: BLE001
            log.warning("single poll: %s", e)

    def _on_vi(self, code: str, active: bool, price: int):
        self.screen.model.set_vi(code, active, price)
        if active and code in self.screen.model.rows:
            asyncio.ensure_future(self._vi_fetch(code))  # 발동 즉시 전 컬럼 채움

    async def _vi_fetch(self, code: str):
        try:
            for row in await self.rest.watch_info([code], exp=True):
                self.screen.on_tick(row["code"], row)
        except Exception as e:  # noqa: BLE001
            log.warning("vi_fetch %s: %s", code, e)

    def _schedule_refresh(self):
        """편입 이벤트 버스트를 모아 한 번의 ka10095로 조회(디바운스)."""
        if self._refresh_task and not self._refresh_task.done():
            return
        self._refresh_task = asyncio.ensure_future(self._refresh_quotes())

    async def _refresh_quotes(self):
        await asyncio.sleep(0.4)  # 편입 이벤트가 몰려 들어오는 동안 코드 모으기
        codes = list(self.screen.model.codes)
        for i in range(0, len(codes), 100):  # ka10095 한 요청당 100종목씩
            chunk = codes[i:i + 100]
            try:
                for row in await self.rest.watch_info(chunk):
                    self.screen.on_tick(row["code"], row)
            except Exception as e:  # noqa: BLE001
                log.warning("watch_info failed: %s", e)
        self._fill_entry_times()

    # --- 상한가 진입시각 채우기 (틱 스캔, 상한가 종목당 1회 캐시) ----------
    def _fill_entry_times(self):
        m = self.screen.model
        todo = []
        for code in list(m.codes):
            d = m.rows[code]
            at_limit = d["upper"] > 0 and d["price"] == d["upper"]  # 현재 상한가
            if at_limit:
                if code in self._entry_cache:
                    self.screen.on_tick(code, {"time": self._entry_cache[code]})  # 재조회로 행 재생성돼도 유지
                elif code not in self._entry_pending:
                    self._entry_pending.add(code)
                    todo.append((d["vol"], code, d["upper"]))
            elif code in self._entry_cache:  # 상한가 이탈 -> 진입시각 지움(재진입 시 재계산)
                del self._entry_cache[code]
                self.screen.on_tick(code, {"time": ""})
        if todo:
            # 거래량 적은 순으로 순차 조회: 틱 적어 1페이지로 끝나는 점상부터 빨리 채워지고,
            # 거래량 많아 여러 페이지 도는 놈은 뒤로 -> 점상 시각이 안 밀린다.
            todo.sort()
            asyncio.ensure_future(self._drain_entries(todo))

    async def _drain_entries(self, todo: list[tuple[int, str, int]]):
        for _, code, upper in todo:
            await self._fetch_entry(code, upper)

    async def _fetch_entry(self, code: str, upper: int):
        try:
            t = await self.rest.last_limit_entry(code, upper)
        except Exception as e:  # noqa: BLE001
            log.warning("last_limit_entry %s: %s", code, e)
            t = ""
        self._entry_pending.discard(code)
        self._entry_cache[code] = t
        self.screen.on_tick(code, {"time": t})


async def _amain(screen):
    app = App(screen)
    await app.start()


class MainWindow(QMainWindow):
    """창 크기/위치를 layout.ini에 기억했다 복원 (컬럼 너비는 ConditionScreen이 담당)."""

    def __init__(self):
        super().__init__()
        self._settings = QSettings("layout.ini", QSettings.IniFormat)
        self._geo_timer = QTimer(self)
        self._geo_timer.setSingleShot(True)
        self._geo_timer.timeout.connect(self._save_geo)
        geo = self._settings.value("geometry")
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
        self._settings.setValue("geometry", self.saveGeometry())
        self._settings.sync()  # 강제 종료돼도 남게

    def closeEvent(self, e):
        self._save_geo()
        super().closeEvent(e)


def main():
    qapp = QApplication(sys.argv)
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
