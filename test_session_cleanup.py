# -*- coding: utf-8 -*-
"""장 마감 뒤 3단매도·자동취소가 스스로 내려가는지 검사.

설정이 그대로 남으면 화면에는 걸린 것처럼 보이고, 다음 날
`auto_balance_sell_on_order`가 새 잔량 기준으로 다시 걸지도 않는다(설정이
있으면 건드리지 않는다).

감시는 종가 동시호가(15:20~15:30)까지 돈다. 그 10분에 상이 무너지면 미체결
매수가 그대로 종가에 체결되기 때문이다. 그래서 해제는 그것이 끝나고 체결
통보까지 온 뒤(15:33)에 한다.

청산키는 내리지 않는다. 2026-09-14부터 애프터마켓(16~20시)이 열려 16시 이후
손쓸 수 있는 수단이 그것뿐이다.
"""
import asyncio
import os
import types
from datetime import datetime, timedelta

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import main
from api import RestClient


class AccountOrders(RestClient):
    """계좌에만 있는 영웅문 주문도 반환한다. 실계좌에는 연결하지 않는다."""

    def __init__(self):
        self.queried = []
        self.cancelled = []
        self.rows = {
            "005930": [("0001", 100, "KRX"), ("hero-0009", 25, "SOR")],
            "000660": [("0003", 50, "NXT")],
            "035420": [("manual-0004", 30, "KRX")],
        }

    async def open_buy_orders(self, code=""):
        self.queried.append(code)
        return [
            {"code": code, "order_no": order_no,
             "remaining_qty": qty, "exchange": exchange}
            for order_no, qty, exchange in self.rows.get(code, [])
        ]

    async def cancel_order(self, code, order_no, qty, exchange="KRX"):
        self.cancelled.append((code, order_no, qty, exchange))
        return {}


def _app():
    app = main.App.__new__(main.App)
    app._balance_sell_settings = {"005930": {"first": 1000}, "071200": {}}
    app._account_auto_cancel_armed = {"071200", "000660"}
    app._exit_hotkey_specs = {"": {"005930": {"label": "F1"}}}
    app._open_buy_orders = {"005930": {"0001": (100, "KRX"),
                                       "0002": (100, "KRX")},
                            "000660": {"0003": (50, "KRX")}}
    app.cleared, app.disarmed, app.scheduled = [], [], []
    app.cancelled, app.stopped, app.swept = [], [], []
    app.orders = types.SimpleNamespace(
        stop_local_submissions=app.stopped.append)
    app._pending_open_buys = types.MethodType(
        main.App._pending_open_buys, app)
    app._cancel_open_buys_now = lambda code, orders, reason, cap=None: (
        app.cancelled.extend((code, o[0]) for o in orders) or [])
    app.rest = AccountOrders()
    app._sweep_open_buys = types.MethodType(main.App._sweep_open_buys, app)
    app._set_balance_sell = lambda code, value: (
        app.cleared.append(code), app._balance_sell_settings.pop(code, None))
    app._set_account_auto_cancel = lambda code, armed: (
        app.disarmed.append(code), app._account_auto_cancel_armed.discard(code))
    app._schedule_session_cleanup = lambda: app.scheduled.append(1)
    return app


async def check_cleanup():
    app = _app()
    before = asyncio.all_tasks()
    main.App._on_session_cleanup(app)
    await asyncio.gather(*(asyncio.all_tasks() - before))
    # 메모리 장부로 취소하지 않고 계좌 조회 결과로 각 주문의 잔량 전부를
    # 취소한다. 장부에 없는 영웅문 주문도 포함하고 감시 밖 종목은 제외한다.
    assert sorted(app.rest.queried) == ["000660", "005930", "071200"]
    assert sorted(app.rest.cancelled) == [
        ("000660", "0003", 0, "NXT"),
        ("005930", "0001", 0, "KRX"),
        ("005930", "hero-0009", 0, "SOR"),
    ], app.rest.cancelled
    assert app.cancelled == [], app.cancelled
    assert app.stopped == [], app.stopped
    assert app._open_buy_orders["005930"], app._open_buy_orders
    assert sorted(app.cleared) == ["005930", "071200"], app.cleared
    assert sorted(app.disarmed) == ["000660", "071200"], app.disarmed
    assert app._balance_sell_settings == {}, app._balance_sell_settings
    assert app._account_auto_cancel_armed == set()
    # 청산키는 그대로다. 애프터마켓에서 쓸 수 있는 유일한 수단이다.
    assert app._exit_hotkey_specs[""]["005930"]["label"] == "F1"
    # 다음 날치를 다시 걸어야 한 번 울리고 끝나지 않는다.
    assert app.scheduled == [1], app.scheduled

    # 내릴 것이 없어도 다음 예약은 건다.
    empty = _app()
    empty._balance_sell_settings, empty._account_auto_cancel_armed = {}, set()
    empty._open_buy_orders = {}
    before = asyncio.all_tasks()
    main.App._on_session_cleanup(empty)
    await asyncio.gather(*(asyncio.all_tasks() - before))
    assert empty.cleared == [] and empty.disarmed == []
    assert empty.cancelled == [], empty.cancelled
    assert empty.rest.queried == [] and empty.rest.cancelled == []
    assert empty.scheduled == [1], empty.scheduled
    print("마감 해제 : 감시 해제 + 계좌 미체결 매수 취소 (청산키 유지)")


def check_sessions():
    """감시는 정규장과 종가 동시호가에서만 돈다."""
    assert main.BALANCE_SELL_SESSIONS == (
        "정규장", "종가 동시호가", "애프터마켓"), main.BALANCE_SELL_SESSIONS
    now = datetime(2026, 10, 26)  # 거래일로 고정해 주말 실행에도 동일하게 검사

    def krx(hour, minute, second=0):
        return main._market_session_states(
            now.replace(hour=hour, minute=minute, second=second))[0]

    assert krx(14, 0) in main.BALANCE_SELL_SESSIONS, "접속매매"
    assert krx(15, 19, 59) in main.BALANCE_SELL_SESSIONS, "마감 직전"
    assert krx(15, 20) in main.BALANCE_SELL_SESSIONS, "종가 동시호가 시작"
    assert krx(15, 29, 59) in main.BALANCE_SELL_SESSIONS, "종가 직전"
    # 애프터마켓도 본다. 2026-09-14에 시간외단일가가 폐지되고 16~20시
    # 접속매매가 그 자리에 섰다.
    assert krx(16, 0) in main.BALANCE_SELL_SESSIONS, "애프터마켓 시작"
    assert krx(19, 59) in main.BALANCE_SELL_SESSIONS, "애프터마켓 끝"
    # 종가 결정~16시, 시가 동시호가, 20시 뒤는 안 돈다.
    assert krx(15, 35) not in main.BALANCE_SELL_SESSIONS, "정규장 종료"
    assert krx(15, 50) not in main.BALANCE_SELL_SESSIONS, "시간외종가"
    assert krx(8, 50) not in main.BALANCE_SELL_SESSIONS, "시가 동시호가"
    assert krx(20, 1) not in main.BALANCE_SELL_SESSIONS, "장 종료"
    # 해제는 두 감시 구간이 끝난 뒤마다 한 번씩이다.
    assert main.SESSION_CLEANUP_TIMES == ((15, 33), (20, 5)),         main.SESSION_CLEANUP_TIMES
    print("감시 국면 : 정규장 + 종가 동시호가 + 애프터마켓")


def check_schedule():
    """지난 시각이면 내일로 넘긴다. 켤 때마다 즉시 울리면 안 된다."""
    app = main.App.__new__(main.App)
    started = []
    app._session_cleanup_timer = types.SimpleNamespace(
        start=started.append)
    main.App._schedule_session_cleanup(app)
    assert len(started) == 1 and started[0] > 0, started

    now = datetime.now()
    times = [now.replace(hour=h, minute=m, second=0, microsecond=0)
             for h, m in main.SESSION_CLEANUP_TIMES]
    cutoff = next((t for t in sorted(times) if t > now),
                  min(times) + timedelta(days=1))
    expected = (cutoff - now).total_seconds() * 1000
    assert abs(started[0] - expected) < 2000, (started[0], expected)
    assert started[0] <= 24 * 3600 * 1000, "하루를 넘겨 걸면 안 된다"
    print(f"예약     : {cutoff:%H:%M}까지 {started[0] / 3600000:.1f}시간")


async def demo():
    await check_cleanup()
    check_sessions()
    check_schedule()
    print("ok")


if __name__ == "__main__":
    asyncio.run(demo())
