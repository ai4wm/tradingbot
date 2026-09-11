# -*- coding: utf-8 -*-
"""네이버 웹뷰 줌 맞춤과 자동 새로고침 판정 검사.

2026-09-11 네이버 증권이 새 화면으로 바뀌었다. 좌우 패널이 붙어 요구 폭이
창보다 커지면서 가로 스크롤바가 생겼고, 주소가 바뀌면서 자동 새로고침
판정(호스트 `finance.naver.com` 고정)이 조용히 안 걸렸다.
"""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QUrl

from ui.stock_news_tab import (
    NAVER_STOCK_PAGES, NEWS_WEB_MIN_ZOOM, _fit_zoom,
    _is_news_web_auto_reload_url, naver_stock_url)

NEW = "https://stock.naver.com/domestic/stock/005930/discussion?filter=all"
OLD = "https://finance.naver.com/item/board.naver?code=005930"


def check_zoom():
    # 가로 스크롤이 없으면 건드리지 않는다.
    assert _fit_zoom(1.0, 1200, 1200) == 1.0
    # 스크롤바 폭 정도 넘치는 것도 그냥 둔다. 건드려 봐야 눈에 안 보인다.
    assert _fit_zoom(1.0, 1205, 1200) == 1.0

    # 1278짜리 창에 1500을 요구하면 그만큼 줄인다(2026-09-11 실제 화면).
    zoom = _fit_zoom(1.0, 1500, 1278)
    assert abs(zoom - 1278 / 1500) < 1e-9, zoom
    # 줄인 배율에서 다시 재도 수렴한다. 진동하면 화면이 떨린다.
    assert _fit_zoom(zoom, 1278, 1278) == zoom

    # 두 번째 측정에서 더 넓어졌으면 더 줄인다. 키우지는 않는다.
    tighter = _fit_zoom(zoom, 1600, 1278)
    assert tighter < zoom, (tighter, zoom)
    assert _fit_zoom(zoom, 800, 1278) == zoom, "여유가 생겨도 키우지 않는다"

    # 아무리 넓어도 읽을 수 있는 선에서 멈춘다.
    assert _fit_zoom(1.0, 99_999, 1278) == NEWS_WEB_MIN_ZOOM
    # 폭을 못 재면(숨은 탭 등) 그대로 둔다.
    assert _fit_zoom(1.0, 1500, 0) == 1.0
    print("줌 맞춤   : 줄이기만 하고 최소 배율에서 멈춤")


def check_auto_reload():
    assert _is_news_web_auto_reload_url(QUrl(NEW)), "새 종목토론 주소"
    assert _is_news_web_auto_reload_url(QUrl(OLD)), "옛 주소도 계속 본다"
    assert _is_news_web_auto_reload_url(
        QUrl("https://stock.naver.com/domestic/stock/005930/news")), "뉴스·공시"
    # 목록이 아닌 탭은 새로고침 대상이 아니다.
    assert not _is_news_web_auto_reload_url(
        QUrl("https://stock.naver.com/domestic/stock/005930/price"))
    # 글 하나를 읽는 중에 새로고침하면 안 된다.
    assert not _is_news_web_auto_reload_url(
        QUrl("https://stock.naver.com/domestic/stock/005930/discussion/4292683"))
    # 남의 사이트는 경로가 비슷해도 아니다.
    assert not _is_news_web_auto_reload_url(
        QUrl("https://example.com/domestic/stock/005930/discussion"))
    assert not _is_news_web_auto_reload_url(
        QUrl("https://finance.naver.com/item/main.naver?code=005930"))
    print("자동 새로고침: 새 주소·옛 주소 둘 다 걸림")


def check_pages():
    """메뉴 주소는 저장한 실제 페이지(프리티 006490)에서 확인한 것이다."""
    expected = {
        "차트·시세": "https://stock.naver.com/domestic/stock/006490/price",
        "종목토론": NEW.replace("005930", "006490"),
        "종목분석": "https://stock.naver.com/domestic/stock/006490/info",
        "리포트": "https://stock.naver.com/domestic/stock/006490/research",
        "뉴스·공시": "https://stock.naver.com/domestic/stock/006490/news",
        "공매도현황": "https://stock.naver.com/domestic/stock/006490/shortTrade",
        "인사이트": "https://stock.naver.com/domestic/stock/006490/investmentinfo",
    }
    built = {title: naver_stock_url("006490", page)
             for title, page in NAVER_STOCK_PAGES}
    assert built == expected, built
    # 토론만 전체보기 조건이 붙는다. 나머지에 붙이면 주소가 달라진다.
    assert "?filter=all" not in naver_stock_url("006490", "news")
    print(f"메뉴 주소  : {len(built)}개 모두 일치")


def demo():
    check_zoom()
    check_auto_reload()
    check_pages()
    print("ok")


if __name__ == "__main__":
    demo()
