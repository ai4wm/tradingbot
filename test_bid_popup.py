# -*- coding: utf-8 -*-
"""매수잔량 확대 창 검사.

창은 0D 호가와 0B 체결 푸시를 같은 스레드에서 받는다. 그 스레드에서
3단매도 판정도 돌기 때문에 틱마다 다시 그리면 발동이 밀린다. 값만 받아
두고 100ms에 한 번 그리는지, 세 줄이 창 높이 안에 들어가는지 본다.
"""
import os
import re
import tempfile
from unittest.mock import patch

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
    assert popup.parent() is None  # 화면에 묶이면 창 최소화 때 같이 숨는다
    popup.resize(240, 96)

    # 1) 매수잔량 아래 고정된 두 칸에 누적·예상체결량을 구분해 표시한다.
    popup.set_value(842_253, 1_234_567, 17_291)
    popup._refresh_text()
    text = popup._label.text()
    assert "842,253" in text and ">1,234,567</td>" in text, text
    assert ">17,291</td>" in text and text.count("width='50%'") == 2, text
    assert "누적" not in text and "예상" not in text, text
    big = max(12, int(popup.height() * 0.42))
    sizes = [int(n) for n in re.findall(r"font-size:(\d+)px", text)]
    assert sizes[0] < sizes[1] <= big and sizes[2] < sizes[1], text
    assert "#6FD3C7" in text and "#FF8FC7" in text, text

    # 2) 같은 값이 또 오면 다시 그리지 않는다. 호가는 매도쪽만 바뀌어도 온다.
    popup._paint_timer.stop()
    popup.set_value(842_253, 1_234_567, 17_291)
    assert not popup._paint_timer.isActive()

    # 3) 거래량만 바뀌어도 예약은 되지만 그 자리에서 그리지는 않는다.
    popup.set_value(842_253, 1_300_000, 17_291)
    assert popup._paint_timer.isActive()
    assert "1,234,567" in popup._label.text()      # 아직 옛 그림
    popup._refresh_text()
    assert "1,300,000" in popup._label.text()

    # 예상체결량만 변해도 갱신하고, 0 또는 미제공이면 거래량만 가운데 둔다.
    popup._paint_timer.stop()
    popup.set_value(842_253, 1_300_000, 205)
    assert popup._paint_timer.isActive()
    assert ">17,291</td>" in popup._label.text()
    popup._refresh_text()
    assert ">205</td>" in popup._label.text()
    popup.set_value(842_253, 1_300_000, 0)
    popup._refresh_text()
    assert ">1,300,000</div>" in popup._label.text()
    assert "<table" not in popup._label.text()
    popup.set_value(842_253, 1_300_000)
    popup._refresh_text()
    assert ">1,300,000</div>" in popup._label.text()
    assert "<table" not in popup._label.text()

    # 4) 세 줄이 기본 크기 안에 들어간다. 넘치면 가운데 정렬이 잘라 먹는다.
    for width, height in ((240, 96), (160, 56), (360, 144)):
        popup.resize(width, height)
        popup._refresh()
        assert popup._label.sizeHint().height() <= height, (
            height, popup._label.sizeHint().height())
        assert popup._label.sizeHint().width() <= width, (
            width, popup._label.sizeHint().width())

    popup.close()
    QSettings(INI, QSettings.IniFormat).clear()
    if os.path.exists(INI):
        os.remove(INI)
    print("ok")


def demo_expected_only_ticks():
    """단일가 종목은 장중에도 예상체결량 단독 갱신을 확대 창에 반영한다."""
    app = QApplication.instance() or QApplication([])
    with tempfile.TemporaryDirectory(prefix="bid-popup-") as tmp:
        settings = QSettings(os.path.join(tmp, "layout.ini"), QSettings.IniFormat)
        with patch.object(gui, "QSettings", return_value=settings):
            screen = gui.ConditionScreen()
        code = "004545"
        screen.model.single.add(code)
        screen.model.add_stock(code, {
            "name": "깨끗한나라우", "bid_qty": 18_975, "vol": 39_936,
            "price": 5_230, "base": 4_030, "upper": 5_230})
        screen.on_tick(code, {"exp_price": 5_230, "exp_qty": 14_152})
        screen._open_bid_popup(code)
        popup = screen._bid_popups[code]
        popup._refresh_text()
        assert ">14,152</td>" in popup._label.text()
        screen.on_tick(code, {"exp_qty": 17_291})
        popup._refresh_text()
        assert ">17,291</td>" in popup._label.text()
        screen.on_tick(code, {"vol": 39_950, "trade_time": 90004})
        popup._refresh_text()
        assert ">39,950</td>" in popup._label.text()
        assert ">17,291</td>" in popup._label.text()  # 단일가는 체결 뒤에도 유지
        screen.on_tick(code, {"exp_price": 5_230, "exp_qty": 0})
        popup._refresh_text()
        assert ">39,950</div>" in popup._label.text()
        assert "<table" not in popup._label.text()
        screen.model.single.discard(code)
        screen.on_tick(code, {"exp_price": 0, "exp_qty": 0})
        popup._refresh_text()
        assert ">39,950</div>" in popup._label.text()
        assert "<table" not in popup._label.text()
        popup.close()
        screen.deleteLater()
        app.processEvents()
    print("ok (단일가 장중 예상체결량 단독 갱신·해제)")


def demo_restore_after_restart():
    """떠 있던 확대 창은 재시작 뒤 그 종목 행이 생기면 같은 자리·크기로 다시 뜬다."""
    app = QApplication.instance() or QApplication([])
    row = {"name": "깨끗한나라우", "bid_qty": 1, "price": 1, "base": 1, "upper": 1}
    with tempfile.TemporaryDirectory(prefix="bid-popup-") as tmp:
        settings = QSettings(os.path.join(tmp, "layout.ini"), QSettings.IniFormat)
        with patch.object(gui, "QSettings", return_value=settings):
            screen = gui.ConditionScreen()
        for code in ("004545", "001527", "003495"):
            screen.model.add_stock(code, dict(row))
        app.processEvents()
        for code in ("004545", "001527", "003495"):
            screen._open_bid_popup(code)
        screen._bid_popups["001527"].setGeometry(300, 200, 280, 120)
        screen._bid_popups["001527"]._save_geo()
        screen._bid_popups["003495"].mouseDoubleClickEvent(None)  # 손으로 닫음
        for popup in list(screen._bid_popups.values()):
            popup.close()                                     # 앱 종료
        screen.deleteLater()
        app.processEvents()

        with patch.object(gui, "QSettings", return_value=settings):
            screen = gui.ConditionScreen()
        screen.model.add_stock("001527", dict(row))           # 편입
        app.processEvents()
        assert set(screen._bid_popups) == {"001527"}, screen._bid_popups
        geo = screen._bid_popups["001527"].geometry()
        assert (geo.x(), geo.y(), geo.width(), geo.height()) == (300, 200, 280, 120), geo
        screen.model.add_stock("003495", dict(row))           # 닫은 종목은 안 뜸
        screen.model.add_stock("004545", dict(row))           # 늦게 편입돼도 뜸
        app.processEvents()
        assert set(screen._bid_popups) == {"001527", "004545"}, screen._bid_popups
        for popup in list(screen._bid_popups.values()):
            popup.close()
        screen.deleteLater()
        app.processEvents()
    print("ok (재시작 뒤 확대 창 복원)")


if __name__ == "__main__":
    demo()
    demo_expected_only_ticks()
    demo_restore_after_restart()
