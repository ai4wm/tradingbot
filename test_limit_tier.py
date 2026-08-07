# -*- coding: utf-8 -*-
"""상한가정렬 묶음 판정과 등락률 정렬 키 검사.

2026-08-07 유니켐이 09:00을 넘기며 맨 위 묶음에서 라인 밖으로 밀려났다.
시각과 누적거래량으로 '장 시작 전'을 재던 것이 원인이었으므로, 그 상황을
그대로 세워 두고 회귀를 막는다. 같은 날 저유동성 우선주가 상시 단일가라
예상값이 안 꺼지는 탓에 상단을 하루 종일 점유하던 것도 함께 잡았다.
"""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

import gui
from gui import _limit_tier


def row(**changes) -> dict:
    """상한가 1204원 종목의 기본 상태. 필요한 값만 바꿔 쓴다."""
    base = dict(name="", upper=1204, base=926, price=0, rate=0.0,
                exp_price=0, exp_rate=0.0, ask_qty=0, bid_qty=0, vol=0)
    base.update(changes)
    return base


def demo():
    QApplication.instance() or QApplication([])

    waiting = row(exp_price=1204, exp_rate=29.9, bid_qty=5_000_000)
    assert _limit_tier(waiting) == 0, "예상상한·매도잔량0·매수잔량 = 맨 위"

    # 장전 시간외종가는 전일 종가로 체결된다. 거래량은 늘어도 등락률은 0이다.
    assert _limit_tier(row(**{**waiting, "vol": 12_345})) == 0, (
        "장전 시간외 체결이 있어도 맨 위를 지켜야 한다")

    # 구분선은 여기까지다. 아래 둘은 선 밖이라 점상알림도 울리지 않는다.
    assert _limit_tier(row(**{**waiting, "ask_qty": 700})) == 1, "매도잔량 있음"
    assert _limit_tier(row(**{**waiting, "bid_qty": 0})) == 1, "매수잔량 없음"

    # 시초가가 잡히면 등락률이 튀고 장 시작 묶음으로 넘어간다.
    opened = row(price=1204, rate=29.9, vol=800_000)
    assert _limit_tier(opened) == 3, "실제 상한가·매도잔량0"
    assert _limit_tier(row(**{**opened, "ask_qty": 500})) == 4, "실제 상한가·매도잔량"

    # 거래 중인데 매도호가가 비었으면 상한가 직전이다. 실제 상한가 바로 아래.
    assert _limit_tier(row(price=1100, rate=18.8, vol=500_000)) == 5, "매도잔량 0"

    # 장 시작 전 나머지는 예상상한이 아니어도 대기열에 남는다.
    assert _limit_tier(row(exp_price=1100, exp_rate=18.0, ask_qty=900,
                           bid_qty=200_000)) == 2, "장 시작 전 나머지"
    # 시초가가 전일종가와 같으면 등락률이 정확히 0이다. 거래는 됐으므로
    # 예상값이 꺼져 있고, 대기열에 남으면 안 된다.
    assert _limit_tier(row(price=926, rate=0.0, vol=500_000,
                           ask_qty=800)) == 6, "보합 출발은 대기열이 아니다"

    # 이미 거래된 종목은 예상값이 있어도 따로 묶지 않는다. VI든 단기과열이든
    # 실제 등락률이 뜬 이상 대기 종목이 아니다.
    vi = row(price=1100, rate=18.5, vol=500_000, exp_price=1204, exp_rate=29.9,
             ask_qty=300)
    assert _limit_tier(vi) == 6, "VI 예상상한도 일반 묶음"
    single = row(price=6100, rate=23.36, vol=3_128, exp_price=6420,
                 exp_rate=29.83, ask_qty=200, upper=6420, base=4945)
    assert _limit_tier(single) == 6, "상시 단일가 우선주도 일반 묶음"

    assert _limit_tier(row(price=900, rate=5.0, vol=100, ask_qty=10)) == 6, "일반"

    # 등락률 정렬 키: 예상값이 살아 있으면 그 값으로 비교한다. 표시는 그대로다.
    model = gui.StockModel()
    model.add_stock("VI", {**vi, "exp_hot": 1})
    model.add_stock("PLAIN", row(price=1260, rate=26.0, vol=500_000, ask_qty=500))
    index = model.index(model.codes.index("VI"), gui.RATE_COL)
    # exp_rate는 모델이 예상체결가와 전일종가로 다시 계산한다. 넘긴 값이 아니라
    # 저장된 값이 정렬 키가 되어야 한다.
    stored_exp = model.rows["VI"]["exp_rate"]
    assert stored_exp > 29, stored_exp
    assert model.data(index, Qt.DisplayRole) == "+18.50", "표시는 실제 등락률"
    assert model.data(index, Qt.UserRole) == stored_exp, "정렬은 예상등락률"
    plain = model.index(model.codes.index("PLAIN"), gui.RATE_COL)
    assert model.data(plain, Qt.UserRole) == 26.0, "예상값 없으면 실제 등락률"
    # 표시상 +18.5 < +26 이지만 정렬은 뒤집힌다.
    assert model.data(index, Qt.UserRole) > model.data(plain, Qt.UserRole)

    print("ok")


if __name__ == "__main__":
    demo()
