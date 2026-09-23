# -*- coding: utf-8 -*-
"""앱 밖에서 산 물량에도 청산키가 걸리고, 감시를 내릴 때 문구도 지우는지.

2026-09-23 와이즈플래닛컴퍼니(0010S0).

    10:14:27.435  청산 F1
    10:14:27.498  80주 체결 → 칸에 「긴급정리」
    10:14:27.519  exit hotkey cleared        ← 잔고가 비어 자동 해제
    10:14:55      86주 재매수 (영웅문)        ← 청산키가 안 걸림
    10:52:37      손으로 F1 재등록            ← 32분 무방비

`_auto_assign_exit_hotkey`가 주문 경로(`_send_order_batch`)에만 붙어 있어
영웅문 매수는 그냥 지나갔다. 그런데 칸에는 「긴급정리」가 남아 있어 걸려
있는 것처럼 보였다.
"""
import logging
import os
import types

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import main  # noqa: E402

logging.disable(logging.CRITICAL)  # 운영 bot.log에 쓰지 않는다


class Screen:
    def __init__(self, rows):
        self.prefix = ""
        self.model = types.SimpleNamespace(rows=rows)
        self.states = []

    def set_order_state(self, code, status, tip, busy):
        self.states.append((code, status))


class Stub:
    """장부 갱신과 청산키 배정 갈래만 세운 대역."""

    def __init__(self, rows, book=None):
        self._open_buy_orders = {}
        self._open_sell_orders = {}
        self._position_book = book if book is not None else {}
        self._position_book_primed = True
        self._position_fill_ids = set()
        self._position_filled = {}
        self._order_cancelled = {}
        self._cancel_sent_orders = set()
        self._emergency_locked = set()
        self._account_auto_cancel_armed = set()
        self._balance_sell_settings = {}
        self._exit_hotkey_specs = {}
        self.screen = Screen(rows)
        self.views = [types.SimpleNamespace(screen=self.screen)]
        self.armed = []
        for name in ("_track_open_buy", "_track_order_book", "_new_fill_qty",
                     "_clear_order_status", "_clear_after_emergency",
                     "_pending_open_buys",
                     "_auto_assign_exit_hotkey_everywhere"):
            setattr(self, name,
                    types.MethodType(getattr(main.App, name), self))

    def _auto_assign_exit_hotkey(self, screen, code):
        self.armed.append(code)

    def _push_pending_orders(self, code):
        pass

    def _set_balance_sell(self, code, value):
        pass

    def _set_account_auto_cancel(self, code, value):
        pass

    def _clear_exit_hotkey(self, code):
        pass


def fill(code="0010S0", order_no="1", qty=86):
    return {"code": code, "side": "buy", "status": "체결", "order_qty": qty,
            "remaining_qty": 0, "fill_qty": qty, "fill_id": order_no}


def demo_outside_buy_arms_hotkey():
    """보유 0에서 체결이 들어오면 청산키를 건다. 영웅문 매수가 여기로 온다."""
    app = Stub({"0010S0": {}}, book={"0010S0": {"held": 0, "sellable": 0}})
    app._track_open_buy("0010S0", "0020107", fill())
    assert app.armed == ["0010S0"], app.armed
    assert app._position_book["0010S0"]["held"] == 86
    print("ok (앱 밖 매수에도 청산키가 걸림)")


def demo_added_buy_does_not_rearm():
    """이미 들고 있으면 안 건다. 손으로 푼 키가 되살아나면 안 된다."""
    app = Stub({"0010S0": {}}, book={"0010S0": {"held": 100, "sellable": 100}})
    app._track_open_buy("0010S0", "0020107", fill())
    assert app.armed == [], app.armed
    assert app._position_book["0010S0"]["held"] == 186
    print("ok (추가 매수는 청산키를 다시 안 걸음)")


def demo_first_buy_on_new_code_arms():
    """장부에 없던 종목도 0에서 시작하므로 걸린다."""
    app = Stub({"0010S0": {}})
    app._track_open_buy("0010S0", "0020107", fill())
    assert app.armed == ["0010S0"], app.armed
    print("ok (처음 보는 종목도 걸림)")


def demo_clear_wipes_order_status():
    """청산 뒤처리가 「긴급정리」 문구도 지운다."""
    app = Stub({"0010S0": {}}, book={"0010S0": {"held": 0, "sellable": 0}})
    app._emergency_locked.add("0010S0")
    app._exit_hotkey_specs = {"": {"0010S0": {"label": "F1"}}}
    app._clear_after_emergency("0010S0")
    assert ("0010S0", "") in app.screen.states, app.screen.states
    print("ok (감시를 내리면 주문상태 칸도 비움)")


def demo_held_keeps_status():
    """보유가 남아 있으면 문구도 그대로 둔다."""
    app = Stub({"0010S0": {}}, book={"0010S0": {"held": 86, "sellable": 86}})
    app._emergency_locked.add("0010S0")
    app._exit_hotkey_specs = {"": {"0010S0": {"label": "F1"}}}
    app._clear_after_emergency("0010S0")
    assert app.screen.states == [], app.screen.states
    print("ok (보유가 남으면 문구 유지)")


if __name__ == "__main__":
    demo_outside_buy_arms_hotkey()
    demo_added_buy_does_not_rearm()
    demo_first_buy_on_new_code_arms()
    demo_clear_wipes_order_status()
    demo_held_keeps_status()
