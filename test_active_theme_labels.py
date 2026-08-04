# -*- coding: utf-8 -*-
"""테마 후보를 수동+네이버로 합치고, 둘 다 없을 때만 보조 출처로 넘어가는지 확인한다."""
import sqlite3
import tempfile
from pathlib import Path

import analysis_db


def _seed(path: Path):
    analysis_db.initialize(path)
    con = sqlite3.connect(path)
    try:
        con.executemany(
            """INSERT OR REPLACE INTO stocks(stock_code, stock_name, market,
                   stock_type, sector_code, sector_name, updated_at)
               VALUES (?,?,'KOSDAQ',?,'','','2026-08-04')""",
            [("078600", "대주전자재료", "COMMON"),
             ("999990", "보조출처만", "COMMON"),
             ("999991", "우선주시험", "PREFERRED")])
        con.executemany(
            """INSERT OR REPLACE INTO themes(theme_id, theme_name, description,
                   updated_at) VALUES (?,?,'','2026-08-04')""",
            [(1, "2차전지"), (2, "태양광에너지"), (3, "우주태양광"), (9, "화학")])
        con.executemany(
            """INSERT OR REPLACE INTO stock_themes(
                   stock_code, theme_id, valid_from, valid_to, source, confidence)
               VALUES (?,?,?,NULL,?,1.0)""",
            [("078600", 1, "20260728", "MANUAL"),   # 수동 등록
             ("078600", 2, "20260725", "NAVER"),    # 네이버 세부 테마
             ("078600", 3, "20260725", "NAVER"),
             ("078600", 9, "20260725", "WICS"),     # 넓은 업종 — 섞이면 안 됨
             ("999990", 9, "20260725", "WICS"),     # 보조 출처밖에 없음
             ("999991", 2, "20260725", "NAVER")])
        con.commit()
    finally:
        con.close()


def demo():
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "themes.db"
        _seed(path)
        labels = analysis_db.active_theme_labels(path)

        merged = labels["078600"]
        assert "2차전지" in merged, f"수동 등록이 빠짐: {merged}"
        assert "태양광에너지" in merged and "우주태양광" in merged, (
            f"네이버 세부 테마가 수동 등록에 가려짐: {merged}")
        assert "화학" not in merged, f"넓은 업종이 섞임: {merged}"
        assert len(merged) == len(set(merged)), f"중복: {merged}"

        # 수동·네이버가 둘 다 없을 때만 보조 출처로 넘어간다.
        assert labels["999990"] == ("화학",), labels["999990"]
        # 우선주는 단기 수급 묶음으로 항상 덧붙는다.
        assert "우선주" in labels["999991"], labels["999991"]
    print("ok")


if __name__ == "__main__":
    demo()
