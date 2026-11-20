# -*- coding: utf-8 -*-
"""한 주문이 여러 번 나뉘어 체결돼도 체결량을 겹쳐 세지 않는지 확인한다.

2026-11-20 노랑풍선 실제 이벤트를 그대로 재생한다. 키움의 체결량은 그 체결분이 아니라
누적이라, 더하면 9→100이 109로 센다.
"""
import asyncio

from order import OrderBatch, OrderEngine


def make(requested, order_no):
    manager = OrderEngine(rest=None)
    batch = OrderBatch("104620", "노랑풍선", 4140, True, [requested])
    child = batch.children[0]
    child.order_no = order_no
    manager.batches["104620"] = batch
    manager._by_order_no[order_no] = (batch, child)
    return manager, batch, child


def fill(manager, order_no, fill_qty, remaining, fill_id):
    manager.on_order_event({"order_no": order_no, "fill_qty": fill_qty, "remaining_qty": remaining,
                            "status": "체결", "fill_id": fill_id})


def demo():
    # 10:03 0001926: 152주 주문, 이벤트 체결량 9 → 100 (누적)
    manager, batch, child = make(152, "0001926")
    fill(manager, "0001926", 9, 143, "6125")
    assert child.filled_qty == 9 and child.remaining_qty == 143
    fill(manager, "0001926", 100, 52, "6126")
    assert child.filled_qty == 100, f"겹쳐 셌다: {child.filled_qty}"      # 옛 코드는 109
    assert batch.total_filled == 100

    # 14:11 0030292: 584주 주문, 99 → 299 → 310 → 584 (누적)
    manager, batch, child = make(584, "0030292")
    seen = []
    for qty, remaining, fill_id in ((99, 485, "a"), (299, 285, "b"), (310, 274, "c"), (584, 0, "d")):
        fill(manager, "0030292", qty, remaining, fill_id)
        seen.append(child.filled_qty)
    assert seen == [99, 299, 310, 584], seen                               # 옛 코드는 99, 398, 708, 584(상한)
    assert child.done and child.remaining_qty == 0

    # 같은 체결 이벤트가 두 번 와도 그대로다(체결번호 중복·누적값 동일).
    manager, batch, child = make(100, "X1")
    fill(manager, "X1", 40, 60, "f1")
    fill(manager, "X1", 40, 60, "f1")
    fill(manager, "X1", 40, 60, "f2")                                      # 번호가 달라도 누적이라 같은 값
    assert child.filled_qty == 40, child.filled_qty

    # 순서가 뒤바뀌어 작은 누적값이 늦게 와도 줄지 않는다.
    manager, batch, child = make(100, "X2")
    fill(manager, "X2", 70, 30, "g1")
    fill(manager, "X2", 30, 70, "g2")
    assert child.filled_qty == 70, child.filled_qty

    # 쓴 금액(체결 + 잔량) × 가격이 주문 수량을 넘지 않는다.
    manager, batch, child = make(152, "0001926")
    fill(manager, "0001926", 9, 143, "1")
    fill(manager, "0001926", 100, 52, "2")
    assert manager._batch_notional(batch) == (100 + 52) * 4140, manager._batch_notional(batch)
    print("ok (누적 체결량을 겹쳐 세지 않음: 9→100=100 · 99→299→310→584 · 중복·역순 이벤트)")


if __name__ == "__main__":
    asyncio.run(asyncio.sleep(0))
    demo()
