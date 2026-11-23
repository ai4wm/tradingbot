# -*- coding: utf-8 -*-
"""검색 결과 정합(`reconcile_news_search_results`)이 같은 결과를 내고 종목 색인으로 도는지 본다.

옛 질의는 이 출처의 기사 전체(4만 건)를 훑어 한 종목에 166ms가 걸렸고, 화면 이벤트 루프에서
종목마다 돌아 창이 끊겼다(2026-11-23 py-spy). 결과는 같아야 하고, 질의 계획은 종목 색인에서
시작해야 한다.
"""
import shutil
import sqlite3
import tempfile
from pathlib import Path

from analysis_db import initialize, reconcile_news_search_results

NOW = "2026-11-23T10:00:00+09:00"


def demo():
    temp = Path(tempfile.mkdtemp())
    try:
        db = initialize(temp / "t.db")
        with sqlite3.connect(db) as connection:
            source_id = connection.execute(
                "SELECT source_id FROM content_sources "
                "WHERE source_code='NAVER_SEARCH_NEWS'").fetchone()[0]
            for code in ("104620", "372320"):
                connection.execute(
                    "INSERT INTO stocks (stock_code, stock_name, updated_at) "
                    "VALUES (?, ?, ?)", (code, code, NOW))

            def news(key, published, codes):
                connection.execute(
                    """INSERT INTO news_items (source_id, source_item_key, published_at_source,
                           first_collected_at, last_seen_at) VALUES (?,?,?,?,?)""",
                    (source_id, key, published, NOW, NOW))
                news_id = connection.execute(
                    "SELECT news_id FROM news_items WHERE source_item_key=?", (key,)).fetchone()[0]
                for code in codes:
                    connection.execute(
                        "INSERT INTO news_stock_maps (news_id, stock_code, mapped_at) "
                        "VALUES (?,?,?)", (news_id, code, NOW))

            news("a", "2026-11-23T09:00:00+09:00", ["104620"])    # 이번 응답에 있음 → ACTIVE
            news("b", "2026-11-23T09:10:00+09:00", ["104620"])    # 응답에서 빠짐 → MISSING
            news("c", "2026-11-23T09:20:00+09:00", ["104620", "372320"])  # 종목이 둘 → 제외
            news("d", "2026-11-20T09:00:00+09:00", ["104620"])    # 응답 범위보다 오래됨 → 제외
            news("e", "2026-11-23T09:30:00+09:00", ["372320"])    # 다른 종목 → 제외

        rows = [{"source_item_key": "a", "published_at_source": "2026-11-23T09:00:00+09:00"},
                {"source_item_key": "z", "published_at_source": "2026-11-23T09:50:00+09:00"}]
        result = reconcile_news_search_results("104620", rows, db)
        assert result == {"active": 1, "missing": 1}, result
        with sqlite3.connect(db) as connection:
            status = dict(connection.execute(
                "SELECT source_item_key, removal_status FROM news_items"))
            plan = " ".join(row[3] for row in connection.execute(
                """EXPLAIN QUERY PLAN SELECT DISTINCT n.news_id, n.source_item_key
                     FROM news_stock_maps m CROSS JOIN news_items n ON n.news_id=m.news_id
                    WHERE m.stock_code=? AND n.source_id=? AND n.published_at_source>=?""",
                ("104620", source_id, "2026-11-23")))
        assert status["a"] == "ACTIVE" and status["b"] == "MISSING", status
        assert status["c"] == "ACTIVE" and status["d"] == "ACTIVE" and status["e"] == "ACTIVE", status
        assert "idx_news_stock_maps_code" in plan, plan      # 종목 색인에서 시작한다
    finally:
        shutil.rmtree(temp, ignore_errors=True)
    print("ok (검색 결과 정합: ACTIVE·MISSING 판정 그대로, 종목 색인으로 시작)")


if __name__ == "__main__":
    demo()
