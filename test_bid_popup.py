# -*- coding: utf-8 -*-
"""매수잔량 확대 창 검사.

창은 0D 호가와 0B 체결 푸시를 같은 스레드에서 받는다. 그 스레드에서
3단매도 판정도 돌기 때문에 틱마다 다시 그리면 발동이 밀린다. 값만 받아
두고 100ms에 한 번 그리는지, 세 줄이 창 높이 안에 들어가는지 본다.
"""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSettings
from PySide6.QtWidgets import QApplication, QWidget

import gui

INI = os.path.join(os.environ.get("TEMP", "."), "trading_bot_bid_popup_test.ini")


class FakeScreen(QWidget):
    prefix = ""

    def __init__(self):
        super().__init__()
        self._settings = QSettings(INI, QSettings.IniFormat)
        self._bid_popups = {}


def demo():
    app = QApplication.instance() or QApplication([])
    screen = FakeScreen()
    popup = gui.BidQtyPopup(screen, "223310", "사토시홀딩스")
    popup.resize(240, 96)

    # 1) 두 값이 다 나오고, 거래량은 매수잔량의 40% 크기다.
    popup.set_value(842_253, 1_234_567)
    popup._refresh_text()
    text = popup._label.text()
    assert "842,253" in text and "1,234,567" in text, text
    big = max(12, int(popup.height() * 0.42))
    assert f"font-size:{big}px" in text, text
    assert f"font-size:{max(8, int(big * 0.40))}px" in text, text

    # 2) 같은 값이 또 오면 다시 그리지 않는다. 호가는 매도쪽만 바뀌어도 온다.
    popup._paint_timer.stop()
    popup.set_value(842_253, 1_234_567)
    assert not popup._paint_timer.isActive()

    # 3) 거래량만 바뀌어도 예약은 되지만 그 자리에서 그리지는 않는다.
    popup.set_value(842_253, 1_300_000)
    assert popup._paint_timer.isActive()
    assert "1,234,567" in popup._label.text()      # 아직 옛 그림
    popup._refresh_text()
    assert "1,300,000" in popup._label.text()

    # 4) 세 줄이 기본 크기 안에 들어간다. 넘치면 가운데 정렬이 잘라 먹는다.
    for height in (96, 56):
        popup.resize(240, height)
        popup._refresh()
        assert popup._label.sizeHint().height() <= height, (
            height, popup._label.sizeHint().height())

    popup.close()
    QSettings(INI, QSettings.IniFormat).clear()
    if os.path.exists(INI):
        os.remove(INI)
    print("ok")


if __name__ == "__main__":
    demo()
