# -*- coding: utf-8 -*-
"""10호가 창(호가중앙)이 조회 없이 웹소켓 값만으로 그려지는지 확인한다.

영웅문 8080 「호가중앙」과 같은 모양이다 — 가격 축이 호가단위로 연속이고
잔량이 있는 자리에만 숫자가 붙는다. 호가창에 실리는 것은 10단뿐이라
그 사이 빈 가격은 `krx_quote_axis`가 계산해서 채운다.

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

from gui import (  # noqa: E402
    BOOK_FIELDS, ConditionScreen, DepthPopup, krx_quote_axis, krx_tick_size)


def _book(price=18910, depth=10):
    """2026-09-23 영웅문 화면과 같은 모양(18,910 · 기준 15,520)."""
    base = 15520
    row = {"name": "레메디", "price": price, "base": base,
           "rate": (price - base) / base * 100,
           "upper": 20150, "lower": 10870,
           "open": 15800, "high": 19150, "low": 15200,
           "vol": 3832061, "prev_vol": 475000}
    for level in range(1, depth + 1):
        suffix = "" if level == 1 else str(level)
        row[f"ask_price{suffix}"] = price + level * 10
        row[f"ask_qty{suffix}"] = 100 * level
        row[f"bid_price{suffix}"] = price - level * 10
        row[f"bid_qty{suffix}"] = 200 * level
    return row


def demo_tick_size():
    """호가단위 표. 2023년 개정으로 코스피·코스닥이 같다."""
    assert krx_tick_size(1_500) == 1
    assert krx_tick_size(2_000) == 1        # 경계는 아래 구간
    assert krx_tick_size(2_010) == 5
    assert krx_tick_size(18_910) == 10
    assert krx_tick_size(20_000) == 10
    assert krx_tick_size(45_000) == 50
    assert krx_tick_size(900_000) == 1_000
    print("ok (호가단위)")


def demo_axis_fills_gaps():
    """빈 가격대를 채워 연속된 축을 만든다. 구간 경계도 넘어간다."""
    axis = krx_quote_axis(18_880, 18_910)
    assert axis == [18910, 18900, 18890, 18880], axis
    # 20,000 경계: 위는 50원, 아래는 10원. 20,010~20,040은 없는 호가다.
    axis = krx_quote_axis(19_990, 20_050)
    assert axis == [20050, 20000, 19990], axis
    # 50,000 경계: 5만 초과는 100원, 이하는 50원
    axis = krx_quote_axis(49_950, 50_100)
    assert axis == [50100, 50000, 49950], axis
    assert len(krx_quote_axis(1, 999_999, cap=30)) == 30   # 상한이 있다
    print("ok (가격 축 · 구간 경계)")


def demo_axis_is_centered_and_stable():
    """기준선이 가운데 고정이고, 현재가가 움직여도 자리와 글자가 안 변한다.

    축을 호가 폭에 맞춰 늘였다 줄이면 줄 수가 바뀌어 글자가 커졌다 작아지고
    현재가 줄도 위아래로 움직인다. 단타에서 눈이 그 줄을 따라다녀야 한다.
    """
    QApplication.instance() or QApplication([])
    screen = ConditionScreen()
    popup = DepthPopup(screen, "387690", "레메디")
    popup.resize(300, 560)

    seen = set()
    for price in (18910, 18950, 18870, 19200):
        popup.set_book(_book(price=price))
        popup._refresh_text()
        html = popup._label.text()
        rows = html.count("<tr")
        cell = re.search(r"font-size:(\d+)px", html).group(1)
        center = html[:html.index("#ffe066")].count("<tr")
        seen.add((rows, cell, center))
        # 기준선은 가운데 한 자리다.
        assert abs(center - rows // 2) <= 1, (price, center, rows)
    assert len(seen) == 1, seen      # 넷이 전부 같아야 한다
    popup.close()
    print(f"ok (기준선 고정 · 글자 고정) {seen}")


def demo_axis_fills_and_covers():
    """축이 호가단위로 연속이고 10단을 다 덮는다."""
    QApplication.instance() or QApplication([])
    screen = ConditionScreen()
    popup = DepthPopup(screen, "387690", "레메디")
    popup.resize(300, 560)
    popup.set_book(_book())
    popup._refresh_text()
    html = popup._label.text()

    axis = DepthPopup._axis(18910, 11)
    assert axis[11] == 18910, axis[9:14]          # 가운데가 기준가
    assert all(a - b == 10 for a, b in zip(axis, axis[1:]))
    # 매도 10단(19,010)과 매수 10단(18,810)이 축 안에 들어온다.
    assert 19010 in axis and 18810 in axis

    assert "기준 15,520" in html and "시 15,800" in html
    assert "상한 20,150" in html and "하한 10,870" in html
    assert "+21.84%" in html, "현재가 등락률"
    popup.close()
    print("ok (연속 축 · 10단 포함)")


def demo_fits_in_window():
    """글자가 창을 넘지 않는다. 넘으면 아래 두 줄이 안 보인다.

    막대를 글자로 그리던 때는 길이에 따라 옆 칸이 밀려 세로줄이 어긋났고,
    글자 크기를 높이만 보고 정해서 하한가·합계 줄이 창 밖으로 나갔다.
    """
    QApplication.instance() or QApplication([])
    screen = ConditionScreen()
    for width, height in ((300, 560), (200, 320), (170, 200), (420, 900)):
        popup = DepthPopup(screen, "387690", "레메디")
        popup.resize(width, height)
        popup.set_book(_book())
        popup._refresh_text()
        popup._label.resize(popup.size())
        need = popup._label.heightForWidth(width) or popup._label.sizeHint().height()
        assert need <= height, (width, height, need)
        popup.close()
    print("ok (네 가지 크기에서 안 잘림)")


def demo_row_columns_are_fixed():
    """호가 줄이 표로 나가고 현재가 줄에 배경이 붙는지."""
    QApplication.instance() or QApplication([])
    screen = ConditionScreen()
    popup = DepthPopup(screen, "387690", "레메디")
    popup.resize(300, 560)
    popup.set_book(_book())
    popup._refresh_text()
    html = popup._label.text()
    # 칸 폭을 못 박은 표라야 세로줄이 맞는다.
    assert "<table" in html and "nowrap" in html, html[:200]
    assert html.count("nowrap") % 4 == 0, html.count("nowrap")
    # 현재가 줄은 노랑 배경.
    assert html.count("#ffe066") == 1, html.count("#ffe066")
    popup.close()
    print("ok (칸 고정 표 · 현재가 줄 강조)")


def demo_partial_depth_survives():
    """편입 직후 5단만 온 상태에서도 그려진다."""
    QApplication.instance() or QApplication([])
    screen = ConditionScreen()
    popup = DepthPopup(screen, "387690", "레메디")
    popup.resize(300, 560)
    popup.set_book(_book(depth=5))
    popup._refresh_text()
    html = popup._label.text()
    assert popup._asks and popup._bids and "<tr" in html
    assert len(popup._asks) == 5 and len(popup._bids) == 5
    popup.close()
    print("ok (5단만 와도 안 깨짐)")


def demo_same_book_skips_paint():
    """값이 그대로면 다시 그리지 않는다. 붕괴 때 틱이 몰아친다."""
    QApplication.instance() or QApplication([])
    screen = ConditionScreen()
    popup = DepthPopup(screen, "387690", "레메디")
    popup.set_book(_book())
    popup._paint_timer.stop()
    popup.set_book(_book())
    assert not popup._paint_timer.isActive(), "같은 값인데 페인트를 걸었다"
    popup.set_book(_book(price=18920))
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
    assert popup._bids[18900] == 9999, popup._bids[18900]
    popup.close()
    print("ok (호가 틱으로 갱신)")


if __name__ == "__main__":
    demo_tick_size()
    demo_axis_fills_gaps()
    demo_axis_is_centered_and_stable()
    demo_axis_fills_and_covers()
    demo_fits_in_window()
    demo_row_columns_are_fixed()
    demo_partial_depth_survives()
    demo_same_book_skips_paint()
    demo_opens_and_closes_with_row()
    demo_tick_updates_without_query()
