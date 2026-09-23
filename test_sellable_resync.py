# -*- coding: utf-8 -*-
"""매도가능수량이 정정 주문에서 새지 않는지 확인한다.

2026-09-21 앤씨앤(092600)에서 청산키가 안 먹었다. 보유 1,091주인데 장부의
매도가능이 990주라 990주만 나갔고, 남은 101주는 F1을 다섯 번 더 눌러도
`emergency exit ignored sellable-zero`로 막혔다. 계좌에는 그 101주를 묶는
매도주문이 하나도 없었다 — 장부에서만 묶여 있었다.

원인은 정정 확인 이벤트가 잔량을 싣고 온 것이다. 같은 날 303주 주문은
확인이 잔량 0으로 와서 멀쩡했고, 101주 주문만 잔량 101로 와서 샜다.

  09:08:44  0008189  접수 101주  잔량 101   -> 매도가능 -101
  09:08:46  0008201  정정 101주  잔량 101   -> 옛 코드는 통째로 무시
  09:08:46  0008189  접수        잔량   0   -> 원주문이 조용히 사라짐
                                               되돌릴 취소수량이 0

아래 이벤트는 그날 bot.log에서 그대로 옮긴 것이다.
"""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import main  # noqa: E402

# 그날 09:08~09:13 사이 092600 매도 이벤트 전부.
# (주문번호, 원주문번호, 상태, 주문수량, 체결수량, 잔량) 순이다.
#
# **상태를 함께 싣는다.** 원주문번호가 실리고 잔량>0인 이벤트가 두 뜻으로
# 온다 — 취소 주문의 `접수`(원주문은 아직 살아 있다)와 정정의 `확인`
# (원주문이 신주문으로 갈아탄다)이다. 상태를 안 보면 둘을 못 가른다.
SELL_EVENTS_20260921 = (
    ("0008065", "0000000", "접수", 101, 0, 101),
    ("0008139", "0008065", "접수", 101, 0, 101),   # 정정 접수
    ("0008139", "0008065", "확인", 101, 0,   0),   # 정정 확인 (잔량 0)
    ("0008065", "0000000", "접수", 101, 0,   0),   # 원주문 소멸
    ("0008189", "0000000", "접수", 101, 0, 101),
    ("0008201", "0008189", "접수", 101, 0, 101),   # 정정 접수
    ("0008201", "0008189", "확인", 101, 0, 101),   # 정정 확인 <- 여기가 샜다
    ("0008189", "0000000", "접수", 101, 0,   0),   # 원주문 소멸
    ("0009262", "0000000", "접수", 303, 0, 303),
    ("0009661", "0009262", "접수", 303, 0, 303),   # 정정 접수
    ("0009661", "0009262", "확인", 303, 0,   0),   # 정정 확인 (잔량 0)
    ("0009262", "0000000", "접수", 303, 0,   0),   # 원주문 소멸
    ("0009662", "0008201", "접수", 101, 0, 101),   # 정정 접수
    ("0009662", "0008201", "확인", 101, 0,   0),   # 취소 확인
    ("0008201", "0008189", "확인", 101, 0,   0),   # 취소 확인
)


def _app(code: str, held: int):
    """장부 메서드만 붙인 최소 객체. App 전체를 세우지 않는다."""
    app = type("Fake", (), {
        name: main.App.__dict__[name] for name in (
            "_track_order_book", "_track_open_buy", "_track_open_sell",
            "_new_fill_qty")})()
    app._open_buy_orders = {}
    app._open_sell_orders = {}
    app._position_book = {code: {"held": held, "sellable": held}}
    app._position_filled = {}
    app._position_fill_ids = set()
    app._order_cancelled = {}
    app._cancel_sent_orders = set()
    app._sell_accepts = {}
    app._position_book_primed = True
    app._emergency_locked = set()
    app._push_pending_orders = lambda *a: None
    app._clear_spent_balance_sell = lambda *a: None
    app._clear_after_emergency = lambda *a: None
    return app


def _event(order_no, original, order_qty, fill_qty, remaining,
           status="접수"):
    return {
        "order_no": order_no, "original_order_no": original,
        "status": status, "order_qty": order_qty, "fill_qty": fill_qty,
        "remaining_qty": remaining, "exchange": "KRX", "fill_id": "",
    }


def _live_sell(app, code):
    return sum(qty for qty, _ in (app._open_sell_orders.get(code) or {}).values())


def demo_amendment_does_not_leak():
    """그날 이벤트를 그대로 태운다. 끝나면 묶인 수량이 없어야 한다."""
    code = "092600"
    app = _app(code, 1091)
    position = app._position_book[code]

    for order_no, original, status, order_qty, fill_qty, remaining in \
            SELL_EVENTS_20260921:
        app._track_open_sell(
            code, order_no,
            _event(order_no, original, order_qty, fill_qty, remaining, status))
        # 매 단계마다 매도가능은 살아 있는 매도 미체결을 뺀 값이어야 한다.
        assert position["sellable"] == \
            position["held"] - _live_sell(app, code), (
                order_no, original, status, remaining, position,
                _live_sell(app, code))

    # 마지막 두 이벤트가 101주 매도를 취소했다. 살아 있는 매도가 없으므로
    # 1,091주 전부 팔 수 있어야 한다. 옛 코드는 990이었다.
    assert _live_sell(app, code) == 0, app._open_sell_orders
    assert position["held"] == 1091, position
    assert position["sellable"] == 1091, position
    print("ok (정정 주문이 매도가능을 묶지 않음)")


def demo_amendment_keeps_binding():
    """정정 중에도 걸어 둔 수량은 묶여 있어야 한다. 풀면 초과 매도가 난다."""
    code = "092600"
    app = _app(code, 1091)
    position = app._position_book[code]
    # 접수 -> 정정 접수 -> 정정 확인(잔량 101)까지. 매도 101주는 살아 있다.
    for order_no, original, status, order_qty, fill_qty, remaining in \
            SELL_EVENTS_20260921[4:7]:
        app._track_open_sell(
            code, order_no,
            _event(order_no, original, order_qty, fill_qty, remaining, status))
        assert position["sellable"] == 1091 - 101, (order_no, status, position)
    # 정정으로 갈아탄 신주문이 장부에 있어야 한다. 원주문은 없어야 한다.
    assert set(app._open_sell_orders[code]) == {"0008201"}, \
        app._open_sell_orders
    print("ok (정정 중에도 걸어 둔 수량은 묶임)")


def demo_amendment_when_sellable_is_short():
    """묶인 수량이 매도가능보다 클 때 정정을 받아도 수량이 생기지 않는다.

    2026-09-23 0010S0. 보유 33주 중 24주가 이미 걸려 매도가능이 9주인
    상태에서 그 24주 주문을 정정했다. 빼기를 먼저 하면 `max(0, 9-24)`가
    모자란 15를 삼키고, 뒤이은 되돌림 +24가 그 몫을 없던 수량으로 만든다.
    청산이 그 15주로 매도를 내 「0주 매도가능」으로 거부됐다.
    """
    code = "0010S0"
    app = _app(code, 33)
    position = app._position_book[code]
    position["sellable"] = 33

    app._track_open_sell(code, "0008557", _event("0008557", "0000000", 24, 0, 24))
    assert position["sellable"] == 9, position      # 24주 묶임

    # 정정 — 원주문이 풀리고 신주문이 묶인다. 합은 0이어야 한다.
    app._track_open_sell(
        code, "0008569", _event("0008569", "0008557", 24, 0, 24, "접수"))
    app._track_open_sell(
        code, "0008569", _event("0008569", "0008557", 24, 0, 24, "확인"))
    assert position["sellable"] == 9, position      # 옛 코드는 24였다
    assert set(app._open_sell_orders[code]) == {"0008569"}, \
        app._open_sell_orders

    # 그 24주가 체결되면 보유만 줄고 매도가능은 그대로다.
    app._track_open_sell(
        code, "0008569", _event("0008569", "0008557", 24, 24, 0, "체결"))
    assert (position["held"], position["sellable"]) == (9, 9), position

    # 남은 9주를 걸면 매도가능이 0이 된다. 청산은 이 주문을 거둬야 판다.
    app._track_open_sell(code, "0008609", _event("0008609", "0000000", 9, 0, 9))
    assert (position["held"], position["sellable"]) == (9, 0), position
    assert _live_sell(app, code) == 9
    print("ok (매도가능이 모자랄 때 정정해도 수량이 안 생김)")


def demo_cancel_accept_is_not_amendment():
    """취소 주문의 접수는 원주문을 건드리지 않는다.

    정정 확인과 똑같이 원주문번호를 싣고 잔량>0으로 온다. 상태만 다르다.
    여기서 갈아타면 아직 살아 있는 원주문이 장부에서 사라지고, 뒤따라 올
    취소 확인이 지울 것을 못 찾는다.
    """
    code = "005930"
    app = _app(code, 500)
    position = app._position_book[code]

    app._track_open_sell(code, "0001", _event("0001", "0000000", 200, 0, 200))
    assert position["sellable"] == 300, position

    # 취소 주문 접수 — 원주문 0001은 아직 200주 살아 있다.
    app._track_open_sell(
        code, "0002", _event("0002", "0001", 200, 0, 200, "접수"))
    assert set(app._open_sell_orders[code]) == {"0001"}, \
        app._open_sell_orders
    assert position["sellable"] == 300, position

    # 취소 확인 — 이제 원주문이 죽고 200주가 풀린다.
    app._track_open_sell(
        code, "0002", _event("0002", "0001", 200, 0, 0, "확인"))
    assert code not in app._open_sell_orders, app._open_sell_orders
    assert position["sellable"] == 500, position
    print("ok (취소 접수와 정정 확인을 가름)")


def demo_fill_updates_both():
    """체결은 보유를 줄이고, 매도가능은 남은 미체결만큼만 묶인다."""
    code = "005930"
    app = _app(code, 500)
    position = app._position_book[code]

    app._track_open_sell(code, "0001", _event("0001", "0000000", 200, 0, 200))
    assert (position["held"], position["sellable"]) == (500, 300), position

    # 부분체결 120주. 잔량 80이 계속 묶인다.
    app._track_open_sell(code, "0001", _event("0001", "0000000", 200, 120, 80))
    assert (position["held"], position["sellable"]) == (380, 300), position

    # 나머지 80주 체결. 묶인 것이 없어지고 보유 300주를 다 팔 수 있다.
    app._track_open_sell(code, "0001", _event("0001", "0000000", 200, 200, 0))
    assert (position["held"], position["sellable"]) == (300, 300), position

    # 매수 체결분은 당일이라도 그대로 팔 수 있다.
    app._track_open_buy(code, "0002", _event("0002", "0000000", 50, 50, 0))
    assert (position["held"], position["sellable"]) == (350, 350), position
    print("ok (체결이 보유·매도가능에 함께 반영)")


def demo_buy_fill_respects_open_sell():
    """매도를 걸어 둔 채 매수가 체결돼도 묶인 수량은 그대로다."""
    code = "005930"
    app = _app(code, 500)
    position = app._position_book[code]
    app._track_open_sell(code, "0001", _event("0001", "0000000", 200, 0, 200))
    app._track_open_buy(code, "0002", _event("0002", "0000000", 100, 100, 0))
    assert (position["held"], position["sellable"]) == (600, 400), position
    print("ok (매수 체결이 묶인 매도를 풀지 않음)")


if __name__ == "__main__":
    demo_amendment_does_not_leak()
    demo_amendment_keeps_binding()
    demo_amendment_when_sellable_is_short()
    demo_cancel_accept_is_not_amendment()
    demo_fill_updates_both()
    demo_buy_fill_respects_open_sell()
