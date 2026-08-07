# -*- coding: utf-8 -*-
"""주문설정 저장 범위 검사.

3단매도와 자동취소는 조건이 맞으면 스스로 주문을 낸다. 디스크에 남으면 앱을
켜자마자 되살아나 사용자가 모르는 사이에 매도나 취소가 나간다. 저장하지도
읽지도 않아야 하고, 예전 버전이 남긴 값도 지워야 한다.

청산키는 사용자가 눌러야 나가지만 앱이 뒤에 있어도 먹는 전역키다. 어제 배정이
남으면 오늘 화면에 없는 종목이 청산되므로 그날만 유효해야 한다.
"""
import json
import logging
import os
import types
from datetime import datetime

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSettings

import main

logging.disable(logging.CRITICAL)  # 운영 bot.log에 쓰지 않는다

INI = os.path.join(
    os.environ.get("TEMP", "."), "trading_bot_order_settings_test.ini")


class Stub:
    """`App`에서 주문설정 저장·복원에 필요한 부분만 세운 대역."""

    def __init__(self):
        self._settings = QSettings(INI, QSettings.IniFormat)
        self._account_auto_cancel_armed = set()
        self._balance_sell_settings = {}
        self._balance_sell_stage = {}
        self._balance_sell_date = {}
        self._exit_hotkey_specs = {}
        for name in ("_save_order_settings", "_load_order_settings"):
            setattr(self, name,
                    types.MethodType(getattr(main.App, name), self))


def seed(**values):
    settings = QSettings(INI, QSettings.IniFormat)
    settings.clear()
    for key, value in values.items():
        settings.setValue("order/" + key, json.dumps(value, ensure_ascii=False))
    settings.sync()


def demo():
    today = datetime.now().strftime("%Y%m%d")
    hotkeys = {"": {"011330": {"key": 1}}}

    # 1) 예전 버전이 남긴 3단매도·자동취소는 되살리지 않고 키까지 지운다.
    seed(auto_cancel_armed=["011330", "001290"],
         balance_sell={"011330": {"setting": {"slot1": 1_000_000},
                                  "stage": 0, "date": today}},
         exit_hotkeys={"date": today, "specs": hotkeys})
    app = Stub()
    app._load_order_settings()
    assert app._account_auto_cancel_armed == set(), app._account_auto_cancel_armed
    assert app._balance_sell_settings == {}, app._balance_sell_settings
    assert app._exit_hotkey_specs == hotkeys, app._exit_hotkey_specs
    disk = QSettings(INI, QSettings.IniFormat)
    assert disk.value("order/auto_cancel_armed") is None
    assert disk.value("order/balance_sell") is None

    # 2) 어제 배정한 청산키는 되살리지 않고 저장분도 지운다.
    seed(exit_hotkeys={"date": "20260101", "specs": hotkeys})
    app = Stub()
    app._load_order_settings()
    assert app._exit_hotkey_specs == {}, app._exit_hotkey_specs
    assert QSettings(INI, QSettings.IniFormat).value("order/exit_hotkeys") is None

    # 3) 날짜 없는 옛 형식도 언제 배정한 것인지 모르므로 버린다.
    seed(exit_hotkeys=hotkeys)
    app = Stub()
    app._load_order_settings()
    assert app._exit_hotkey_specs == {}, app._exit_hotkey_specs

    # 4) 저장은 청산키만, 날짜와 함께. 나머지는 켜 둬도 디스크에 안 남는다.
    seed()
    app = Stub()
    app._account_auto_cancel_armed = {"011330"}
    app._balance_sell_settings = {"011330": {"slot1": 1_000_000}}
    app._balance_sell_stage = {"011330": 0}
    app._balance_sell_date = {"011330": today}
    app._exit_hotkey_specs = dict(hotkeys)
    app._save_order_settings()
    disk = QSettings(INI, QSettings.IniFormat)
    assert disk.value("order/auto_cancel_armed") is None
    assert disk.value("order/balance_sell") is None
    stored = json.loads(disk.value("order/exit_hotkeys"))
    assert stored["date"] == today, stored
    assert stored["specs"] == hotkeys, stored

    QSettings(INI, QSettings.IniFormat).clear()
    if os.path.exists(INI):
        os.remove(INI)
    print("ok")


if __name__ == "__main__":
    demo()
