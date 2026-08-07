# -*- coding: utf-8 -*-
"""상한가 종목 기사에서 사전에 없는 표현을 뽑아 본다.

`theme_keywords.NEWS_THEME_KEYWORDS`는 손으로 채운다. 새 재료가 나올 때마다
빠진 것을 뒤늦게 알아채는 대신, 상한가 간 종목의 당일 기사를 거꾸로 훑어
"자주 붙는데 사전에 없는 말"을 빈도순으로 보여 준다. 사람이 보고 고른다.

    .\\.venv\\Scripts\\python.exe theme_keyword_candidates.py [일수] [최소건수]
"""
import re
import sys
from collections import Counter
from contextlib import closing
from datetime import datetime, timedelta

from analysis_db import (
    DB_PATH, NEWS_THEME_MAX_CODES, connect, split_ls_news_stock_codes,
)
from theme_keywords import match_news_themes

# 시황·시세속보·리포트는 왜 올랐는지 알려 주지 않는다. 후보에서 뺀다.
_SKIP = re.compile(
    r"\[리포트 브리핑\]|\[클릭 ?e종목\]|목표가|증시요약|시황|급등락주|"
    r"상한가 진입|상한가 출발|상승률 상위|거래량 (?:급증|증가|갱신)|"
    r"장중수급포착|순매수행진|투자경고|투자주의|투자유의|^<[유코넥]>|"
    r"상승폭 확대|하락폭 확대|급등세|급락세|거래일 연속|연속 상승|"
    r"신고가|신저가|반등|소폭|약세|보합|변동성완화장치|VI 발동")
# 사건과 무관한 조사·수식어. 표현 후보에서 제외한다.
_STOP = {"관련주", "특징주", "종목", "코스피", "코스닥", "장중", "오늘",
         "기록중", "돌파", "강세", "약세", "급등", "급락", "상승", "하락",
         "거래일", "연속", "확대", "소식에", "상한가", "만에", "하루"}
_WORD = re.compile(r"[가-힣]{2,}")


def limit_up_titles(days: int, db_path=DB_PATH) -> list[str]:
    """최근 상한가 종목의 그날 기사 제목. 종목 1~2개짜리만 본다."""
    since = (datetime.now() - timedelta(days=days)).strftime("%Y%m%d")
    with closing(connect(db_path)) as connection:
        events = {
            (row["trade_date"], row["stock_code"])
            for row in connection.execute(
                "SELECT trade_date, stock_code FROM limit_up_events "
                "WHERE trade_date>=?", (since,)).fetchall()
        }
        if not events:
            return []
        rows = connection.execute(
            "SELECT news_date, title, stock_code, related_stock_codes "
            "FROM ls_realtime_news WHERE news_date>=?", (since,)).fetchall()
    titles = []
    for row in rows:
        title = str(row["title"] or "")
        if not title or _SKIP.search(title):
            continue
        codes = list(dict.fromkeys(
            split_ls_news_stock_codes(row["stock_code"])
            + split_ls_news_stock_codes(row["related_stock_codes"])))
        if not (1 <= len(codes) <= NEWS_THEME_MAX_CODES):
            continue
        if any((row["news_date"], code) in events for code in codes):
            titles.append(title)
    return titles


def candidates(titles: list[str], minimum: int) -> list[tuple[str, int]]:
    """사전에 안 걸리는 제목에서 반복되는 2~3어절 표현을 센다."""
    counter = Counter()
    for title in titles:
        if match_news_themes(title):
            continue  # 이미 사전이 잡는 기사
        words = [w for w in _WORD.findall(title) if w not in _STOP]
        for size in (2, 3):
            for i in range(len(words) - size + 1):
                counter[" ".join(words[i:i + size])] += 1
    return [(phrase, n) for phrase, n in counter.most_common() if n >= minimum]


def demo():
    days = int(sys.argv[1]) if len(sys.argv) > 1 else 30
    minimum = int(sys.argv[2]) if len(sys.argv) > 2 else 3
    titles = limit_up_titles(days)
    print(f"최근 {days}일 상한가 종목 기사 {len(titles):,}건")
    uncovered = [t for t in titles if not match_news_themes(t)]
    covered = len(titles) - len(uncovered)
    rate = covered / len(titles) * 100 if titles else 0.0
    print(f"사전이 잡은 기사 {covered:,}건 ({rate:.1f}%) · 남은 {len(uncovered):,}건")
    print()
    found = candidates(titles, minimum)
    print(f"{minimum}건 이상 반복된 표현 {len(found)}개")
    for phrase, n in found[:40]:
        print(f"  {n:>4}  {phrase}")


if __name__ == "__main__":
    demo()
