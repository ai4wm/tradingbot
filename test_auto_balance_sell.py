# -*- coding: utf-8 -*-
"""주문을 낸 종목에 3단매도가 없을 때만 자동으로 걸리는지 검사.

진입만 하고 3단매도를 잊으면 상한가가 무너질 때 방어가 통째로 없다. 반대로
손으로 맞춰 둔 설정을 주문 한 번에 덮어쓰면 그게 더 나쁘다. 그래서 없을 때만
걸고, 설정창과 같은 조건(최우선 매수호가 = 상한가)에서만 건다.
"""
import os
import tempfile
import types

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSettings  # noqa: E402

import gui  # noqa: E402

CODE = "950200"
UPPER = 3750
MANUAL = {"first": 111, "second": 22, "third": 0,
          "first_ratio": 0.0, "second_ratio": 1.0, "third_ratio": 1.0,
          "market_sell": True}


class Screen:
    """`auto_balance_sell_on_order`가 쓰는 부분만 세운 대역."""

    def __init__(self, row, settings_values=(), existing=None):
        self.model = types.SimpleNamespace(
            rows={CODE: row}, balance_sell_settings={})
        if existing:
            self.model.balance_sell_settings[CODE] = dict(existing)
        handle, self._ini_path = tempfile.mkstemp(suffix=".ini")
        os.close(handle)
        self._settings = QSettings(self._ini_path, QSettings.IniFormat)
        for key, value in settings_values:
            self._settings.setValue(key, value)
        self.applied = []
        self.auto_balance_sell_on_order = types.MethodType(
            gui.ConditionScreen.auto_balance_sell_on_order, self)

    def set_balance_sell_setting(self, code, setting):
        self.applied.append((code, setting))
        self.model.balance_sell_settings[code] = dict(setting)


def row(bid_qty, bid_price=UPPER, upper=UPPER):
    return {"bid_qty": bid_qty, "bid_price": bid_price, "upper": upper}


def demo():
    # 상한가에 붙어 있고 설정이 없으면 현재 잔량 기준으로 건다.
    # 잔량 100만 -> 구간표 50만/30만/20만, 비율은 새 설정창 기본값.
    screen = Screen(row(1_000_000))
    assert screen.auto_balance_sell_on_order(CODE) is True
    _, setting = screen.applied[0]
    assert (setting["first"], setting["second"], setting["third"]) == (
        500_000, 300_000, 200_000), setting
    assert (setting["first_ratio"], setting["second_ratio"],
            setting["third_ratio"]) == (0.0, 1.0, 1.0), setting
    assert setting["market_sell"] is False, setting
    # 두 번째 주문은 이미 걸려 있으므로 그대로 둔다.
    assert screen.auto_balance_sell_on_order(CODE) is False
    assert len(screen.applied) == 1, screen.applied

    # 손으로 맞춘 설정은 덮어쓰지 않는다.
    manual = Screen(row(1_000_000), existing=MANUAL)
    assert manual.auto_balance_sell_on_order(CODE) is False
    assert manual.applied == [], manual.applied
    assert manual.model.balance_sell_settings[CODE] == MANUAL

    # 상한가가 아니면 걸지 않는다. 잔량이 상한가 대기 물량이 아니라서
    # 첫 호가 틱에 그대로 발동해 시초가에 던지게 된다.
    off = Screen(row(1_000_000, bid_price=3745))
    assert off.auto_balance_sell_on_order(CODE) is False
    assert off.applied == [], off.applied
    # 상한가를 모르는 종목도 마찬가지다.
    unknown = Screen(row(1_000_000, bid_price=0, upper=0))
    assert unknown.auto_balance_sell_on_order(CODE) is False
    # 잔량이 없으면 기준을 만들 수 없다.
    empty = Screen(row(0))
    assert empty.auto_balance_sell_on_order(CODE) is False

    # 마지막으로 쓰던 단계 체크와 시장가 상태를 그대로 따른다.
    last = Screen(row(1_000_000), settings_values=(
        (gui.BALANCE_SELL_STAGE_LAST_KEYS[2], "false"),
        (gui.BALANCE_SELL_MARKET_LAST_KEY, "true")))
    assert last.auto_balance_sell_on_order(CODE) is True
    _, setting = last.applied[0]
    assert setting["third"] == 0, setting        # 해제한 단계 = 기준 0
    assert setting["market_sell"] is True, setting

    # 모든 단계를 꺼 뒀으면 걸 것이 없다.
    none_on = Screen(row(1_000_000), settings_values=[
        (key, "false") for key in gui.BALANCE_SELL_STAGE_LAST_KEYS])
    assert none_on.auto_balance_sell_on_order(CODE) is False

    for stub in (screen, manual, off, unknown, empty, last, none_on):
        os.unlink(stub._ini_path)
    print("ok")


if __name__ == "__main__":
    demo()
