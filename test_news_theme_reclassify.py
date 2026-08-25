# -*- coding: utf-8 -*-
"""뉴스 테마 소급이 지금 사전으로 재현 안 되는 연결을 지우는지 검사.

붙이기만 하던 시절에는 사전에서 뺀 판정이 DB에 그대로 남았다. 2026-08-25
이건산업이 '부동산 자산 매각 추진'으로 인수합병에 잡혔는데, 사전을 고친 뒤
소급을 돌려도 인수합병이 안 지워져 손으로 40건을 지워야 했다.
"""
import os
import sqlite3
import tempfile

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import analysis_db  # noqa: E402

TITLE = "[특징주] 이건산업, 시총 육박 부동산 자산 매각 추진 소식에 '↑'"
CODE = "008250"


def themes_of(path, code):
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
    from pathlib import Path
    db_path = Path(path)
    analysis_db.initialize(db_path)
    day = analysis_db.datetime.now().strftime("%Y%m%d")
    setup = sqlite3.connect(path)
    with setup as db:
        db.execute(
            "INSERT INTO stocks(stock_code, stock_name, updated_at)"
            " VALUES (?, ?, ?)", (CODE, "이건산업", day))
        # 같은 발행사 우선주와, 앞 5자리만 같은 남의 종목.
        db.execute(
            "INSERT INTO stocks(stock_code, stock_name, updated_at)"
            " VALUES (?, ?, ?)", ("00825K", "이건산업3우B", day))
        db.execute(
            "INSERT INTO stocks(stock_code, stock_name, updated_at)"
            " VALUES (?, ?, ?)", ("008259", "남의회사", day))
        db.execute(
            """INSERT INTO ls_realtime_news(
                   news_key, realkey, news_date, news_time, source_id,
                   source_name, stock_code, title, body, related_stock_codes,
                   body_size, original_url, original_url_source,
                   original_url_confidence, original_url_checked_at,
                   received_at, updated_at)
               VALUES ('k1','r1',?, '090758','1','테스트',?,?,'','',0,
                       '','',0,'',?,?)""", (day, CODE, TITLE, day, day))
        # 옛 사전이 남긴 찌꺼기. 지금 제목으로는 다시 나오지 않는다.
        db.execute(
            "INSERT INTO themes(theme_name, description, updated_at)"
            " VALUES ('인수합병','','')")
        theme_id = db.execute(
            "SELECT theme_id FROM themes WHERE theme_name='인수합병'"
        ).fetchone()[0]
        db.execute(
            """INSERT INTO stock_themes(stock_code, theme_id, valid_from,
                   valid_to, source, confidence)
               VALUES (?, ?, ?, NULL, 'NEWS', 0.5)""", (CODE, theme_id, day))
        # 다른 출처는 재분류가 건드리면 안 된다.
        db.execute(
            "INSERT INTO themes(theme_name, description, updated_at)"
            " VALUES ('건축자재','','')")
        wics = db.execute(
            "SELECT theme_id FROM themes WHERE theme_name='건축자재'"
        ).fetchone()[0]
        db.execute(
            """INSERT INTO stock_themes(stock_code, theme_id, valid_from,
                   valid_to, source, confidence)
               VALUES (?, ?, ?, NULL, 'WICS', 0.9)""", (CODE, wics, day))
    setup.close()

    assert themes_of(path, CODE) == ["건축자재", "인수합병"], themes_of(path, CODE)
    analysis_db.backfill_news_themes(db_path=db_path)
    after = themes_of(path, CODE)
    assert "자산매각" in after, after        # 새 사전이 붙인다
    assert "인수합병" not in after, after    # 옛 판정은 지운다
    assert "건축자재" in after, after        # 다른 출처는 그대로
    # 회사 사건은 우선주에도 붙는다. 뉴스는 보통주 코드만 싣는다.
    assert themes_of(path, "00825K") == ["자산매각"], themes_of(path, "00825K")
    # 앞 5자리만 같고 이름이 다르면 남의 종목이다.
    assert themes_of(path, "008259") == [], themes_of(path, "008259")

    # 두 번 돌려도 결과가 같다.
    analysis_db.backfill_news_themes(db_path=db_path)
    assert themes_of(path, CODE) == after, themes_of(path, CODE)
    os.unlink(path)
    print("ok")


if __name__ == "__main__":
    demo()
