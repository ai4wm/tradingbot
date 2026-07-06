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
from PySide6.QtWidgets import QApplication, QMainWindow

import config
from api import RestClient
from gui import ConditionScreen
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
        self._info_cache: dict[str, dict] = {}  # code -> ka10001 결과 (중복 조회 방지)
        self._loop = asyncio.get_event_loop()

        self.ws.on_condition_list = self._on_condition_list
        self.ws.on_condition_event = self._on_condition_event
        self.ws.on_real = self.screen.on_tick

        screen.start_btn.toggled.connect(self._on_toggle)

    async def start(self):
        self.ws_task = asyncio.create_task(self.ws.run(self.rest.tokens.token))

    # --- 조건검색 목록 -> 콤보박스 ---------------------------------------
    def _on_condition_list(self, items):
        self.screen.condition_combo.clear()
        for seq, name in items:
            self.screen.condition_combo.addItem(name, seq)
        log.info("condition list: %d", len(items))

    # --- 등록/해제 토글 --------------------------------------------------
    def _on_toggle(self, checked: bool):
        seq = self.screen.condition_combo.currentData()
        if checked and seq is not None:
            self.screen.start_btn.setText("해제")
            asyncio.ensure_future(self.ws.register_condition(seq))
        else:
            self.screen.start_btn.setText("등록")
            asyncio.ensure_future(self._stop())

    async def _stop(self):
        await self.ws.clear_condition()
        for code in list(self.screen.model.codes):
            await self.ws.remove_real(code)
            self.screen.model.remove_stock(code)

    # --- 편입/이탈 -------------------------------------------------------
    def _on_condition_event(self, code: str, is_insert: bool, time_str: str):
        if is_insert:
            asyncio.ensure_future(self._on_insert(code, time_str))
        else:
            self.screen.on_excluded(code)
            asyncio.ensure_future(self.ws.remove_real(code))

    async def _on_insert(self, code: str, time_str: str):
        # 이름 없이 먼저 행을 띄우고(그리드가 빈 값 허용), 시세부터 등록해 즉시 움직이게.
        base = {"name": code, "time": time_str}
        self.screen.on_included(code, base)
        await self.ws.register_real(code)
        # ka10001로 종목명/업종/전일거래량 채움 (rate limit 1req/s)
        if code not in self._info_cache:
            try:
                self._info_cache[code] = await self.rest.stock_info(code)
            except Exception as e:  # noqa: BLE001
                log.warning("stock_info %s failed: %s", code, e)
                return
        self.screen.on_tick(code, self._info_cache[code])


async def _amain(screen):
    app = App(screen)
    await app.start()


def main():
    qapp = QApplication(sys.argv)
    loop = qasync.QEventLoop(qapp)
    asyncio.set_event_loop(loop)

    win = QMainWindow()
    win.setWindowTitle("[0156] 조건검색실시간")
    screen = ConditionScreen()
    win.setCentralWidget(screen)
    win.resize(900, 560)
    win.show()

    with loop:
        loop.create_task(_amain(screen))
        loop.run_forever()


if __name__ == "__main__":
    main()
