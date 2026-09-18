# -*- coding: utf-8 -*-
"""네이버 종목뉴스에 걸린 기사는 제목에 종목명이 없어도 테마에 붙는지 검사.

검색 API는 종목명으로 찾는 것이라 「[특징주] 전기장비株, 아마존 AI 전력장비
계약」이 선도전기(007610) 재료인지 판정할 수 없다. 그래서 「제목에 종목명이
있을 때만」 규칙으로 잘랐고, 2026-09-18 그 기사가 상한가 재료였는데 테마 칸에
한 줄도 안 떴다.

네이버 증권 종목뉴스(`m.stock.naver.com/api/news/stock/<코드>`)는 네이버가
그 종목에 직접 매핑한 목록이다. 거기 있으면 제목에 이름이 없어도 그 종목
기사가 맞다. AI 판정이 아니라 네이버의 판정을 그대로 쓴다.

저장과 소급이 **같은 조건**이어야 한다. 어긋나면 소급이 저장 때 붙인 연결을
도로 지운다(`backfill_news_themes`의 삭제 단계).
"""
import shutil
import sqlite3
import tempfile
from pathlib import Path

from analysis_db import (
    active_theme_labels, backfill_news_themes, initialize, save_news_items)
from naver_news_api import title_key

# 제목에 종목명이 없다. 지금까지 버려지던 모양 그대로다.
SECTOR = {
    "source_item_key": "key-sector",
    "canonical_url": "https://example.test/sector",
    "original_url": "https://example.test/sector",
    "naver_url": "https://n.example.test/sector",
    "publisher": "example.test",
    "published_at_source": "2026-09-18T10:12:00+09:00",
    "title": "[특징주] 전기장비株, 아마존 AI 데이터센터 계약.. '불기둥'",
    "summary": "선도전기는 오전 9시55분 전장대비 29.96% 상승한 4945원으로 "
               "상한가를 기록했다.",
    "current_hash": "hash-sector",
    "duplicate_key": title_key(
        "[특징주] 전기장비株, 아마존 AI 데이터센터 계약.. '불기둥'"),
    "material_type": "기타",
    "material_confidence": 0.35,
}
# 종목명이 요약에만 스친 시황. 네이버 종목뉴스에도 없다 → 계속 버려야 한다.
NOISE = {
    **SECTOR,
    "source_item_key": "key-noise",
    "canonical_url": "https://example.test/noise",
    "original_url": "https://example.test/noise",
    "title": "모더나 암백신 3상 성공에…삼양바이오팜 상한가",
    "summary": "선도전기 등 전력주도 강세였다. 데이터센터 기대가 이어졌다.",
    "current_hash": "hash-noise",
    "duplicate_key": title_key("모더나 암백신 3상 성공에…삼양바이오팜 상한가"),
}


def _prepare(db_path: Path):
    initialize(db_path)
    with sqlite3.connect(db_path) as connection:
        connection.execute(
            """INSERT OR IGNORE INTO stocks(stock_code, stock_name, updated_at)
               VALUES ('007610', '선도전기', '2026-09-18T00:00:00+09:00')""")


def demo():
    temp = Path(tempfile.mkdtemp(prefix="naver_stock_page_"))
    db_path = temp / "test.db"
    try:
        _prepare(db_path)
        rows = [SECTOR, NOISE]

        # 1) 종목뉴스 목록이 없으면 예전 그대로 — 둘 다 안 붙는다.
        save_news_items("007610", "선도전기", rows, db_path=db_path)
        assert active_theme_labels(db_path).get("007610") is None, \
            active_theme_labels(db_path)

        # 2) 네이버가 섹터 기사만 그 종목에 걸어 뒀다면 그것만 붙는다.
        save_news_items(
            "007610", "선도전기", rows, db_path=db_path,
            naver_linked_keys={SECTOR["duplicate_key"]})
        labels = active_theme_labels(db_path).get("007610") or ()
        assert "데이터센터" in labels, labels
        assert "바이오" not in labels, labels  # 시황 기사가 새지 않았다

        # 3) 연결 방식이 기록돼야 소급이 같은 판단을 할 수 있다.
        with sqlite3.connect(db_path) as connection:
            connection.row_factory = sqlite3.Row
            methods = {
                r["match_method"]
                for r in connection.execute(
                    "SELECT match_method FROM news_stock_maps")
            }
        assert methods == {"NAVER_STOCK_PAGE", "QUERY_STOCK"}, methods

        # 4) 소급이 그 연결을 도로 지우면 안 된다. 저장과 소급이 같은
        #    조건을 봐야 한다 — 어긋나면 삭제 단계가 방금 붙인 것을 지운다.
        backfill_news_themes(db_path=db_path)
        after = active_theme_labels(db_path).get("007610") or ()
        assert "데이터센터" in after, after
        print("ok (네이버 종목뉴스 면제)")
    finally:
        shutil.rmtree(temp, ignore_errors=True)


if __name__ == "__main__":
    demo()
