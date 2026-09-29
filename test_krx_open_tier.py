# -*- coding: utf-8 -*-
"""통합 모드 점상 판정이 「09:00 이후 KRX 체결」로 시가를 가르는지 확인한다.

KRX 모드는 등락률 0이 「아직 KRX 시가 전」 신호다. 통합 모드는 NXT 체결이
등락률을 먼저 채워(2026-09-29 NXT 프리마켓 +25%) KRX 예상 상한 종목이 점상
줄에서 빠졌다. NXT 체결은 누적거래량도 올려 KRX 예상체결가까지 껐다.
체결 틱에 거래소(FID 9081)와 체결 시각(FID 20)이 실려 오므로, 09:00 이후
KRX 체결이 왔는지로 둘 다 가른다. 매도·매수잔량은 통합 그대로라 KRX·NXT
둘 다 매도 0인 종목만 줄 위에 오른다.
"""
import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

import gui  # noqa: E402
import ws  # noqa: E402
from gui import TIER_LIMIT_CLEAN, TIER_WAIT, TIER_WAIT_CLEAN, _limit_tier  # noqa: E402


def row(**changes) -> dict:
    """HLB제약 09-29 09:01 통합 화면: KRX는 VI 연장, NXT는 상한 체결."""
    base = dict(name="", upper=12500, base=9620, price=12500, rate=29.94,
                exp_price=12500, exp_rate=29.94, ask_qty=0, bid_qty=900_000,
                vol=50_000, ask_price=0, bid_price=12500)
    base.update(changes)
    return base


def demo_tier():
    d = row()
    assert _limit_tier(d) == TIER_LIMIT_CLEAN          # KRX 모드 판정: 등락률 있음
    assert _limit_tier(d, krx_opened=False) == TIER_WAIT_CLEAN
    # 통합 매도잔량 그대로: NXT에 매도가 남아 있으면 줄 위로 못 간다.
    assert _limit_tier(row(ask_qty=300), krx_opened=False) == TIER_WAIT
    # KRX 시가가 정해지면 평소 판정.
    assert _limit_tier(d, krx_opened=True) == TIER_LIMIT_CLEAN
    print("ok (통합: 09:00 이후 KRX 체결 전까지 점상 판정)")


def demo_parse():
    code, f = ws.parse_real_item({"item": "047920_AL", "type": "0B", "values": {
        "10": "+12500", "13": "5000", "20": "090234", "9081": "KRX"}})
    assert f["trade_ex"] == "KRX" and f["trade_time"] == 90234, f
    print("ok (체결 거래소·시각 매핑)")


def demo_model():
    QApplication.instance() or QApplication([])
    m = gui.StockModel()
    m.add_stock("047920", {"name": "HLB제약"})
    assert m.krx_open_state("047920") is None          # KRX 모드는 쓰지 않는다
    m.unified = True
    m.update_stock("047920", {"exp_price": 12500, "exp_hot": 1, "vol": 100})

    # NXT 체결(프리마켓·09:00:30 메인마켓): 시가 전 그대로, 예상체결가 안 꺼짐.
    m.update_stock("047920", {"price": 12500, "vol": 900, "trade_ex": "NXT",
                              "trade_time": 90045})
    assert m.krx_open_state("047920") is False
    assert m.rows["047920"]["exp_price"] == 12500

    # 09:00 전 KRX 체결(장전 시간외, 전일 종가)은 시가가 아니다.
    m.update_stock("047920", {"vol": 1000, "trade_ex": "KRX",
                              "trade_time": 83512})
    assert m.krx_open_state("047920") is False

    # 09:00 이후 첫 KRX 체결 = 시가 결정. 예상체결가도 이때 꺼진다.
    m.update_stock("047920", {"price": 12500, "vol": 50_000, "trade_ex": "KRX",
                              "trade_time": 90234})
    assert m.krx_open_state("047920") is True
    assert m.rows["047920"]["exp_price"] == 0

    # 거래소 칸이 빠진 09:00 이후 체결은 KRX로 본다(예전 동작 쪽).
    m.add_stock("109670", {"name": "씨싸이트"})
    m.update_stock("109670", {"price": 16730, "vol": 10, "trade_time": 90224})
    assert m.krx_open_state("109670") is True
    print("ok (NXT 체결은 무시, 09:00 이후 KRX 체결에 시가)")


def demo_krx_vi_marks_open():
    """재실행·편입 직후 KRX 체결 전에 VI가 걸려도 시가를 가린다(2026-09-29)."""
    QApplication.instance() or QApplication([])
    m = gui.StockModel()
    m.unified = True
    m.add_stock("047920", {"name": "HLB제약", "base": 9620})
    m.add_stock("131100", {"name": "티엔엔터테인먼트", "base": 2860})

    m.note_krx_vi("047920", True, "정적", 9620)        # 시가 VI 발동: 시가 전
    assert m.krx_open_state("047920") is False
    m.note_krx_vi("047920", False, "정적", 9620)       # 해제 = KRX 시가
    assert m.krx_open_state("047920") is True

    m.note_krx_vi("131100", True, "정적", 3040)        # 장중 VI: 이미 거래 중
    assert m.krx_open_state("131100") is True

    m.single.add("131100")                               # 단일가 종목은 등락률로
    assert m.krx_open_state("131100") is None
    print("ok (KRX VI: 시가 VI는 그대로, 장중 VI·해제는 시가 정해짐)")


def demo_ws_routes_only_krx_vi():
    client = ws.WSClient()
    got = []
    client.on_krx_vi = lambda *a: got.append(a)
    client._log_vi_raw = lambda *a: None
    for item in ("047920_NX", "047920_AL", "047920"):
        client._on_vi({"values": {
            "9001": item, "1221": "12500", "1223": "090013", "1224": "000000",
            "1225": "정적", "1236": "9620", "1238": "+29.94"}})
    assert got == [("047920", True, "정적", 9620)], got
    print("ok (NXT·통합 사본은 넘기지 않고 KRX VI만)")


if __name__ == "__main__":
    demo_tier()
    demo_parse()
    demo_model()
    demo_krx_vi_marks_open()
    demo_ws_routes_only_krx_vi()
