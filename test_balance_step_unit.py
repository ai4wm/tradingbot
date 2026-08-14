# -*- coding: utf-8 -*-
"""3단 잔량 입력칸의 증감 단위를 확인한다(화살표 1만·휠 10만)."""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QPoint, QPointF, Qt  # noqa: E402
from PySide6.QtGui import QWheelEvent  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

import gui  # noqa: E402


def wheel(box, notches, modifiers=Qt.NoModifier):
    box.wheelEvent(QWheelEvent(
        QPointF(0, 0), QPointF(0, 0), QPoint(0, 0), QPoint(0, 120 * notches),
        Qt.NoButton, modifiers, Qt.NoScrollPhase, False))


class FakeModifiers:
    """stepBy는 실제 키보드 상태를 읽는다. 시험에서는 그 값을 갈아 끼운다."""

    def __init__(self, real, modifiers):
        self.real = real
        self.modifiers = modifiers

    def keyboardModifiers(self):
        return self.modifiers

    def __getattr__(self, name):
        return getattr(self.real, name)


def demo():
    app = QApplication.instance() or QApplication([])
    box = gui.BalanceStepSpinBox()
    box.setRange(0, 2_147_483_647)
    box.setFocus()
    real = gui.QApplication

    box.setValue(1_000_000)
    wheel(box, 1)
    assert box.value() == 1_100_000, box.value()
    wheel(box, -1)
    assert box.value() == 1_000_000, box.value()
    box.stepBy(1)
    assert box.value() == 1_010_000, box.value()

    for modifiers, wheel_step, arrow_step in (
            (Qt.ShiftModifier, 10_000, 100_000),
            (Qt.ControlModifier, 1_000_000, 1_000_000)):
        gui.QApplication = FakeModifiers(real, modifiers)
        try:
            box.setValue(10_000_000)
            wheel(box, 1)
            assert box.value() == 10_000_000 + wheel_step, box.value()
            box.setValue(10_000_000)
            box.stepBy(1)
            assert box.value() == 10_000_000 + arrow_step, box.value()
        finally:
            gui.QApplication = real
    del app
    print("ok")


if __name__ == "__main__":
    demo()
