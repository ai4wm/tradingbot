# -*- coding: utf-8 -*-
"""틱 프로필·이상 순간 판정을 가짜 틱으로 확인한다. 실제 기록은 건드리지 않는다."""
import json
import tempfile
from pathlib import Path

import tick_profile as tp


def clock(sec):
    return f"18:{sec // 60:02d}:{sec % 60:02d}.000"


def book_row(sec, ask_total, bid_total, ask_price=10010, wall=None):
    """호가 틱. wall=(가격, 수량)이면 그 매도 가격에 수량이 더해진다."""
    v = {"121": str(ask_total), "125": str(bid_total), "61": "100", "71": "200"}
    for i in range(1, 11):
        price, qty = ask_price + 10 * (i - 1), 100
        if wall and price == wall[0]:
            qty += wall[1]
        v[str(40 + i)] = f"+{price}"
        v[str(60 + i)] = str(qty)
        v[str(50 + i)] = f"-{ask_price - 10 * i}"
        v[str(70 + i)] = "200"
    return {"t": clock(sec), "ty": "0D", "v": v}


def make_rows():
    rows = []
    for sec in range(0, 130):
        rows.append({"t": clock(sec), "ty": "0B", "v": {"10": "+10000", "15": "-10" if sec % 2 else "+10", "13": str(1000 + sec * 10)}})
    rows.append({"t": clock(40), "ty": "0B", "v": {"10": "+10000", "15": "+500", "13": "99999"}})   # 평균의 50배
    for sec in range(0, 130):
        total = 1000
        wall = None
        for start in (30, 50, 70, 90):                       # 같은 가격·수량 벽 4번 (30초, 50초, 70초, 90초)
            if start <= sec < start + 5:
                total, wall = 9000, (10020, 8000)
        rows.append(book_row(sec, total, 2000, wall=wall))
    # 가격이 한 칸 내려가며 최우선 잔량이 뛴다. 총잔량은 그대로 → 벽이 아니다.
    rows.append(book_row(110, 1000, 2000, ask_price=10000))
    for i, sec in enumerate(range(0, 130, 10)):                # 프로그램: 평소 +5주씩, 100초에 +600
        buy = 5 * i + (600 if sec >= 100 else 0)
        rows.append({"t": clock(sec), "ty": "0w", "v": {"206": str(buy), "202": str(3 * i), "210": str(buy - 3 * i), "13": str(1000 + sec * 10)}})
    return sorted(rows, key=lambda r: r["t"])


def demo_events():
    rows = make_rows()
    prof = tp.profile(rows)
    walls = []
    found = tp.events(rows, prof, walls=walls)
    texts = [text for _, text in found]
    assert sum("매도 총잔량 +8,000" in t for t in texts) == 4, texts          # 벽 4번
    # 가짜 체결이 초당 10주씩 있으므로 5초 동안 60주(양쪽 끝 포함)다.
    assert all("10,020원에 +8,000주" in t and "체결 60주" in t for t in texts if "총잔량" in t), texts
    assert not any("매수 총잔량" in t for t in texts)
    assert sum("대량 체결 500주" in t for t in texts) == 1, texts
    assert sum("프로그램 매수 +605" in t and "표본부족" in t for t in texts) == 1, texts   # 평소 +5에 급증 +600, 갱신 12회라 표본부족
    # 시각 110초의 가격 이동(총잔량 변화 없음)은 벽이 아니다.
    assert not any(t.startswith("18:01:5") and "총잔량" in text for t, text in found), found
    repeats = tp.repeated_walls(walls)
    assert repeats == [("매도", 10020, 8000.0, 4, "18:00:30", "18:01:30")], repeats
    print("ok (벽 출현·소멸 4건, 가격 이동 오탐 없음, 반복 묶음, 대량 체결, 프로그램 급증)")


def demo_wall_requires_vanish_and_no_trades():
    rows = []
    for sec in range(0, 100):
        rows.append({"t": clock(sec), "ty": "0B", "v": {"10": "+10000", "15": "-10", "13": "1"}})
        total = 9000 if sec >= 30 else 1000                  # 벽이 나타나고 사라지지 않는다
        rows.append(book_row(sec, total, 2000, wall=(10020, 8000) if sec >= 30 else None))
    prof = tp.profile(rows)
    assert not [t for _, t in tp.events(rows, prof) if "총잔량" in t], "사라지지 않은 물량은 벽이 아니다"
    rows = []
    for sec in range(0, 100):
        qty = 3000 if 30 <= sec < 35 else 10
        rows.append({"t": clock(sec), "ty": "0B", "v": {"10": "+10000", "15": f"-{qty}" if sec == 32 else "-10", "13": "1"}})
        rows.append(book_row(sec, 9000 if 30 <= sec < 35 else 1000, 2000, wall=(10020, 8000) if 30 <= sec < 35 else None))
    prof = tp.profile(rows)
    assert not [t for _, t in tp.events(rows, prof) if "총잔량" in t], "체결로 줄어든 물량은 벽이 아니다"
    print("ok (사라지지 않거나 체결로 줄어든 물량은 벽이 아님)")


def demo_profile_values():
    prof = tp.profile(make_rows())
    book = prof["book"]
    assert book["tick"] == 10 and abs(book["tick_pct"] - 0.1) < 0.02, book
    assert 9 < book["levels_in_1pct"] < 11 and book["empty_ask"] == 0
    assert prof["program"]["updates"] == 12 and prof["program"]["corr_n"] >= 0
    assert tp.tick_size(1999) == 1 and tp.tick_size(2000) == 5 and tp.tick_size(19999) == 10 and tp.tick_size(500000) == 1000
    assert tp.pearson([1, 2, 3, 4], [2, 4, 6, 8]) > 0.999 and tp.pearson([1, 1, 1], [1, 2, 3]) is None
    print("ok (호가단위·1% 안 호가 수·프로그램 갱신·상관 계산)")


def demo_load_and_report():
    with tempfile.TemporaryDirectory(prefix="tick_profile_") as root:
        day = Path(root) / "20261120"
        day.mkdir()
        rows = make_rows()
        half = len(rows) // 2
        (day / "002990.jsonl").write_text("\n".join(json.dumps(r) for r in rows[:half]) + "\n", encoding="utf-8")
        (day / "002990_AL.jsonl").write_text("\n".join(json.dumps(r) for r in rows[half:]) + "\n", encoding="utf-8")
        (day / "002990_NX.jsonl").write_text(json.dumps({"t": "18:00:00.000", "ty": "0B", "v": {"10": "+1", "15": "+1"}}) + "\n", encoding="utf-8")
        loaded = tp.load("002990", "20261120", root)
        assert len(loaded) == len(rows), "KRX와 통합 파일을 시간순으로 합치고 NX는 뺀다"
        assert [r["t"] for r in loaded] == sorted(r["t"] for r in loaded)
        assert "기록이 없습니다" in tp.report("999999", "20261120", root=root)
        full = tp.report("002990", "20261120", root=root)
        assert "[체결]" in full and "[프로그램]" in full and "[호가]" in full and "[반복 패턴]" in full, full
        windowed = tp.window(loaded, "18:01-18:01")
        assert windowed and all(r["t"][:5] == "18:01" for r in windowed)
    print("ok (파일 합치기·구간 자르기·없는 종목)")


if __name__ == "__main__":
    demo_events()
    demo_wall_requires_vanish_and_no_trades()
    demo_profile_values()
    demo_load_and_report()
