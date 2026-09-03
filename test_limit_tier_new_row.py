# -*- coding: utf-8 -*-
"""편입 직후 행이 상한가정렬에서 위로 튀지 않는지 검사.

체결(0B)에는 잔량이 없다. 새 행에 체결만 먼저 닿으면 현재가는 차고
매도잔량은 0으로 남아 '매도가 빈 종목'(TIER_NO_ASK)으로 잘못 묶여
맨 위로 올라갔다가 첫 호가(0D)에 내려갔다.
"""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import gui  # noqa: E402

BLANK = {"upper": 0, "price": 0, "rate": 0.0, "exp_price": 0, "exp_rate": 0.0,
         "ask_qty": 0, "bid_qty": 0, "ask_price": 0, "bid_price": 0}


def row(**over):
    return dict(BLANK, **over)


def demo():
    # 편입 직후 값이 전부 0. 맨 아래다.
    assert gui._limit_tier(row()) == gui.TIER_PLAIN

    # 체결만 먼저 닿은 상태. 호가를 받은 적이 없으므로 여전히 맨 아래다.
    fill_only = row(price=12_500, rate=3.2)
    assert gui._limit_tier(fill_only) == gui.TIER_PLAIN, gui._limit_tier(fill_only)

    # 첫 호가가 닿으면 평범한 종목 그대로다.
    quoted = row(price=12_500, rate=3.2, ask_qty=4_100, bid_qty=9_000,
                 ask_price=12_500, bid_price=12_495)
    assert gui._limit_tier(quoted) == gui.TIER_PLAIN

    # 진짜로 매도호가가 빈 종목은 매수호가가 남는다. 상한가 직전 묶음이다.
    empty_ask = row(price=12_500, rate=3.2, ask_qty=0, bid_qty=9_000,
                    ask_price=0, bid_price=12_500)
    assert gui._limit_tier(empty_ask) == gui.TIER_NO_ASK

    # 실제 상한가·점상 대기 판정은 그대로다.
    assert gui._limit_tier(row(upper=13_000, price=13_000, rate=29.9,
                               bid_qty=500_000, bid_price=13_000)
                           ) == gui.TIER_LIMIT_CLEAN
    assert gui._limit_tier(row(upper=13_000, exp_price=13_000, bid_qty=500_000,
                               bid_price=13_000)) == gui.TIER_WAIT_CLEAN
    print("ok")


if __name__ == "__main__":
    demo()
