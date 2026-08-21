# -*- coding: utf-8 -*-
"""매도 체결로 주문가능금액이 바뀌면 종목별 주문가능수량 캐시도 버리는지 검사.

캐시를 비우는 곳이 매수 주문액 변화 한 군데뿐이라, 매도로 현금이 늘어도
이미 조회해 둔 (종목, 가격) 수량이 매도 전 값으로 남았다. 2026-08-21 소마젠
900주 매도 뒤 주문가능금액은 299,903 -> 3,730,011로 갱신됐지만 수량은 옛
금액 기준 그대로였다.
"""
import asyncio
import logging
import os
import types

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import main

logging.disable(logging.CRITICAL)  # 운영 bot.log에 쓰지 않는다


class Screen:
    def __init__(self):
        self.cleared = 0
        self.summaries = []

    def order_target(self):
        return "950200", 3750

    def clear_orderable_quantity(self):
        self.cleared += 1

    def set_account_summary(self, summary):
        self.summaries.append(summary)


class Stub:
    """`_load_account_summary`가 쓰는 부분만 세운 대역."""

    def __init__(self, values):
        self._values = list(values)
        self._account_summary = None
        self._orderable_cache = {("950200", 3750): {"quantity": 79}}
        self.screen = Screen()
        self.views = [types.SimpleNamespace(screen=self.screen)]
        self.queued = []
        self.orders = types.SimpleNamespace(committed_notional=lambda: 0)
        self.rest = types.SimpleNamespace(account_summary=self._next)
        for name in ("_load_account_summary", "_reload_orderable_quantities"):
            setattr(self, name, types.MethodType(getattr(main.App, name), self))

    async def _next(self):
        return dict(self._values.pop(0))

    def _queue_orderable_quantity(self, screen, code, price):
        self.queued.append((code, price))


SUMMARY = {"estimated_assets": 3667153, "cash_orderable": 299903,
           "cash_deposit": 3674903}


async def demo():
    # 첫 조회는 비교 대상이 없어 캐시를 건드리지 않는다.
    app = Stub([SUMMARY, SUMMARY])
    await app._load_account_summary()
    assert app._orderable_cache, "첫 조회에서 캐시를 버렸다"
    # 금액이 같으면 그대로 둔다. 1분마다 도는 조회가 캐시를 계속 지우면
    # 매 분 종목별 조회가 새로 나가 유량 제한에 걸린다.
    await app._load_account_summary()
    assert app._orderable_cache, "값이 같은데 캐시를 버렸다"
    assert app.queued == [], app.queued

    # 매도 체결로 주문가능금액이 늘면 수량 캐시를 버리고 다시 조회한다.
    sold = dict(SUMMARY, cash_orderable=3730011)
    app = Stub([SUMMARY, sold])
    await app._load_account_summary()
    await app._load_account_summary()
    assert app._orderable_cache == {}, app._orderable_cache
    assert app.queued == [("950200", 3750)], app.queued
    assert app.screen.cleared == 1, app.screen.cleared
    print("ok")


if __name__ == "__main__":
    asyncio.run(demo())
