# -*- coding: utf-8 -*-
"""미체결이나 3단매도가 남은 종목은 조건에서 빠져도 행을 지우지 않는지 검사.

상한가가 무너지면 그 종목은 상한가 조건에서도 빠진다. 그때 행을 지우면 미체결을
취소할 버튼도, 3단매도를 끌 셀도 함께 사라진다. 3단매도는 전량 매도 뒤에도
살아 있어서, 안 보인다고 꺼진 것으로 읽고 아래에 매수를 걸면 체결되는 순간
그대로 다시 팔린다(2026-08-26 이건산업).
"""
import os
import types

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

import gui  # noqa: E402

CODE = "008250"
PLAIN = "000660"


class Model:
    def __init__(self):
        self.order_cancellable: set[str] = set()
        self.balance_sell_settings: dict[str, dict] = {}
        self.removed: list[str] = []
        self.order_status: dict[str, str] = {}

    def remove_stock(self, code):
        self.removed.append(code)

    def set_order_status(self, code, text, cancellable=False):
        self.order_status[code] = text
        if cancellable:
            self.order_cancellable.add(code)
        else:
            self.order_cancellable.discard(code)

    def set_order_target(self, code):
        pass


def screen(model):
    stub = types.SimpleNamespace(
        model=model,
        auto_remove=types.SimpleNamespace(isChecked=lambda: True),
        margin_order_check=types.SimpleNamespace(setEnabled=lambda _on: None),
        order_status_value=types.SimpleNamespace(setText=lambda _t: None),
        _order_target_code="",
        _excluded_with_orders=set(),
        _refresh_order_target_display=lambda: None,
        _refresh_order_actions=lambda: None,
    )
    for name in ("on_excluded", "_remove_excluded", "set_order_state",
                 "_holds_excluded_row", "_release_excluded"):
        setattr(stub, name,
                types.MethodType(getattr(gui.ConditionScreen, name), stub))
    return stub


def demo():
    app = QApplication.instance() or QApplication([])

    # 주문이 없는 종목은 예전처럼 바로 지운다.
    model = Model()
    plain = screen(model)
    plain.on_excluded(PLAIN)
    assert model.removed == [PLAIN], model.removed

    # 미체결이 남은 종목은 붙잡아 둔다.
    model = Model()
    model.order_cancellable.add(CODE)
    stub = screen(model)
    stub.on_excluded(CODE)
    assert model.removed == [], model.removed
    assert CODE in stub._excluded_with_orders

    # 주문이 정리되면 그때 지운다.
    stub.set_order_state(CODE, "", "", False)
    assert model.removed == [CODE], model.removed
    assert CODE not in stub._excluded_with_orders

    # 3단매도가 걸려 있으면 미체결이 없어도 붙잡아 둔다. 전량 매도 뒤에도
    # 설정은 살아 있어서, 행이 사라지면 꺼진 것으로 읽게 된다.
    model = Model()
    model.balance_sell_settings[CODE] = {"first": 1}
    armed = screen(model)
    armed.on_excluded(CODE)
    assert model.removed == [], model.removed
    armed.set_order_state(CODE, "", "", False)   # 주문이 없어도 남는다
    assert model.removed == [], model.removed
    model.balance_sell_settings.pop(CODE)
    armed._release_excluded(CODE)                # 3단매도를 끄면 그때 지운다
    assert model.removed == [CODE], model.removed

    # 잔량이 남아 있는 동안의 상태 갱신으로는 지우지 않는다.
    model = Model()
    model.order_cancellable.add(CODE)
    held = screen(model)
    held.on_excluded(CODE)
    held.set_order_state(CODE, "접수", "", True)
    assert model.removed == [], model.removed
    del app
    print("ok")


if __name__ == "__main__":
    demo()
