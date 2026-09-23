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
    view._entry_broken = set()
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


def demo_entry_survives_break():
    """상한가가 무너져도 진입시각은 화면에 남는다 (2026-09-23).

    그날 몇 시에 붙었는지는 무너진 뒤에도 정보다. 조건에 남아 있는 한 틱이
    계속 오므로 재진입을 볼 수 있어 캐시를 버릴 이유가 없다. 위 `demo`의
    이탈 갈래와는 다르다 — 그쪽은 실시간 등록이 풀려 틱이 끊긴다.
    """
    app = QApplication.instance() or QApplication([])
    screen = ConditionScreen()
    view = _view(screen)
    code = "047770"
    screen.model.add_stock(code, {"name": "코데즈컴바인"})
    _at_limit(screen, code, 1300)
    view._entry_cache[code] = "09:01:32"
    screen.on_tick(code, {"time": "09:01:32"})

    screen.on_tick(code, {"price": 1250})  # 상한가가 무너진다
    view.fill_entry_time(code)
    assert screen.model.rows[code]["time"] == "09:01:32", screen.model.rows[code]
    assert view._entry_cache[code] == "09:01:32"
    assert code in view._entry_broken

    del app
    print("ok (무너져도 진입시각이 남음)")


def demo_reentry_costs_no_query():
    """다시 붙는 틱이 곧 재진입 시각이라 조회가 나가지 않는다.

    `last_limit_entry`는 ka10079 틱차트를 최신에서 과거로 훑고 활발한
    상한이면 3페이지까지 페이징한다. REST는 초당 1건이라 무너졌다 붙을
    때마다 부르면 비싸다.
    """
    app = QApplication.instance() or QApplication([])
    screen = ConditionScreen()
    view = _view(screen)
    code = "047770"
    screen.model.add_stock(code, {"name": "코데즈컴바인"})
    _at_limit(screen, code, 1300)
    view._entry_cache[code] = "09:01:32"
    screen.on_tick(code, {"time": "09:01:32"})

    started, saved = [], []
    original = main.asyncio.ensure_future
    original_save = main.save_last_entry_time

    def fake(coro):
        coro.close()
        started.append(1)

    main.asyncio.ensure_future = fake
    # 운영 DB에 쓰지 않는다. 저장 여부만 센다.
    main.save_last_entry_time = lambda *a: saved.append(a) or False
    try:
        screen.on_tick(code, {"price": 1250})   # 무너짐
        view.fill_entry_time(code)
        _at_limit(screen, code, 1300)           # 재진입
        view.fill_entry_time(code)
    finally:
        main.asyncio.ensure_future = original
        main.save_last_entry_time = original_save

    assert started == [], f"재진입에 조회가 나갔다: {started}"
    assert code not in view._entry_broken
    new_time = screen.model.rows[code]["time"]
    assert new_time != "09:01:32", "마지막 진입으로 안 바뀜"
    assert len(new_time) == 8 and new_time[2] == ":", new_time
    assert saved and saved[-1][1] == code, saved   # 그날 기록도 갱신한다

    del app
    print(f"ok (재진입 조회 0건, 시각 {new_time})")


if __name__ == "__main__":
    demo()
    demo_pending_query_dropped_on_exclude()
    demo_entry_survives_break()
    demo_reentry_costs_no_query()
