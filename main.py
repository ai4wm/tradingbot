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
        self._refresh_task = None  # 디바운스된 벌크 조회 태스크
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
            # 행은 즉시 표시(종목코드 자리표시), 시세는 벌크 조회로 한꺼번에 채움.
            self.screen.on_included(code, {"name": code, "time": time_str})
            asyncio.ensure_future(self.ws.register_real(code))
            self._schedule_refresh()
        else:
            self.screen.on_excluded(code)
            asyncio.ensure_future(self.ws.remove_real(code))

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
