# -*- coding: utf-8 -*-
"""청산키가 한 종목에 하나만 걸리고, 풀 칸 없는 키가 생기지 않는지 확인한다.

2026-09-29 486510: 창 2에 F1(재실행 복원, 그 창엔 행이 없음), 메인창에 F5
(주문 때 자동 배정)가 함께 걸려 있었다. F1은 보이지 않는 채 살아 있어서,
비어 있는 줄 알고 누르면 486510이 청산될 상태였다.
"""
import logging
import os
import types

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

import gui  # noqa: E402
import main  # noqa: E402

logging.disable(logging.CRITICAL)  # 운영 bot.log에 쓰지 않는다


def demo_one_key_per_stock():
    """다른 창에 이미 키가 걸린 종목은 자동 배정을 건너뛴다."""
    app = types.SimpleNamespace(_exit_hotkey_specs={
        "w2_": {"486510": {"label": "F1", "key": 112}}})
    screen = types.SimpleNamespace(prefix="", model=types.SimpleNamespace(
        exit_hotkeys={}))
    main.App._auto_assign_exit_hotkey(app, screen, "486510")
    assert screen.model.exit_hotkeys == {}, "두 번째 키를 걸었다"
    print("ok (한 종목에 청산키 하나)")


def demo_keyed_row_is_held():
    QApplication.instance() or QApplication([])
    screen = gui.ConditionScreen()
    screen.model.add_stock("486510", {"name": "시험"})
    screen.auto_remove.setChecked(True)
    screen.model.exit_hotkeys["486510"] = (112, "F1")
    screen.on_excluded("486510")                       # 조건에서 빠져도
    assert "486510" in screen.model.rows, "청산키가 걸린 행을 지웠다"

    app = types.SimpleNamespace(
        _exit_hotkey_specs={screen.prefix: {"486510": {"label": "F1"}}},
        _global_hotkeys=types.SimpleNamespace(unregister=lambda token: None),
        _clear_hotkey_conflict=lambda code: None,
        _save_order_settings=lambda: None)
    main.App._set_global_exit_hotkey(app, screen, "486510", None)  # 풀면
    assert "486510" not in screen.model.rows, "키를 풀었는데 행이 남았다"
    print("ok (청산키 걸린 행은 조건 이탈에도 남고, 풀면 지워짐)")


def demo_restored_key_gets_a_row():
    """재실행 복원 키가 있는데 스냅샷에 없으면 행을 붙이고 알림은 안 울린다."""
    added, beeps = [], []
    screen = types.SimpleNamespace(
        model=types.SimpleNamespace(codes=[], rows={}),
        proxy=types.SimpleNamespace(pinned=set()),
        _excluded_with_orders=set(),
        on_included_many=lambda codes: added.extend(codes))
    view = types.SimpleNamespace(
        prefix="w2_", screen=screen, seq="1",
        app=types.SimpleNamespace(
            _exit_hotkey_specs={"w2_": {"486510": {"label": "F1"}}},
            queue_real=lambda *a, **k: None,
            _restore_stock_order_settings=lambda *a: None),
        _real_suffix=lambda: "_AL", _schedule_refresh=lambda: None,
        _maybe_beep=lambda: beeps.append(1),
        _forget_entry_time=lambda code: None)
    main.View.on_snapshot(view, [])
    assert added == ["486510"] and beeps == [], (added, beeps)
    assert "486510" in screen._excluded_with_orders

    main.View.on_snapshot(view, ["005930"])              # 진짜 편입은 울린다
    assert beeps == [1], beeps
    print("ok (복원 키에 행을 붙임, 편입 알림은 진짜 편입만)")


def demo_clear_all():
    """모든 창의 청산키를 한 번에 푼다. 닫힌 창 몫의 저장분도 비운다."""
    QApplication.instance() or QApplication([])
    main_screen, w2 = gui.ConditionScreen(), gui.ConditionScreen(prefix="w2_")
    main_screen.model.exit_hotkeys["486510"] = (116, "F5")
    w2.model.exit_hotkeys["486510"] = (112, "F1")
    saved = []
    app = types.SimpleNamespace(
        views=[types.SimpleNamespace(screen=main_screen),
               types.SimpleNamespace(screen=w2)],
        _exit_hotkey_specs={"": {"486510": {"label": "F5"}},
                            "w2_": {"486510": {"label": "F1"}},
                            "w9_": {"000001": {"label": "F9"}}},   # 닫힌 창
        _global_hotkeys=types.SimpleNamespace(unregister=lambda token: None),
        _clear_hotkey_conflict=lambda code: None,
        _save_order_settings=lambda: saved.append(1))
    app._set_global_exit_hotkey = (
        lambda *a, **k: main.App._set_global_exit_hotkey(app, *a, **k))
    assert main.App._exit_hotkey_count(app) == 3
    main.App._clear_all_exit_hotkeys(app)
    assert app._exit_hotkey_specs == {}, app._exit_hotkey_specs
    assert not main_screen.model.exit_hotkeys and not w2.model.exit_hotkeys
    assert saved == [1], saved                        # 저장은 끝에 한 번

    print("ok (청산키 전부 해제)")


if __name__ == "__main__":
    demo_clear_all()
    demo_one_key_per_stock()
    demo_keyed_row_is_held()
    demo_restored_key_gets_a_row()
