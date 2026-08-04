# -*- coding: utf-8 -*-
"""3단매도·청산키의 미체결 취소와 매도 송신 순서를 확인한다.

    .\.venv\Scripts\python.exe test_sell_cancel_order.py

핵심 조건
  1. 미체결이 없으면 매도 전에 어떤 조회·취소도 나가지 않는다(지연 0).
  2. 미체결이 있으면 취소가 매도보다 먼저 송신된다.
  3. 매도는 취소 응답을 기다리지 않는다.
"""
import asyncio
import logging
import sys
import types

# main을 import하면 bot.log 핸들러가 붙는다. 검증 기록이 운영 로그에 섞이지
# 않도록 파일 핸들러를 떼고 화면으로만 남긴다.
import main as _main  # noqa: E402  (핸들러 정리를 위해 먼저 import)

for _handler in list(logging.getLogger().handlers):
    if isinstance(_handler, logging.FileHandler):
        logging.getLogger().removeHandler(_handler)
        _handler.close()
logging.getLogger().setLevel(logging.CRITICAL)


class FakeRest:
    """호출 순서를 기록하는 최소 REST 대역. 취소 응답은 일부러 느리다."""

    def __init__(self):
        self.calls = []

    async def cancel_order(self, code, order_no, qty, exchange="KRX"):
        self.calls.append(f"cancel_start:{order_no}")
        await asyncio.sleep(0.05)
        self.calls.append(f"cancel_done:{order_no}")
        return {"order_no": "9" + order_no}

    async def cancel_open_buy_orders(self, code):
        self.calls.append("sweep")
        return 0, 0

    async def holding_position(self, code):
        self.calls.append("position")
        return {"held_qty": 500, "sellable_qty": 500}

    async def open_buy_orders(self, code):
        self.calls.append("open_buys_query")
        return list(self.pending)

    async def sell_order(self, code, qty, price, market=False):
        self.calls.append("sell")
        return {"order_no": "0001"}


class FakeSettings:
    """메모리에만 쓰는 설정 대역. 사용자 layout.ini를 건드리지 않는다."""

    def __init__(self):
        self.saved = {}

    def setValue(self, key, value):
        self.saved[key] = value

    def value(self, key, default=""):
        return self.saved.get(key, default)

    def sync(self):
        pass


def _app(pending):
    app = _main.App.__new__(_main.App)
    app.rest = FakeRest()
    app.rest.pending = pending
    app.views = []
    app._settings = FakeSettings()
    app._account_auto_cancel_armed = set()
    app._balance_sell_settings = {}
    app._balance_sell_date = {}
    app._exit_hotkey_specs = {}
    app.orders = types.SimpleNamespace(
        stop_local_submissions=lambda code: None, batches={})
    app._balance_sell_stage = {}
    app._emergency_status_dismissed = set()
    app._cancel_sent_orders = set()
    app._position_book = {}
    app._open_sell_orders = {}
    app._position_fill_ids = set()
    app._sell_accepts = {}
    app._open_buy_orders = {
        "005930": {o["order_no"]: (o["remaining_qty"], o["exchange"])
                   for o in pending}
    } if pending else {}
    return app


def _splits(count):
    return [{"code": "005930", "order_no": f"{i:04d}",
             "remaining_qty": 534, "exchange": "KRX"}
            for i in range(1, count + 1)]


ONE_PENDING = [{"code": "005930", "order_no": "0009",
                "remaining_qty": 300, "exchange": "KRX"}]


async def check_balance_no_pending():
    app = _app([])
    await app._execute_balance_stage("005930", 2, 0.5, 70000, 10, False)
    calls = app.rest.calls
    # 매도 전에는 잔고조회 하나뿐이어야 한다. 취소도 스윕도 앞서면 안 된다.
    assert calls[:2] == ["position", "sell"], calls
    print("balance(미체결 0):", calls)


async def check_balance_pending():
    app = _app(ONE_PENDING)
    await app._execute_balance_stage("005930", 2, 0.5, 70000, 10, False)
    calls = app.rest.calls
    assert calls[0] == "cancel_start:0009", calls
    # 매도는 취소 응답을 기다리지 않으므로 이 시점에 취소는 아직 진행 중이다.
    assert "cancel_done:0009" not in calls, calls
    await asyncio.sleep(0.1)
    assert calls.index("sell") < calls.index("cancel_done:0009"), calls
    print("balance(미체결 1):", calls)


async def check_balance_nine_splits():
    """9분할 미체결이어도 매도가 주문 5건/초 창 안에 들어가야 한다."""
    import api

    app = _app(_splits(9))
    await app._execute_balance_stage("005930", 3, 1.0, 70000, 5, False)
    calls = app.rest.calls
    sent = [c for c in calls if c.startswith("cancel_start") or c == "sell"]
    # 매도 앞의 취소는 창 하나를 다 쓰지 않아야 한다(ORDER_BURST-1건 이하).
    before = sent[:sent.index("sell")]
    assert len(before) <= api.ORDER_BURST - 1, sent
    assert len(before) == api.ORDER_BURST - 1, sent
    await asyncio.sleep(0.2)
    started = {c for c in app.rest.calls if c.startswith("cancel_start")}
    assert len(started) == 9, sorted(started)  # 나머지는 매도 뒤에 이어서
    print(f"balance(9분할): 매도 앞 취소 {len(before)}건, 총 취소 "
          f"{len(started)}건")


async def check_emergency_no_pending():
    app = _app([])
    await app._emergency_exit_async("005930", 69000, True)
    calls = app.rest.calls
    assert calls == ["position", "open_buys_query", "sell"], calls
    print("청산키(미체결 0):", calls)


async def check_emergency_pending():
    app = _app(ONE_PENDING)
    await app._emergency_exit_async("005930", 69000, True)
    calls = app.rest.calls
    assert calls.index("cancel_start:0009") < calls.index("sell"), calls
    assert "cancel_done:0009" not in calls, calls
    await asyncio.sleep(0.1)
    assert calls.index("sell") < calls.index("cancel_done:0009"), calls
    # 이미 조회한 목록을 쓰므로 미체결 조회는 한 번뿐이어야 한다.
    assert calls.count("open_buys_query") == 1, calls
    print("청산키(미체결 1):", calls)


async def check_emergency_with_book():
    """장부가 서 있으면 청산키가 계좌조회 없이 취소+매도를 바로 낸다."""
    app = _app(ONE_PENDING)
    app._position_book = {"005930": {"held": 500, "sellable": 500}}
    await app._emergency_exit_async("005930", 69000, True)
    calls = app.rest.calls
    assert "position" not in calls, calls
    assert "open_buys_query" not in calls, calls
    assert calls.index("cancel_start:0009") < calls.index("sell"), calls
    print("청산키(장부) :", calls)


async def check_sell_resend_guard():
    """응답이 유실됐을 때 접수 이벤트 유무로 재전송을 갈라야 한다."""
    import api

    # (1) 접수 이벤트가 들어온 경우: 다시 보내면 중복 매도다 -> 재전송 금지
    app = _app([])
    tries = []

    async def lost_response(code, qty, price, market=False):
        tries.append(qty)
        # 응답은 유실됐지만 거래소는 접수했다 -> 웹소켓으로 접수 이벤트 도착
        app._track_open_sell(code, f"S{len(tries)}", {
            "original_order_no": "0000000", "remaining_qty": qty,
            "fill_qty": 0, "fill_id": "", "exchange": "KRX"})
        raise api.OrderSendUnknown("timeout")

    app.rest.sell_order = lost_response
    result = await app._send_sell_order("005930", 500, 70000, False, "청산")
    assert len(tries) == 1, tries
    assert result["order_no"] == "S1", result

    # (2) 접수 이벤트가 없는 경우: 미접수 확정 -> 재전송
    app2 = _app([])
    tries2 = []

    async def never_accepted(code, qty, price, market=False):
        tries2.append(qty)
        raise api.OrderSendUnknown("timeout")

    app2.rest.sell_order = never_accepted
    try:
        await app2._send_sell_order("005930", 500, 70000, False, "청산")
        raise AssertionError("끝내 확인 못 하면 예외여야 한다")
    except api.OrderSendUnknown:
        pass
    assert len(tries2) == 3, tries2
    print(f"중복 방지    : 접수됨 {len(tries)}회 전송, 미접수 {len(tries2)}회 전송")


def check_open_buy_book():
    """웹소켓 이벤트로 장부가 정확히 쌓이고 지워지는지 확인한다."""
    app = _app([])

    def feed(order_no, original, remaining, status="접수"):
        app._track_open_buy("005930", order_no, {
            "original_order_no": original, "remaining_qty": remaining,
            "status": status, "exchange": "KRX"})

    feed("0006678", "0000000", 100)          # 신규 매수 접수
    feed("0006682", "0000000", 100)
    assert set(app._open_buy_orders["005930"]) == {"0006678", "0006682"}
    feed("0006678", "0000000", 40, "체결")    # 일부 체결
    assert app._open_buy_orders["005930"]["0006678"][0] == 40
    feed("0006749", "0006678", 100)          # 취소 주문 접수 -> 새 주문 아님
    assert "0006749" not in app._open_buy_orders["005930"]
    feed("0006749", "0006678", 0, "확인")     # 취소 확인 -> 원주문 소멸
    assert set(app._open_buy_orders["005930"]) == {"0006682"}
    feed("0006682", "0000000", 0, "체결")     # 전량 체결
    assert "005930" not in app._open_buy_orders
    print("장부        : OK")


async def check_book_prime():
    """재접속 직후 계좌 조회로 장부를 채우되, 최신 웹소켓 상태는 지키는지."""
    app = _app([])
    app.rest.pending = _splits(2)

    async def all_open_buys(code=""):
        app.rest.calls.append("prime_query")
        return [dict(o, code="005930") for o in app.rest.pending]

    app.rest.open_buy_orders = all_open_buys
    # 조회 직전에 웹소켓으로 먼저 들어온 최신 잔량
    app._track_open_buy("005930", "0001", {
        "original_order_no": "0000000", "remaining_qty": 12,
        "status": "체결", "exchange": "KRX"})
    await app._prime_open_buy_book()
    book = app._open_buy_orders["005930"]
    assert set(book) == {"0001", "0002"}, book
    assert book["0001"][0] == 12, book   # 조회값 534로 되돌아가면 안 된다
    assert book["0002"][0] == 534, book
    print("장부 초기화  :", {k: v[0] for k, v in book.items()})


def check_position_book():
    """체결·매도접수·취소로 보유·매도가능 수량이 맞게 움직이는지 확인한다."""
    app = _app([])
    app._position_book = {"005930": {"held": 0, "sellable": 0}}
    pos = app._position_book["005930"]

    def buy(order_no, remaining, fill=0, fill_id="", original="0000000"):
        app._track_open_buy("005930", order_no, {
            "original_order_no": original, "remaining_qty": remaining,
            "fill_qty": fill, "fill_id": fill_id, "exchange": "KRX"})

    def sell(order_no, remaining, fill=0, fill_id="", original="0000000"):
        app._track_open_sell("005930", order_no, {
            "original_order_no": original, "remaining_qty": remaining,
            "fill_qty": fill, "fill_id": fill_id, "exchange": "KRX"})

    buy("B1", 100)                       # 매수 접수 100
    assert (pos["held"], pos["sellable"]) == (0, 0), pos
    buy("B1", 40, fill=60, fill_id="f1")  # 60주 체결
    assert (pos["held"], pos["sellable"]) == (60, 60), pos
    buy("B1", 40, fill=60, fill_id="f1")  # 같은 체결 재수신 -> 중복 반영 금지
    assert (pos["held"], pos["sellable"]) == (60, 60), pos
    sell("S1", 50)                       # 매도 50주 접수 -> 그만큼 묶임
    assert (pos["held"], pos["sellable"]) == (60, 10), pos
    sell("S2", 0, original="S1")         # 매도 취소 확인 -> 되돌아옴
    assert (pos["held"], pos["sellable"]) == (60, 60), pos
    sell("S3", 20)                       # 20주 매도 접수
    sell("S3", 0, fill=20, fill_id="f2")  # 체결 -> 보유만 감소
    assert (pos["held"], pos["sellable"]) == (40, 40), pos
    print("잔고장부     :", pos)


async def check_sell_uses_book():
    """장부가 있으면 잔고조회 없이 매도하고, 거부되면 조회로 되돌아간다."""
    app = _app([])
    app._position_book = {"005930": {"held": 500, "sellable": 500}}
    sold = await app._sell_account_position("005930", 1.0, 70000, "잔량 3단계")
    assert sold == 500, sold
    assert app.rest.calls == ["sell"], app.rest.calls  # position 조회 없음

    rejected = _app([])
    rejected._position_book = {"005930": {"held": 500, "sellable": 500}}
    calls = rejected.rest.calls
    first = {"done": False}

    async def flaky_sell(code, qty, price, market=False):
        calls.append(f"sell:{qty}")
        if not first["done"]:
            first["done"] = True
            raise RuntimeError("주문가능수량을 초과하였습니다")
        return {"order_no": "0002"}

    rejected.rest.sell_order = flaky_sell
    rejected._prime_position_book = _noop
    sold = await rejected._sell_account_position(
        "005930", 1.0, 70000, "잔량 3단계")
    assert sold == 500, sold
    assert calls == ["sell:500", "position", "sell:500"], calls
    assert "005930" not in rejected._position_book
    print("매도 경로    :", calls)


async def _noop(*_args, **_kwargs):
    return None


def _bare_rest(post):
    import api

    rest = api.RestClient.__new__(api.RestClient)
    rest._order_gate = asyncio.Lock()
    rest._order_sent = __import__("collections").deque(maxlen=api.ORDER_BURST)
    rest._client = types.SimpleNamespace(post=post)
    rest.tokens = types.SimpleNamespace(token=_immediate_token)
    return rest


async def check_order_rate_limit():
    """주문 5건까지는 간격 없이 나가고, 6건째만 창이 열릴 때까지 기다린다."""
    import time
    import api

    sent = []

    async def fake_post(*_args, **_kwargs):
        sent.append(time.monotonic())
        raise RuntimeError("stop-after-send")

    rest = _bare_rest(fake_post)
    start = time.monotonic()
    for _ in range(6):
        try:
            await rest._order_request("kt10003", {})
        except RuntimeError:
            pass
    assert len(sent) == 6, sent
    assert sent[4] - start < 0.05, [round(t - start, 3) for t in sent]
    assert sent[5] - start >= api.ORDER_WINDOW - 0.05, [
        round(t - start, 3) for t in sent]
    print("주문제한     : 5건 즉시, 6건째 "
          f"{sent[5] - start:.2f}초 대기")


async def check_order_rate_limit_retry():
    """유량 거부는 재전송하고, 잔고 거부는 그대로 올려보내야 한다."""
    import api

    attempts = []

    def responder(messages):
        async def post(*_args, **kwargs):
            attempts.append(kwargs["headers"]["api-id"])
            return types.SimpleNamespace(
                status_code=200,
                raise_for_status=lambda: None,
                json=lambda: {
                    "return_code": "1",
                    "return_msg": messages[min(len(attempts) - 1,
                                               len(messages) - 1)],
                },
            )
        return post

    rest = _bare_rest(responder(["초당 요청 제한을 초과하였습니다", ""]))
    try:
        await rest._order_request("kt10001", {})
    except RuntimeError:
        pass
    assert len(attempts) >= 2, attempts  # 유량 거부 -> 재전송

    attempts.clear()
    rest = _bare_rest(responder(["주문가능수량을 초과하였습니다"]))
    try:
        await rest._order_request("kt10001", {})
        raise AssertionError("잔고 거부는 예외로 올라와야 한다")
    except RuntimeError as error:
        assert "주문가능수량" in str(error), error
    assert len(attempts) == 1, attempts  # 잔고 거부는 재전송 금지
    print("유량 재시도  : 유량 거부만 재전송, 잔고 거부는 즉시 보고")


async def check_order_send_is_parallel():
    """느린 취소가 뒤따르는 매도의 송신을 막지 않아야 한다.

    주문 락을 응답까지 잡으면 취소 왕복시간만큼 매도가 밀린다.
    """
    import time

    events = []

    async def slow_post(*_args, **kwargs):
        api_id = kwargs["headers"]["api-id"]
        events.append((f"{api_id}:send", time.monotonic()))
        await asyncio.sleep(0.3 if api_id == "kt10003" else 0.0)
        events.append((f"{api_id}:done", time.monotonic()))
        raise RuntimeError("stop-after-send")

    rest = _bare_rest(slow_post)

    async def fire(api_id):
        try:
            await rest._order_request(api_id, {})
        except RuntimeError:
            pass

    start = time.monotonic()
    cancel = asyncio.ensure_future(fire("kt10003"))  # 취소, 응답 0.3초
    await asyncio.sleep(0)
    await fire("kt10001")                            # 매도
    await cancel
    names = [name for name, _ in events]
    sell_sent = next(t for name, t in events if name == "kt10001:send")
    assert names.index("kt10003:send") < names.index("kt10001:send"), names
    assert names.index("kt10001:send") < names.index("kt10003:done"), names
    assert sell_sent - start < 0.05, sell_sent - start
    print(f"병렬송신     : 취소 뒤 매도 {(sell_sent - start) * 1000:.0f}ms "
          "(취소 응답 0.3초를 기다리지 않음)")


async def _immediate_token():
    return "token"


async def main_check():
    check_open_buy_book()
    check_position_book()
    await check_sell_uses_book()
    await check_order_rate_limit()
    await check_order_send_is_parallel()
    await check_order_rate_limit_retry()
    await check_book_prime()
    await check_balance_no_pending()
    await check_balance_pending()
    await check_balance_nine_splits()
    await check_emergency_no_pending()
    await check_emergency_pending()
    await check_emergency_with_book()
    await check_sell_resend_guard()
    print("OK")


if __name__ == "__main__":
    sys.exit(asyncio.run(main_check()) or 0)
