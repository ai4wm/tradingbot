# -*- coding: utf-8 -*-
"""나머지취소 검사.

미체결 매수 각 건에서 100주만 남기고 나머지를 부분취소한다. 부분취소는 잔량이
남아 취소확인(잔량 0)이 오지 않으므로, 전량취소 장부(`_cancel_sent_orders`)에
주문번호를 남기면 안 된다. 남기면 이후 비상정지·3단매도의 전량취소가 그
주문을 건너뛴다.
"""
import asyncio
import logging
import os
import types

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import main

logging.disable(logging.CRITICAL)  # 운영 bot.log에 쓰지 않는다


class Rest:
    def __init__(self):
        self.sent = []

    async def cancel_order(self, code, order_no, qty, exchange="KRX"):
        self.sent.append((code, order_no, qty, exchange))
        return {"order_no": "C" + order_no}


class Stub:
    def __init__(self, book):
        self._open_buy_orders = {"011330": dict(book)}
        self._cancel_sent_orders = set()
        self.rest = Rest()
        for name in ("_trim_open_buys", "_trim_one_open_buy"):
            setattr(self, name,
                    types.MethodType(getattr(main.App, name), self))


def run(book) -> Stub:
    app = Stub(book)
    loop = asyncio.new_event_loop()
    try:
        asyncio.set_event_loop(loop)
        app._trim_open_buys("011330")
        pending = asyncio.all_tasks(loop)
        if pending:
            loop.run_until_complete(asyncio.gather(*pending))
    finally:
        asyncio.set_event_loop(None)
        loop.close()
    return app


def demo():
    # 230주 9분할 -> 130주씩 9건 취소, 100주씩 9건이 남는다.
    app = run({str(n): (230, "KRX") for n in range(9)})
    assert len(app.rest.sent) == 9, app.rest.sent
    assert {qty for _c, _o, qty, _e in app.rest.sent} == {130}, app.rest.sent

    # 전량취소 장부를 오염시키면 안 된다.
    assert app._cancel_sent_orders == set(), app._cancel_sent_orders

    # 100주 이하는 건드리지 않는다. 100주 정확히도 제외다.
    app = run({"a": (100, "KRX"), "b": (60, "KRX"), "c": (101, "NXT")})
    assert app.rest.sent == [("011330", "c", 1, "NXT")], app.rest.sent

    # 취소할 게 없으면 아무것도 보내지 않는다.
    assert run({"a": (100, "KRX")}).rest.sent == []
    assert run({}).rest.sent == []

    # 거래소 코드는 원주문 것을 그대로 쓴다.
    app = run({"x": (500, "SOR")})
    assert app.rest.sent == [("011330", "x", 400, "SOR")], app.rest.sent

    print("ok")


if __name__ == "__main__":
    demo()
