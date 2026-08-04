# -*- coding: utf-8 -*-
"""테마 열이 긴 이름을 `…` 없이 그대로 잘라 그리는지 확인한다.

말줄임은 스타일(CE_ItemViewItem)이 option.text를 받아 처리한다. 그래서
스타일에 빈 글자를 넘기고 원문을 직접 그리는지 두 지점에서 확인한다.
"""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QRect  # noqa: E402
from PySide6.QtGui import (  # noqa: E402
    QPainter, QPixmap, QStandardItem, QStandardItemModel,
)
from PySide6.QtWidgets import (  # noqa: E402
    QApplication, QProxyStyle, QStyle, QStyleOptionViewItem, QTableView,
)

import gui  # noqa: E402

LONG_THEME = "2차전지(전고체배터리)·리튬이온소재·장비"
CELL = QRect(0, 0, 60, 20)  # 글자보다 훨씬 좁은 칸


class RecordingStyle(QProxyStyle):
    """스타일이 넘겨받은 글자를 기록한다. drawControl은 가상 함수라 가로챌 수 있다."""

    def __init__(self):
        super().__init__()
        self.item_texts = []

    def drawControl(self, element, option, painter, widget=None):
        if element == QStyle.CE_ItemViewItem:
            self.item_texts.append(option.text)
        super().drawControl(element, option, painter, widget)


def demo():
    app = QApplication.instance() or QApplication([])
    drawn = []

    class Recorder(QPainter):
        """QPainter는 가상 함수가 아니라 파이썬에서 호출한 것만 잡힌다."""

        def drawText(self, *args):
            if args and isinstance(args[-1], str):
                drawn.append(args[-1])
            return super().drawText(*args)

    style = RecordingStyle()
    model = QStandardItemModel(1, 1)
    model.setItem(0, 0, QStandardItem(LONG_THEME))
    view = QTableView()
    view.setStyle(style)
    view.setModel(model)
    view.setColumnWidth(0, CELL.width())

    pixmap = QPixmap(CELL.size())
    pixmap.fill()
    painter = Recorder(pixmap)
    option = QStyleOptionViewItem()
    option.rect = CELL
    option.widget = view
    option.font = view.font()
    option.palette = view.palette()
    gui.ClipTextDelegate(view).paint(painter, option, model.index(0, 0))
    painter.end()

    assert style.item_texts == [""], (
        f"스타일에 글자를 넘겨 `…`로 줄어들 수 있음: {style.item_texts}")
    assert LONG_THEME in drawn, f"전체 테마명을 직접 그리지 않음: {drawn}"
    assert not any("…" in text for text in drawn), f"말줄임표가 남음: {drawn}"
    del app
    print("ok")


if __name__ == "__main__":
    demo()
