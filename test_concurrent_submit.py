# -*- coding: utf-8 -*-
"""9분할 매수가 순차가 아니라 한꺼번에 나가는지 검사.

예전 워커는 한 건씩 응답까지 기다렸다. 2026-09-04 09:01 에이프로젠에서
9건에 986ms가 걸렸고 9번째는 유량 거부까지 맞았다. 동시호가 마감선은
랜덤이라 그 1초가 배분 탈락으로 이어진다. 048770 실측에서 같은 9건을
동시에 던지면 78ms였다.
"""
import asyncio
import logging
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import order  # noqa: E402

logging.disable(logging.CRITICAL)  # 운영 bot.log에 쓰지 않는다

DELAY = 0.05  # 주문 한 건의 왕복시간 흉내


class Rest:
    """왕복시간이 있는 REST 대역. 동시에 도는지 겹침으로 확인한다."""

    def __init__(self):
        self.serial = 0
        self.inflight = 0
        self.peak = 0
        self.cancelled = []

    async def buy_order(self, code, qty, price):
        self.inflight += 1
        self.peak = max(self.peak, self.inflight)
        try:
            await asyncio.sleep(DELAY)
            self.serial += 1
            return {"order_no": "%07d" % self.serial}
        finally:
            self.inflight -= 1

    async def cancel_order(self, code, order_no, qty, exchange="KRX"):
        await asyncio.sleep(DELAY)
        self.cancelled.append(order_no)


async def demo():
    rest = Rest()
    manager = order.OrderEngine(rest)
    started = asyncio.get_event_loop().time()
    batch = manager.submit("003060", "시험", 2245, [1] * 9, auto_cancel=True)
    await asyncio.sleep(0)
    while any(not child.order_no for child in batch.children):
        await asyncio.sleep(0.01)
    elapsed = asyncio.get_event_loop().time() - started

    # 9건이 겹쳐서 날아갔으면 한 건 왕복시간에 가깝다. 순차면 9배다.
    assert rest.peak == 9, rest.peak
    assert elapsed < DELAY * 3, elapsed
    assert batch.sent_count == 9, batch.sent_count
    # 주문번호는 모두 등록돼야 취소·체결 추적이 된다.
    assert len(manager._by_order_no) == 9, manager._by_order_no

    # 접수된 뒤의 일괄취소는 그대로 돈다.
    count, qty = manager.cancel_submitted_children("003060")
    assert count == 9, count
    while len(rest.cancelled) < 9:
        await asyncio.sleep(0.01)
    assert len(rest.cancelled) == 9, rest.cancelled
    print("ok")


if __name__ == "__main__":
    asyncio.run(demo())
