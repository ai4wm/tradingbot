# -*- coding: utf-8 -*-
"""3단매도 설정창이 비모달인지 본다.

모달(`exec()`)이면 떠 있는 동안 다른 창의 클릭이 전부 막혔다. 같은 칸을 다시 눌러도 창이
하나여야 하고, 떠 있는 사이 설정이 다른 곳에서 바뀌면 '현재 적용값'이 따라가야 한다.
"""
import os
import tempfile
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSettings  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

import gui  # noqa: E402

CODE = "000001"


def demo():
    app = QApplication.instance() or QApplication([])
    settings = QSettings(os.path.join(tempfile.mkdtemp(), "t.ini"), QSettings.IniFormat)
    with patch.object(gui, "QSettings", return_value=settings):
        screen = gui.ConditionScreen()
    screen.model.add_stock(CODE, {
        "name": "시험", "price": 1_000, "base": 1_000, "upper": 1_300,
        "lower": 700, "bid_qty": 700_000})
    screen.model.rows[CODE]["bid_qty"] = 700_000
    screen.show()
    row = screen.model.codes.index(CODE)
    index = screen.proxy.mapFromSource(screen.model.index(row, gui.BALANCE_SELL_COL))

    screen._on_cell_clicked(index)                      # 돌아와야 한다(exec로 막히면 안 돌아옴)
    dialog = screen._balance_dialogs.get(CODE)
    assert dialog is not None and dialog.isVisible() and not dialog.isModal()
    screen._on_cell_clicked(index)                      # 다시 눌러도 하나
    assert screen._balance_dialogs[CODE] is dialog

    # 떠 있는 사이 다른 곳이 설정을 걸면 현재 적용값·해제 버튼이 따라간다.
    assert not dialog.off_btn.isEnabled() and "없음" in dialog.applied_label.text()
    screen.set_balance_sell_setting(CODE, {
        "first": 300_000, "second": 100_000, "third": 50_000,
        "first_ratio": 0.0, "second_ratio": 0.5, "third_ratio": 1.0, "market_sell": True})
    dialog._refresh_live()
    assert dialog.off_btn.isEnabled(), "해제 버튼이 살아나야 한다"
    assert "하한가" in dialog.applied_label.text() and "50% 매도" in dialog.applied_label.text(), (
        dialog.applied_label.text())

    dialog.reject()                                     # 닫으면 목록에서 빠진다
    app.processEvents()
    assert CODE not in screen._balance_dialogs
    print("ok (3단매도 설정창 비모달: 열린 채 복귀, 중복 없음, 외부 변경 반영)")


if __name__ == "__main__":
    demo()
