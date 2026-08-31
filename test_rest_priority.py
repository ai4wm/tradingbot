# -*- coding: utf-8 -*-
"""사용자가 고른 종목 조회가 배경 조회를 제치고 먼저 나가는지 검사.

REST는 초당 1건이라 배경 조회 뒤에 붙으면 매수 버튼이 몇 초씩 잠긴 채 남는다
(2026-08-31: 취소 뒤 다른 종목을 골랐는데 활성화까지 몇 초 걸렸다).
"""
import asyncio
import os
import types

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import api  # noqa: E402
import config  # noqa: E402


async def demo():
    rest = object.__new__(api.RestClient)
    rest._sem = asyncio.Semaphore(1)
    rest._last_call = 0.0
    rest._priority_pending = 0
    order = []

    async def call(name, priority):
        await rest._throttle(priority)
        order.append(name)

    saved = config.REST_RATE_LIMIT
    config.REST_RATE_LIMIT = 0.05
    try:
        # 우선 조회가 등록된 뒤 들어온 배경 조회는 순번이 와도 자리를 내준다.
        first = asyncio.ensure_future(call("user", True))
        for _ in range(3):
            await asyncio.sleep(0)          # 우선 요청이 대기 등록될 때까지
        tasks = [asyncio.ensure_future(call(f"bg{i}", False)) for i in range(3)]
        await asyncio.gather(first, *tasks)
    finally:
        config.REST_RATE_LIMIT = saved

    # 이미 세마포어에 들어간 요청은 못 제친다. 그 뒤 것들만 양보한다.
    assert order[0] == "user", order
    assert rest._priority_pending == 0, rest._priority_pending
    print("ok", order)


if __name__ == "__main__":
    asyncio.run(demo())
