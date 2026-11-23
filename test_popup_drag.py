# -*- coding: utf-8 -*-
"""확대 창 이동·크기 조절을 윈도우에 넘기는지, 못 넘기면 예전 방식으로 되돌아가는지 본다.

메인 스레드가 시세 틱으로 바쁘면 마우스 이동 이벤트마다 `move()`를 부르는 방식은
끊기듯 따라온다. 몇 픽셀 끌린 순간 `startSystemMove`로 넘기고, 넘긴 뒤에는 놓음
이벤트가 안 올 수 있어 위치 저장은 타이머가 한다. 클릭·더블클릭(닫기)은 그대로다.
"""
import os
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QEvent, QPoint, QPointF, QSettings, Qt  # noqa: E402
from PySide6.QtGui import QMouseEvent  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

import gui  # noqa: E402


class Screen:
    prefix = "t_"
    _bid_popups: dict = {}

    def __init__(self, path):
        self._settings = QSettings(path, QSettings.IniFormat)


def event(kind, local, global_, buttons=Qt.MouseButton.LeftButton):
    button = (Qt.MouseButton.LeftButton if kind != QEvent.MouseMove
              else Qt.MouseButton.NoButton)
    return QMouseEvent(kind, QPointF(local), QPointF(global_), button, buttons,
                       Qt.KeyboardModifier.NoModifier)


class FakeHandle:
    """OS 이동을 받아 주는 창 핸들."""

    def __init__(self, accept=True):
        self.accept, self.moves, self.resizes = accept, 0, []

    def startSystemMove(self):
        self.moves += 1
        return self.accept

    def startSystemResize(self, edges):
        self.resizes.append(edges)
        return self.accept


def make(folder, handle):
    popup = gui.BidQtyPopup(Screen(os.path.join(folder, "t.ini")), "005930", "삼성전자")
    popup.windowHandle = lambda: handle
    popup.resize(240, 96)
    popup.move(100, 100)
    popup.show()
    return popup


def drag(popup, start, end):
    popup.mousePressEvent(event(QEvent.MouseButtonPress, start, start))
    popup.mouseMoveEvent(event(QEvent.MouseMove, end, end))


def demo():
    QApplication.instance() or QApplication([])
    with tempfile.TemporaryDirectory() as folder:
        # 1) OS가 받아 주면: 끌린 순간 한 번만 넘기고 앱은 move()를 부르지 않는다.
        handle = FakeHandle(True)
        popup = make(folder, handle)
        before = popup.pos()
        drag(popup, QPoint(20, 20), QPoint(60, 50))
        assert handle.moves == 1 and popup.pos() == before, (handle.moves, popup.pos())
        popup.mouseMoveEvent(event(QEvent.MouseMove, QPoint(90, 70), QPoint(90, 70)))
        assert handle.moves == 1                       # 같은 끌기에서 다시 넘기지 않는다
        assert popup._drag_offset is None              # 놓음 이벤트가 안 와도 상태가 남지 않는다

        # 2) 조금 움직인 것(4픽셀 미만)은 클릭이다. 넘기지도, 옮기지도 않는다.
        handle2 = FakeHandle(True)
        popup2 = make(folder, handle2)
        before = popup2.pos()
        drag(popup2, QPoint(20, 20), QPoint(22, 21))
        assert handle2.moves == 0 and popup2.pos() == before

        # 3) 모서리를 잡고 끌면 크기 조절을 넘긴다.
        handle3 = FakeHandle(True)
        popup3 = make(folder, handle3)
        grip = QPoint(popup3.width() - 4, popup3.height() - 4)
        drag(popup3, grip, grip + QPoint(30, 30))
        assert handle3.resizes and handle3.moves == 0, (handle3.resizes, handle3.moves)

        # 4) OS가 안 받아 주면 예전 방식 그대로 움직이고, 다시는 시도하지 않는다.
        handle4 = FakeHandle(False)
        popup4 = make(folder, handle4)
        start = popup4.pos()
        drag(popup4, QPoint(20, 20), QPoint(60, 50))
        assert popup4.pos() == start + QPoint(40, 30), popup4.pos()
        popup4.mouseMoveEvent(event(QEvent.MouseMove, QPoint(70, 60), QPoint(70, 60)))
        assert handle4.moves == 1                      # 실패한 뒤 재시도하지 않는다
        assert popup4.pos() == start + QPoint(50, 40)

        # 5) 이동·크기 변화는 타이머가 저장한다(놓음 이벤트에 기대지 않는다).
        popup.move(300, 300)
        assert popup._geo_timer.isActive()
        popup._geo_timer.stop()
        popup._save_geo()
        assert popup._screen._settings.value("t_bidqty_popup_005930") is not None
        for item in (popup, popup2, popup3, popup4):
            item.close()
    print("ok (확대 창 이동·크기 조절은 OS에 넘김, 클릭 보존, 실패하면 예전 방식)")


if __name__ == "__main__":
    demo()
