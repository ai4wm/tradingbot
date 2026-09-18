# -*- coding: utf-8 -*-
"""실시간 뉴스 등락률 칸이 보이는 행만 조회하는지 확인한다.

공시가 떠도 그 종목이 지금 얼마나 올랐는지가 화면에 없었다. 그런데 웹소켓
실시간 등록은 95칸(`config.REAL_REG_LIMIT`)뿐이고 그것은 매매 화면이 쓴다.
2026-09-18 실측으로 뉴스 500행에 실린 고유 종목만 252개라, 등록하면 매매
종목이 밀려난다. 그래서 REST `ka10095`로 **보이는 행만** 주기 조회한다.

한 화면이 20~30행이라 보통 한 번으로 끝난다(ka10095는 100종목까지 싣고
REST는 초당 1건이 상한이다). 스크롤을 내리면 그 자리 것을 조회한다.
"""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtWidgets import (  # noqa: E402
    QApplication, QTableWidget, QTableWidgetItem)

from gui import BLUE, RED, NumericTableWidgetItem  # noqa: E402
from ui.realtime_news_tab import (  # noqa: E402
    DISCLOSURE_TINT_COLUMNS, LS_NEWS_RATE_COLUMN, LS_NEWS_RATE_MAX_CODES,
    RealtimeNewsTabMixin as Mixin)


def _screen(rows):
    """행만 채운 최소 화면. AnalysisWindow 전체를 세우지 않는다.

    실제 화면과 같이 **새 행이 맨 위(0행)로 들어가고** 도착 순번은 늘어난다.
    """
    table = QTableWidget(0, 6)
    for seq, (code, name) in enumerate(rows, 1):
        table.insertRow(0)
        table.setItem(0, 0, NumericTableWidgetItem(str(seq), seq))
        table.setItem(0, 1, QTableWidgetItem(f"09:{seq:02d}:00"))
        stock = QTableWidgetItem(name)
        stock.setData(Qt.ItemDataRole.UserRole, [code] if code else [])
        table.setItem(0, 2, stock)
        table.setItem(0, 3, QTableWidgetItem("제목"))
        table.setItem(0, 4, QTableWidgetItem("한국거래소"))
        table.setItem(0, LS_NEWS_RATE_COLUMN,
                      NumericTableWidgetItem("", 0.0))
    screen = type("Fake", (), {
        name: Mixin.__dict__[name] for name in (
            "_ls_news_visible_codes", "_paint_ls_news_rate",
            "_ls_news_oldest_row", "_sort_ls_news_table",
            "_renumber_ls_news_table", "_apply_ls_news_search_filter",
            "_ls_news_row_matches_search")})()
    screen._ls_news_table = table
    screen._ls_news_rates = {}
    screen._ls_news_sort_column = -1
    screen._ls_news_sort_desc = True
    screen._ls_news_search = _EmptySearch()
    screen._update_ls_news_count = lambda *a: None
    return screen


class _EmptySearch:
    """검색어가 없는 상태. 정렬 뒤 필터 재적용 경로만 태운다."""

    def text(self):
        return ""


def demo_visible_only():
    """보이는 행만 센다. 숨긴 행과 화면 밖은 조회하지 않는다."""
    QApplication.instance() or QApplication([])
    rows = [(f"{n:06d}", f"종목{n}") for n in range(1, 401)]
    screen = _screen(rows)
    table = screen._ls_news_table
    table.verticalHeader().setDefaultSectionSize(24)
    table.resize(800, 240)   # 24px * 10행
    table.show()

    codes = screen._ls_news_visible_codes()
    assert codes, codes
    # 400행이 다 오면 조회가 4번으로 늘어난다. 한 화면치만 와야 한다.
    assert len(codes) <= 20, len(codes)
    # 맨 위는 가장 나중에 들어온 행이다.
    assert codes[0] == "000400", codes[:3]

    # 숨긴 행은 세지 않는다 — 검색식이 걸러 낸 행까지 조회할 이유가 없다.
    table.setRowHidden(0, True)
    assert "000400" not in screen._ls_news_visible_codes()

    # 종목코드가 없는 행(시장 안내 공시)은 건너뛴다.
    blank = _screen([("", "-"), ("005930", "삼성전자")])
    blank._ls_news_table.resize(800, 240)
    blank._ls_news_table.show()
    assert blank._ls_news_visible_codes() == ["005930"]
    print(f"ok (보이는 행만 조회) {len(codes)}종목")


def demo_max_codes():
    """ka10095 한 번에 실리는 100종목을 넘기지 않는다."""
    QApplication.instance() or QApplication([])
    screen = _screen([(f"{n:06d}", f"종목{n}") for n in range(1, 301)])
    table = screen._ls_news_table
    table.verticalHeader().setDefaultSectionSize(4)
    table.resize(800, 4000)  # 일부러 300행을 다 보이게 한다
    table.show()
    codes = screen._ls_news_visible_codes()
    assert len(codes) == LS_NEWS_RATE_MAX_CODES, len(codes)
    print(f"ok (한 번에 {LS_NEWS_RATE_MAX_CODES}종목까지)")


def demo_paint():
    """받은 값만 그린다. 색은 상승 빨강·하락 파랑이다."""
    QApplication.instance() or QApplication([])
    # 새 행이 위로 들어가므로 표시 순서는 선도전기·동양·앤씨앤이다.
    screen = _screen([("092600", "앤씨앤"), ("001520", "동양"),
                      ("007610", "선도전기")])
    screen._ls_news_rates = {"092600": 29.93, "001520": -3.5}

    for row in range(3):
        screen._paint_ls_news_rate(row)
    cell = lambda row: screen._ls_news_table.item(row, LS_NEWS_RATE_COLUMN)

    assert cell(2).text() == "+29.93", cell(2).text()   # 앤씨앤
    assert cell(2).foreground().color() == RED
    assert cell(1).text() == "-3.50", cell(1).text()    # 동양
    assert cell(1).foreground().color() == BLUE
    # 아직 안 받은 종목은 빈칸이다. 0.00으로 채우면 하한가와 구별이 안 된다.
    assert cell(0).text() == "", cell(0).text()         # 선도전기

    # 정렬용 숫자가 함께 들어가야 한다(문자열 정렬은 -3.5 > +29.93).
    assert cell(2).data(Qt.ItemDataRole.UserRole) == 29.93
    assert cell(1) < cell(2)
    print("ok (등락률 표시·색·정렬)")


def demo_sort():
    """머리글을 누르면 그 칸으로 정렬하고, 번호 칸이 원래 순서로 되돌린다."""
    QApplication.instance() or QApplication([])
    screen = _screen([("092600", "앤씨앤"), ("001520", "동양"),
                      ("007610", "선도전기")])
    screen._ls_news_rates = {
        "092600": 29.93, "001520": -3.50, "007610": 12.00}
    for row in range(3):
        screen._paint_ls_news_rate(row)
    names = lambda: [screen._ls_news_table.item(r, 2).text()
                     for r in range(3)]
    # 새 행이 위로 들어갔으니 처음은 도착 역순이다.
    assert names() == ["선도전기", "동양", "앤씨앤"], names()

    screen._sort_ls_news_table(LS_NEWS_RATE_COLUMN)
    assert names() == ["앤씨앤", "선도전기", "동양"], names()
    # 같은 칸을 다시 누르면 뒤집는다.
    screen._sort_ls_news_table(LS_NEWS_RATE_COLUMN)
    assert names() == ["동양", "선도전기", "앤씨앤"], names()

    # 번호 칸은 화면 번호가 아니라 도착 순번으로 정렬한다. 화면 번호로
    # 정렬하면 이미 보이는 순서 그대로라 원래 순서로 못 돌아온다.
    screen._sort_ls_news_table(0)
    assert names() == ["선도전기", "동양", "앤씨앤"], names()
    print("ok (정렬 · 번호 칸으로 원래 순서 복귀)")


def demo_trim_drops_oldest():
    """500행을 넘겨 지울 때는 맨 아랫행이 아니라 가장 먼저 온 행이다.

    등락률로 정렬해 두면 맨 아랫행은 제일 많이 내린 종목일 뿐이다. 거기를
    지우면 방금 들어온 뉴스가 날아간다.
    """
    QApplication.instance() or QApplication([])
    screen = _screen([("092600", "앤씨앤"), ("001520", "동양"),
                      ("007610", "선도전기")])
    screen._ls_news_rates = {
        "092600": 29.93, "001520": -3.50, "007610": 12.00}
    for row in range(3):
        screen._paint_ls_news_rate(row)

    # 정렬 전: 맨 아랫행이 곧 가장 오래된 행이다(앤씨앤이 1번으로 왔다).
    assert screen._ls_news_oldest_row() == 2
    assert screen._ls_news_table.item(2, 2).text() == "앤씨앤"

    # 등락률 오름차순으로 정렬하면 맨 아래가 앤씨앤(+29.93)이 된다.
    screen._sort_ls_news_table(LS_NEWS_RATE_COLUMN)
    screen._sort_ls_news_table(LS_NEWS_RATE_COLUMN)
    assert screen._ls_news_table.item(2, 2).text() == "앤씨앤"
    # 그래도 가장 오래된 행은 여전히 앤씨앤이라 답이 같아야 한다.
    oldest = screen._ls_news_oldest_row()
    assert screen._ls_news_table.item(oldest, 2).text() == "앤씨앤", oldest

    # 내림차순이면 맨 아래가 동양인데, 지울 것은 앤씨앤이다.
    screen._sort_ls_news_table(LS_NEWS_RATE_COLUMN)
    assert screen._ls_news_table.item(2, 2).text() == "동양"
    oldest = screen._ls_news_oldest_row()
    assert screen._ls_news_table.item(oldest, 2).text() == "앤씨앤", oldest
    print("ok (잘라 낼 행은 가장 먼저 온 행)")


def demo_column_layout():
    """등락률은 맨 뒤다. 앞에 끼우면 0~4를 박아 쓰는 자리가 다 어긋난다."""
    assert LS_NEWS_RATE_COLUMN == 5
    # 공시 행 배경이 새 칸에도 이어져야 줄이 끊겨 보이지 않는다.
    assert LS_NEWS_RATE_COLUMN in DISCLOSURE_TINT_COLUMNS
    # 신규 강조가 쓰는 시간·제목 칸은 여전히 빠져 있어야 한다.
    assert 1 not in DISCLOSURE_TINT_COLUMNS
    assert 3 not in DISCLOSURE_TINT_COLUMNS
    print("ok (칸 배치)")


if __name__ == "__main__":
    demo_visible_only()
    demo_max_codes()
    demo_paint()
    demo_sort()
    demo_trim_drops_oldest()
    demo_column_layout()
