# -*- coding: utf-8 -*-
"""20만주 미만 구간의 3단 제안 비율(60·40·30%)과 단계 역전 방지를 확인한다."""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import gui  # noqa: E402


def demo():
    assert gui._balance_sell_suggestion(100_000) == (60_000, 40_000, 30_000)
    assert gui._balance_sell_suggestion(50_000) == (30_000, 20_000, 15_000)
    assert gui._balance_sell_suggestion(0) == (0, 0, 0)
    # 20만주부터는 구간표를 그대로 쓴다.
    assert gui._balance_sell_suggestion(200_000) == (100_000, 50_000, 20_000)

    for current in range(1, 200_000, 37):
        first, second, third = gui._balance_sell_suggestion(current)
        assert first > second > third >= 1, (current, first, second, third)
    print("ok")


if __name__ == "__main__":
    demo()
