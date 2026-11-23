# -*- coding: utf-8 -*-
"""오른 이유가 제목에서 제대로 잘리고, 틀린 이유는 비워 두는지 본다.

제목은 2026-11-23 실제 기사다. 특징주 기사는 상한가보다 10~30분 늦게 나온다.
"""
import shutil
import sqlite3
import tempfile
from pathlib import Path

import analysis_db
import gui
from analysis_db import extract_move_reason, initialize, limit_up_reasons

CASES = (
    ("큐로셀", "[특징주] 림카토 건보 적용 소식에…큐로셀 ‘상한가’", "림카토 건보 적용 소식"),
    ("큐로셀", "[특징주] 첫 국산 CAR-T '림카토' 건보 진입…큐로셀, 장 초반 상한가",
     "첫 국산 CAR-T '림카토' 건보 진입"),
    ("큐로셀", "큐로셀, 국산 항암제 '림카토' 건보 적용에 '상한가'", "국산 항암제 '림카토' 건보 적용"),
    ("휴림로봇", "[특징주] 휴림로봇, 삼성전기 로봇 공급 본계약 체결에 23%↑", "삼성전기 로봇 공급 본계약 체결"),
    ("금양그린파워", "[특징주] 금양그린파워, AI 데이터센터 2300억 공사 수주에 이틀째 상승세",
     "AI 데이터센터 2300억 공사 수주"),
    ("애드바이오텍", "[특징주] 한중 교류 확대 기대감에 화장품株 '애드바이오텍·뷰티스킨' 강세",
     "한중 교류 확대 기대감에 화장품株"),
    ("큐로셀", "큐로셀, 장 초반 상한가", ""),            # 움직임 말뿐이면 이유 없음
    ("휴림로봇", "[특징주] 한국화장품, 한중 관계 개선 기대에 7%대 강세", ""),  # 다른 종목
)


def row(connection, key, source, codes, title, published):
    connection.execute(
        """INSERT INTO ls_realtime_news (news_key, news_date, source_name, stock_code,
               related_stock_codes, title, published_at, received_at, updated_at)
           VALUES (?, '20261123', ?, ?, '[]', ?, ?, '', '')""",
        (key, source, codes, title, "2026-11-23T" + published))


def demo():
    for name, title, expected in CASES:
        got = extract_move_reason(title, name)
        assert got == expected, (title, got, expected)

    temp = Path(tempfile.mkdtemp())
    try:
        db = initialize(temp / "t.db")
        with sqlite3.connect(db) as connection:
            for code, name in (("372320", "큐로셀"), ("090710", "휴림로봇"),
                               ("078590", "휴림에이텍"), ("000660", "SK하이닉스"),
                               ("179530", "애드바이오텍"), ("123690", "한국화장품")):
                connection.execute(
                    "INSERT INTO stocks (stock_code, stock_name, updated_at) "
                    "VALUES (?,?,'')", (code, name))
            # 큐로셀: 일반 기사는 안 쓰고 특징주(T)만 쓴다.
            row(connection, "1", "한국경제", "000000372320",
                "큐로셀, 국산 항암제 '림카토' 건보 적용에 '상한가'", "09:11:00+09:00")
            row(connection, "2", "이투데이", "000000372320",
                "[특징주] 림카토 건보 적용 소식에…큐로셀 ‘상한가’", "09:14:00+09:00")
            # 휴림에이텍: 기계가 찍은 속보뿐 → 이유 없음.
            row(connection, "3", "서울경제", "000000078590",
                "<코>휴림에이텍, 상한가 진입.. +29.81% ↑", "09:14:00+09:00")
            # 하락 기사는 뺀다.
            row(connection, "4", "뉴스핌", "000000000660",
                "[특징주] 삼전닉스, 美 반도체주 폭락 여파에 프리마켓서 6% 급락", "08:31:00+09:00")
            # 테마 기사는 관련 종목코드가 5개 붙는다. 제목에 이름이 있는 종목만 받는다.
            connection.execute(
                """INSERT INTO ls_realtime_news (news_key, news_date, source_name,
                       stock_code, related_stock_codes, title, published_at,
                       received_at, updated_at)
                   VALUES ('6','20261123','뉴스핌','179530',
                       '["179530","123690","161890"]',
                       '[특징주] 한중 교류 확대 기대감에 화장품株 ''애드바이오텍·뷰티스킨'' 강세',
                       '2026-11-23T09:57:00+09:00','','')""")
            # 휴림로봇: 재료급 공시만 있으면 공시(D)로 채운다. 판정은 호출부가 준다.
            row(connection, "5", "한국거래소", "000000090710",
                "(주)휴림로봇 단일판매ㆍ공급계약체결", "09:40:00+09:00")
        out = limit_up_reasons("20261123", db, is_material=lambda s, t: "공급계약" in t)
        assert out["372320"][:2] == ("림카토 건보 적용 소식", "T"), out
        assert "078590" not in out and "000660" not in out, out
        assert out["372320"][2].startswith("[특징주]"), out
        assert out["090710"][:2] == ("단일판매ㆍ공급계약체결", "D"), out
        assert out["179530"][:2] == ("한중 교류 확대 기대감에 화장품株", "T"), out
        assert "123690" not in out and "161890" not in out, out
        none = limit_up_reasons("20261123", db)            # 공시 판정이 없으면 D도 없다
        assert "090710" not in none, none
    finally:
        shutil.rmtree(temp, ignore_errors=True)

    # 칸에 맨 앞으로 붙고, 이유가 없으면 예전 모양 그대로다.
    text = gui._theme_cell_text(("바이오",), (), reason=("림카토 건보 적용 소식", "T", "t"))
    assert text == "▶림카토 건보 적용 소식 | 바이오", text
    assert gui._theme_cell_text(("바이오",), ()) == "바이오"
    assert gui._theme_cell_text(("바이오",), (), has_news=False) == "?바이오"
    print("ok (오른 이유: 제목에서 이유 절 추출, 특징주 우선, 하락·속보 제외, 칸 맨 앞 표시)")


if __name__ == "__main__":
    demo()
