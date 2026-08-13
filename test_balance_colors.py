# -*- coding: utf-8 -*-
"""3단매도 칸 배경색 가독성 검사.

소진 표시가 옅은 회색(#BDBDBD)이라 평상시 옅은 초록(#CDECCF)과 명도가
붙어 자세히 봐야 갈렸다. 어두운 남회색으로 바꿨으니 다시 붙지 않게 막는다.
"""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

import gui

SETTING = {"first": 100000, "second": 50000, "third": 0}


def bg(model, code):
    index = model.index(model.codes.index(code), gui.BALANCE_SELL_COL)
    return model.data(index, Qt.BackgroundRole)


def fg(model, code):
    index = model.index(model.codes.index(code), gui.BALANCE_SELL_COL)
    return model.data(index, Qt.ForegroundRole)


def demo():
    QApplication.instance() or QApplication([])
    model = gui.StockModel()
    model.add_stock("LIVE", {"name": "감시중"})
    model.add_stock("DONE", {"name": "소진"})
    model.balance_sell_settings = {"LIVE": dict(SETTING), "DONE": dict(SETTING)}
    # third=0이라 켜 둔 단계는 2개. 2단계를 지나면 소진이다.
    model.balance_sell_stage = {"LIVE": 1, "DONE": 2}

    live, done = bg(model, "LIVE"), bg(model, "DONE")
    assert live.name() == "#cdeccf", live.name()
    assert done.name() == "#37474f", done.name()
    # 명도 차이가 충분해야 곁눈질로 갈린다. 옛 #BDBDBD는 차이가 25뿐이었다.
    assert live.lightness() - done.lightness() > 100, (
        f"{live.lightness()} vs {done.lightness()}")
    assert fg(model, "DONE") == gui.WHITE, "어두운 배경엔 흰 글씨"
    assert fg(model, "LIVE").name() == "#111111"

    # 점멸 중에는 배경이 경보색이라 글씨색도 거기에 맞춘다.
    model.balance_blink_on = True
    model.balance_alert_stage = {"DONE": 2}
    assert bg(model, "DONE").name() == "#ff9800", bg(model, "DONE").name()
    assert fg(model, "DONE").name() == "#111111", "주황 위엔 검은 글씨"
    model.balance_alert_stage = {"DONE": 3}
    assert bg(model, "DONE").name() == "#d50000"
    assert fg(model, "DONE") == gui.WHITE, "빨강 위엔 흰 글씨"
    # 점멸이 꺼진 반주기에는 소진색으로 돌아간다 -> 깜박임이 더 잘 보인다.
    model.balance_blink_on = False
    assert bg(model, "DONE").name() == "#37474f"

    print("ok")


if __name__ == "__main__":
    demo()
