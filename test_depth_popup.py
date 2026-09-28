# -*- coding: utf-8 -*-
"""10호가 창이 조회 없이 웹소켓 값만으로 그려지는지 확인한다.

**줄 구조가 고정이다.** 위 열 줄은 언제나 매도 10~1단, 아래 열 줄은 매수
1~10단이고 그 사이에 기준선이 있다. 가격은 그 줄에 실려 바뀌고 현재가
표시만 실시간으로 오르내린다.

필요한 값이 전부 이미 온다.

    10호가·잔량   0D FID 41~80
    현재가·등락률  0B FID 10·12
    시·고·저      0B FID 16·17·18
    기준가·상하한  편입 조회(ka10095)로 STORED에 남아 있음

REST는 한 건도 안 나간다. `ka10095`는 5단까지만 주므로 편입 직후 한 틱은
6~10단이 비고 다음 호가 틱이 채운다.
"""
import os
import re

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from gui import BOOK_FIELDS, ConditionScreen, DepthPopup  # noqa: E402


def _book(price=18910, depth=10):
    """2026-09-23 영웅문 화면과 같은 모양(기준 15,520)."""
    base = 15520
    row = {"name": "레메디", "price": price, "base": base,
           "rate": (price - base) / base * 100,
           "upper": 20150, "lower": 10870,
           "open": 15800, "high": 19150, "low": 15200,
           "vol": 3832061, "prev_vol": 475000}
    for level in range(1, depth + 1):
        suffix = "" if level == 1 else str(level)
        row[f"ask_price{suffix}"] = 18920 + (level - 1) * 10
        row[f"ask_qty{suffix}"] = 100 * level
        row[f"bid_price{suffix}"] = 18900 - (level - 1) * 10
        row[f"bid_qty{suffix}"] = 200 * level
    return row


def _dusan():
    """현재가가 호가 사이에 낀 경우. 매도1 81,700 · 현재가 81,600 · 매수1 81,500."""
    row = {"name": "두산", "price": 81600, "base": 86500, "rate": -5.66,
           "upper": 112400, "lower": 60600, "open": 86100, "high": 86100,
           "low": 80500, "vol": 2635898, "prev_vol": 900000}
    asks = sorted([(81700, 1668), (81800, 3305), (81900, 2325), (82000, 7365),
                   (82100, 1442), (82200, 2197), (82300, 2174), (82400, 714),
                   (82500, 900), (82600, 1100)])
    bids = sorted([(81500, 7191), (81400, 6266), (81300, 10983), (81200, 3952),
                   (81100, 7589), (81000, 15323), (80900, 8149), (80800, 8867),
                   (80700, 500), (80600, 700)], reverse=True)
    for i, (p, q) in enumerate(asks, 1):
        s = "" if i == 1 else str(i)
        row[f"ask_price{s}"], row[f"ask_qty{s}"] = p, q
    for i, (p, q) in enumerate(bids, 1):
        s = "" if i == 1 else str(i)
        row[f"bid_price{s}"], row[f"bid_qty{s}"] = p, q
    return row


def demo_rows_are_fixed():
    """줄 자리가 고정이다. 현재가가 오르내려도 매도 10 · 매수 10 그대로."""
    QApplication.instance() or QApplication([])
    screen = ConditionScreen()
    popup = DepthPopup(screen, "387690", "레메디")
    popup.resize(300, 560)

    shapes, fonts = set(), set()
    for price in (18910, 18920, 18900, 18950, 18880):
        popup.set_book(_book(price=price))
        popup._refresh_text()
        sides = [s for s, _p, _q in popup._rows]
        shapes.add((len(popup._rows), sides.count("ask"), sides.count("bid")))
        fonts.add(re.search(r"font-size:(\d+)px", popup._label.text()).group(1))
    assert shapes == {(20, 10, 10)}, shapes
    assert len(fonts) == 1, fonts          # 글자도 안 흔들린다
    # 위 열 줄이 매도, 아래 열 줄이 매수다.
    assert all(s == "ask" for s, _p, _q in popup._rows[:10])
    assert all(s == "bid" for s, _p, _q in popup._rows[10:])
    # 매도는 먼 값에서 가까운 값으로, 매수는 그 반대다.
    assert popup._rows[0][1] > popup._rows[9][1]
    assert popup._rows[10][1] > popup._rows[19][1]
    popup.close()
    print(f"ok (매도 10 · 매수 10 고정 · 글자 {fonts.pop()}px)")


def demo_current_price_is_outlined():
    """현재가는 가격 칸만 노란 테두리다. 줄 전체를 칠하지 않는다."""
    QApplication.instance() or QApplication([])
    screen = ConditionScreen()
    popup = DepthPopup(screen, "387690", "레메디")
    popup.resize(300, 560)

    # 현재가가 매도 1단(18,920)과 같을 때.
    popup.set_book(_book(price=18920))
    popup._refresh_text()
    html = popup._label.text()
    assert html.count("#ffd24d") == 1, html.count("#ffd24d")
    assert "bgcolor='#ffe066'" not in html, "줄 전체를 칠하면 막대가 가린다"

    # 매수 1단(18,900)과 같을 때도 한 줄.
    popup.set_book(_book(price=18900))
    popup._refresh_text()
    assert popup._label.text().count("#ffd24d") == 1

    # 호가 사이에 끼면 테두리가 없다. 머리글에는 남는다.
    popup.set_book(_dusan())
    popup._refresh_text()
    html = popup._label.text()
    assert html.count("#ffd24d") == 0, "사이에 낀 현재가에 테두리가 붙었다"
    assert "81,600" in html, "머리글에 현재가가 없다"
    popup.close()
    print("ok (현재가 노란 테두리 · 사이에 끼면 머리글만)")


def demo_split_and_baseline():
    """매도와 매수를 가르는 가로선, 막대가 출발하는 세로 기준선."""
    QApplication.instance() or QApplication([])
    screen = ConditionScreen()
    popup = DepthPopup(screen, "387690", "레메디")
    popup.resize(300, 560)
    popup.set_book(_book())
    popup._refresh_text()
    html = popup._label.text()
    # 가로 기준선은 매수 첫 줄 위 한 자리뿐이고, 그 줄의 칸마다 붙는다.
    assert html.count("border-top:2px") == 6, html.count("border-top:2px")
    # 세로 기준선은 잔량이 0인 줄에도 있어야 0점이 보인다.
    assert html.count("border-right:1px") == 20
    assert html.count("border-left:1px") == 20
    popup.close()
    print("ok (매도·매수 가로선 · 막대 세로 기준선)")


def demo_fits_in_window():
    """글자가 창을 넘지 않는다. 넘으면 아래 두 줄이 안 보인다."""
    QApplication.instance() or QApplication([])
    screen = ConditionScreen()
    for width, height in ((300, 560), (200, 320), (170, 260), (420, 900)):
        popup = DepthPopup(screen, "387690", "레메디")
        popup.resize(width, height)
        popup.set_book(_book())
        popup._refresh_text()
        popup._label.resize(popup.size())
        need = (popup._label.heightForWidth(width)
                or popup._label.sizeHint().height())
        assert need <= height, (width, height, need)
        popup.close()
    print("ok (네 가지 크기에서 안 잘림)")


def demo_columns_are_fixed():
    """칸 폭을 못 박은 표라야 세로줄이 맞는다."""
    QApplication.instance() or QApplication([])
    screen = ConditionScreen()
    popup = DepthPopup(screen, "387690", "레메디")
    popup.resize(300, 560)
    popup.set_book(_book())
    popup._refresh_text()
    html = popup._label.text()
    assert "<table" in html and html.count("nowrap") == 4 * 20, \
        html.count("nowrap")
    assert "기준 15,520" in html and "시 15,800" in html
    assert "상한 20,150" in html and "하한 10,870" in html
    assert "+21.84%" in html, "현재가 등락률"   # (18910-15520)/15520
    popup.close()
    print("ok (칸 고정 표 · 머리글)")


def demo_partial_depth_keeps_places():
    """5단만 와도 자리는 스무 줄 그대로다. 아래 줄이 위로 밀려오지 않는다."""
    QApplication.instance() or QApplication([])
    screen = ConditionScreen()
    popup = DepthPopup(screen, "387690", "레메디")
    popup.resize(300, 560)
    popup.set_book(_book(depth=5))
    popup._refresh_text()
    assert len(popup._rows) == 20, len(popup._rows)
    quoted = [r for r in popup._rows if r[1]]
    assert len(quoted) == 10, len(quoted)
    # 못 받은 단은 빈 줄로 남는다(매도 10~6단, 매수 6~10단).
    assert popup._rows[0][1] == 0 and popup._rows[19][1] == 0
    popup.close()
    print("ok (5단만 와도 자리 유지)")


def demo_same_book_skips_paint():
    """값이 그대로면 다시 그리지 않는다. 붕괴 때 틱이 몰아친다."""
    QApplication.instance() or QApplication([])
    screen = ConditionScreen()
    popup = DepthPopup(screen, "387690", "레메디")
    popup.set_book(_book())
    popup._paint_timer.stop()
    popup.set_book(_book())
    assert not popup._paint_timer.isActive(), "같은 값인데 페인트를 걸었다"
    popup.set_book(_book(price=18930))
    assert popup._paint_timer.isActive(), "값이 바뀌었는데 안 걸었다"
    popup.close()
    print("ok (같은 값이면 페인트 건너뜀)")


def demo_opens_and_closes_with_row():
    """조건에서 빠지면 창도 닫힌다. 매수잔량 창과 목록이 섞이지 않는다."""
    QApplication.instance() or QApplication([])
    screen = ConditionScreen()
    screen.model.add_stock("387690", {"name": "레메디"})
    screen.on_tick("387690", _book())
    screen._open_depth_popup("387690")
    screen._open_bid_popup("387690")
    assert set(screen._depth_popups) == {"387690"}
    assert set(screen._bid_popups) == {"387690"}

    screen.model.rows.pop("387690")
    screen._close_orphan_depth_popups()
    assert screen._depth_popups == {}, screen._depth_popups
    print("ok (행이 사라지면 창도 닫힘)")


def demo_tick_updates_without_query():
    """호가 틱 하나로 갱신된다. 조회 경로를 타지 않는다."""
    QApplication.instance() or QApplication([])
    screen = ConditionScreen()
    screen.model.add_stock("387690", {"name": "레메디"})
    screen.on_tick("387690", _book())
    screen._open_depth_popup("387690")
    popup = screen._depth_popups["387690"]
    popup._paint_timer.stop()

    assert "bid_qty" in BOOK_FIELDS
    screen.on_tick("387690", {"bid_qty": 9999})
    assert popup._paint_timer.isActive(), "호가 틱인데 안 그렸다"
    assert popup._rows[10] == ("bid", 18900, 9999), popup._rows[10]
    popup.close()
    print("ok (호가 틱으로 갱신)")


if __name__ == "__main__":
    demo_rows_are_fixed()
    demo_current_price_is_outlined()
    demo_split_and_baseline()
    demo_fits_in_window()
    demo_columns_are_fixed()
    demo_partial_depth_keeps_places()
    demo_same_book_skips_paint()
    demo_opens_and_closes_with_row()
    demo_tick_updates_without_query()
