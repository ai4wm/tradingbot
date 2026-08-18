# -*- coding: utf-8 -*-
"""상한가가 무너져 조건에서 빠진 종목이 재진입하면 진입시각을 다시 받는지 확인한다.

이탈하면 실시간 등록이 풀려 상한가가 깨지는 틱이 오지 않는다. 캐시를 그대로
두면 재편입·재진입 뒤에도 무너지기 전 시각을 계속 보여 준다.
"""
import asyncio
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

import main  # noqa: E402
from gui import ConditionScreen  # noqa: E402


def _view(screen):
    """QSettings·타이머 없이 진입시각 갈래만 쓰는 최소 뷰."""
    view = main.View.__new__(main.View)
    view.screen = screen
    view._entry_cache = {}
    view._entry_pending = set()
    return view


def _at_limit(screen, code, price):
    screen.on_tick(code, {"price": price, "upper": price, "vol": 1000})


def demo():
    app = QApplication.instance() or QApplication([])

    screen = ConditionScreen()
    screen.auto_remove.setChecked(True)  # 조건 이탈 시 행 삭제
    view = _view(screen)

    started = []
    original = main.asyncio.ensure_future

    def fake(coro):
        coro.close()  # 조회는 띄우지 않고 호출만 센다
        started.append(1)

    main.asyncio.ensure_future = fake
    try:
        code = "047770"
        screen.model.add_stock(code, {"name": "코데즈컴바인"})
        _at_limit(screen, code, 1300)

        # 첫 진입: 조회 한 번, 결과가 캐시에 남는다.
        view.fill_entry_time(code)
        assert len(started) == 1, started
        view._entry_pending.discard(code)
        view._entry_cache[code] = "090132"
        screen.on_tick(code, {"time": "090132"})

        # 상한가가 무너지며 조건에서 이탈. 실시간이 끊겨 이탈 틱은 오지 않는다.
        screen.on_excluded(code)
        view._forget_entry_time(code)
        assert code not in screen.model.rows, "자동삭제인데 행이 남음"

        # 재편입 후 상한가 재진입: 옛 시각을 되살리지 않고 다시 조회해야 한다.
        screen.model.add_stock(code, {"name": "코데즈컴바인"})
        _at_limit(screen, code, 1300)
        view.fill_entry_time(code)
        assert len(started) == 2, f"재진입인데 조회를 다시 안 함: {started}"
        assert screen.model.rows[code]["time"] != "090132", "옛 진입시각이 되살아남"
    finally:
        main.asyncio.ensure_future = original

    del app
    print("ok (재진입 시 진입시각 재조회)")


def demo_pending_query_dropped_on_exclude():
    """조회가 도는 중에 이탈하면 그 결과를 캐시에 넣지 않는다."""
    app = QApplication.instance() or QApplication([])

    screen = ConditionScreen()
    view = _view(screen)
    code = "047770"
    screen.model.add_stock(code, {"name": "코데즈컴바인"})
    _at_limit(screen, code, 1300)

    class _Rest:
        async def last_limit_entry(self, code, upper):
            return "090132"

    view.app = type("A", (), {"rest": _Rest(), "_analysis": None})()

    view._entry_pending.add(code)
    view._forget_entry_time(code)  # 응답 오기 전에 이탈
    asyncio.run(view._drain_entries([(0, code, 1300)]))

    assert code not in view._entry_cache, "취소된 조회 결과가 캐시에 들어감"
    assert screen.model.rows[code]["time"] == "", "취소된 조회 결과가 화면에 실림"

    del app
    print("ok (이탈 중 조회 결과 폐기)")


if __name__ == "__main__":
    demo()
    demo_pending_query_dropped_on_exclude()
