# -*- coding: utf-8 -*-
"""100주씩 주문이 설정 횟수에 미달할 때 자투리를 마지막 1건으로 붙이는지 확인한다.

100주 단위로만 끊어 보내면 100주에 못 미치는 잔량이 통째로 남았다. 설정한
주문 건수를 넘기지 않는 선에서 그 잔량까지 태운다.
"""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from gui import ConditionScreen  # noqa: E402
from order import fixed_quantities  # noqa: E402


def _screen(available_qty, split_count, upper=1000):
    screen = ConditionScreen()
    screen.model.add_stock("B", {"name": "나종목"})
    screen.model.rows["B"]["upper"] = upper
    screen._order_target_code = "B"
    screen.order_enable_check.setChecked(True)
    screen.split_buttons[split_count].setChecked(True)
    screen.set_orderable_quantity("B", upper, {
        "code": "B", "price": upper,
        "cash_amount": available_qty * upper, "cash_qty": available_qty,
        "margin_amount": available_qty * upper, "margin_qty": available_qty,
        "applied_margin_rate": 100, "stock_margin_rate": "100%",
        "reserved_base": 0,
    })
    return screen


def _sent(available_qty, split_count):
    """100주씩 버튼을 눌렀을 때 실제로 나가는 (건수, 총수량, 분할내역)."""
    screen = _screen(available_qty, split_count)
    got = []
    screen.order_requested.connect(
        lambda code, mode, count, auto, total, price: got.append((count, total)))
    screen._request_order("fixed")
    if not got:
        return 0, 0, []
    count, total = got[0]
    return count, total, fixed_quantities(total)


def demo():
    app = QApplication.instance() or QApplication([])

    # 설정 9회에 850주: 100주 8건으로는 50주가 남는다 -> 마지막 1건에 붙인다.
    count, total, plan = _sent(850, 9)
    assert (count, total) == (9, 850), (count, total)
    assert plan == [100] * 8 + [50], plan

    # 딱 떨어지면 자투리 건이 생기지 않는다.
    count, total, plan = _sent(900, 9)
    assert (count, total) == (9, 900), (count, total)
    assert plan == [100] * 9, plan

    # 설정 횟수를 이미 다 쓰면 자투리는 붙이지 않는다. 건수가 먼저다.
    count, total, plan = _sent(950, 9)
    assert (count, total) == (9, 900), (count, total)
    assert plan == [100] * 9, plan

    # 설정 횟수가 적으면 예전 그대로 100주씩만 나간다.
    count, total, plan = _sent(850, 3)
    assert (count, total) == (3, 300), (count, total)
    assert plan == [100] * 3, plan

    # 100주에 미달하면 100주씩 버튼은 그대로 막는다(분할매수가 처리).
    assert _sent(50, 9) == (0, 0, []), _sent(50, 9)

    # 예상주문 줄에도 자투리 건이 드러나야 한다.
    preview = _screen(850, 9).order_preview_value.text()
    assert "실제 9회" in preview and "100주씩+50주" in preview, preview
    assert "총 850주" in preview, preview

    # 총수량만으로 분할내역이 정해진다(main._submit_order가 쓰는 갈래).
    assert fixed_quantities(0) == []
    assert fixed_quantities(50) == [50]
    assert fixed_quantities(1_000) == [100] * 10

    # 계획은 언제나 설정 횟수를 넘지 않는다.
    for available in range(0, 1_500, 17):
        for split in range(1, 10):
            plan = fixed_quantities(ConditionScreen._fixed_total(available, split))
            assert len(plan) <= split, (available, split, plan)
            assert sum(plan) <= available, (available, split, plan)
            assert all(quantity > 0 for quantity in plan), (available, split, plan)

    del app
    print("ok (100주씩 자투리 주문)")


if __name__ == "__main__":
    demo()
