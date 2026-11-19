# -*- coding: utf-8 -*-
"""거래 시작과 신규 편입 때 행 순서와 점상 구분선이 함께 바뀌는지 검사한다.

운영 설정·DB·증권사 연결 없이 실제 정렬/화면 갱신 메서드만 연결한다.
"""
import os
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import config  # noqa: E402
config.market_delay = lambda day=None: (0, 0)  # 평일 시각으로 고정(수능일에 돌려도 같게)

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import QApplication, QCheckBox

import gui


class ScreenHarness:
    _on_data_changed = gui.ConditionScreen._on_data_changed
    _resort_proxy = gui.ConditionScreen._resort_proxy

    def __init__(self):
        self.model = gui.StockModel()
        self.proxy = gui.TieredProxy()
        self.proxy.setSourceModel(self.model)
        self.proxy.setSortRole(Qt.UserRole)
        self.proxy.setDynamicSortFilter(False)
        self.proxy.limit_mode = True
        self.limit_sort = QCheckBox()
        self.limit_sort.setChecked(True)
        self.theme_sort = QCheckBox()
        self._sort_col = gui.BID_QTY_COL
        self.table = gui.ThemeGroupedTableView()
        self.table.setModel(self.proxy)
        self._resort_timer = QTimer()
        self._resort_timer.setSingleShot(True)
        self._resort_timer.timeout.connect(self._resort_proxy)
        self.sort_count = 0
        self.waiting = set()
        self.model.dataChanged.connect(self._on_data_changed)
        self.model.rowsInserted.connect(self._on_data_changed)

    def _apply_sort(self):
        self.sort_count += 1
        self.proxy.sort(self._sort_col, Qt.DescendingOrder)

    def _jumsang_alert(self, waiting):
        self.waiting = waiting

    def codes(self):
        return [self.proxy.row_code(row) for row in range(self.proxy.rowCount())]

    def close(self):
        self._resort_timer.stop()
        self.table.close()


def waiting(qty):
    return dict(base=1000, upper=1300, price=1000, rate=0.0, vol=0,
                exp_price=1300, exp_hot=1, bid_qty=qty, bid_price=1300,
                ask_qty=0)


def demo():
    app = QApplication.instance() or QApplication([])
    with patch.object(gui, "_opening_over", return_value=False):
        screen = ScreenHarness()
        try:
            for code, qty in (("A", 1000), ("B", 500), ("C", 400)):
                screen.model.add_stock(code, {"name": code, **waiting(qty)})
            screen._resort_proxy()
            assert screen.codes() == ["A", "B", "C"]

            # A가 먼저 거래를 시작하며 잔량도 줄었다. 이벤트 루프/200ms를
            # 기다리기 전에 B·C가 위로 와야 선이 그 위에 잘못 생기지 않는다.
            screen.model.update_stock("A", dict(
                price=1300, rate=30.0, vol=100, bid_qty=10,
                trade_time=90203, trade_ex="KRX"))
            assert screen.codes() == ["B", "C", "A"]
            assert screen.table.waiting_group() == (1, {"B", "C"}, 2)
            assert screen.waiting == {"B", "C"}
            assert not screen._resort_timer.isActive()

            # 같은 묶음의 평소 잔량 변화는 즉시 전체 정렬하지 않고 모은다.
            before = screen.sort_count
            screen.model.update_stock("B", {"bid_qty": 300})
            assert screen.sort_count == before
            assert screen._resort_timer.isActive()
            assert screen.codes() == ["B", "C", "A"]
            screen._resort_timer.stop()
            screen._resort_proxy()
            assert screen.codes() == ["C", "B", "A"]

            # 새 편입은 위로 강제하지 않는다. 시세가 없는 동안 맨 아래,
            # 첫 시세가 와도 실제 거래 종목이면 대기 종목 아래다.
            screen.model.add_stock("NEW", {"name": "새 편입"})
            assert screen.codes()[-1] == "NEW"
            screen.model.update_stock("NEW", dict(
                base=1000, upper=1300, price=1100, rate=10.0, vol=100,
                trade_time=90205, trade_ex="KRX", bid_qty=1000000,
                bid_price=1090, ask_qty=100, ask_price=1100))
            assert screen.codes()[:2] == ["C", "B"]
            assert screen.codes().index("NEW") > 1

            # 조회가 이미 같은 값을 채워 둬도 첫 KRX 체결 신호만으로
            # 보합 종목의 대기 판정과 행 순서가 즉시 갱신되어야 한다.
            screen.model.update_stock("C", dict(
                price=1000, rate=0.0, vol=0,
                trade_time=90206, trade_ex="KRX"))
            assert screen.codes()[0] == "B"
            assert screen.table.waiting_group()[1] == {"B"}

            # 재정렬 중 발생한 표시 갱신으로 재귀 정렬하지 않는다.
            with patch.object(screen.proxy, "_refresh_theme_sort_keys",
                              side_effect=lambda: screen.model.dataChanged.emit(
                                  screen.model.index(0, 0),
                                  screen.model.index(0, gui.RATE_COL))):
                before = screen.sort_count
                screen._resort_proxy()
                assert screen.sort_count == before + 1

            screen.table.resize(900, 240)
            screen.table.show()
            app.processEvents()
            assert not screen.table.grab().isNull()
        finally:
            screen.close()
    print("ok (거래 시작 즉시 정렬·구분선 일치, 잔량 갱신 묶음, 신규 편입 아래 배치)")


if __name__ == "__main__":
    demo()
