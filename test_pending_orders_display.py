# -*- coding: utf-8 -*-
"""선택 종목의 미체결 요약이 주문줄에 제대로 나오는지 확인한다."""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

import main  # noqa: E402


class _Screen:
    def __init__(self):
        self.shown = []

    def set_pending_orders(self, code, buy, sell, position, trim=0):
        self.shown.append((code, buy, sell, position, trim))


class _View:
    def __init__(self, screen):
        self.screen = screen


class _App:
    _push_pending_orders = main.App._push_pending_orders

    def __init__(self, screen):
        self.views = [_View(screen)]
        # 주문번호 -> (잔량, 거래소). 잔량 0은 이미 체결·취소된 주문이다.
        self._open_buy_orders = {"005930": {"A1": (300, "KRX"),
                                            "A2": (200, "KRX"),
                                            "A3": (0, "KRX")}}
        self._open_sell_orders = {"005930": {"B1": (100, "KRX")}}
        self._position_book = {"005930": {"held": 800, "sellable": 700}}


def demo():
    app = QApplication.instance() or QApplication([])
    screen = _Screen()
    _App(screen)._push_pending_orders("005930")
    # 나머지취소 수량은 100주 넘는 건에서만 나온다: (300-100)+(200-100)=300
    assert screen.shown[-1] == ("005930", (2, 500), (1, 100), (800, 700), 300), (
        screen.shown[-1])

    # 종목코드 접미사(_AL/_NX)와 A접두사가 붙어 와도 같은 장부를 봐야 한다.
    screen.shown.clear()
    _App(screen)._push_pending_orders("A005930_AL")
    assert screen.shown[-1][0] == "005930", screen.shown[-1]

    # 미체결이 없는 종목은 0건으로 알려야 한다(표시가 옛 값에 남지 않게).
    screen.shown.clear()
    _App(screen)._push_pending_orders("000660")
    assert screen.shown[-1] == ("000660", (0, 0), (0, 0), (0, 0), 0), screen.shown[-1]

    # 화면 쪽 문구 — 대상이 아닌 종목의 갱신은 무시해야 한다.
    from gui import ConditionScreen
    real = ConditionScreen()
    real._order_target_code = "005930"
    real.set_pending_orders("005930", (2, 500), (1, 100), (800, 700))
    text = real.pending_order_value.text()
    assert "체결 <b>800주</b> (묶임 100)" in text, text   # 미체결 매도 100주가 묶임
    assert "미체결매수 2건 500주" in text and "미체결매도 1건 100주" in text, text

    real.set_pending_orders("000660", (9, 9999), (0, 0), (0, 0))
    assert "800주" in real.pending_order_value.text(), (
        f"다른 종목 정보가 덮어씀: {real.pending_order_value.text()}")

    real.set_pending_orders("005930", (0, 0), (0, 0), (0, 0))
    assert real.pending_order_value.text() == "체결·미체결 없음", (
        real.pending_order_value.text())

    # 전량 매도가능하면 '묶임'을 붙이지 않는다.
    real.set_pending_orders("005930", (0, 0), (0, 0), (500, 500))
    assert real.pending_order_value.text() == "체결 <b>500주</b>", (
        real.pending_order_value.text())

    # 3단매도 셀 — 켜 둔 단계를 다 지나면 더 나갈 매도가 없다. 소리는 그때만
    # 들리므로 자리를 비운 뒤에도 알 수 있게 배경색으로 남아야 한다.
    # 옅은 회색은 평상시 옅은 초록과 명도가 붙어 안 갈렸다(test_balance_colors).
    from PySide6.QtCore import Qt
    from PySide6.QtGui import QColor
    from gui import BALANCE_SELL_COL
    real.model.add_stock("005930", {})
    real.model.balance_sell_settings["005930"] = {
        "first": 3_000_000, "second": 700_000, "third": 0}  # 켠 단계 2개
    cell = real.model.index(real.model.codes.index("005930"), BALANCE_SELL_COL)
    for stage, expected in ((0, "#CDECCF"), (1, "#CDECCF"), (2, "#37474F")):
        real.model.balance_sell_stage["005930"] = stage
        assert real.model.data(cell, Qt.BackgroundRole) == QColor(expected), (
            stage, real.model.data(cell, Qt.BackgroundRole))
    # 점멸 중에는 경고색이 우선한다(단계 소진 여부와 무관).
    real.model.balance_alert_stage["005930"] = 2
    real.model.balance_blink_on = True
    assert real.model.data(cell, Qt.BackgroundRole) == QColor("#FF9800"), (
        real.model.data(cell, Qt.BackgroundRole))
    del app
    print("ok")


if __name__ == "__main__":
    demo()
