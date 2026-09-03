# -*- coding: utf-8 -*-
"""9분할 취소와 전량매도가 한 창에 들어가는지, 실패 시 안전장치가 도는지 검사.

2026-09-03 09:49 엠아이큐브솔루션에서 취소 9건이 5건/4건으로 갈라져
매도가 0.73초 밀렸고, 그사이 2회차 배분이 들어와 4건이 20주씩 더 체결됐다.
실측 버스트 천장이 10건이라 ORDER_BURST를 10으로 올렸다.
"""
import asyncio
import logging
import os
import types

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import api  # noqa: E402
import main  # noqa: E402

logging.disable(logging.CRITICAL)  # 운영 bot.log에 쓰지 않는다

CODE = "373170"
PENDING = [("000575%d" % n, 39, "KRX") for n in range(9)]


class Stub:
    """취소 전송과 스윕만 세운 대역. 실제 REST는 부르지 않는다."""

    def __init__(self, fail: bool = False):
        self._cancel_sent_orders = set()
        self.sent = []
        self.swept = []
        self._fail = fail
        for name in ("_cancel_open_buys_now", "_sweep_filled_buys",
                     "_auto_cancel_account"):
            setattr(self, name, types.MethodType(getattr(main.App, name), self))
        self.rest = types.SimpleNamespace(
            cancel_order=self._cancel_order,
            cancel_filled_buy_orders=self._sweep)

    async def _cancel_order(self, code, order_no, qty, exchange):
        if self._fail:
            raise RuntimeError("kt10003 주문 유량 재시도 실패")
        self.sent.append(order_no)

    async def _sweep(self, code, minimum_filled=100):
        self.swept.append(code)
        return 1, 39

    async def _cancel_one_open_buy(self, code, order_no, qty, exchange, reason):
        self.sent.append(order_no)


async def demo():
    # 9분할이면 취소가 한 번에 다 나가고 매도 자리가 한 칸 남는다.
    screen = Stub()
    left = screen._cancel_open_buys_now(CODE, PENDING, "잔량 2번")
    await asyncio.sleep(0)
    assert left == [], left
    assert len(screen.sent) == 9, screen.sent
    assert api.ORDER_BURST - 1 == 9, api.ORDER_BURST

    # 10건이 넘으면 매도 뒤로 미룬다. 매도가 창 밖으로 밀리지 않는다.
    wide = Stub()
    spill = wide._cancel_open_buys_now(
        CODE, PENDING + [("0005760", 39, "KRX")], "잔량 2번")
    assert len(spill) == 1, spill

    # 취소가 유량으로 끝내 실패하면 계좌 조회 스윕이 뒤를 받는다.
    broken = Stub(fail=True)
    await broken._auto_cancel_account(CODE, "0005750", 39, "KRX")
    await asyncio.sleep(0)
    assert broken.swept == [CODE], broken.swept

    # 스윕은 100주 이상 체결된 주문만 고른다. 체결 없는 분할은 배분을
    # 기다리는 중이라 살려 둔다.
    assert "minimum_filled" in api.RestClient.cancel_filled_buy_orders.__code__.co_varnames
    print("ok")


if __name__ == "__main__":
    asyncio.run(demo())
