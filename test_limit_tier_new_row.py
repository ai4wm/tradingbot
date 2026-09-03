# -*- coding: utf-8 -*-
"""편입 직후 행이 상한가정렬에서 위로 튀지 않는지 검사.

편입 시 행은 값이 전부 0으로 만들어지고 REST 백필은 0.4초 뒤에 나간다.
그 0을 일반 비교에 그대로 섞으면 등락률이 음수인 종목들 위로 올라가
맨 위에 떴다가 첫 시세가 닿는 순간 제자리로 떨어졌다.

체결(0B)에는 잔량이 없다는 것도 함께 본다. 체결만 먼저 닿은 행은
현재가는 차고 매도잔량이 0으로 남아 '매도가 빈 종목'으로 오인됐다.
"""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

import gui  # noqa: E402

BLANK = {"upper": 0, "price": 0, "rate": 0.0, "exp_price": 0, "exp_rate": 0.0,
         "ask_qty": 0, "bid_qty": 0, "ask_price": 0, "bid_price": 0}


def row(**over):
    return dict(BLANK, **over)


def check_tiers():
    # 편입 직후. 어떤 묶음에도 넣지 않는다.
    assert gui._limit_tier(row()) == gui.TIER_BLANK

    # 체결만 먼저 닿은 상태. 호가를 받은 적이 없으므로 일반 묶음이다.
    fill_only = row(price=12_500, rate=3.2)
    assert gui._limit_tier(fill_only) == gui.TIER_PLAIN, gui._limit_tier(fill_only)

    # 첫 호가가 닿으면 평범한 종목 그대로다.
    assert gui._limit_tier(row(price=12_500, rate=3.2, ask_qty=4_100,
                               bid_qty=9_000, ask_price=12_500,
                               bid_price=12_495)) == gui.TIER_PLAIN

    # 진짜로 매도호가가 빈 종목은 매수호가가 남는다. 상한가 직전 묶음이다.
    assert gui._limit_tier(row(price=12_500, rate=3.2, bid_qty=9_000,
                               bid_price=12_500)) == gui.TIER_NO_ASK

    # 실제 상한가·점상 대기 판정은 그대로다.
    assert gui._limit_tier(row(upper=13_000, price=13_000, rate=29.9,
                               bid_qty=500_000, bid_price=13_000)
                           ) == gui.TIER_LIMIT_CLEAN
    assert gui._limit_tier(row(upper=13_000, exp_price=13_000, bid_qty=500_000,
                               bid_price=13_000)) == gui.TIER_WAIT_CLEAN


def check_position(order):
    """등락률이 음수인 종목이 섞인 표에 새 행을 넣어 자리를 확인한다."""
    model = gui.StockModel()
    proxy = gui.TieredProxy()
    proxy.setSourceModel(model)
    proxy.setSortRole(Qt.UserRole)
    proxy.limit_mode = True
    proxy.setDynamicSortFilter(False)
    for number, rate in enumerate((25.0, 3.0, -4.0, -11.0), start=1):
        code = "%06d" % number
        model.add_stock(code, {"name": code, "price": 10_000, "rate": rate,
                               "upper": 13_000, "ask_qty": 100, "bid_qty": 100,
                               "ask_price": 10_000, "bid_price": 9_995})
    model.add_stock("999999", {"name": "999999"})  # 편입 직후, 값 없음
    proxy.invalidate()
    proxy.sort(gui.FIELDS.index("rate"), order)
    return [proxy.row_code(r) for r in range(proxy.rowCount())]


def demo():
    QApplication.instance() or QApplication([])
    check_tiers()
    for order in (Qt.DescendingOrder, Qt.AscendingOrder):
        placed = check_position(order)
        assert placed[-1] == "999999", (order, placed)
    print("ok")


if __name__ == "__main__":
    demo()
