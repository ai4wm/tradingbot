# -*- coding: utf-8 -*-
"""개장이 늦은 날(수능일·연초 첫 거래일) 시각 판정을 확인한다."""
import os
import types
from datetime import date, datetime

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import config  # noqa: E402
import gui  # noqa: E402
import main  # noqa: E402


def demo():
    suneung, first, plain = date(2026, 11, 19), date(2027, 1, 4), date(2026, 11, 18)
    assert config.market_delay(suneung) == (3600, 3600)
    assert config.market_delay(first) == (3600, 0)      # 01-02 토 → 01-04 월
    assert config.market_delay(plain) == (0, 0)

    def states(day, h, m, s=0):
        return main._market_session_states(datetime.combine(day, datetime.min.time())
                                           .replace(hour=h, minute=m, second=s))[:2]

    assert states(suneung, 9, 50) == ("시가 동시호가", "개장 전")
    assert states(suneung, 10, 0) == ("정규장", "개장 전")
    assert states(suneung, 10, 0, 30) == ("정규장", "메인마켓")
    assert states(suneung, 16, 25) == ("종가 동시호가", "일시휴장")
    assert states(suneung, 16, 50) == ("시간외종가", "애프터마켓")
    assert states(suneung, 17, 30) == ("애프터마켓", "애프터마켓")
    assert states(suneung, 20, 0) == ("종료", "종료")
    assert states(first, 9, 30) == ("시가 동시호가", "개장 전")
    assert states(first, 15, 25) == ("종가 동시호가", "일시휴장")  # 마감은 평소대로
    assert states(plain, 8, 10) == ("개장 전", "프리마켓")
    assert states(plain, 9, 0) == ("정규장", "일시휴장")

    # 정리 15:33 → 16:33, 20:05는 그대로.
    at = datetime(2026, 11, 19, 10, 5)
    assert main._market_dt(at, 15, 33) == datetime(2026, 11, 19, 16, 33)
    assert main._market_dt(at, 20, 5, shift=False) == datetime(2026, 11, 19, 20, 5)
    # 지나면 다음 날 것(평일 시각).
    assert main._market_dt(datetime(2026, 11, 19, 21), 15, 33) == datetime(2026, 11, 20, 15, 33)

    # 점상 줄: 10:03 전까지 「시가 전」이 산다.
    real = config.market_delay
    config.market_delay = lambda day=None: real(suneung)
    try:
        assert gui._opening_at(9, 3) == "100300"
        model = types.SimpleNamespace(krx_opened=set(), _touch_krx_day=lambda: None)
        gui.StockModel.note_krx_vi(model, "A", True, "정적", "100010")   # 시가 VI
        gui.StockModel.note_krx_vi(model, "B", True, "정적", "090010")   # 장전
        assert model.krx_opened == {"B"}
    finally:
        config.market_delay = real
    print("ok (수능일 10시 개장·16:30 마감, 연초 첫 거래일 10시 개장)")


if __name__ == "__main__":
    demo()
