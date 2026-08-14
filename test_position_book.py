# -*- coding: utf-8 -*-
"""영웅문 등 앱 밖 매수 체결이 보유수량 장부에 잡히는지 검사.

체결 이벤트는 웹소켓으로 바로 오지만, 장부에 없는 종목이면 통째로 버려져
앱 재시작 전까지 보유수량이 안 보였다. 계좌를 다시 읽지 않고 체결만으로
세우되, 장부를 세운 적 없을 때는 만들지 않아야 한다(덜 팔림 방지).
"""
import logging
import os
import types

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import main

logging.disable(logging.CRITICAL)  # 운영 bot.log에 쓰지 않는다


class Stub:
    """`App`에서 장부 갱신에 필요한 부분만 세운 대역."""

    def __init__(self, primed: bool, book: dict | None = None):
        self._open_buy_orders = {}
        self._position_book = book if book is not None else {}
        self._position_book_primed = primed
        self._position_fill_ids = set()
        self._position_filled = {}
        self._cancel_sent_orders = set()
        self.pushed = []
        for name in ("_track_open_buy", "_track_order_book", "_new_fill_qty"):
            setattr(self, name,
                    types.MethodType(getattr(main.App, name), self))

    def _push_pending_orders(self, code):
        self.pushed.append(code)


def fill(code="011330", order_no="1", qty=197, fill_id="A1") -> dict:
    return {"code": code, "side": "buy", "status": "체결", "order_qty": qty,
            "remaining_qty": 0, "fill_qty": qty, "fill_id": fill_id}


def event(order_qty, fill_qty, remaining, fill_id, code="049630") -> dict:
    return {"code": code, "side": "buy", "status": "체결",
            "order_qty": order_qty, "remaining_qty": remaining,
            "fill_qty": fill_qty, "fill_id": fill_id}


def demo():
    # 장부를 세운 뒤 처음 보는 종목 -> 0에서 시작해 체결량만큼 잡힌다.
    app = Stub(primed=True)
    app._track_open_buy("011330", "1", fill())
    assert app._position_book["011330"] == {"held": 197, "sellable": 197}, (
        app._position_book)

    # 같은 체결번호가 두 번 와도 한 번만 센다.
    app._track_open_buy("011330", "1", fill())
    assert app._position_book["011330"]["held"] == 197

    # 한 주문의 분할 체결은 잔량 기준으로 누적된다.
    part = Stub(primed=True)
    part._track_open_buy("011330", "1", event(297, 197, 100, "A1", "011330"))
    part._track_open_buy("011330", "1", event(297, 100, 0, "A2", "011330"))
    assert part._position_book["011330"] == {"held": 297, "sellable": 297}, (
        part._position_book)

    # 체결량이 누적으로 실려 와도 부풀지 않는다. 2026-08-14 049630 실제
    # 이벤트: 109주 주문에 34(잔량 75) -> 100(잔량 9)이 왔고, 실제 체결은
    # 100주다. 그냥 더하면 134가 되어 매도가 거부된다.
    dup = Stub(primed=True)
    dup._track_open_buy("049630", "7850", event(109, 34, 75, "3551"))
    dup._track_open_buy("049630", "7850", event(109, 100, 9, "3552"))
    assert dup._position_book["049630"] == {"held": 100, "sellable": 100}, (
        dup._position_book)

    # 잔량 9주 자동취소 확인은 체결량이 0이라 장부를 건드리지 않는다.
    dup._track_open_buy("049630", "7850", event(109, 0, 0, ""))
    assert dup._position_book["049630"]["held"] == 100

    # 체결량이 누적이 아니라 그 체결분으로 와도 덜 세지 않는다.
    each = Stub(primed=True)
    each._track_open_buy("049630", "7850", event(200, 100, 100, "1"))
    each._track_open_buy("049630", "7850", event(200, 100, 0, "2"))
    assert each._position_book["049630"]["held"] == 200, each._position_book

    # 부분체결 도중 재접속: 조회한 100주에 다음 이벤트의 누적 109가 겹쳐
    # 209가 되면 안 된다. 센 기록을 지우지 않아 차분 9만 더한다.
    warm = Stub(primed=True)
    warm._track_open_buy("049630", "7850", event(109, 100, 9, "1"))
    warm._position_book["049630"] = {"held": 100, "sellable": 100}  # 조회 결과
    warm._position_fill_ids.clear()                                 # prime과 동일
    warm._track_open_buy("049630", "7850", event(109, 109, 0, "2"))
    assert warm._position_book["049630"]["held"] == 109, warm._position_book

    # 장부를 못 세운 상태에서는 만들지 않는다. 항목이 없어야 매도가 계좌
    # 조회 경로로 돌아가 실제 보유보다 덜 파는 일이 없다.
    cold = Stub(primed=False)
    cold._track_open_buy("011330", "1", fill())
    assert "011330" not in cold._position_book, cold._position_book

    # 이미 들고 있던 종목은 전과 같이 그대로 더한다.
    held = Stub(primed=True, book={"005930": {"held": 10, "sellable": 4}})
    held._track_open_buy("005930", "1", fill(code="005930", qty=5))
    assert held._position_book["005930"] == {"held": 15, "sellable": 9}

    print("ok")


if __name__ == "__main__":
    demo()
