# -*- coding: utf-8 -*-
"""미보유 종목의 3단매도 단계가 호가 틱마다 헛돌지 않는지 검사.

보유하지 않은 종목에 3단매도를 걸어 두면, 잔고 장부에 항목이 아예 없어
소진 가드를 통과해 틱마다 매도가 발동했다. 낼 주문이 없으니 체결은 없지만
발동마다 보유수량 조회와 미체결 조회 TR이 나간다(2026-08-13 골드앤에스
09:01:12~24 6회). 장부를 세운 뒤에는 항목 없음도 미보유로 믿고 소진한다.
"""
import asyncio
import logging
import os
import types
from datetime import datetime

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import main
import rank

logging.disable(logging.CRITICAL)  # 운영 bot.log에 쓰지 않는다

SETTING = {"first": 60000, "second": 30000, "third": 0,
           "first_ratio": 0.0, "second_ratio": 1.0, "third_ratio": 1.0,
           "market_sell": True}


class Stub:
    """`App`에서 단계 판정에 필요한 부분만 세운 대역."""

    def __init__(self, primed: bool, book: dict | None = None,
                 pending: list | None = None, stage: int = 1,
                 setting: dict | None = None):
        code = "035290"
        self._balance_sell_settings = {code: dict(setting or SETTING)}
        self._balance_sell_date = {code: datetime.now().strftime("%Y%m%d")}
        self._balance_sell_stage = {code: stage}  # 1 = 1단계가 소진된 뒤
        self._balance_sell_tasks = {}
        self._position_book = book if book is not None else {}
        self._position_book_primed = primed
        self._pending = pending or []
        self.views = []
        self.executed = []
        self.sounds = []
        self._check_balance_sell = types.MethodType(
            main.App._check_balance_sell, self)

    def _pending_open_buys(self, code):
        return self._pending

    def _complete_balance_stage(self, code, depth, sound="balance_sold"):
        self._balance_sell_stage[code] = depth
        self.sounds.append(sound)

    def _finish_balance_sell(self, code):
        self._balance_sell_tasks.pop(code, None)

    async def _execute_balance_stage(self, code, depth, number, ratio, price,
                                     bid_qty, market_sell):
        """판 것이 있을 때만 진행도가 오르는 실제 동작을 흉내낸다."""
        self.executed.append((code, depth, bid_qty))
        booked = self._position_book.get(code)
        if not booked or booked["held"] <= 0:
            return  # 매도가능 0 -> 주문 없이 돌아오고 진행도는 그대로다
        booked["held"] = booked["sellable"] = 0
        self._complete_balance_stage(code, depth)


async def run(app, ticks=(29000, 28000, 27000)):
    """2단계 기준(30000)을 밑도는 호가를 여러 번 흘린다.

    실제로는 틱 사이가 초 단위라 앞선 매도가 끝난 뒤 다음 틱이 온다.
    """
    for bid_qty in ticks:
        app._check_balance_sell("035290", bid_qty)
        await asyncio.sleep(0)
        await asyncio.sleep(0)
    return app.executed


async def demo():
    saved = main._market_session_states
    main._market_session_states = lambda now: ("정규장", "", "")
    try:
        # 장부를 세운 뒤 항목이 없다 = 미보유. 첫 틱에서 소진하고 끝난다.
        # 앞 단계를 지나온 뒤(진행도 1)라 다 판 것으로 보고 무음이다.
        app = Stub(primed=True)
        assert await run(app) == [], app.executed
        assert app._balance_sell_stage["035290"] == 2
        assert app.sounds == [None], app.sounds

        # 장부를 못 세운 상태에서는 실제 보유일 수 있어 전과 같이 재시도한다.
        cold = Stub(primed=False)
        assert len(await run(cold)) == 3, cold.executed

        # 미체결 매수가 남아 있으면 곧 들어올 물량이므로 재시도한다.
        buying = Stub(primed=True, pending=[("0008355", 31, "KRX")])
        assert len(await run(buying)) == 3, buying.executed

        # 실제로 들고 있으면 판다. 판 뒤에는 진행도가 올라 다시 안 부른다.
        held = Stub(primed=True,
                    book={"035290": {"held": 900, "sellable": 900}})
        assert await run(held) == [("035290", 2, 29000)], held.executed

        # 장부에 항목이 있고 보유 0인 기존 경로는 그대로 소진한다.
        sold = Stub(primed=True, book={"035290": {"held": 0, "sellable": 0}})
        assert await run(sold) == [], sold.executed

        # 진행도 0(아무 단계도 안 지난 상태)의 1단계도 똑같이 소진한다.
        # 진행도가 0으로 남으면 나중에 사도 `_resell_late_buy_fill`이
        # progress<=0에서 돌아가 되살리기가 아예 안 걸린다.
        first = dict(SETTING, first_ratio=0.5)
        fresh = Stub(primed=True, stage=0, setting=first)
        assert await run(fresh, ticks=(50000, 49000, 48000)) == [], (
            fresh.executed)
        assert fresh._balance_sell_stage["035290"] == 1
        # 애초에 안 들고 있던 종목이라 sound5로 알린다(무음이 아니다).
        assert fresh.sounds == ["balance_unheld"], fresh.sounds
        assert rank.KIWOOM_ALERT_FILES["balance_unheld"].name == "sound5.wav"
        assert "balance_unheld" in rank.TONES  # 파일이 없을 때의 대체음
    finally:
        main._market_session_states = saved

    print("ok")


if __name__ == "__main__":
    asyncio.run(demo())
