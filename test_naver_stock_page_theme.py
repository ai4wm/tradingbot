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


def demo_own_theme_mark():
    """종목이 이미 가진 테마는 제목에 종목명이 없어도 오늘 재료로 표시한다.

    선도전기는 `전력설비`와 `철도`를 함께 가지고 있다. 오늘 어느 쪽으로
    오르는지가 정보인데 지금은 둘 다 회색으로 나란히 있어 알 수 없다.

    **없던 테마는 만들지 않는다.** 그래서 「원전주 급등」 시황이 더코디에
    원자력을 붙이던 사고는 이 갈래로는 일어나지 않는다.
    """
    from analysis_db import news_theme_labels

    temp = Path(tempfile.mkdtemp(prefix="own_theme_mark_"))
    db_path = temp / "test.db"
    try:
        _prepare(db_path)
        # 선도전기에 전력설비·철도를 NAVER 분류로 심는다.
        from analysis_db import save_theme_snapshot
        save_theme_snapshot(
            [{"code": "1", "name": "전력설비", "members": ["007610"]},
             {"code": "2", "name": "철도", "members": ["007610"]}],
            "20260917", "NAVER", 0.95, db_path=db_path)

        base = dict(SECTOR)
        # 제목에 종목명이 없고, 종목이 이미 가진 테마가 제목에 걸린다.
        hit = {
            **base, "source_item_key": "k1",
            "canonical_url": "https://example.test/1",
            "title": "AI 데이터센터 전력 폭증… 전력설비주 매수세 몰린다",
            "summary": "선도전기 등이 강세다.",
            "current_hash": "h1", "current_hash2": "",
            "duplicate_key": title_key(
                "AI 데이터센터 전력 폭증… 전력설비주 매수세 몰린다"),
        }
        # 종목이 안 가진 테마 → 붙으면 안 된다(덱스터에 로봇을 붙이던 경우).
        miss = {
            **base, "source_item_key": "k2",
            "canonical_url": "https://example.test/2",
            "title": "로봇에 힘 싣는 삼성…전문 자회사 거래 3배 '쑥'",
            "summary": "선도전기도 언급됐다.",
            "current_hash": "h2",
            "duplicate_key": title_key(
                "로봇에 힘 싣는 삼성…전문 자회사 거래 3배 '쑥'"),
        }
        # 지수·시황은 어느 테마로 올랐는지 못 알려 준다.
        wrap = {
            **base, "source_item_key": "k3",
            "canonical_url": "https://example.test/3",
            "title": "코스피, 6900선 회복…2차전지·전력설비 강세",
            "summary": "선도전기도 올랐다.",
            "current_hash": "h3",
            "duplicate_key": title_key(
                "코스피, 6900선 회복…2차전지·전력설비 강세"),
        }
        save_news_items(
            "007610", "선도전기", [hit, miss, wrap], db_path=db_path)

        fresh = news_theme_labels(db_path, first_seen_on="20260918")
        marked = set(fresh.get("007610") or ())
        assert marked == {"전력설비"}, marked

        # 종목이 가진 테마 자체는 늘지 않았다. 표식만 붙었다.
        labels = set(active_theme_labels(db_path).get("007610") or ())
        assert labels == {"전력설비", "철도"}, labels
        assert "로봇" not in labels and "데이터센터" not in labels, labels

        # 소급이 그 표식을 지우면 안 된다.
        backfill_news_themes(db_path=db_path)
        after = set(
            news_theme_labels(db_path, first_seen_on="20260918")
            .get("007610") or ())
        assert after == {"전력설비"}, after
        print("ok (가진 테마만 오늘 재료로 표시)")
    finally:
        shutil.rmtree(temp, ignore_errors=True)


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
    demo_own_theme_mark()
