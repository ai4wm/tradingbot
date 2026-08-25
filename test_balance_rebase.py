# -*- coding: utf-8 -*-
"""'현재 잔량으로 다시 계산'이 숫자만 바꾸고 저장하지 않는지 확인한다.

설정이 있든 없든 적용은 Enter다. 버튼 한 번에 실계좌 감시 기준이 바뀌면
되돌릴 방법이 없다.
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

    # 잔량 100만주 -> 표에서 50만/30만/20만. 입력칸만 바뀌고 저장은 없다.
    screen = Stub(dict(SETTING), 1_000_000)
    dialog = gui.BalanceSellDialog(screen, CODE)
    dialog._rebase_now()
    assert screen.saved == [], screen.saved
    assert (dialog.first_edit.value(), dialog.second_edit.value(),
            dialog.third_edit.value()) == (500_000, 300_000, 200_000), (
        dialog.first_edit.value())
    # 적용 전이라 기존 설정은 그대로다.
    assert screen.model.balance_sell_settings[CODE] == SETTING
    assert dialog.error_label.text()

    # 설정이 없을 때도 같다.
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
