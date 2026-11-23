# -*- coding: utf-8 -*-
"""영웅문 매수도 접수 순간 청산키가 걸리고, 아무것도 안 남으면 풀리는지 본다.

체결 때만 걸면 동시호가에 넣은 9분할이 09:00 체결 전까지 청산키 없이 남는다.
"""
import main


class _App:
    _track_open_buy = main.App._track_open_buy
    _track_open_sell = main.App._track_open_sell
    _pending_open_buys = main.App._pending_open_buys
    _track_order_book = main.App._track_order_book

    def __init__(self, held=0):
        self._open_buy_orders, self._open_sell_orders = {}, {}
        self._position_book = {"005930": {"held": held, "sellable": held}}
        self._position_book_primed = True
        self._emergency_locked = set()
        self._order_cancelled, self._cancel_sent_orders = {}, set()
        self.calls = []
        self._sell_accepts = {}

    def _clear_spent_balance_sell(self, code):
        pass

    def _new_fill_qty(self, order_no, event):
        return int(event.get("fill_qty") or 0)

    def _auto_assign_exit_hotkey_everywhere(self, code):
        self.calls.append("assign")

    def _clear_exit_hotkey(self, code):
        self.calls.append("clear")

    def _push_pending_orders(self, code):
        pass


def _accept(app, no, qty=106):
    app._track_open_buy("005930", no, {
        "status": "접수", "original_order_no": "0000000",
        "order_qty": qty, "remaining_qty": qty, "fill_qty": 0})


def _cancel(app, no):
    app._track_open_buy("005930", "C" + no, {
        "status": "확인", "original_order_no": no,
        "order_qty": 106, "remaining_qty": 0, "fill_qty": 0})


def demo():
    # 9분할 접수 → 키는 첫 접수에서 한 번만. 전부 취소 → 한 번 풂.
    app = _App()
    for i in range(9):
        _accept(app, f"A{i}")
    assert app.calls == ["assign"], app.calls
    for i in range(9):
        _cancel(app, f"A{i}")
    assert app.calls == ["assign", "clear"], app.calls

    # 100주 체결 뒤 나머지 취소 → 보유가 있으니 풀지 않는다.
    app = _App()
    _accept(app, "A0")
    app._track_open_buy("005930", "A0", {
        "status": "체결", "original_order_no": "0000000",
        "order_qty": 106, "remaining_qty": 6, "fill_qty": 100})
    _cancel(app, "A0")
    assert "clear" not in app.calls, app.calls

    # 이미 보유 중(키를 손으로 풀었을 수 있음) → 새 주문에 다시 걸지 않는다.
    app = _App(held=300)
    _accept(app, "A0")
    assert app.calls == [], app.calls

    # 장부를 못 세웠으면 보유를 모르니 풀지 않는다.
    app = _App()
    app._position_book, app._position_book_primed = {}, False
    _accept(app, "A0")
    _cancel(app, "A0")
    assert app.calls == ["assign"], app.calls
    print("ok (영웅문 접수 시 청산키, 빈 키 해제)")
    demo_cancel_key()
    demo_sold_outside()


def demo_sold_outside():
    """영웅문에서 다 팔면(3단매도 없이) 청산·취소키가 풀린다. 남은 게 있으면 안 푼다."""
    def sell(app, qty, remaining=0):
        app._track_open_sell("005930", "S1", {
            "status": "체결", "original_order_no": "0000000",
            "order_qty": qty, "remaining_qty": remaining, "fill_qty": qty})

    app = _App(held=100)
    sell(app, 100)
    assert app.calls == ["clear"], app.calls

    app = _App(held=100)                       # 일부만 팔았다
    sell(app, 40, remaining=0)
    assert app.calls == [], app.calls

    app = _App(held=100)                       # 들어올 매수가 남았다
    app._open_buy_orders["005930"] = {"B1": (50, "KRX")}
    sell(app, 100)
    assert app.calls == [], app.calls

    app = _App(held=100)                       # 장부를 못 세웠다
    app._position_book_primed = False
    sell(app, 100)
    assert app.calls == [], app.calls
    print("ok (영웅문 전량 매도 시 청산·취소키 해제)")


def demo_cancel_key():
    """취소키는 장부의 미체결 매수를 한 번에 전부 취소한다(버스트 상한 없이)."""
    import types
    sent, fallback = [], []
    app = types.SimpleNamespace(
        _open_buy_orders={"005930": {"A1": (106, "KRX"), "A2": (105, "KRX"),
                                     "A3": (0, "KRX")}},
        views=[],
        _cancel_order=lambda code: fallback.append(code))
    app._cancel_open_sells = lambda code, reason: (0, 0)
    app._show_cancel_sent = lambda *a: None
    app._pending_open_buys = lambda code: main.App._pending_open_buys(app, code)
    app._cancel_open_buys_now = lambda code, orders, reason, cap=9: (
        sent.append((code, sorted(orders), reason, cap)) or [])
    main.App._cancel_key_pressed(app, "005930", "F5")
    assert sent == [("005930", [("A1", 106, "KRX"), ("A2", 105, "KRX")],
                     "취소키", None)], sent
    # 장부에 없으면 주문 셀 취소 버튼과 같은 길로 거둔다.
    main.App._cancel_key_pressed(app, "000660", "F6")
    assert fallback == ["000660"], fallback
    print("ok (취소키: 미체결 매수 전부 취소)")
    demo_cancel_sells()


def demo_cancel_sells():
    """취소키는 미체결 매도도 거둔다. 2026-11-23 매도 297주가 걸려 있었는데 매수가 없어
    F2를 다섯 번 눌러도 `orders=0`으로 아무것도 안 나갔다."""
    import asyncio
    import types
    sent, fallback, shown = [], [], []

    async def cancel_order(code, order_no, qty, exchange):
        sent.append((code, order_no, qty, exchange))

    app = types.SimpleNamespace(
        _open_buy_orders={}, views=[], _cancel_sent_orders=set(),
        _open_sell_orders={"005930": {"S1": (297, "KRX"), "S2": (0, "KRX")}},
        rest=types.SimpleNamespace(cancel_order=cancel_order),
        _cancel_order=lambda code: fallback.append(code))
    app._pending_open_buys = lambda code: main.App._pending_open_buys(app, code)
    app._cancel_open_sells = lambda code, reason: main.App._cancel_open_sells(app, code, reason)
    app._cancel_one_open_buy = (
        lambda *a, **k: main.App._cancel_one_open_buy(app, *a, **k))
    app._show_cancel_sent = lambda *a: shown.append(a)
    app._cancel_open_buys_now = lambda code, orders, reason, cap=9: (
        sent.extend((code, o[0], 0, o[2]) for o in orders) or [])

    async def press():
        main.App._cancel_key_pressed(app, "005930", "F2")
        await asyncio.sleep(0)
        await asyncio.sleep(0)

    asyncio.run(press())                      # 매도만: 잔량 있는 한 건만, 매수 조회 길은 안 탄다
    assert sent == [("005930", "S1", 0, "KRX")], sent
    assert fallback == [] and shown and shown[0][4:] == (1, 297), (fallback, shown)
    asyncio.run(press())                      # 다시 눌러도 같은 주문을 또 취소하지 않는다
    assert len(sent) == 1, sent
    assert fallback == ["005930"], fallback   # 보낼 것이 없으면 예전 길(계좌 조회)

    app._open_buy_orders = {"005930": {"B1": (50, "KRX")}}   # 매수·매도 함께
    app._open_sell_orders = {"005930": {"S3": (100, "KRX")}}
    sent.clear(); shown.clear()
    asyncio.run(press())
    assert ("005930", "S3", 0, "KRX") in sent and ("005930", "B1", 0, "KRX") in sent, sent
    assert shown[-1][2:] == (1, 50, 1, 100), shown
    print("ok (취소키: 미체결 매도도 취소, 중복 전송 없음)")


if __name__ == "__main__":
    demo()
