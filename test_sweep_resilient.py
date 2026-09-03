# -*- coding: utf-8 -*-
"""마지막 안전장치인 스윕이 한 건 실패로 멈추지 않는지 검사.

스윕은 계좌 조회 목록으로 도는데 그 조회는 앞서 보낸 취소가 반영되기 전일
수 있다. 2026-09-03 11:21 023790에서 두 번째 주문이 571435(원주문이 이미
취소주문입니다)로 튕겼고, 그 예외가 반복문을 끊어 뒤의 주문들이 통째로
건너뛰어졌다.
"""
import asyncio

import api

ORDERS = [
    {"code": "023790", "order_no": "000%d" % n, "exchange": "KRX",
     "remaining_qty": 1}
    for n in range(5)
]


class Stub(api.RestClient):
    def __init__(self, failures):
        self.tried = []
        self._failures = failures

    async def cancel_order(self, code, order_no, qty, exchange="KRX"):
        self.tried.append(order_no)
        error = self._failures.get(order_no)
        if error:
            raise RuntimeError(error)
        return {}


async def demo():
    # 이미 취소된 주문이 중간에 섞여도 끝까지 돈다.
    benign = Stub({"0001": "[2000](571435:원주문이취소주문입니다)",
                   "0003": "[2000](506550:취소할 수량이 없습니다)"})
    sent, qty = await benign._cancel_each(ORDERS)
    assert benign.tried == ["0000", "0001", "0002", "0003", "0004"], benign.tried
    assert (sent, qty) == (3, 3), (sent, qty)

    # 진짜 오류도 나머지를 막지 않는다. 안전장치가 도중에 끊기면 안 된다.
    broken = Stub({"0000": "[2000](900001:서버 오류)"})
    sent, _ = await broken._cancel_each(ORDERS)
    assert len(broken.tried) == 5, broken.tried
    assert sent == 4, sent

    # 세 응답 코드 모두 '이미 목적을 이룸'으로 본다.
    for code in api.NOTHING_TO_CANCEL:
        assert api.is_nothing_to_cancel("[2000](%s:...)" % code), code
    assert not api.is_nothing_to_cancel("[2000](900001:서버 오류)")
    print("ok")


if __name__ == "__main__":
    asyncio.run(demo())
