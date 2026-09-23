# -*- coding: utf-8 -*-
"""10호가 창이 조회 없이 웹소켓 값만으로 그려지는지 확인한다.

필요한 값이 전부 이미 온다 — `0D`가 FID 41~80으로 10단 호가와 잔량을,
`0B`가 현재가·등락률을 싣는다. REST는 한 건도 안 나간다.

`ka10095`는 5단까지만 주므로 편입 직후 한 틱은 6~10단이 비고 다음 호가
틱이 채운다. 그때 창이 깨지지 않아야 한다.
"""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from gui import BOOK_FIELDS, ConditionScreen, DepthPopup  # noqa: E402


def _book(price=41000, rate=3.5, depth=10):
    """0D/0B가 실은 것과 같은 모양. depth 단까지만 채운다."""
    row = {"name": "와이즈플래닛", "price": price, "rate": rate}
    for level in range(1, depth + 1):
        suffix = "" if level == 1 else str(level)
        row[f"ask_price{suffix}"] = price + level * 50
        row[f"ask_qty{suffix}"] = 100 * level
        row[f"bid_price{suffix}"] = price - level * 50
        row[f"bid_qty{suffix}"] = 200 * level
    return row


def demo_draws_ten_levels():
    """20줄이 다 들어가고 잔량 합이 맞는지."""
    QApplication.instance() or QApplication([])
    screen = ConditionScreen()
    popup = DepthPopup(screen, "0010S0", "와이즈플래닛")
    popup.set_book(_book())
    popup._refresh_text()

    assert len(popup._rows) == 20, len(popup._rows)
    # 매도는 위에서 아래로 10 -> 1이라 첫 줄이 가장 비싼 호가다.
    assert popup._rows[0] == ("ask", 41500, 1000), popup._rows[0]
    assert popup._rows[9] == ("ask", 41050, 100), popup._rows[9]
    assert popup._rows[10] == ("bid", 40950, 200), popup._rows[10]
    assert popup._rows[19] == ("bid", 40500, 2000), popup._rows[19]

    text = popup._label.text()
    assert "41,500" in text and "40,500" in text
    # 매수 11,000 / 매도 5,500 -> 매수 비중 66%
    assert "매수 66%" in text, text
    assert "+3.50%" in text, text
    popup.close()
    print("ok (10단 20줄 · 잔량 비중)")


def demo_partial_depth_survives():
    """편입 직후 5단만 온 상태에서도 그려진다. 6~10단은 빈 줄이다."""
    QApplication.instance() or QApplication([])
    screen = ConditionScreen()
    popup = DepthPopup(screen, "0010S0", "와이즈플래닛")
    popup.set_book(_book(depth=5))
    popup._refresh_text()
    assert len(popup._rows) == 20
    assert popup._rows[0] == ("ask", 0, 0), popup._rows[0]   # 10단은 아직 빈다
    assert popup._rows[9] == ("ask", 41050, 100), popup._rows[9]
    popup.close()
    print("ok (5단만 와도 안 깨짐)")


def demo_same_book_skips_paint():
    """값이 그대로면 다시 그리지 않는다. 붕괴 때 틱이 몰아친다."""
    QApplication.instance() or QApplication([])
    screen = ConditionScreen()
    popup = DepthPopup(screen, "0010S0", "와이즈플래닛")
    popup.set_book(_book())
    popup._paint_timer.stop()
    popup.set_book(_book())
    assert not popup._paint_timer.isActive(), "같은 값인데 페인트를 걸었다"
    popup.set_book(_book(price=41050))
    assert popup._paint_timer.isActive(), "값이 바뀌었는데 안 걸었다"
    popup.close()
    print("ok (같은 값이면 페인트 건너뜀)")


def demo_opens_and_closes_with_row():
    """조건에서 빠지면 창도 닫힌다. 매수잔량 창과 같은 규칙이다."""
    QApplication.instance() or QApplication([])
    screen = ConditionScreen()
    screen.model.add_stock("0010S0", {"name": "와이즈플래닛"})
    screen.on_tick("0010S0", _book())
    screen._open_depth_popup("0010S0")
    assert "0010S0" in screen._depth_popups

    # 매수잔량 창과 목록이 섞이지 않아야 한다.
    screen._open_bid_popup("0010S0")
    assert set(screen._depth_popups) == {"0010S0"}
    assert set(screen._bid_popups) == {"0010S0"}

    screen.model.rows.pop("0010S0")
    screen._close_orphan_depth_popups()
    assert screen._depth_popups == {}, screen._depth_popups
    print("ok (행이 사라지면 창도 닫힘)")


def demo_tick_updates_without_query():
    """호가 틱 하나로 창이 갱신된다. 조회 경로를 타지 않는다."""
    QApplication.instance() or QApplication([])
    screen = ConditionScreen()
    screen.model.add_stock("0010S0", {"name": "와이즈플래닛"})
    screen.on_tick("0010S0", _book())
    screen._open_depth_popup("0010S0")
    popup = screen._depth_popups["0010S0"]
    popup._paint_timer.stop()

    # 최우선 매수잔량만 바뀐 0D 틱.
    assert "bid_qty" in BOOK_FIELDS
    screen.on_tick("0010S0", {"bid_qty": 9999})
    assert popup._paint_timer.isActive(), "호가 틱인데 안 그렸다"
    assert popup._rows[10][2] == 9999, popup._rows[10]
    popup.close()
    print("ok (호가 틱으로 갱신)")


if __name__ == "__main__":
    demo_draws_ten_levels()
    demo_partial_depth_survives()
    demo_same_book_skips_paint()
    demo_opens_and_closes_with_row()
    demo_tick_updates_without_query()
