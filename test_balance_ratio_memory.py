# -*- coding: utf-8 -*-
"""3단매도 단계별 비율의 마지막 선택 기억 검사.

단계 체크·시장가와 같은 방식이다. 적용값이 있는 종목은 그 값을 보여 줄 뿐이므로
마지막 선택을 덮어쓰면 안 된다. 덮어쓰면 다음에 여는 새 종목이 남의 설정으로
시작한다.
"""
import os
import tempfile
import types

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSettings  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

import gui  # noqa: E402

CODE = "023790"
TRIM = -1.0


class Stub:
    def __init__(self, path, config=None):
        self.model = types.SimpleNamespace(
            rows={CODE: {"name": "시험", "bid_qty": 1_000_000,
                         "bid_price": 1_000, "upper": 1_000}},
            balance_sell_settings={CODE: config} if config else {})
        self._settings = QSettings(path, QSettings.IniFormat)
        self.saved = []

    def set_balance_sell_setting(self, code, config):
        self.saved.append((code, config))


def demo():
    QApplication.instance() or QApplication([])
    handle, path = tempfile.mkstemp(suffix=".ini")
    os.close(handle)
    try:
        # 기본값: 1번 소리만, 2·3번 전량매도.
        dialog = gui.BalanceSellDialog(Stub(path), CODE)
        assert [c.currentData() for c in dialog._ratio_combos()] == [
            0.0, 1.0, 1.0], [c.currentData() for c in dialog._ratio_combos()]

        # 2번을 나머지취소로 고른다 -> 즉시 저장.
        combo = dialog.second_sell_combo
        combo.setCurrentIndex(combo.findData(TRIM))
        assert combo.currentText().endswith(gui.TRIM_BUTTON_TEXT), \
            combo.currentText()

        # 다음에 여는 다른 창이 그 선택으로 시작한다.
        dialog = gui.BalanceSellDialog(Stub(path), CODE)
        assert [c.currentData() for c in dialog._ratio_combos()] == [
            0.0, TRIM, 1.0], [c.currentData() for c in dialog._ratio_combos()]

        # 이미 걸린 종목을 열면 그 종목 값을 보여 주되 기억은 안 바뀐다.
        applied = {"at_upper": True, "first": 900_000, "second": 600_000,
                   "third": 300_000, "first_ratio": 0.0, "second_ratio": .50,
                   "third_ratio": 1.0, "market_sell": False}
        dialog = gui.BalanceSellDialog(Stub(path, applied), CODE)
        assert dialog.second_sell_combo.currentData() == .50, \
            dialog.second_sell_combo.currentData()
        dialog = gui.BalanceSellDialog(Stub(path), CODE)
        assert dialog.second_sell_combo.currentData() == TRIM, \
            dialog.second_sell_combo.currentData()
    finally:
        os.unlink(path)
    print("ok")


if __name__ == "__main__":
    demo()
