# -*- coding: utf-8 -*-
"""상한가정렬 묶음 판정 검사.

2026-08-07 유니켐이 09:00을 넘기며 맨 위 묶음에서 라인 밖으로 밀려났다.
시각과 누적거래량으로 '장 시작 전'을 재던 것이 원인이었으므로, 그 상황을
그대로 세워 두고 회귀를 막는다.
"""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from gui import _limit_tier


def row(**changes) -> dict:
    """상한가 1204원 종목의 기본 상태. 필요한 값만 바꿔 쓴다."""
    base = dict(upper=1204, price=0, rate=0.0, exp_price=0, exp_rate=0.0,
                ask_qty=0, bid_qty=0, vol=0)
    base.update(changes)
    return base


def demo():
    waiting = row(exp_price=1204, exp_rate=29.9, bid_qty=5_000_000)
    assert _limit_tier(waiting) == 0, "예상상한·매도잔량0·매수잔량 = 맨 위"

    # 장전 시간외종가는 전일 종가로 체결된다. 거래량은 늘어도 등락률은 0이다.
    assert _limit_tier(row(**{**waiting, "vol": 12_345})) == 0, (
        "장전 시간외 체결이 있어도 맨 위를 지켜야 한다")

    assert _limit_tier(row(**{**waiting, "ask_qty": 700})) == 1, "매도잔량 있음"
    assert _limit_tier(row(**{**waiting, "bid_qty": 0})) == 1, "매수잔량 없음"

    # 시초가가 잡히면 등락률이 튀고 장 시작 묶음으로 넘어간다.
    opened = row(price=1204, rate=29.9, vol=800_000)
    assert _limit_tier(opened) == 2, "실제 상한가·매도잔량0"
    assert _limit_tier(row(**{**opened, "ask_qty": 500})) == 3, "실제 상한가·매도잔량"

    # 장중 VI 예상상한은 이미 오른 상태라 대기 묶음이 아니다.
    vi = row(price=1100, rate=18.5, vol=500_000, exp_price=1204, exp_rate=29.9)
    assert _limit_tier(vi) == 4, "장중 예상상한·매도잔량0"
    assert _limit_tier(row(**{**vi, "ask_qty": 300})) == 5, "장중 예상상한·매도잔량"

    assert _limit_tier(row(price=900, rate=5.0, vol=100)) == 6, "일반 종목"
    print("ok")


if __name__ == "__main__":
    demo()
