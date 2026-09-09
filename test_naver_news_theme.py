# -*- coding: utf-8 -*-
"""네이버 뉴스의 테마 편입이 제목으로 걸러지는지 검사.

네이버 뉴스는 종목명으로 검색해 가져온다. 그래서 요약문에 이름만 스친 시황
기사가 잔뜩 딸려 온다. 그대로 테마에 붙이면 "원전주 급등" 마감시황이 그날
검색에 걸린 모든 종목에 원자력을 붙인다. 2026-09-07~09 실데이터에서 테마가
잡힌 47건 중 33건이 그런 것이었다.

`confidence`로는 못 거른다. 제목이 아니라 제목+요약을 보고 매기므로 종목명이
나열된 시황 기사도 0.9가 된다. 제목에 종목명이 그대로 있을 때만 붙인다.
"""
import os
import sqlite3
import tempfile
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import analysis_db  # noqa: E402

CODE, NAME = "224060", "더코디"
OTHER, OTHER_NAME = "225430", "케이엠제약"

# 제목에 종목명이 있다. 이 종목의 재료가 맞다.
REAL = "[풍문레이다] 범LG가 3세 구본호, 더코디 품는다…로봇·AI 신사업 시동"
# 종목명으로 검색해서 딸려 온 시황. 제목에 이 종목 이야기가 없다.
NOISE = "[EBN 데이터센터] 코스닥 상한가 6종목...원전주 한전기술·오르비텍 급등"


def _row(key: str, title: str, published: str) -> dict:
    return {"source_item_key": key, "canonical_url": f"https://x/{key}",
            "original_url": f"https://x/{key}", "naver_url": f"https://n/{key}",
            "publisher": "www.nbntv.kr", "published_at_source": published,
            "title": title, "summary": f"{NAME} {OTHER_NAME} 관련 요약",
            "current_hash": key, "duplicate_key": key,
            "material_type": "기타", "material_confidence": 0.5}


def themes_of(path: str, code: str) -> list[str]:
    # `with sqlite3.connect(...)`는 커밋만 하고 닫지 않는다. 윈도우에서 파일이
    # 잠긴 채 남아 시험 끝에 지우지 못한다.
    db = sqlite3.connect(path)
    try:
        return sorted(
            name for (name,) in db.execute(
                """SELECT t.theme_name FROM stock_themes st
                     JOIN themes t ON t.theme_id=st.theme_id
                    WHERE st.stock_code=? AND st.valid_to IS NULL""", (code,)))
    finally:
        db.close()


def demo():
    handle, path = tempfile.mkstemp(suffix=".db")
    os.close(handle)
    os.unlink(path)
    db_path = Path(path)
    analysis_db.initialize(db_path)
    now = analysis_db.datetime.now()
    day, stamp = now.strftime("%Y%m%d"), now.strftime("%Y-%m-%dT08:16:00+09:00")

    setup = sqlite3.connect(path)
    with setup as db:
        for code, name in ((CODE, NAME), (OTHER, OTHER_NAME)):
            db.execute(
                "INSERT INTO stocks(stock_code, stock_name, updated_at)"
                " VALUES (?, ?, ?)", (code, name, day))
    setup.close()

    # 같은 시황 기사가 두 종목의 검색에 다 걸린다. 실제로 그렇게 들어온다.
    analysis_db.save_news_items(
        CODE, NAME, [_row("a", REAL, stamp), _row("b", NOISE, stamp)],
        db_path=db_path)
    analysis_db.save_news_items(
        OTHER, OTHER_NAME, [_row("b", NOISE, stamp)], db_path=db_path)

    assert themes_of(path, CODE) == ["로봇", "인수합병"], themes_of(path, CODE)
    assert themes_of(path, OTHER) == [], themes_of(path, OTHER)
    print("저장     : 제목에 종목명 있는 기사만 붙음")

    # 소급도 같은 기준이어야 한다. 여기서 빠지면 삭제 단계가 방금 붙인 것을
    # 도로 지운다. 이 검사가 그 회귀를 잡는다.
    analysis_db.backfill_news_themes(db_path=db_path)
    assert themes_of(path, CODE) == ["로봇", "인수합병"], themes_of(path, CODE)
    assert themes_of(path, OTHER) == [], themes_of(path, OTHER)

    # 두 번 돌려도 같다.
    analysis_db.backfill_news_themes(db_path=db_path)
    assert themes_of(path, CODE) == ["로봇", "인수합병"], themes_of(path, CODE)
    print("소급     : 지웠다 붙였다 하지 않음")

    os.unlink(path)
    print("ok")


if __name__ == "__main__":
    demo()
