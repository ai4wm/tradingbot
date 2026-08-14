# -*- coding: utf-8 -*-
"""이미 설정된 종목에서 '현재 잔량으로 재설정'이 바로 적용되는지 확인한다.

설정이 없을 때는 예전처럼 제안만 하고 Enter를 기다린다.
"""
import os
import tempfile
import types

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSettings  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

import gui  # noqa: E402

CODE = "035290"
SETTING = {"first": 60_000, "second": 30_000, "third": 10_000,
           "first_ratio": 0.0, "second_ratio": 0.5, "third_ratio": 1.0,
           "market_sell": True}


class Stub:
    """설정창이 읽고 쓰는 부분만 세운 대역. 운영 layout.ini는 건드리지 않는다."""

    def __init__(self, config, bid_qty):
        self.model = types.SimpleNamespace(
            rows={CODE: {"name": "시험", "bid_qty": bid_qty,
                         "bid_price": 1_000, "upper": 1_000}},
            balance_sell_settings={CODE: config} if config else {})
        handle, path = tempfile.mkstemp(suffix=".ini")
        os.close(handle)
        self._ini_path = path
        self._settings = QSettings(path, QSettings.IniFormat)
        self.saved = []

    def set_balance_sell_setting(self, code, config):
        self.saved.append((code, config))


def demo():
    app = QApplication.instance() or QApplication([])

    # 잔량 100만주 -> 표에서 50만/30만/20만
    screen = Stub(dict(SETTING), 1_000_000)
    dialog = gui.BalanceSellDialog(screen, CODE)
    dialog._rebase_now()
    assert len(screen.saved) == 1, screen.saved
    code, config = screen.saved[0]
    assert code == CODE
    assert (config["first"], config["second"], config["third"]) == (
        500_000, 300_000, 200_000), config
    # 비율·시장가 등 나머지 설정은 그대로 따라간다.
    assert config["second_ratio"] == 0.5, config
    assert config["market_sell"] is True, config
    assert not dialog.isVisible()

    # 설정이 없으면 제안만 하고 저장하지 않는다.
    fresh = Stub(None, 1_000_000)
    fresh_dialog = gui.BalanceSellDialog(fresh, CODE)
    fresh_dialog._rebase_now()
    assert fresh.saved == [], fresh.saved
    assert fresh_dialog.first_edit.value() == 500_000

    for stub in (screen, fresh):
        os.unlink(stub._ini_path)
    del app
    print("ok")


if __name__ == "__main__":
    demo()
