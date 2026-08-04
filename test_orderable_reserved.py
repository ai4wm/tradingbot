# -*- coding: utf-8 -*-
"""동시호가에 두 종목을 연달아 주문할 때 두 번째 수량이 흔들리지 않는지 확인한다.

계좌 주문가능금액은 미체결 접수분을 이미 뺀 값이라, 앱 로컬 누적 주문액을
또 빼면 같은 주문이 두 번 차감된다. 계좌 조회가 언제 도느냐와 무관하게
같은 수량이 나와야 한다.
"""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from gui import ConditionScreen  # noqa: E402


def _detail(code, price, amount, qty, reserved_base):
    return {
        "code": code,
        "price": price,
        "cash_amount": amount,
        "cash_qty": qty,
        "margin_amount": amount,
        "margin_qty": qty,
        "applied_margin_rate": 100,
        "stock_margin_rate": "100%",
        "reserved_base": reserved_base,
    }


def _screen():
    screen = ConditionScreen()
    screen.model.rows["A"] = {"name": "가종목", "upper": 10000}
    screen.model.rows["B"] = {"name": "나종목", "upper": 20000}
    return screen


def demo():
    app = QApplication.instance() or QApplication([])

    # A 종목에 300만원(300주 × 1만원)을 주문한 상태.
    ordered = 3_000_000

    # (1) 계좌 조회가 아직 안 돈 경우: 캐시된 주문 전 값 + 로컬 예약금 차감.
    stale = _screen()
    stale._order_target_code = "B"
    stale.set_order_reserved(ordered)
    stale.set_orderable_quantity("B", 20000, _detail("B", 20000, 10_000_000, 500, 0))
    before_poll = stale._current_orderable_qty()

    # (2) 계좌 조회가 이미 돈 경우: 서버가 A 증거금을 뺀 값 + 같은 시점 기준선.
    fresh = _screen()
    fresh._order_target_code = "B"
    fresh.set_order_reserved(ordered)
    fresh.set_orderable_quantity(
        "B", 20000, _detail("B", 20000, 7_000_000, 350, ordered))
    after_poll = fresh._current_orderable_qty()

    assert before_poll == after_poll == 350, (before_poll, after_poll)

    # 계좌 요약(kt00001) 경로도 같은 기준선을 쓴다.
    summary = _screen()
    summary._order_target_code = "B"
    summary.set_order_reserved(ordered)
    summary.set_account_summary({
        "estimated_assets": 20_000_000,
        "cash_orderable": 7_000_000,
        "orderable_by_margin": {20: 7_000_000},
        "reserved_base": ordered,
    })
    summary.set_orderable_quantity(
        "B", 20000, _detail("B", 20000, 7_000_000, 350, ordered))
    assert summary._current_orderable_qty() == 350, summary._current_orderable_qty()

    # 기준선이 없는 응답(구버전·조회 실패 후)은 예약금을 전액 빼는 안전 방향.
    unknown = _screen()
    unknown._order_target_code = "B"
    unknown.set_order_reserved(ordered)
    unknown.set_orderable_quantity(
        "B", 20000, _detail("B", 20000, 10_000_000, 500, None))
    assert unknown._current_orderable_qty() == 350, unknown._current_orderable_qty()

    # 아직 아무 주문도 없으면 계좌 가능수량이 그대로 나온다.
    idle = _screen()
    idle._order_target_code = "B"
    idle.set_orderable_quantity("B", 20000, _detail("B", 20000, 10_000_000, 500, 0))
    assert idle._current_orderable_qty() == 500, idle._current_orderable_qty()

    # 한도를 다 쓰면 0주. 남은금액이 음수로 넘어가지 않아야 한다.
    spent = _screen()
    spent._order_target_code = "B"
    spent.set_order_reserved(10_000_000)
    spent.set_orderable_quantity("B", 20000, _detail("B", 20000, 10_000_000, 500, 0))
    assert spent._current_orderable_qty() == 0, spent._current_orderable_qty()

    del app
    print("ok")


def demo_consecutive():
    """1·2·3·4번째 주문이 남은 금액에서 계속 정확히 빠지는지 확인한다."""
    from order import OrderEngine

    engine = OrderEngine(None)
    engine._put = lambda *a: None   # 전송 큐 없이 장부만 본다

    app = QApplication.instance() or QApplication([])
    screen = ConditionScreen()
    for code in "ABCD":
        screen.model.rows[code] = {"name": code, "upper": 10000}

    def fill(batch):
        """전량 체결 처리."""
        for child in batch.children:
            child.order_no = child.order_no or f"O{id(child)}"
            child.filled_qty = child.requested_qty
            child.remaining_qty = 0
            child.done = True
        batch.sent_count = len(batch.children)

    # 계좌 1,000만원. 종목마다 100주(100만원)씩 네 번 주문한다.
    remaining_seen = []
    for step, code in enumerate("ABCD"):
        screen._order_target_code = code
        # 매번 캐시된 주문 전 값(기준선 0)을 본다 — 가장 불리한 조건.
        screen.set_order_reserved(engine.committed_notional())
        screen.set_orderable_quantity(
            code, 10000, _detail(code, 10000, 10_000_000, 1000, 0))
        remaining_seen.append(screen._current_orderable_qty())
        assert remaining_seen[-1] == 1000 - step * 100, remaining_seen
        fill(engine.submit(code, code, 10000, [100], False))

    assert remaining_seen == [1000, 900, 800, 700], remaining_seen

    # 5번째로 A를 재주문해도 앞선 400만원이 예약금에 남아 있어야 한다.
    screen._order_target_code = "A"
    screen.set_order_reserved(engine.committed_notional())
    screen.set_orderable_quantity(
        "A", 10000, _detail("A", 10000, 10_000_000, 1000, 0))
    assert screen._current_orderable_qty() == 600, (
        screen._current_orderable_qty())
    engine.submit("A", "A", 10000, [50], False)
    assert engine.committed_notional() == 4_500_000, engine.committed_notional()

    del app
    print("ok (연속 주문)")


def demo_cancel_then_reorder():
    """주문 → 취소 → 재주문에서 취소한 돈이 예약금에 남지 않는지 확인한다."""
    from order import OrderEngine

    engine = OrderEngine(None)
    engine._put = lambda *a: None

    # 1) 500주 주문 뒤 전량 취소되면 사용액은 0이어야 한다.
    batch = engine.submit("A", "가", 10000, [500], False)
    assert engine.committed_notional() == 5_000_000, engine.committed_notional()
    child = batch.children[0]
    child.order_no = "O1"
    batch.sent_count = 1
    child.cancel_sent = True
    child.remaining_qty = 0
    child.done = True
    assert engine.committed_notional() == 0, engine.committed_notional()

    # 2) 취소가 확인되기 전에는 재주문을 막아야 중복 주문이 되지 않는다.
    live = engine.submit("B", "나", 10000, [100], False)
    try:
        engine.submit("B", "나", 10000, [100], False)
    except ValueError:
        pass
    else:
        raise AssertionError("미체결 잔량이 있는데 재주문이 통과함")
    live.children[0].order_no = "O2"
    live.sent_count = 1
    live.children[0].remaining_qty = 0
    live.children[0].done = True

    # 3) 취소 뒤 재주문하면 새 주문액만 잡힌다(취소분이 남아 있으면 안 된다).
    engine.submit("A", "가", 10000, [300], False)
    assert engine.committed_notional() == 3_000_000, engine.committed_notional()

    # 4) 일부만 체결되고 나머지를 취소한 뒤 재주문하면 체결분은 남는다.
    partial = engine.submit("C", "다", 10000, [200], False)
    partial.children[0].order_no = "O3"
    partial.sent_count = 1
    partial.children[0].filled_qty = 80
    partial.children[0].remaining_qty = 0
    partial.children[0].done = True
    engine.submit("C", "다", 10000, [100], False)
    assert engine.committed_notional() == 3_000_000 + 800_000 + 1_000_000, (
        engine.committed_notional())

    # 5) 9분할 중 일부만 나간 상태에서 취소해도 그 종목 재주문이 막히면 안 된다.
    split = engine.submit("D", "라", 10000, [100] * 9, False)
    for sent in split.children[:3]:
        sent.order_no = f"S{id(sent)}"
        split.sent_count += 1
    engine.stop_local_submissions("D")          # 미전송 6건 중단
    for sent in split.children[:3]:             # 접수분 취소 확인
        sent.remaining_qty = 0
        sent.done = True
    engine.submit("D", "라", 10000, [100], False)   # 여기서 막히면 버그
    assert engine.batches["D"].total_requested == 100

    print("ok (취소 후 재주문)")


def demo_cancel_then_other_stock():
    """A를 취소한 뒤 B를 주문할 때 풀린 금액이 B에 그대로 쓰이는지 확인한다."""
    from order import OrderEngine

    app = QApplication.instance() or QApplication([])

    def board():
        engine = OrderEngine(None)
        engine._put = lambda *a: None
        screen = ConditionScreen()
        screen.model.rows["A"] = {"name": "가", "upper": 10000}
        screen.model.rows["B"] = {"name": "나", "upper": 20000}
        return engine, screen

    def pick_b(screen, engine, amount, qty, base):
        """B로 대상을 옮기고 그때의 주문가능수량을 읽는다."""
        screen._order_target_code = "B"
        screen.set_order_reserved(engine.committed_notional())
        screen.set_orderable_quantity(
            "B", 20000, _detail("B", 20000, amount, qty, base))
        return screen._current_orderable_qty()

    # (1) A에 500만원이 걸려 있는 동안은 B가 절반만 가능해야 한다.
    engine, screen = board()
    batch = engine.submit("A", "가", 10000, [500], False)
    batch.children[0].order_no = "O1"
    batch.sent_count = 1
    assert pick_b(screen, engine, 10_000_000, 500, 0) == 250, "A 주문 중 B 과다"

    # (2) A를 전량 취소하면 B는 전액을 쓸 수 있다 — 캐시/재조회 결과가 같아야 한다.
    batch.children[0].remaining_qty = 0
    batch.children[0].done = True
    cached = pick_b(screen, engine, 10_000_000, 500, 0)          # 취소 전 캐시
    _, fresh_screen = board()
    fresh_screen.model.rows["B"] = {"name": "나", "upper": 20000}
    refetched = pick_b(fresh_screen, engine, 10_000_000, 500, 0)  # 취소 뒤 재조회
    assert cached == refetched == 500, (cached, refetched)

    # (3) 200주만 체결되고 나머지를 취소한 경우, 체결분 200만원만 빠져야 한다.
    engine, screen = board()
    partial = engine.submit("A", "가", 10000, [500], False)
    partial.children[0].order_no = "O2"
    partial.sent_count = 1
    partial.children[0].filled_qty = 200
    partial.children[0].remaining_qty = 0
    partial.children[0].done = True
    stale = pick_b(screen, engine, 10_000_000, 500, 0)            # 주문 전 캐시
    _, screen2 = board()
    screen2.model.rows["B"] = {"name": "나", "upper": 20000}
    polled = pick_b(screen2, engine, 8_000_000, 400, 2_000_000)   # 서버 갱신본
    assert stale == polled == 400, (stale, polled)

    del app
    print("ok (취소 후 다른 종목)")


def demo_status_clears_after_cancel():
    """취소한 종목의 주문 버튼이 다시 켜지는지 확인한다."""
    app = QApplication.instance() or QApplication([])
    screen = ConditionScreen()
    screen.model.add_stocks([("000001", {"name": "가"})])
    screen.model.rows["000001"]["upper"] = 10000
    screen.order_enable_check.setChecked(True)
    screen._order_target_code = "000001"
    screen.set_orderable_quantity(
        "000001", 10000, _detail("000001", 10000, 10_000_000, 1000, 0))
    assert screen.fixed_qty_order_btn.isEnabled(), "처음부터 주문 버튼이 꺼져 있음"

    def clearable(status, cancellable):
        """주문 셀 클릭이 이 상태를 지워 주는지(=재주문 가능해지는지)."""
        screen.set_order_state("000001", status, status, cancellable)
        return (status in ("장종료", "오류", "수량부족", "분할부족",
                           "대상없음")
                or status.endswith("완료")
                or ("취소" in status
                    and "000001" not in screen.model.order_cancellable))

    # 취소가 끝난 주문은 다시 주문할 수 있어야 한다.
    # 앱 분할주문 취소, 계좌 미체결 취소, 취소 대상 없음을 모두 포함한다.
    for status in ("자 취소", "수 취소", "취소전송", "취소없음"):
        assert clearable(status, False), f"{status} 상태에서 재주문이 막힘"

    # 취소 확인 전(잔량 살아 있음)에는 그대로 막아야 중복 주문이 안 난다.
    assert not clearable("자 취소", True), "취소 확인 전인데 재주문이 열림"

    # 진행 중인 주문도 막아야 한다.
    assert not clearable("자 1/3", True), "전송 중인데 재주문이 열림"

    # 완료·오류는 원래대로 열려야 한다.
    for status in ("자 완료", "오류", "장종료"):
        assert clearable(status, False), f"{status} 상태에서 재주문이 막힘"

    del app
    print("ok (취소 뒤 주문버튼)")


if __name__ == "__main__":
    demo()
    demo_consecutive()
    demo_cancel_then_reorder()
    demo_cancel_then_other_stock()
    demo_status_clears_after_cancel()
