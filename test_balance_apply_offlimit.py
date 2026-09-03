# -*- coding: utf-8 -*-
"""상한가가 아니어도 3단매도를 직접 걸 수 있는지 검사.

걸어 둔 뒤 상한가가 무너져도 감시는 계속 돈다. 거는 순간에만 막는 것은
앞뒤가 맞지 않아 관문을 풀었다. 대신 상한가에서 걸었는지를 설정에 남겨
감사 로그로 추적한다. 자동 설정(주문과 함께)은 상한가 확인을 유지한다.
"""
import os
import tempfile
import types

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSettings  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

import gui  # noqa: E402

CODE = "023790"


class Stub:
    """설정창이 읽고 쓰는 부분만 세운 대역. 운영 layout.ini는 안 건드린다."""

    def __init__(self, bid_price, upper):
        self.model = types.SimpleNamespace(
            rows={CODE: {"name": "시험", "bid_qty": 1_000_000,
                         "bid_price": bid_price, "upper": upper}},
            balance_sell_settings={})
        handle, path = tempfile.mkstemp(suffix=".ini")
        os.close(handle)
        self._ini_path = path
        self._settings = QSettings(path, QSettings.IniFormat)
        self.saved = []

    def set_balance_sell_setting(self, code, config):
        self.saved.append((code, config))


def apply_with(bid_price, upper):
    screen = Stub(bid_price, upper)
    dialog = gui.BalanceSellDialog(screen, CODE)
    dialog.first_check.setChecked(True)
    dialog.first_edit.setValue(500_000)
    dialog._apply()
    os.unlink(screen._ini_path)
    return screen.saved, dialog.error_label.text()


def demo():
    QApplication.instance() or QApplication([])

    # 상한가에서 걸면 그대로 저장되고 at_upper가 남는다.
    saved, error = apply_with(bid_price=1_000, upper=1_000)
    assert len(saved) == 1, (saved, error)
    assert saved[0][1]["at_upper"] is True, saved

    # 상한가가 아니어도 걸린다. 다만 at_upper가 False로 남는다.
    saved, error = apply_with(bid_price=870, upper=1_000)
    assert len(saved) == 1, (saved, error)
    assert saved[0][1]["at_upper"] is False, saved

    # 상한가를 아직 모르는 행도 막지 않는다.
    saved, _ = apply_with(bid_price=0, upper=0)
    assert len(saved) == 1 and saved[0][1]["at_upper"] is False, saved

    # 잔량보다 큰 기준은 여전히 막는다. 관문을 통째로 연 것이 아니다.
    screen = Stub(870, 1_000)
    dialog = gui.BalanceSellDialog(screen, CODE)
    dialog.first_check.setChecked(True)
    dialog.first_edit.setValue(2_000_000)
    dialog._apply()
    assert screen.saved == [], screen.saved
    assert "매수잔량" in dialog.error_label.text()
    os.unlink(screen._ini_path)
    print("ok")


if __name__ == "__main__":
    demo()
