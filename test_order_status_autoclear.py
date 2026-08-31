# -*- coding: utf-8 -*-
"""취소가 끝나면 주문 상태를 스스로 지우는지 검사.

'수 취소'가 남아 있으면 매수 버튼이 잠긴다. 푸는 방법이 주문 상태 칸을 직접
누르는 것뿐이라, 다른 종목을 눌렀다 돌아와도 안 풀렸다(2026-08-31 티케이지애강:
09:02:26에 9건 취소확인이 끝났는데 버튼이 계속 잠겨 있었다).
"""
import os
import types

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

import gui  # noqa: E402

CODE = "022220"


class Model:
    def __init__(self):
        self.order_status = {}
        self.order_cancellable = set()
        self.balance_sell_settings = {}

    def set_order_status(self, code, text, cancellable=False):
        if text:
            self.order_status[code] = text
        else:
            self.order_status.pop(code, None)
        if cancellable:
            self.order_cancellable.add(code)
        else:
            self.order_cancellable.discard(code)


def screen():
    stub = types.SimpleNamespace(
        model=Model(),
        _order_target_code=CODE,
        _excluded_with_orders=set(),
        order_status_value=types.SimpleNamespace(setText=lambda _t: None),
        order_status_acknowledged=types.SimpleNamespace(
            emit=lambda _c: stub.acked.append(_c)),
        _refresh_order_actions=lambda: None,
        acked=[],
    )
    for name in ("set_order_state", "_release_excluded",
                 "_holds_excluded_row", "_remove_excluded"):
        setattr(stub, name,
                types.MethodType(getattr(gui.ConditionScreen, name), stub))
    return stub


def demo():
    app = QApplication.instance() or QApplication([])

    # 취소 확인이 아직 안 온 동안에는 상태를 남겨 둔다.
    s = screen()
    s.set_order_state(CODE, "수 취소", "", True)
    assert s.model.order_status.get(CODE) == "수 취소", s.model.order_status
    assert s.acked == [], s.acked

    # 잔량이 0이 되면 스스로 지운다.
    s.set_order_state(CODE, "수 취소", "", False)
    assert CODE not in s.model.order_status, s.model.order_status
    assert s.acked == [CODE], s.acked

    # 취소가 아닌 상태는 그대로 둔다. 완료·오류는 사람이 보고 지운다.
    for text in ("자 완료", "오류", "수량부족", "자 3/9"):
        t = screen()
        t.set_order_state(CODE, text, "", False)
        assert t.model.order_status.get(CODE) == text, (text, t.model.order_status)
        assert t.acked == [], (text, t.acked)
    del app
    print("ok")


if __name__ == "__main__":
    demo()
