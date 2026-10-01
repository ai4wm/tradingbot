# -*- coding: utf-8 -*-
"""Ctrl+V로 조건 밖 종목을 조건검색 창에 붙잡아 두는지 확인한다.

창에 없으면 추가+고정, 있으면 고정만, 이미 고정이면 선택만. 고정이라 조건
이탈·재조회에도 남고, 고정을 풀면 조건에 없는 종목만 사라진다.
운영 layout.ini와 클립보드는 건드리지 않는다.
"""
import os
import types

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

import gui  # noqa: E402
import main  # noqa: E402


class FakeSettings:
    def __init__(self):
        self.saved = {}

    def setValue(self, key, value):
        self.saved[key] = value

    def value(self, key, default=None):
        return self.saved.get(key, default)

    def sync(self):
        pass


def _screen():
    QApplication.instance() or QApplication([])
    screen = gui.ConditionScreen(prefix="paste_test_")
    screen._settings = FakeSettings()
    screen.proxy.pinned = set()
    screen.auto_remove.setChecked(True)
    screen.code_add_requested.connect(
        lambda code: (screen._excluded_with_orders.add(code),
                      screen.on_included(code, {"name": code})))
    return screen


def demo_paste():
    screen = _screen()
    screen._paste_code("대한제강")
    assert not screen.model.rows and not screen.proxy.pinned

    screen._paste_code(" A084010\n")                     # 없는 종목: 추가+고정
    assert "084010" in screen.model.rows and "084010" in screen.proxy.pinned
    current = screen.proxy.mapToSource(screen.table.currentIndex())
    assert screen.model.codes[current.row()] == "084010"

    screen.on_excluded("084010")                        # 조건 이탈·재조회에도 남는다
    assert "084010" in screen.model.rows

    screen._paste_code("084010")                        # 이미 고정: 그대로
    assert screen.model.codes.count("084010") == 1

    screen.on_included("005930", {"name": "삼성전자"})    # 편입 종목: 고정만
    screen._paste_code("005930")
    assert "005930" in screen.proxy.pinned and screen.model.codes.count("005930") == 1

    screen._paste_code("0011a0")                        # 글자 섞인 코드도 받는다
    assert "0011A0" in screen.model.rows

    screen._set_pinned("084010", False)                 # 조건 밖: 풀면 사라진다
    assert "084010" not in screen.model.rows
    screen._set_pinned("005930", False)                 # 편입 종목: 행은 남는다
    assert "005930" in screen.model.rows
    assert screen._settings.saved["paste_test_pinned"] == "0011A0"
    print("ok (Ctrl+V 추가·고정·선택, 고정 풀면 조건 밖만 사라짐)")


def demo_pinned_survives_snapshot():
    """재실행·재조회 스냅샷에 없는 고정 종목은 행을 붙이고 알림은 안 울린다."""
    added, beeps = [], []
    screen = types.SimpleNamespace(
        model=types.SimpleNamespace(codes=[], rows={}),
        proxy=types.SimpleNamespace(pinned={"084010"}),
        _excluded_with_orders=set(),
        on_included_many=lambda codes: added.extend(codes))
    view = types.SimpleNamespace(
        prefix="", screen=screen, seq="1",
        app=types.SimpleNamespace(
            _exit_hotkey_specs={}, queue_real=lambda *a, **k: None,
            _restore_stock_order_settings=lambda *a: None),
        _real_suffix=lambda: "_AL", _schedule_refresh=lambda: None,
        _maybe_beep=lambda: beeps.append(1),
        _forget_entry_time=lambda code: None)
    main.View.on_snapshot(view, [])
    assert added == ["084010"] and beeps == [], (added, beeps)
    assert "084010" in screen._excluded_with_orders
    main.View.on_snapshot(view, ["084010"])             # 진짜 편입되면 붙잡음 해제
    assert "084010" not in screen._excluded_with_orders
    print("ok (스냅샷에 없는 고정 종목 행 유지)")


if __name__ == "__main__":
    demo_paste()
    demo_pinned_survives_snapshot()
