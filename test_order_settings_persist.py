# -*- coding: utf-8 -*-
"""3단매도·자동취소·청산키가 재시작 뒤 그대로 살아나는지 확인한다.

가장 중요한 건 3단매도 '단계'다. 단계를 잃고 0으로 되살아나면 이미 나간
1단계 매도가 재시작 직후 한 번 더 나간다. 아래 시험이 그걸 막는다.
"""
import os
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSettings  # noqa: E402

import main  # noqa: E402

TODAY = datetime.now().strftime("%Y%m%d")
YESTERDAY = (datetime.now() - timedelta(days=1)).strftime("%Y%m%d")
SETTING = {"first": 5000, "second": 3000, "third": 1000,
           "first_ratio": 0.3, "second_ratio": 0.5, "third_ratio": 1.0,
           "market_sell": False}
HOTKEY = {"key": 65, "modifiers": 67108864, "text": "a", "label": "Ctrl+A"}


class _App:
    """저장·복원에 필요한 App 속성만 갖춘 대역."""

    _save_order_settings = main.App._save_order_settings
    _load_order_settings = main.App._load_order_settings

    def __init__(self, settings):
        self._settings = settings
        self._account_auto_cancel_armed = set()
        self._balance_sell_settings = {}
        self._balance_sell_stage = {}
        self._balance_sell_date = {}
        self._exit_hotkey_specs = {}


def demo():
    with tempfile.TemporaryDirectory() as tmp:
        ini = str(Path(tmp) / "layout.ini")

        before = _App(QSettings(ini, QSettings.IniFormat))
        before._account_auto_cancel_armed = {"005930", "000660"}
        before._balance_sell_settings = {"005930": dict(SETTING),
                                         "226340": dict(SETTING)}
        before._balance_sell_stage = {"005930": 2, "226340": 0}  # 1·2단계 이미 나감
        before._balance_sell_date = {"005930": TODAY, "226340": YESTERDAY}
        before._exit_hotkey_specs = {"": {"005930": dict(HOTKEY)}}
        before._save_order_settings()

        after = _App(QSettings(ini, QSettings.IniFormat))
        after._load_order_settings()

        assert after._account_auto_cancel_armed == {"005930", "000660"}, (
            after._account_auto_cancel_armed)
        assert after._exit_hotkey_specs == {"": {"005930": HOTKEY}}, (
            after._exit_hotkey_specs)
        assert after._balance_sell_settings.get("005930") == SETTING, (
            after._balance_sell_settings)
        # 핵심: 단계가 0으로 리셋되면 나갔던 매도가 재시작 직후 다시 나간다.
        assert after._balance_sell_stage.get("005930") == 2, (
            f"단계 유실 — 중복 매도 위험: {after._balance_sell_stage}")
        # 어제 설정은 원래 당일 만료라 되살리지 않는다.
        assert "226340" not in after._balance_sell_settings, (
            after._balance_sell_settings)

        # 저장한 적 없는 첫 실행은 손상이 아니므로 경고 없이 빈 상태여야 한다.
        first_run = _App(QSettings(str(Path(tmp) / "new.ini"), QSettings.IniFormat))
        first_run._load_order_settings()
        assert first_run._balance_sell_settings == {}, first_run._balance_sell_settings
        assert first_run._account_auto_cancel_armed == set(), (
            first_run._account_auto_cancel_armed)
        assert first_run._exit_hotkey_specs == {}, first_run._exit_hotkey_specs

        # 저장값이 깨져도 앱이 죽지 않고 빈 상태로 시작해야 한다.
        broken = QSettings(ini, QSettings.IniFormat)
        broken.setValue("order/balance_sell", "{not json")
        broken.sync()
        recovered = _App(QSettings(ini, QSettings.IniFormat))
        recovered._load_order_settings()
        assert recovered._balance_sell_settings == {}, recovered._balance_sell_settings
        assert recovered._account_auto_cancel_armed == {"005930", "000660"}
    print("ok")


if __name__ == "__main__":
    demo()
