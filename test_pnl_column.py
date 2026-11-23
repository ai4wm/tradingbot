# -*- coding: utf-8 -*-
"""보유 종목의 실시간 순손익 칸과 확대 창이 장부 값·현재가·수수료·세금으로 맞게 나오는지 본다.

순손익 = 평가금액 − 매입금액 − 매수 수수료 − 매도 수수료 − 매도 세금.
수수료 0.015%(10원 미만 버림), 세금 0.20%(1원 미만 버림).
"""
import os
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSettings, Qt  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

import gui  # noqa: E402


def demo():
    QApplication.instance() or QApplication([])
    model = gui.StockModel()
    model.add_stock("439960", {"name": "테스트", "price": 4_000, "upper": 5_200,
                               "base": 4_000})
    cell = model.index(0, gui.PNL_COL)

    def text():
        return model.data(cell, Qt.DisplayRole)

    assert text() == ""                          # 보유가 없으면 비어 있다
    changed = []
    model.dataChanged.connect(
        lambda a, b, *_: changed.extend(range(a.column(), b.column() + 1)))

    # 100주를 평균 4,000원에 보유. 값이 안 변해도 비용이 있어 본전이 아니라 손실이다.
    # 수수료 60 + 60, 세금 800 → −920.
    model.set_position("439960", 100, 400_000)
    assert text() == "-920 -0.23%", text()
    assert gui.PNL_COL in changed

    # 현재가가 오르면 같은 칸이 바로 갱신된다(체결 틱).
    changed.clear()
    model.update_stock("439960", {"price": 4_240})
    assert gui.PNL_COL in changed, changed
    assert text() == "+23,032 +5.76%", text()   # 24,000 − 60 − 60 − 848
    assert model.data(cell, Qt.ForegroundRole).name().lower() == "#d32f2f"
    assert model.data(cell, Qt.UserRole) == 23_032.0   # 정렬은 손익금

    # 내리면 파랑. 툴팁에 분해가 있다.
    model.update_stock("439960", {"price": 3_880})
    assert text() == "-12,886 -3.22%", text()   # −12,000 − 60 − 50 − 776
    assert model.data(cell, Qt.ForegroundRole).name().lower() == "#1976d2"
    tip = model.data(cell, Qt.ToolTipRole)
    assert "평균단가 4,000.0원" in tip and "세금 776원" in tip and "-12,886" in tip, tip

    # 추가 매수로 평균단가가 올라간다: 100주 + 50주@4,300 → 150주, 615,000원.
    model.set_position("439960", 150, 615_000)
    assert text() == "-34,334 -5.58%", text()

    # 영웅문 실시간잔고 역산: 총매입 220,000 · 총평가 219,750 → 총손익 −749 · −0.34%.
    model.set_position("439960", 50, 220_000)
    model.update_stock("439960", {"price": 4_395})
    assert text() == "-749 -0.34%", text()

    # 매입금액을 모르는 항목(청산 경로가 만든 장부)은 빈칸 — 틀린 값을 보이지 않는다.
    model.set_position("439960", 150, 0)
    assert text() == "", text()

    # 다 팔면 칸이 비고, 같은 값을 또 줘도 갱신 신호를 내지 않는다.
    model.set_position("439960", 150, 615_000)
    model.set_position("439960", 0, 0)
    assert text() == "" and "439960" not in model.positions
    changed.clear()
    model.set_position("439960", 0, 0)
    assert not changed, changed

    # 보유하지 않은 종목은 시세가 바뀌어도 손익 칸을 건드리지 않는다.
    model.update_stock("439960", {"price": 4_300})
    assert gui.PNL_COL not in changed, changed
    demo_popup(model)
    print("ok (실시간 순손익: 수수료·세금 반영, 시세 틱 갱신, 영웅문 −749 역산, 확대 창)")


def demo_popup(model):
    """확대 창: 보유가 있으면 손익률·손익금, 없으면 보유 없음. 같은 값이면 다시 그리지 않는다."""
    class Screen:
        prefix = "t_"
        _pnl_popups = {}

        def __init__(self, path):
            self._settings = QSettings(path, QSettings.IniFormat)

    with tempfile.TemporaryDirectory() as folder:
        screen = Screen(os.path.join(folder, "t.ini"))
        popup = gui.PnlPopup(screen, "439960", "테스트")
        assert "보유 없음" in popup._label.text()
        model.set_position("439960", 100, 400_000)
        model.update_stock("439960", {"price": 4_240})
        popup.set_pnl(model._pnl("439960"))
        popup._refresh_text()
        html = popup._label.text()
        assert "+5.76%" in html and "+23,032원" in html and "100주 · 평균 4,000" in html, html
        assert popup.WIN.name().lower() in html.lower()
        assert "순손익 +23,032원" in popup.toolTip()
        popup._paint_timer.stop()
        popup.set_pnl(model._pnl("439960"))      # 같은 값: 페인트를 다시 걸지 않는다
        assert not popup._paint_timer.isActive()
        model.update_stock("439960", {"price": 3_900})
        popup.set_pnl(model._pnl("439960"))
        popup._refresh_text()
        assert popup.LOSE.name().lower() in popup._label.text().lower()
        popup.close()


if __name__ == "__main__":
    demo()
