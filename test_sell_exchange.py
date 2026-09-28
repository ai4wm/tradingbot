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
    app._sell_exchange_now = lambda _code: app._sell_exchange
    send = types.MethodType(main.App._send_sell_order, app)
    for exchange in ("KRX", "SOR"):
        app._sell_exchange = exchange
        asyncio.run(send("005930", 1, 70000, False, "test"))
    assert app.rest.sent == ["KRX", "SOR"], app.rest.sent
    assert main.App._sell_exchange == "KRX", "기본값은 KRX"
    print("ok (한 관문에서 거래소를 싣는다 · 기본 KRX)")


class _Passed(Exception):
    pass


def demo_nxt_premarket_watch():
    """08:00~08:50 NXT 프리마켓은 NXT 가능 종목만 감시하고, 매도는 SOR로 낸다."""
    real_states, real_order = main._market_session_states, main._balance_stage_order
    today = main.datetime.now().strftime("%Y%m%d")

    def gate_open(code, states):
        main._market_session_states = lambda _now: states
        app = types.SimpleNamespace(
            _balance_sell_settings={code: {"first": 100}},
            _balance_sell_date={code: today},
            _market=types.SimpleNamespace(nxt={"000001"}))
        try:
            main.App._check_balance_sell(app, code, 0)
        except _Passed:
            return True
        return False

    def passed(_setting):
        raise _Passed

    try:
        main._balance_stage_order = passed
        premarket = ("개장 전", "프리마켓", "")
        assert gate_open("000001", premarket), "NXT 종목은 프리마켓에 봐야 한다"
        assert not gate_open("000002", premarket), "KRX 전용은 볼 시장이 없다"
        assert not gate_open("000001", ("시가 동시호가", "일시휴장", "")), \
            "08:50~09:00은 여전히 쉰다"
        assert gate_open("000002", ("정규장", "메인마켓", ""))

        # 프리마켓 매도는 스위치와 상관없이 종목을 본다. NXT 가능은 SOR
        # (키움이 NXT로 보낸다), KRX 전용은 KRX(SOR이면 NXT로 가서 거부된다).
        main._market_session_states = lambda _now: premarket
        for switch in ("KRX", "SOR"):
            app = types.SimpleNamespace(
                _sell_exchange=switch,
                _market=types.SimpleNamespace(nxt={"000001"}))
            assert main.App._sell_exchange_now(app, "000001") == "SOR"
            assert main.App._sell_exchange_now(app, "000002") == "KRX"
        main._market_session_states = lambda _now: ("정규장", "메인마켓", "")
        assert main.App._sell_exchange_now(app, "000002") == "SOR"  # 스위치대로
        app._sell_exchange = "KRX"
        assert main.App._sell_exchange_now(app, "000001") == "KRX"
    finally:
        main._market_session_states = real_states
        main._balance_stage_order = real_order
    print("ok (NXT 프리마켓 감시는 NXT 종목만 · 그때 매도는 SOR)")


if __name__ == "__main__":
    demo_limit_follows_switch_market_stays_krx()
    demo_single_gate_carries_exchange()
    demo_nxt_premarket_watch()
