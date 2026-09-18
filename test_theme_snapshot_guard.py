# -*- coding: utf-8 -*-
"""빈 테마 스냅샷이 기존 연결을 지우지 못하게 막는지 검사한다.

2026-09-15 네이버 테마 수집이 화면 교체로 0개를 받아 왔다. 예외가 아니라
`COMPLETED · 테마 0개`로 끝났고 `replace_source=True`가 그 0개로 살아 있던
6,421건을 전부 만료시켰다. 전 종목의 세부 테마가 사라져 동양(001520)은
`건축자재`(WICS) 하나만 남았고, 상한가 당일 테마 칸에 오늘 재료가 한 줄도
안 보였다.

운영 DB를 건드리면 안 되므로 임시 DB에 스키마를 새로 만들어 돈다.
"""
import os
import shutil
import sqlite3
import tempfile
from pathlib import Path

from analysis_db import (
    active_theme_labels, initialize, save_theme_snapshot)

SNAPSHOT = [
    {"code": "178", "name": "전선", "members": ["000500", "001440"]},
    {"code": "556", "name": "뉴로모픽 반도체", "members": ["092600"]},
]


def _prepare(db_path: Path):
    initialize(db_path)
    with sqlite3.connect(db_path) as connection:
        # updated_at이 NOT NULL이라 빼면 INSERT OR IGNORE가 조용히 건너뛴다.
        connection.executemany(
            """INSERT OR IGNORE INTO stocks(stock_code, stock_name, updated_at)
               VALUES (?, ?, '2026-09-18T00:00:00+09:00')""",
            [("000500", "가온전선"), ("001440", "대한전선"),
             ("092600", "앤씨앤")])


def demo():
    temp = Path(tempfile.mkdtemp(prefix="theme_guard_"))
    db_path = temp / "test.db"
    try:
        _prepare(db_path)
        themes, saved = save_theme_snapshot(
            SNAPSHOT, "20260918", "NAVER", 0.95, db_path=db_path)
        assert themes == 2 and saved == 3, (themes, saved)
        before = active_theme_labels(db_path)
        assert before.get("092600") == ("뉴로모픽 반도체",), before
        assert before.get("000500") == ("전선",), before

        # 수집이 0개면 성공이 아니라 고장이다. 교체를 거부해야 한다.
        for empty in ([], ()):
            try:
                save_theme_snapshot(
                    list(empty), "20260919", "NAVER", 0.95, db_path=db_path)
            except ValueError:
                pass
            else:
                raise AssertionError("빈 스냅샷이 교체를 통과했다")

        # 한 줄도 사라지지 않았어야 한다.
        after = active_theme_labels(db_path)
        assert after == before, (before, after)

        # replace_source=False(신규만 수집)는 빈 목록이어도 그냥 통과한다.
        # 이미 아는 테마를 건너뛴 결과라 0개가 정상이다.
        themes, saved = save_theme_snapshot(
            [], "20260919", "NAVER", 0.95,
            replace_source=False, db_path=db_path)
        assert (themes, saved) == (0, 0), (themes, saved)
        assert active_theme_labels(db_path) == before

        # 정상 스냅샷이면 교체가 된다. 가드가 갱신 자체를 막으면 안 된다.
        moved = [
            {"code": "178", "name": "전선", "members": ["000500"]},
            {"code": "999", "name": "데이터센터", "members": ["001440"]},
        ]
        save_theme_snapshot(
            moved, "20260919", "NAVER", 0.95, db_path=db_path)
        latest = active_theme_labels(db_path)
        assert latest.get("001440") == ("데이터센터",), latest
        assert "092600" not in latest, latest  # 빠진 종목은 만료된다
        print("ok (빈 스냅샷 방어)")
    finally:
        shutil.rmtree(temp, ignore_errors=True)


if __name__ == "__main__":
    demo()
