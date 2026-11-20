# -*- coding: utf-8 -*-
"""당일 순위와 화면에서 비교 후보를 추린다. 최종 대장 판단은 근거와 함께 AI가 한다."""
from __future__ import annotations

import math
import re


def _number(value):
    try:
        result = float(str(value or 0).replace(",", "").replace("%", ""))
        return result if math.isfinite(result) else 0
    except (ValueError, TypeError):
        return 0


def select_leaders(db, now, quotes, rankings, limit=12):
    """이전 대화/관심종목을 받지 않는다. 오늘 자료가 없으면 비교를 중단한다."""
    today = now.date().isoformat()
    compact = today.replace("-", "")
    pool, warnings = {}, []
    latest = None
    if db is not None:
        # 두 날짜 표기를 모두 사용하며 기존 (trade_date, stock_code) 색인을 살린다.
        latest = db.execute("SELECT MAX(replace(trade_date,'-','')) FROM daily_prices").fetchone()[0]
        sql = ("SELECT p.*,s.stock_name FROM daily_prices p JOIN stocks s USING(stock_code) "
               "WHERE p.trade_date IN (?,?) AND p.change_rate>0 ")
        daily = list(db.execute(sql + "ORDER BY p.change_rate DESC LIMIT 200", (today, compact)))
        daily += list(db.execute(sql + "ORDER BY p.trading_value DESC LIMIT 100", (today, compact)))
        for row in daily:
            r = dict(row)
            pool[r["stock_code"]] = {
                "stock_code": r["stock_code"], "stock_name": r["stock_name"],
                "price": r["close_price"], "change_rate": r["change_rate"],
                "volume": r["volume"], "trading_value": r["trading_value"],
                "at_limit": bool(r["upper_price"] and r["close_price"] == r["upper_price"]),
                "consecutive_days": None, "source": "당일 저장 일봉",
                "as_of": r["collected_at"], "market": r["source"],
            }
    screen_count = 0
    for code, quote in quotes.items():
        if not re.fullmatch(r"[0-9A-Z]{6}", code):
            continue
        # 복사 날짜는 틱 수신 날짜를 보증하지 않는다. 이 한계는 아래에 명시한다.
        stamp = str(quote.get("copied_at") or "")
        if stamp[:10] != today:
            continue
        v = quote.get("values") or {}
        price = _number(v.get("price"))
        if price <= 0 or _number(v.get("rate")) <= 0:
            continue
        screen_count += 1
        pool[code] = {
            "stock_code": code, "stock_name": v.get("name", ""),
            "price": price, "change_rate": _number(v.get("rate")),
            "volume": _number(v.get("vol")), "trading_value": _number(v.get("acc_value")),
            "at_limit": bool(_number(v.get("upper")) > 0 and price == _number(v.get("upper"))),
            "consecutive_days": quote.get("consecutive_days"),
            "bid_qty": v.get("bid_qty"), "ask_qty": v.get("ask_qty"),
            "source": "앱 화면", "as_of": stamp,
            "market": quote.get("display_market", "기록 없음"),
        }
    rank_counts = {}
    if rankings and str(rankings.get("finished_at") or "")[:10] != today:
        warnings.append("조회 날짜가 오늘이 아닌 순위 자료를 제외했습니다.")
        rankings = {"warnings": rankings.get("warnings", [])}
    for key in ("change_rate", "trading_value"):
        rows = (rankings or {}).get(key, [])
        rank_counts[key] = len(rows)
        for r in rows:
            code = str(r.get("code") or "")
            if not re.fullmatch(r"[0-9A-Z]{6}", code) or _number(r.get("rate")) <= 0:
                continue
            old = pool.get(code, {})
            # KRX 순위와 통합 화면의 가격·잔량을 하나의 시세로 섞지 않는다.
            same = old.get("market") == "KRX" and old.get("price") == _number(r.get("price"))
            entry = dict(old) if same else {"stock_code": code}
            entry.update(stock_name=r.get("name", ""), price=_number(r.get("price")),
                         change_rate=_number(r.get("rate")), market="KRX",
                         source="키움 KRX 당일 순위", as_of=(rankings or {}).get("finished_at"))
            if key == "trading_value":
                entry.update(trading_value=_number(r.get("trading_value")),
                             volume=_number(r.get("volume")))
            entry[key + "_rank"] = r.get("rank")
            # 서로 다른 거래소의 화면 연상 숫자는 별도 참고 자료로 남긴다.
            if old.get("consecutive_days") is not None and not same:
                entry["screen_streak_reference"] = {
                    "consecutive_days": old["consecutive_days"], "market": old.get("market"),
                    "as_of": old.get("as_of"), "at_limit": old.get("at_limit")}
            pool[code] = entry
    if db is not None:
        events = db.execute(
            "SELECT stock_code,consecutive_days,first_hit_time,last_entry_time,break_count "
            "FROM limit_up_events WHERE trade_date IN (?,?)", (today, compact))
        for r in events:
            if r["stock_code"] in pool:
                pool[r["stock_code"]]["today_limit_event"] = dict(r)
    if len(pool) < 2:
        raise ValueError("오늘 대장을 비교할 당일 후보 자료가 부족합니다. "
                         "키움 연결과 당일 시세를 확인해 주세요. 이전 분석 종목이나 과거 일봉으로 대신 비교하지 않습니다.")
    if screen_count:
        warnings.append("화면 복사 시각은 틱 수신 시각이 아닙니다. 화면 시세의 당일 수신 여부와 지연은 확인되지 않았습니다.")
    if latest and str(latest).replace("-", "") != compact:
        warnings.append(f"저장 일봉 최신일 {latest}: 당일 후보 선정에는 과거 일봉을 사용하지 않았습니다.")
    warnings.extend((rankings or {}).get("warnings", []))
    if not all(rank_counts.values()):
        warnings.append("당일 상승률·거래대금 순위 중 누락된 자료가 있습니다. 확인된 범위에서만 비교합니다.")

    def strength(r):
        streak = r.get("consecutive_days")
        if streak is None:
            streak = (r.get("today_limit_event") or {}).get("consecutive_days")
        if streak is None:
            ref = r.get("screen_streak_reference") or {}
            streak = ref.get("consecutive_days") if ref.get("at_limit") else 0
        return (_number(streak), bool(r.get("at_limit")), _number(r.get("change_rate")),
                _number(r.get("trading_value")), r["stock_code"])

    strong = sorted(pool.values(), key=strength, reverse=True)
    money = sorted(pool.values(), key=lambda r: (_number(r.get("trading_value")),
                   _number(r.get("change_rate")), r["stock_code"]), reverse=True)
    # 연상·상한가 후보와 거래대금 후보를 함께 확보. 단순 대금 순으로 점상을 배제하지 않는다.
    chosen = {}
    for row in strong[:limit // 2] + money[:limit // 2] + strong:
        chosen.setdefault(row["stock_code"], row)
        if len(chosen) >= limit:
            break
    for row in chosen.values():
        row["selection_reason"] = []
        if row in strong[:limit // 2]:
            row["selection_reason"].append("연상·상한가·상승률 후보")
        if row in money[:limit // 2]:
            row["selection_reason"].append("상승 종목 중 거래대금 후보")
    return list(chosen), {
        "task": "today_leaders", "trade_date": today,
        "scope": "키움 KRX 상승률·거래대금 상위와 앱 화면 및 당일 저장 시세. 전 종목 전수 비교는 아님.",
        "rank_counts": rank_counts, "screen_positive_count": screen_count,
        "observed_positive_count": len(pool), "detail_count": len(chosen),
        "selection_rule": "강도 상위 6개와 거래대금 상위 6개를 합친 후 중복분을 강도 순으로 보충. 최종 대장 순위 아님.",
        "candidates": list(chosen.values()), "warnings": warnings,
        "missing": "분봉 강도·선도 시간·테마 동반 상승·상한가 유지 이력의 실시간 비교는 불완전함",
    }
