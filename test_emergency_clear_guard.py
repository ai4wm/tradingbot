# -*- coding: utf-8 -*-
"""청산이 실패했을 때 청산키가 살아남는지 확인한다.

2026-09-23 0010S0. 청산키를 눌렀는데 매도가 거부됐고, 그 21ms 뒤에
청산키·3단매도·자동취소가 전부 풀렸다. 보유 9주가 그대로 남아 있었다.

    09:08:04.267  청산 F1
    09:08:05.298  ERROR emergency cancel/sell failed  held=9 sellable=0
    09:08:05.319  emergency cleared settings  hotkey=True
    09:08:05.340  exit hotkey cleared

거부 경로가 `self._position_book.pop(code)`로 장부를 버렸기 때문이다.
`_clear_after_emergency`는 「장부에 항목이 없으면 보유 0」으로 읽는다 —
체결이 없는 종목을 빠르게 판정하려고 일부러 둔 규칙인데, `pop`은
「없다」가 아니라 「모른다」라서 그 규칙을 오염시킨다.

이제 버리지 않고 방금 읽은 계좌 값으로 덮어쓴다.
"""
import logging
import os
import types

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import main  # noqa: E402

logging.disable(logging.CRITICAL)  # 운영 bot.log에 쓰지 않는다


class Stub:
    """`_clear_after_emergency`가 보는 상태만 세운 대역."""

    def __init__(self, book):
        self._position_book = book
        self._position_book_primed = True
        self._open_buy_orders = {}
        self._emergency_locked = {"0010S0"}
        self._account_auto_cancel_armed = set()
        self._balance_sell_settings = {}
        self._exit_hotkey_specs = {"F1": {"0010S0"}}
        self.cleared = []
        for name in ("_clear_after_emergency", "_pending_open_buys"):
            setattr(self, name,
                    types.MethodType(getattr(main.App, name), self))

    def _set_balance_sell(self, code, value):
        self.cleared.append(("balance_sell", code))

    def _set_account_auto_cancel(self, code, value):
        self.cleared.append(("auto_cancel", code))

    def _clear_exit_hotkey(self, code):
        self.cleared.append(("hotkey", code))


def demo_held_keeps_hotkey():
    """보유가 남아 있으면 아무것도 내리지 않는다."""
    app = Stub({"0010S0": {"held": 9, "sellable": 0}})
    app._clear_after_emergency("0010S0")
    assert app.cleared == [], app.cleared
    # 표식도 그대로여야 다음 청산키가 같은 경로를 탄다.
    assert "0010S0" in app._emergency_locked
    print("ok (보유가 남으면 청산키 유지)")


def demo_missing_entry_still_clears():
    """항목이 아예 없으면 보유 0으로 믿는다. 이 규칙 자체는 그대로다.

    체결이 한 번도 없었던 종목은 장부에 항목이 안 생긴다. 그때까지
    계좌를 조회하면 청산이 1초 느려진다(2026-09-11 006490 실측 1,085ms).
    """
    app = Stub({"005930": {"held": 100, "sellable": 100}})
    app._clear_after_emergency("0010S0")
    assert ("hotkey", "0010S0") in app.cleared, app.cleared
    print("ok (항목이 없으면 내림, 기존 규칙 유지)")


def demo_requery_refills_book():
    """거부 뒤 재조회가 장부를 비우지 않고 계좌 값으로 채우는지.

    `pop`이 남아 있으면 이 검사가 실패한다.
    """
    import inspect
    for name in ("_sell_account_position", "_emergency_exit_async"):
        src = inspect.getsource(getattr(main.App, name))
        assert "_position_book.pop(code, None)" not in src, name
        assert "self._position_book[code] = {" in src, name
    print("ok (거부 뒤 장부를 계좌 값으로 덮어씀)")


def demo_pending_buy_keeps_hotkey():
    """미체결 매수가 남아 있어도 내리지 않는다."""
    app = Stub({"0010S0": {"held": 0, "sellable": 0}})
    app._open_buy_orders = {"0010S0": {"0008609": (30, "KRX")}}
    app._clear_after_emergency("0010S0")
    assert app.cleared == [], app.cleared
    print("ok (미체결 매수가 남으면 청산키 유지)")


if __name__ == "__main__":
    demo_held_keeps_hotkey()
    demo_missing_entry_still_clears()
    demo_requery_refills_book()
    demo_pending_buy_keeps_hotkey()
