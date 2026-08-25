# -*- coding: utf-8 -*-
"""상한가 표의 ★ 열만 다시 칠하는 경로를 검사.

관심종목을 하나 넣고 뺄 때마다 상한가 표를 통째로 다시 세우면 최대 600행 x
14열을 새로 만든다. 달라지는 것은 ★ 열 하나뿐이라 그 열만 고친다.
"""
import os
import types

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtGui import QColor  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QApplication, QTableWidget, QTableWidgetItem,
)

from ui.limit_up_tab import LimitUpTabMixin  # noqa: E402
import ui.limit_up_tab as limit_up_tab  # noqa: E402

WATCHED = "005930"
UNWATCHED = "000660"
COL = LimitUpTabMixin.LIMIT_WATCH_COL


def demo():
    app = QApplication.instance() or QApplication([])
    table = QTableWidget(2, COL + 1)
    for row, code in enumerate((WATCHED, UNWATCHED)):
        item = QTableWidgetItem("☆")
        item.setData(Qt.ItemDataRole.UserRole + 2, code)
        table.setItem(row, COL, item)
    # ★ 열이 아닌 칸은 손대지 않는지 함께 본다.
    other = QTableWidgetItem("그대로")
    table.setItem(0, 0, other)

    screen = types.SimpleNamespace(
        _limit_table=table, LIMIT_WATCH_COL=COL)
    screen._refresh_limit_watch_marks = types.MethodType(
        LimitUpTabMixin._refresh_limit_watch_marks, screen)

    saved = limit_up_tab.realtime_watch_codes
    limit_up_tab.realtime_watch_codes = lambda: {WATCHED}
    try:
        screen._refresh_limit_watch_marks()
    finally:
        limit_up_tab.realtime_watch_codes = saved

    assert table.item(0, COL).text() == "★", table.item(0, COL).text()
    assert table.item(1, COL).text() == "☆", table.item(1, COL).text()
    assert table.item(0, COL).foreground().color() == QColor("#f4b400")
    assert table.item(1, COL).foreground().color() == QColor("#808080")
    assert table.item(0, 0).text() == "그대로"
    # 표가 없어도 터지지 않는다(탭을 아직 안 만든 상태).
    empty = types.SimpleNamespace(LIMIT_WATCH_COL=COL)
    types.MethodType(
        LimitUpTabMixin._refresh_limit_watch_marks, empty)()
    del app
    print("ok")


if __name__ == "__main__":
    demo()
