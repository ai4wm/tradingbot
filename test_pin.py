# -*- coding: utf-8 -*-
"""순위 칸 고정(📌) 검사.

정렬은 200ms마다 다시 도므로 고정이 정렬 단계에 들어가야 유지된다. 상한가
정렬 구분선은 tier 0·1 묶음이 끊기는 자리에 그리는데, 일반 종목을 고정해
맨 위에 올려도 선이 사라지지 않아야 한다.
"""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication

import gui


def build():
    """등락률 내림차순으로 세운 4종목. 하나만 점상 대기다."""
    model = gui.StockModel()
    rows = (
        # exp_hot=1은 0H(단일가 국면)에서만 온다. 없으면 모델이 예상체결가를
        # 연속매매 echo로 보고 버린다.
        ("000001", "점상대기", dict(rate=0.0, exp_price=1204, exp_hot=1,
                                 upper=1204, ask_qty=0, bid_qty=5_000_000)),
        ("000002", "급등주", dict(rate=12.0)),
        ("000003", "보합주", dict(rate=1.0)),
        ("000004", "약세주", dict(rate=-3.0)),
    )
    for code, name, fields in rows:
        model.add_stock(code, {"name": name, **fields})
    proxy = gui.TieredProxy()
    proxy.setSourceModel(model)
    proxy.setSortRole(Qt.UserRole)
    proxy.limit_mode = True
    proxy.sort(gui.RATE_COL, Qt.DescendingOrder)
    return model, proxy


def order(proxy) -> list[str]:
    return [proxy.row_code(row) for row in range(proxy.rowCount())]


def demo():
    QApplication.instance() or QApplication([])
    model, proxy = build()

    assert order(proxy)[0] == "000001", f"점상 대기가 맨 위: {order(proxy)}"

    # 꼴찌를 고정하면 정렬·방향과 무관하게 맨 위로 온다.
    proxy.pinned.add("000004")
    proxy.invalidate()
    proxy.sort(gui.RATE_COL, Qt.DescendingOrder)
    assert order(proxy)[0] == "000004", f"내림차순 고정: {order(proxy)}"
    proxy.sort(gui.RATE_COL, Qt.AscendingOrder)
    assert order(proxy)[0] == "000004", f"오름차순 고정: {order(proxy)}"

    # 세로 헤더는 번호 대신 표식을, 나머지 행은 그대로 번호를 보여 준다.
    assert proxy.headerData(0, Qt.Vertical, Qt.DisplayRole) == "📌"
    assert proxy.headerData(1, Qt.Vertical, Qt.DisplayRole) == 2

    # 구분선: 고정 종목이 위에 있어도 점상 대기 묶음을 계속 감싸야 한다.
    view = gui.ThemeGroupedTableView()
    view.setModel(proxy)
    proxy.sort(gui.RATE_COL, Qt.DescendingOrder)
    last_row, jumsang = view.waiting_group()
    assert "000001" in jumsang, f"점상 알림 대상 유지: {jumsang}"
    assert last_row == order(proxy).index("000001"), (
        f"선이 점상 대기 행까지 내려와야 한다: last_row={last_row} "
        f"order={order(proxy)}")

    # 해제하면 원래 순서로 돌아간다.
    proxy.pinned.discard("000004")
    proxy.invalidate()
    proxy.sort(gui.RATE_COL, Qt.DescendingOrder)
    assert order(proxy)[0] == "000001", f"해제 후: {order(proxy)}"
    assert proxy.headerData(0, Qt.Vertical, Qt.DisplayRole) == 1

    print("ok", order(proxy))


if __name__ == "__main__":
    demo()
