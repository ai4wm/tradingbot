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


def demo_draws_continuous_axis():
    """호가 20단을 덮는 연속 축이 나오고 빈 줄이 생긴다."""
    QApplication.instance() or QApplication([])
    screen = ConditionScreen()
    popup = DepthPopup(screen, "387690", "레메디")
    popup.set_book(_book())
    popup._refresh_text()

    prices = [p for p, _a, _b in popup._rows]
    # 매도 10단(19,010) ~ 매수 10단(18,810)에 위아래 3틱 여유.
    assert prices[0] == 19040, prices[:3]
    assert prices[-1] == 18780, prices[-3:]
    assert prices == sorted(prices, reverse=True)
    # 축이 호가단위로 연속이어야 한다.
    assert all(a - b == 10 for a, b in zip(prices, prices[1:]))

    quoted = [(p, a, b) for p, a, b in popup._rows if a or b]
    assert len(quoted) == 20, len(quoted)
    blanks = [(p, a, b) for p, a, b in popup._rows if not a and not b]
    assert blanks, "빈 가격대가 하나도 없다"

    text = popup._label.text()
    assert "기준 15,520" in text and "시 15,800" in text
    assert "상한 20,150" in text and "하한 10,870" in text
    assert "+21.84%" in text, "현재가 등락률"   # (18910-15520)/15520
    popup.close()
    print(f"ok (연속 축 {len(popup._rows)}줄 · 호가 20단 · 빈 줄 {len(blanks)})")


def demo_partial_depth_survives():
    """편입 직후 5단만 온 상태에서도 그려진다."""
    QApplication.instance() or QApplication([])
    screen = ConditionScreen()
    popup = DepthPopup(screen, "387690", "레메디")
    popup.set_book(_book(depth=5))
    popup._refresh_text()
    quoted = [(p, a, b) for p, a, b in popup._rows if a or b]
    assert len(quoted) == 10, len(quoted)
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
    row = next(r for r in popup._rows if r[0] == 18900)
    assert row[2] == 9999, row
    popup.close()
    print("ok (호가 틱으로 갱신)")


if __name__ == "__main__":
    demo_tick_size()
    demo_axis_fills_gaps()
    demo_draws_continuous_axis()
    demo_partial_depth_survives()
    demo_same_book_skips_paint()
    demo_opens_and_closes_with_row()
    demo_tick_updates_without_query()
