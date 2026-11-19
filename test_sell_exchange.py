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
    app._after_hours_type = lambda _code: ""
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


def demo_after_hours_check():
    """`시간외` 체크: KRX 전용만 오전 61·오후 81(가격 없음), NXT 가능은 매도SOR대로.

    시각으로 자동 전환하지 않는다. 체크가 꺼져 있으면 15:35여도 보통 지정가다.
    체크는 08:40·16:00에 저절로 꺼진다.
    """
    real_dt = main.datetime
    bodies, started = [], []
    rest = RestClient.__new__(RestClient)

    async def fake(api_id, body):
        bodies.append((body["dmst_stex_tp"], body["trde_tp"], body["ord_uv"]))
        return {"ord_no": "1"}

    rest._order_request = fake
    app = types.SimpleNamespace(
        rest=rest, _sell_accepts={}, _sell_exchange="SOR",
        _sell_after_hours=False, _market=types.SimpleNamespace(nxt={"000001"}),
        _after_hours_timer=types.SimpleNamespace(
            start=started.append, stop=lambda: started.append("stop")))
    app._sell_exchange_now = lambda _code: app._sell_exchange
    app._after_hours_type = types.MethodType(main.App._after_hours_type, app)
    send = types.MethodType(main.App._send_sell_order, app)

    def at(hour, minute):
        class Fixed(real_dt):
            @classmethod
            def now(cls, tz=None):
                return real_dt(2026, 11, 18, hour, minute)
        main.datetime = Fixed

    try:
        at(15, 35)
        asyncio.run(send("000002", 1, 7000, False, "test"))   # 체크 꺼짐
        main.App._on_after_hours_sell(app, True)
        asyncio.run(send("000002", 1, 7000, False, "test"))   # KRX 전용 → 81
        asyncio.run(send("000001", 1, 7000, False, "test"))   # NXT 가능 → SOR
        at(8, 20)
        asyncio.run(send("000002", 1, 7000, False, "test"))   # 오전 → 61
        main.App._on_after_hours_sell(app, True)
        main.App._on_after_hours_sell(app, False)
    finally:
        main.datetime = real_dt
    assert bodies == [("SOR", "0", "7000"), ("KRX", "81", ""),
                      ("SOR", "0", "7000"), ("KRX", "61", "")], bodies
    # 15:35에 켜면 16:00(25분), 08:20에 켜면 08:40(20분)에 꺼진다.
    assert started == [25 * 60_000, 20 * 60_000, "stop"], started
    print("ok (시간외 체크: KRX 전용 오전 61·오후 81, NXT는 SOR, 08:40·16:00 꺼짐)")


if __name__ == "__main__":
    demo_limit_follows_switch_market_stays_krx()
    demo_single_gate_carries_exchange()
    demo_nxt_premarket_watch()
    demo_after_hours_check()
