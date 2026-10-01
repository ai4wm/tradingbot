# -*- coding: utf-8 -*-
"""연상을 종목마다 제 일봉으로 세고, 그날 결과를 파일에 남기는지 확인한다.

전에는 전일 상한 목록(ka10017)에 기댔는데 08시 전에 켜면 빈 목록이 와서
연상이 전부 사라졌다(2026-09-29 07:45). 이제 편입 종목마다 일봉 한 번으로
전일거래량과 연상을 함께 채우고, 같은 날 재실행하면 다시 조회하지 않는다.
운영 캐시 파일은 건드리지 않는다 — 경로를 임시 폴더로 바꿔 끼운다.
"""
import asyncio
import logging
import os
import tempfile
import types
from datetime import date, datetime
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import main  # noqa: E402
from api import RestClient  # noqa: E402

logging.disable(logging.CRITICAL)  # 운영 bot.log에 쓰지 않는다
TODAY = datetime.now().strftime("%Y%m%d")


def _bar(dt, close, pred_pre=0, qty=1):
    return {"dt": dt, "cur_prc": str(close), "pred_pre": f"{pred_pre:+d}",
            "trde_qty": str(qty)}


def _summary(rows, today_base=0):
    rest = RestClient.__new__(RestClient)

    async def request(api_id, body, path=""):
        return {"stk_dt_pole_chart_qry": rows}

    rest.request = request
    return asyncio.run(RestClient.daily_summary(rest, "000000", today_base))


def demo_cur_prc_is_last_trade():
    """2026-09-29 실측 원본(HLB글로벌 003580). cur_prc는 애프터마켓까지 본
    마지막 체결가라 09-23 정규장 종가(1,583 = 09-28 기준가)와 다르다."""
    rows = [_bar(TODAY, 2420, +365, 2_244_211),
            _bar("20260928", 2055, +472, 397_165),
            _bar("20260923", 1608, +29, 72_861)]
    info = _summary(rows)
    assert info == {"prev_vol": 397_165, "streak": 1, "yclose": 2055,
                    "last_date": "20260928"}, info
    print("ok (기준가 = cur_prc - pred_pre)")


def demo_regular_close_streak():
    """씨싸이트 09-29: 정규장 종가 기준 2연상(사용자 결정).

    09-23은 정규장 9,900원(+12.88%)에 풀렸다가 애프터마켓에서 11,400원
    상한으로 끝났다. 세지 않는다 — 09-28 상한 12,870원이 9,900 × 1.3이다.
    인접 cur_prc끼리 나누던 옛 방식은 12,870 / 11,400 = +12.9%로 0이었다.
    """
    rows = [_bar("20260928", 12870, +2970, 37_055),
            _bar("20260923", 11400, +2630, 898_662),
            _bar("20260922", 8770, +2020), _bar("20260921", 6870, +210)]
    info = _summary(rows, today_base=12870)     # 장 전: 오늘 행 없음
    assert info["streak"] == 1 and info["yclose"] == 12870, info  # +오늘 = 2

    # 애프터마켓 없이 사흘 연속 상한이면 그대로 3이다.
    clean = [_bar("20260928", 21970, +5070), _bar("20260923", 16900, +3900),
             _bar("20260922", 13000, +3000), _bar("20260921", 10000, 0)]
    assert _summary(clean, today_base=21970)["streak"] == 3
    print("ok (정규장 종가 = 다음 날 기준가 · 씨싸이트 2연상)")


def demo_halt_day_is_skipped():
    """씨싸이트 10-01: 09-29 상한 뒤 09-30 매매거래정지(거래량 0). 정지일은
    가격이 그대로라 0%로 끊겼다. 건너뛰고 그 앞을 이어 센다."""
    rows = [_bar("20260930", 16730, 0, 0),
            _bar("20260929", 16730, +3860, 500_000),
            _bar("20260928", 12870, +2970, 37_055),
            _bar("20260923", 11400, +2630, 898_662)]
    info = _summary(rows, today_base=16730)
    assert info["streak"] == 2 and info["prev_vol"] == 0, info
    print("ok (거래정지일은 건너뛰고 센다)")


def demo_listing_day_is_not_limit():
    """2026-09-29 상장한 486510: 첫날 +45%는 상한가가 아니다(연상 1이 떴다).

    첫날 기준가는 공모가고 가격제한폭이 60~400%다. 공모가 × 4로 끝난
    날만 상한가다.
    """
    listing = [_bar("20260929", 18220, +5655)]          # 공모가 12,565 → +45%
    assert _summary(listing, today_base=18220)["streak"] == 0
    capped = [_bar("20260929", 48000, +36000)]           # 공모가 12,000 × 4
    assert _summary(capped, today_base=48000)["streak"] == 1
    print("ok (상장 첫날 +45%는 상한 아님, ×4만 상한)")


def _needs_ref(rows, code, ref):
    """StockModel.set_prev_vol_ref와 같은 판정(대역용)."""
    current = int(rows[code].get("prev_vol") or 0)
    return bool(ref) and (not current or not (ref / 5 <= current <= ref * 5))


def demo_broken_prev_vol_is_rejected():
    """2026-09-30 윈팩(정답 619,917): 처음 600,000, 그다음 60,619,847로 떴다."""
    from PySide6.QtWidgets import QApplication
    import gui
    QApplication.instance() or QApplication([])
    m = gui.StockModel()
    m.unified = True
    m.add_stock("097800", {"name": "윈팩"})               # KRX 전용
    m.update_stock("097800", {"prev_vol": 600_000})        # 기준 전: 들어감
    assert m.set_prev_vol_ref("097800", 619_917)           # 틀린 값은 바꿈
    m.update_stock("097800", {"prev_vol": 619_917})
    for broken in (600_000, 60_619_847, 620_000):          # 계산값은 전부 버림
        m.update_stock("097800", {"prev_vol": broken, "_real_suffix": "_AL"})
        assert m.rows["097800"]["prev_vol"] == 619_917, broken

    # NXT 종목·통합 모드: 통합 전일거래량은 KRX 일봉값 이상 5배 이하만.
    m.nxt.add("028300")
    m.add_stock("028300", {"name": "HLB"})
    m.set_prev_vol_ref("028300", 344_213)
    m.update_stock("028300", {"prev_vol": 867_169})        # 통합 2.5배: 받음
    assert m.rows["028300"]["prev_vol"] == 867_169
    for broken in (300_000, 60_000_000):                   # 일봉보다 작거나 튐
        m.update_stock("028300", {"prev_vol": broken})
        assert m.rows["028300"]["prev_vol"] == 867_169, broken
    assert not m.set_prev_vol_ref("028300", 344_213)       # 정상 통합값은 둠
    print("ok (KRX 전용은 일봉값만, NXT 통합은 일봉 이상 5배 이하)")


def _app(tmp):
    app = types.SimpleNamespace(
        views=[], _prevvol_pending=set(), _prevvol_queue=[],
        _daily_retry_at={}, _limit_cnt={})
    app._daily_cache_path = lambda: Path(tmp) / "daily_summary.json"
    app._start_prevvol_workers = lambda: None
    app._show_streak_refresh_status = lambda: None
    app._save_daily_cache = lambda: main.App._save_daily_cache(app)
    app._daily_cache = main.App._load_daily_cache(app)
    return app


def demo_cache_survives_restart():
    with tempfile.TemporaryDirectory() as tmp:
        app = _app(tmp)
        expected = main._previous_trading_day(date.today())
        info = {"prev_vol": 100, "streak": 2, "yclose": 5000,
                "last_date": expected}

        async def summary(code, today_base=0):
            return dict(info)

        app.rest = types.SimpleNamespace(daily_summary=summary)
        asyncio.run(main.App._fetch_prev_vol(app, "000001"))
        assert app._limit_cnt == {"000001": (2, 5000)}, app._limit_cnt

        # 같은 날 재실행: 파일에서 읽고, 그 종목은 다시 큐에 넣지 않는다.
        again = _app(tmp)
        assert again._daily_cache["000001"]["streak"] == 2
        rows = {"000001": {"prev_vol": 0}, "000002": {"prev_vol": 0}}
        pushed = []
        model = types.SimpleNamespace(
            codes=list(rows), rows=rows, refresh_streaks=lambda: None,
            set_prev_vol_ref=lambda c, ref: _needs_ref(rows, c, ref),
            update_stock=lambda c, f: pushed.append((c, f)))
        main.App.ensure_prev_vol(again, model)
        assert again._prevvol_queue == ["000002"], again._prevvol_queue
        assert pushed == [("000001", {"prev_vol": 100})], pushed
        assert again._limit_cnt == {"000001": (2, 5000)}, again._limit_cnt

    print("ok (그날 결과는 재실행해도 다시 조회 안 함)")


def demo_unified_prev_vol_is_kept():
    """일봉 거래량은 KRX분이다. 편입 조회가 준 통합 전일거래량을 덮지 않는다
    (HLB 09-28: 통합 86만 주, 일봉 34만 주)."""
    with tempfile.TemporaryDirectory() as tmp:
        app = _app(tmp)
        ticks = []
        rows = {"028300": {"prev_vol": 867_169, "base": 39700},
                "003580": {"prev_vol": 0, "base": 2055}}
        screen = types.SimpleNamespace(
            model=types.SimpleNamespace(
                rows=rows, refresh_streaks=lambda: None,
                set_prev_vol_ref=lambda c, ref: _needs_ref(rows, c, ref)),
            on_tick=lambda c, f: ticks.append((c, f)))
        app.views = [types.SimpleNamespace(screen=screen)]
        expected = main._previous_trading_day(date.today())

        async def summary(code, today_base=0):
            return {"prev_vol": 344_213, "streak": 1, "yclose": today_base,
                    "last_date": expected}

        app.rest = types.SimpleNamespace(daily_summary=summary)
        for code in rows:
            asyncio.run(main.App._fetch_prev_vol(app, code))
        assert ticks == [("003580", {"prev_vol": 344_213})], ticks
        assert app._limit_cnt["028300"] == (1, 39700)   # 오늘 기준가를 넘겼다
    print("ok (통합 전일거래량은 덮지 않음)")


def demo_stale_morning_chart_is_not_kept():
    """이른 아침 일봉이 직전 거래일을 안 담고 있으면 저장하지 않고 다시 받는다."""
    class Morning(datetime):
        @classmethod
        def now(cls, tz=None):
            return datetime.combine(date.today(), datetime.min.time()).replace(
                hour=8, minute=10)

    real = main.datetime
    main.datetime = Morning
    try:
        _stale_morning()
    finally:
        main.datetime = real
    print("ok (묵은 아침 일봉은 저장 안 하고 5분 뒤 재시도)")


def _stale_morning():
    with tempfile.TemporaryDirectory() as tmp:
        app = _app(tmp)

        async def summary(code, today_base=0):
            return {"prev_vol": 1, "streak": 0, "yclose": 1,
                    "last_date": "20000101"}

        app.rest = types.SimpleNamespace(daily_summary=summary)
        asyncio.run(main.App._fetch_prev_vol(app, "000001"))
        assert "000001" not in app._daily_cache
        assert "000001" in app._daily_retry_at


def demo_yesterday_file_is_dropped():
    with tempfile.TemporaryDirectory() as tmp:
        (Path(tmp) / "daily_summary.json").write_text(
            '{"date": "20000101", "rows": {"000001": {"streak": 3}}}',
            encoding="utf-8")
        assert _app(tmp)._daily_cache == {}
        # 같은 날이라도 계산 방식이 바뀐 저장분은 버린다.
        (Path(tmp) / "daily_summary.json").write_text(
            '{"date": "%s", "rows": {"000001": {"streak": 0}}}' % TODAY,
            encoding="utf-8")
        assert _app(tmp)._daily_cache == {}
    print("ok (어제 저장분은 버림)")


def demo_refresh_shows_progress():
    """연상 재수집을 누르면 상태줄에 진행도와 완료가 뜬다."""
    shown = []
    label = types.SimpleNamespace(setText=shown.append)
    app = types.SimpleNamespace(
        _analysis=types.SimpleNamespace(_collection_status=label),
        _prevvol_pending={"000001", "000002"}, _streak_refresh_total=2)
    main.App._show_streak_refresh_status(app)
    app._prevvol_pending.clear()
    main.App._show_streak_refresh_status(app)
    main.App._show_streak_refresh_status(app)          # 끝난 뒤엔 조용하다
    assert shown == ["연상 재수집 중 · 0/2", "연상 재수집 완료 · 2종목"], shown
    assert app._streak_refresh_total == 0
    print("ok (재수집 진행·완료 표시)")


if __name__ == "__main__":
    demo_refresh_shows_progress()
    demo_cur_prc_is_last_trade()
    demo_regular_close_streak()
    demo_halt_day_is_skipped()
    demo_listing_day_is_not_limit()
    demo_broken_prev_vol_is_rejected()
    demo_cache_survives_restart()
    demo_unified_prev_vol_is_kept()
    demo_stale_morning_chart_is_not_kept()
    demo_yesterday_file_is_dropped()
