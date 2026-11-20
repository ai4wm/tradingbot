# -*- coding: utf-8 -*-
"""틱 기록: 내용·파일 나눔·가득 참·수신 스레드 비차단을 확인한다."""
import asyncio
import json
import os
import tempfile
import threading
import time
from pathlib import Path

import tick_recorder
from tick_recorder import TickRecorder


def read_all(root):
    rows = {}
    for path in Path(root).rglob("*.jsonl"):
        rows[path.stem] = [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    return rows


def demo_writes_and_splits():
    with tempfile.TemporaryDirectory(prefix="tick_rec_") as root:
        rec = TickRecorder(root, flush_seconds=0.05)
        values = {"10": "+10380", "13": "123"}
        rec.put({"type": "0B", "item": "002990_AL", "values": values})
        rec.put({"type": "0D", "item": "002990", "values": {"41": "+10390", "61": "19"}})
        rec.put({"type": "0H", "item": "002990", "values": {"10": "+10300"}})
        rec.put({"type": "00", "item": "x", "values": {"9001": "A"}})     # 주문 이벤트는 안 남긴다
        rec.put({"type": "1h", "item": "002990", "values": {"1223": "1"}})  # VI는 따로 기록 중
        rec.put({"type": "0w", "item": "002990", "values": {"202": "10", "206": "20"}})  # 프로그램매매
        rec.put({"type": "02", "item": "", "values": {"841": "3", "9001": "A002990", "843": "I"}})  # 조건 편입
        values["10"] = "mutated"                                          # 넣은 뒤 바뀌어도 기록은 그대로
        rec.stop()
        rows = read_all(root)
        assert sorted(rows) == ["002990", "002990_AL", "_condition"], rows.keys()
        assert [r["ty"] for r in rows["002990"]] == ["0D", "0H", "0w"]
        assert rows["_condition"][0]["v"]["843"] == "I"
        assert rows["002990_AL"][0]["v"]["10"] == "+10380", "복사 전에 바뀐 값이 기록됨"
        assert len(rows["002990_AL"][0]["t"]) == 12 and rows["002990_AL"][0]["t"][2] == ":"
        assert (Path(root) / time.strftime("%Y%m%d") / "002990.jsonl").is_file()
        assert rec.written == 5 and rec.dropped == 0, (rec.written, rec.dropped)
    print("ok (종류 선별·종목별 파일·값 복사)")


def demo_full_queue_never_blocks():
    with tempfile.TemporaryDirectory(prefix="tick_rec_") as root:
        rec = TickRecorder(root, maxsize=100, flush_seconds=3600)  # 쓰기 스레드가 안 돈다
        start = time.perf_counter()
        for i in range(10_000):
            rec.put({"type": "0B", "item": "A", "values": {"10": str(i)}})
        elapsed = time.perf_counter() - start
        assert rec.dropped == 9_900, rec.dropped
        assert elapsed < 1.0, f"put이 막혔다: {elapsed:.3f}s"
        rec._stop.set(); rec._thread.join(timeout=5)
    print(f"ok (가득 차면 버림, 10,000건 {elapsed * 1000:.0f}ms)")


def demo_write_failure_swallowed():
    with tempfile.TemporaryDirectory(prefix="tick_rec_") as root:
        blocker = Path(root) / "blocked"
        blocker.write_text("파일이라 폴더를 못 만든다")
        rec = TickRecorder(str(blocker), flush_seconds=0.05)
        rec.put({"type": "0B", "item": "A", "values": {"10": "1"}})
        rec.stop()
        assert rec.dropped == 1 and rec.written == 0
    print("ok (쓰기 오류는 삼키고 버림으로 센다)")


def demo_dispatch_overhead():
    """실제 `_dispatch`가 기록 유무와 상관없이 같은 일을 하고 시간이 거의 같다."""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    import ws
    msg = {"trnm": "REAL", "data": [
        {"type": "0D", "item": f"{i:06d}", "values": {str(f): "+10000" for f in range(41, 81)}}
        for i in range(30)]}

    def run(recorder):
        client = ws.WSClient()
        client.tick_recorder = recorder
        got = []
        client.on_real = lambda code, fields: got.append(code)
        start = time.perf_counter()
        loop = asyncio.new_event_loop()
        try:
            for _ in range(300):
                loop.run_until_complete(client._dispatch(msg))
        finally:
            loop.close()
        return time.perf_counter() - start, got

    with tempfile.TemporaryDirectory(prefix="tick_rec_") as root:
        off, got_off = run(None)
        rec = TickRecorder(root, flush_seconds=0.05)
        on, got_on = run(rec)
        rec.stop()
        assert got_on == got_off and len(got_on) == 9_000, (len(got_on), len(got_off))
        assert rec.dropped == 0 and rec.written == 9_000, (rec.written, rec.dropped)
        per_tick_us = (on - off) / 9_000 * 1e6
        assert on < off * 1.5 + 0.05, f"기록이 수신을 늦춘다: off {off:.3f}s on {on:.3f}s"
    print(f"ok (9,000틱 수신 기록 끔 {off * 1000:.0f}ms · 켬 {on * 1000:.0f}ms, 틱당 {per_tick_us:.1f}µs)")


def demo_switch():
    """체크박스: 즉시 켜고 끄고, 선택을 저장하고, 꺼지면 아무것도 안 쓴다."""
    import types
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication
    import gui
    import main
    app = QApplication.instance() or QApplication([])
    with tempfile.TemporaryDirectory(prefix="tick_rec_") as root:
        rec = TickRecorder(root, flush_seconds=0.05)
        saved = {}
        screen = gui.ConditionScreen()
        host = types.SimpleNamespace(
            ws=types.SimpleNamespace(tick_recorder=rec), screen=screen,
            _settings=types.SimpleNamespace(setValue=saved.__setitem__, sync=lambda: None))
        host._refresh_tick_stats = types.MethodType(main.App._refresh_tick_stats, host)
        screen.tick_record_check.toggled.connect(lambda on: main.App._on_tick_record(host, on))
        screen.tick_record_check.setChecked(True)
        rec.put({"type": "0B", "item": "A", "values": {"10": "1"}})
        screen.tick_record_check.setChecked(False)
        assert rec.enabled is False and saved["order/tick_record"] == "false"
        assert "FF9800" in screen.tick_record_check.styleSheet(), "꺼진 상태 표시가 없다"
        rec.put({"type": "0B", "item": "A", "values": {"10": "2"}})   # 꺼진 동안은 버리지도 않는다
        screen.tick_record_check.setChecked(True)
        assert rec.enabled is True and saved["order/tick_record"] == "true"
        assert screen.tick_record_check.styleSheet() == ""
        rec.put({"type": "0B", "item": "A", "values": {"10": "3"}})
        rec.stop()
        values = [r["v"]["10"] for r in read_all(root)["A"]]
        assert values == ["1", "3"], values
        assert rec.dropped == 0
        assert "건 저장" in screen.tick_record_check.toolTip()
        screen.deleteLater(); app.processEvents()
    print("ok (틱기록 체크: 즉시 켜고 끔·저장·꺼짐 표시)")


def demo_folder_menu():
    """우클릭: 오늘 폴더·틱 폴더 열기, 경로 복사. 오늘 폴더가 없으면 틱 폴더를 연다."""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtCore import QUrl
    from PySide6.QtGui import QDesktopServices
    from PySide6.QtWidgets import QApplication
    import gui
    app = QApplication.instance() or QApplication([])
    assert os.path.isabs(tick_recorder.TICK_DIR), "TICK_DIR는 실행 폴더와 무관해야 한다"
    assert tick_recorder.TICK_DIR.endswith(os.path.join("data", "ticks"))
    with tempfile.TemporaryDirectory(prefix="tick_menu_") as root:
        real_dir, real_open = tick_recorder.TICK_DIR, QDesktopServices.openUrl
        tick_recorder.TICK_DIR = os.path.join(root, "ticks")
        opened = []
        QDesktopServices.openUrl = staticmethod(lambda url: opened.append(url.toLocalFile()))
        screen = gui.ConditionScreen()
        try:
            texts = [a.text() for a in screen._tick_menu().actions()]
            assert texts == ["오늘 폴더 열기", "틱 폴더 열기", "경로 복사"], texts
            assert "우클릭" in screen.tick_record_check.toolTip()
            actions = {a.text(): a for a in screen._tick_menu().actions()}
            actions["오늘 폴더 열기"].trigger()          # 오늘 폴더가 아직 없다 → 틱 폴더
            today = os.path.join(tick_recorder.TICK_DIR, time.strftime("%Y%m%d"))
            os.makedirs(today)
            actions["오늘 폴더 열기"].trigger()
            actions["틱 폴더 열기"].trigger()
            actions["경로 복사"].trigger()
            copied = QApplication.clipboard().text()
        finally:
            tick_recorder.TICK_DIR, QDesktopServices.openUrl = real_dir, real_open
        norm = lambda p: os.path.normcase(os.path.normpath(p))
        assert [norm(x) for x in opened] == [norm(os.path.join(root, "ticks")), norm(today),
                                            norm(os.path.join(root, "ticks"))], opened
        assert norm(copied) == norm(os.path.join(root, "ticks")), copied
        screen.deleteLater(); app.processEvents()
    print("ok (우클릭 메뉴: 오늘 폴더·틱 폴더·경로 복사)")


if __name__ == "__main__":
    demo_writes_and_splits()
    demo_full_queue_never_blocks()
    demo_write_failure_swallowed()
    demo_dispatch_overhead()
    demo_switch()
    demo_folder_menu()
