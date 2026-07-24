# -*- coding: utf-8 -*-
"""분할매수와 원주문별 누적 100주 자동취소 상태기계."""
import asyncio
import logging
from dataclasses import dataclass, field

log = logging.getLogger("order")


def split_quantity(total_qty: int, count: int) -> list[int]:
    """총수량을 count개로 균등 분배. 앞 주문부터 짜투리 1주씩 배정."""
    if not 1 <= count <= 9:
        raise ValueError("분할 횟수는 1~9여야 합니다")
    if total_qty < count:
        raise ValueError("분할 횟수보다 주문가능수량이 적습니다")
    base, extra = divmod(total_qty, count)
    return [base + (1 if i < extra else 0) for i in range(count)]


@dataclass
class ChildOrder:
    requested_qty: int
    order_no: str = ""
    filled_qty: int = 0
    remaining_qty: int = 0
    cancel_sent: bool = False
    done: bool = False
    seen_fills: set[str] = field(default_factory=set)

    def __post_init__(self):
        self.remaining_qty = self.requested_qty


@dataclass
class OrderBatch:
    code: str
    name: str
    price: int
    auto_cancel: bool
    quantities: list[int]
    children: list[ChildOrder] = field(init=False)
    sent_count: int = 0
    error: str = ""

    def __post_init__(self):
        self.children = [ChildOrder(q) for q in self.quantities]

    @property
    def total_requested(self):
        return sum(self.quantities)

    @property
    def total_filled(self):
        return sum(c.filled_qty for c in self.children)

    @property
    def cancel_count(self):
        return sum(c.cancel_sent for c in self.children)

    @property
    def remaining_qty(self):
        return sum(c.remaining_qty for c in self.children)


class OrderEngine:
    """취소(priority=0)가 신규매수(priority=10)보다 먼저 나가는 단일 송신 큐."""

    def __init__(self, rest, on_update=None):
        self.rest = rest
        self.on_update = on_update
        self.batches: dict[str, OrderBatch] = {}
        self._by_order_no: dict[str, tuple[OrderBatch, ChildOrder]] = {}
        self._queue = asyncio.PriorityQueue()
        self._serial = 0
        self._worker_task = None

    def _ensure_worker(self):
        if not self._worker_task or self._worker_task.done():
            self._worker_task = asyncio.create_task(self._worker())

    def _put(self, priority: int, action: str, batch: OrderBatch, child: ChildOrder):
        self._serial += 1
        self._queue.put_nowait((priority, self._serial, action, batch, child))
        self._ensure_worker()

    def submit(self, code: str, name: str, price: int,
               quantities: list[int], auto_cancel: bool) -> OrderBatch:
        old = self.batches.get(code)
        if (old and not old.error
                and (old.remaining_qty > 0 or old.sent_count < len(old.children))):
            raise ValueError("이미 진행 중인 주문이 있습니다")
        if not price or not quantities or any(q <= 0 for q in quantities):
            raise ValueError("주문가격 또는 수량이 올바르지 않습니다")
        batch = OrderBatch(code, name, price, auto_cancel, quantities)
        self.batches[code] = batch
        for child in batch.children:
            self._put(10, "buy", batch, child)
        self._notify(batch, "대기")
        return batch

    def manual_cancel(self, code: str):
        batch = self.batches.get(code)
        if not batch:
            raise ValueError("취소할 주문이 없습니다")
        queued = 0
        for child in batch.children:
            if child.order_no and child.remaining_qty > 0 and not child.cancel_sent:
                child.cancel_sent = True
                self._put(0, "cancel", batch, child)
                queued += 1
        if not queued:
            raise ValueError("취소할 잔량이 없습니다")
        self._notify(batch, "취소대기")

    async def _worker(self):
        while not self._queue.empty():
            _, _, action, batch, child = await self._queue.get()
            try:
                if action == "buy" and batch.error:
                    child.remaining_qty = 0
                    child.done = True
                    continue
                if action == "buy":
                    result = await self.rest.buy_order(
                        batch.code, child.requested_qty, batch.price)
                    child.order_no = result["order_no"]
                    batch.sent_count += 1
                    if child.order_no:
                        self._by_order_no[child.order_no] = (batch, child)
                    self._notify(batch, "전송")
                else:
                    await self.rest.cancel_order(
                        batch.code, child.order_no, child.remaining_qty)
                    self._notify(batch, "취소전송")
            except Exception as exc:  # noqa: BLE001
                batch.error = str(exc)
                if action == "cancel":
                    child.cancel_sent = False
                else:
                    # 첫 매수 거절 뒤 같은 분할묶음의 나머지 주문은 전송하지 않는다.
                    for pending in batch.children:
                        if not pending.order_no:
                            pending.remaining_qty = 0
                            pending.done = True
                self._notify(batch, "오류")
                log.exception("%s %s failed", action, batch.code)
            finally:
                self._queue.task_done()

    def on_order_event(self, event: dict):
        """웹소켓 type=00 주문체결 이벤트 반영."""
        order_no = str(event.get("order_no") or "").strip()
        pair = self._by_order_no.get(order_no)
        # 취소 확인은 새 취소주문번호가 9203에, 원 매수주문번호가 904에
        # 들어올 수 있으므로 원주문번호로도 기존 자식 주문을 찾는다.
        if not pair:
            original_order_no = str(
                event.get("original_order_no") or "").strip()
            pair = self._by_order_no.get(original_order_no)
        if not pair:
            return
        batch, child = pair
        fill_id = str(event.get("fill_id") or "").strip()
        fill_qty = max(0, int(event.get("fill_qty") or 0))
        if fill_qty and fill_id:
            if fill_id in child.seen_fills:
                return
            child.seen_fills.add(fill_id)
        if fill_qty:
            child.filled_qty = min(
                child.requested_qty, child.filled_qty + fill_qty)
        if event.get("remaining_qty") is not None:
            child.remaining_qty = max(0, int(event["remaining_qty"]))
        else:
            child.remaining_qty = max(
                0, child.requested_qty - child.filled_qty)
        status = str(event.get("status") or "")
        if child.remaining_qty == 0 or "취소" in status or "완료" in status:
            child.done = True
        if (batch.auto_cancel and child.filled_qty >= 100
                and child.remaining_qty > 0 and not child.cancel_sent):
            child.cancel_sent = True
            self._put(0, "cancel", batch, child)
        self._notify(batch, "체결")

    def _notify(self, batch: OrderBatch, state: str):
        if self.on_update:
            self.on_update(batch, state)

    def committed_notional(self) -> int:
        """취소 확정 전 잔량과 이미 체결된 수량을 합산한 당일 사용액."""
        total = 0
        for batch in self.batches.values():
            for child in batch.children:
                if child.order_no or (not batch.error and not child.done):
                    total += (child.filled_qty + child.remaining_qty) * batch.price
        return total
