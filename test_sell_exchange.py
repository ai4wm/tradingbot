# -*- coding: utf-8 -*-
"""3단매도·청산 매도가 고른 거래소로 나가는지 확인한다.

**시장가는 늘 KRX다.** NXT 프리·애프터마켓은 지정가만 받아 SOR이 그쪽으로
보내면 거부된다. 청산은 하한가 지정가라 SOR을 그대로 탄다.
"""
import asyncio
import os
import types

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import main  # noqa: E402
from api import RestClient  # noqa: E402


def _rest():
    rest = RestClient.__new__(RestClient)
    rest.sent = []

    async def fake(api_id, body):
        rest.sent.append(body["dmst_stex_tp"])
        return {"ord_no": "1"}

    rest._order_request = fake
    return rest


def demo_limit_follows_switch_market_stays_krx():
    rest = _rest()
    asyncio.run(rest.sell_order("005930", 1, 70000))
    asyncio.run(rest.sell_order("005930", 1, 70000, exchange="SOR"))
    asyncio.run(rest.sell_order("005930", 1, market=True, exchange="SOR"))
    assert rest.sent == ["KRX", "SOR", "KRX"], rest.sent
    print("ok (지정가는 스위치대로 · 시장가는 KRX)")


def demo_single_gate_carries_exchange():
    """3단매도·청산 둘 다 `_send_sell_order` 한 곳을 지난다."""
    app = types.SimpleNamespace(rest=_rest(), _sell_accepts={})
    send = types.MethodType(main.App._send_sell_order, app)
    for exchange in ("KRX", "SOR"):
        app._sell_exchange = exchange
        asyncio.run(send("005930", 1, 70000, False, "test"))
    assert app.rest.sent == ["KRX", "SOR"], app.rest.sent
    assert main.App._sell_exchange == "KRX", "기본값은 KRX"
    print("ok (한 관문에서 거래소를 싣는다 · 기본 KRX)")


if __name__ == "__main__":
    demo_limit_follows_switch_market_stays_krx()
    demo_single_gate_carries_exchange()
