# -*- coding: utf-8 -*-
"""틱 기록(data/ticks)에서 종목별 프로필과 이상 순간을 뽑는다.

사람과 AI가 같이 쓴다: `python tick_profile.py 002990 [날짜] [--구간 HH:MM-HH:MM]`

**기준은 전부 그 종목 자신의 기록에서 계산한다.** 종목마다 평균 체결 크기, 프로그램 증가
속도, 호가 두께가 달라서(마녀공장 프로그램 증가분 평균 146주, 금양그린파워 27주) 전체에
같은 숫자를 쓰면 안 된다. 표본이 적으면 표본 수와 함께 「표본부족」으로 표시한다.

임계값(배수·최소 수량)은 시험값이다. 정하는 것은 사용자이고 여기서 매매나 알림에
연결하지 않는다. 인자로 바꿀 수 있다.
"""
from __future__ import annotations

import json
import math
import os
import statistics
import sys
from pathlib import Path

from tick_recorder import TICK_DIR

MIN_SAMPLES = 30          # 이보다 적으면 통계에 「표본부족」을 붙인다
LEVELS = range(1, 11)


def num(value) -> int:
    try:
        return int(float(str(value).replace("+", "").replace(" ", "") or 0))
    except ValueError:
        return 0


def secs(clock: str) -> float:
    return int(clock[0:2]) * 3600 + int(clock[3:5]) * 60 + float(clock[6:])


def tick_size(price: int) -> int:
    """KRX 주권 호가단위. 가격대 경계에서는 실제와 다를 수 있다."""
    for limit, size in ((2000, 1), (5000, 5), (20000, 10), (50000, 50),
                        (200000, 100), (500000, 500)):
        if price < limit:
            return size
    return 1000


def load(code: str, date: str, root: str = TICK_DIR) -> list[dict]:
    """KRX·통합 파일을 시간순으로 합친다(`_NX`는 별도 시장이라 뺀다)."""
    rows = []
    for name in (f"{code}.jsonl", f"{code}_AL.jsonl"):
        path = Path(root) / date / name
        if path.is_file():
            with open(path, encoding="utf-8") as file:
                rows += [json.loads(line) for line in file if line.strip()]
    rows.sort(key=lambda r: r["t"])
    return rows


def window(rows: list[dict], span: str | None) -> list[dict]:
    if not span:
        return rows
    start, end = span.split("-")
    return [r for r in rows if start <= r["t"][:5] <= end]


def pearson(x: list[float], y: list[float]):
    n = len(x)
    if n < 3:
        return None
    mx, my = sum(x) / n, sum(y) / n
    sx = math.sqrt(sum((a - mx) ** 2 for a in x))
    sy = math.sqrt(sum((b - my) ** 2 for b in y))
    return None if sx == 0 or sy == 0 else sum((a - mx) * (b - my) for a, b in zip(x, y)) / (sx * sy)


def trades(rows):
    return [(secs(r["t"]), abs(num(r["v"].get("10"))), abs(num(r["v"].get("15"))), r)
            for r in rows if r["ty"] == "0B" and r["v"].get("10")]


def price_at(series, t):
    last = None
    for ts, price, *_ in series:
        if ts > t:
            break
        last = price
    return last


def program_deltas(rows, series):
    """연속된 0w 누적값의 차이 = 그 구간에 새로 체결된 프로그램 수량."""
    w = [r for r in rows if r["ty"] == "0w" and r["v"].get("206") is not None]
    out = []
    for a, b in zip(w, w[1:]):
        dt = secs(b["t"]) - secs(a["t"])
        if dt <= 0:
            continue
        buy = num(b["v"]["206"]) - num(a["v"]["206"])
        sell = num(b["v"]["202"]) - num(a["v"]["202"])
        pa, pb = price_at(series, secs(a["t"])), price_at(series, secs(b["t"]))
        out.append({"t": b["t"][:8], "dt": dt, "buy": buy, "sell": sell, "net": buy - sell,
                    "vol": num(b["v"].get("13")) - num(a["v"].get("13")),
                    "dprice": (pb - pa) if pa and pb else None})
    return out


def book_stats(rows):
    """호가 구조: 단위 %, 1% 안 호가 수, 평균 잔량, 빈 호가, 1% 이동 비용."""
    snaps = [r["v"] for r in rows if r["ty"] == "0D"]
    prices, empty_a, empty_b, avg_a, avg_b, cost_up, cost_down, cover = ([] for _ in range(8))
    for v in snaps:
        ap = [abs(num(v.get(str(40 + i), 0))) for i in LEVELS]
        aq = [abs(num(v.get(str(60 + i), 0))) for i in LEVELS]
        bp = [abs(num(v.get(str(50 + i), 0))) for i in LEVELS]
        bq = [abs(num(v.get(str(70 + i), 0))) for i in LEVELS]
        if not ap[0] or not bp[0]:
            continue
        prices.append(bp[0])
        empty_a.append(sum(q == 0 for q in aq))
        empty_b.append(sum(q == 0 for q in bq))
        na, nb = [q for q in aq if q], [q for q in bq if q]
        avg_a.append(statistics.mean(na) if na else 0)
        avg_b.append(statistics.mean(nb) if nb else 0)
        cost_up.append(sum(p * q for p, q in zip(ap, aq) if p and p <= ap[0] * 1.01))
        cost_down.append(sum(p * q for p, q in zip(bp, bq) if p and p >= bp[0] * 0.99))
        far = max((p for p, q in zip(ap, aq) if p), default=ap[0])
        cover.append((far - ap[0]) / ap[0] * 100)
    if not prices:
        return None
    price = statistics.median(prices)
    size = tick_size(int(price))
    pct = size / price * 100
    mean = statistics.mean
    return {"snapshots": len(prices), "price": price, "tick": size, "tick_pct": pct,
            "levels_in_1pct": 1 / pct, "empty_ask": mean(empty_a), "empty_bid": mean(empty_b),
            "avg_ask_qty": mean(avg_a), "avg_bid_qty": mean(avg_b),
            "ask_money": mean(avg_a) * price, "bid_money": mean(avg_b) * price,
            "cost_up_1pct": mean(cost_up), "cost_down_1pct": mean(cost_down),
            "ask_cover_pct": mean(cover)}


def profile(rows):
    series = trades(rows)
    sizes = [q for _, _, q, _ in series if q]
    deltas = program_deltas(rows, series)
    result = {"trades": len(series), "book": book_stats(rows), "program": None, "trade_size": None}
    if sizes:
        ordered = sorted(sizes)
        result["trade_size"] = {"mean": statistics.mean(sizes), "median": statistics.median(sizes),
                                "p95": ordered[int(len(ordered) * 0.95) - 1 if len(ordered) > 20 else -1],
                                "max": ordered[-1]}
    if deltas:
        speed = [d["buy"] / d["dt"] * 60 for d in deltas]
        usable = [d for d in deltas if d["dprice"] is not None]
        prog_total = sum(d["buy"] + d["sell"] for d in deltas)
        vol_total = sum(d["vol"] for d in deltas)
        result["program"] = {
            "updates": len(deltas), "interval_median": statistics.median(d["dt"] for d in deltas),
            "buy_speed_mean": statistics.mean(speed),   # 주/분
            "dependence": prog_total / vol_total * 100 if vol_total > 0 else None,
            "corr_net_price": pearson([d["net"] for d in usable], [d["dprice"] for d in usable]),
            "corr_n": len(usable)}
    result["_deltas"], result["_series"] = deltas, series
    return result


def events(rows, prof, *, trade_x=20.0, program_x=8.0, wall_x=8.0, wall_min=1000,
           wall_seconds=60.0, wall_traded=0.2, walls=None):
    """이 종목 평소 대비 이상 순간. 임계값은 시험값이다. walls 리스트를 주면 벽 정보를 담아 준다."""
    found = []
    ts = prof["trade_size"]
    if ts:
        for t, price, qty, r in prof["_series"]:
            if qty >= max(ts["mean"] * trade_x, 1):
                found.append((r["t"][:8], f"대량 체결 {qty:,}주 = 평균 {ts['mean']:,.0f}주의 {qty / ts['mean']:.0f}배 @{price:,}"))
    pr = prof["program"]
    if pr and pr["buy_speed_mean"] > 0:
        for d in prof["_deltas"]:
            speed = d["buy"] / d["dt"] * 60
            if d["buy"] > 0 and speed >= pr["buy_speed_mean"] * program_x:
                found.append((d["t"], f"프로그램 매수 +{d['buy']:,}주/{d['dt']:.0f}초 = 평소 속도의 {speed / pr['buy_speed_mean']:.0f}배"
                              f"{' (표본부족)' if pr['updates'] < MIN_SAMPLES else ''}"))
    # 호가 벽은 총잔량(121 매도·125 매수)의 증감으로 잡는다. 최우선 호가 잔량은 가격이 한 칸
    # 움직이면 다른 호가의 잔량이 올라와 뛰므로 새 주문과 구별이 안 된다. 총잔량은 새 주문·취소·
    # 체결로만 변한다. 어느 가격에 걸렸는지는 10호가에서 가격별로 따로 찾는다.
    def levels(v, price0, qty0):
        return {abs(num(v.get(str(price0 + i)))): abs(num(v.get(str(qty0 + i)))) for i in LEVELS
                if abs(num(v.get(str(price0 + i))))}

    book = [(secs(r["t"]), r["t"][:12], abs(num(r["v"].get("121"))), abs(num(r["v"].get("125"))),
             levels(r["v"], 40, 60), levels(r["v"], 50, 70), abs(num(r["v"].get("61"))),
             abs(num(r["v"].get("71")))) for r in rows if r["ty"] == "0D" and r["v"].get("121")]
    if book and prof["book"]:
        base_a = statistics.median(b[6] for b in book) or 1       # 평소 최우선 잔량 한 칸
        base_b = statistics.median(b[7] for b in book) or 1
        series = prof["_series"]
        for side, total_idx, level_idx, base in (("매도", 2, 4, base_a), ("매수", 3, 5, base_b)):
            threshold = max(base * wall_x, wall_min)
            for i in range(1, len(book)):
                gain = book[i][total_idx] - book[i - 1][total_idx]
                if gain < threshold:
                    continue
                for j in range(i + 1, len(book)):
                    if book[j][0] - book[i][0] > wall_seconds:
                        break
                    if book[j][total_idx] <= book[i - 1][total_idx] + gain * 0.2:      # 80% 이상 사라짐
                        traded = sum(q for t, _, q, _ in series if book[i][0] <= t <= book[j][0])
                        if traded <= gain * wall_traded:
                            added = {pr: q - book[i - 1][level_idx].get(pr, 0) for pr, q in book[i][level_idx].items()}
                            pr, q = max(added.items(), key=lambda kv: kv[1], default=(0, 0))
                            where = f"{pr:,}원에 +{q:,}주" if q >= gain * 0.5 else "10호가 밖이거나 여러 호가"
                            if walls is not None:
                                walls.append({"side": side, "price": pr if q >= gain * 0.5 else 0, "qty": gain,
                                              "t": book[i][1][:8]})
                            found.append((book[i][1][:8], f"{side} 총잔량 +{gain:,}주 ({where}) 출현 → {book[j][0] - book[i][0]:.0f}초 뒤 소멸, "
                                          f"그 사이 체결 {traded:,}주 (평소 한 칸 {base:,.0f}주의 {gain / base:.0f}배)"))
                        break
    return sorted(found)


def repeated_walls(walls: list[dict], tolerance=0.10, minimum=3):
    """같은 쪽·같은 가격·비슷한 수량이 반복된 벽을 묶는다. 같은 주체일 수 있다는 단서일 뿐이다."""
    groups: list[list[dict]] = []
    for w in sorted(walls, key=lambda w: w["t"]):
        if not w["price"]:
            continue
        for g in groups:
            ref = g[0]
            if ref["side"] == w["side"] and ref["price"] == w["price"] and abs(w["qty"] - ref["qty"]) <= ref["qty"] * tolerance:
                g.append(w)
                break
        else:
            groups.append([w])
    return [(g[0]["side"], g[0]["price"], statistics.mean(x["qty"] for x in g), len(g), g[0]["t"], g[-1]["t"])
            for g in groups if len(g) >= minimum]


def fmt(value, spec=",.0f", none="-"):
    return none if value is None else format(value, spec)


def report(code: str, date: str, span: str | None = None, root: str = TICK_DIR, **limits) -> str:
    rows = window(load(code, date, root), span)
    if not rows:
        return f"{code} {date}: 기록이 없습니다 ({root})"
    prof = profile(rows)
    out = [f"== {code} {date}{' ' + span if span else ''} · 기록 {rows[0]['t'][:8]}~{rows[-1]['t'][:8]} · 틱 {len(rows):,}건 =="]
    ts, pr, bk = prof["trade_size"], prof["program"], prof["book"]
    if ts:
        out.append(f"[체결] {prof['trades']:,}건 · 1건 평균 {ts['mean']:,.0f}주 · 중앙 {ts['median']:,.0f} · 상위5% {ts['p95']:,} · 최대 {ts['max']:,}"
                   f"{' (표본부족)' if prof['trades'] < MIN_SAMPLES else ''}")
    if pr:
        corr = pr["corr_net_price"]
        out.append(f"[프로그램] 갱신 {pr['updates']}회(중앙 간격 {pr['interval_median']:.0f}초) · 매수 평균 속도 {pr['buy_speed_mean']:,.0f}주/분"
                   f" · 의존도 {fmt(pr['dependence'], '.1f')}% · 순매수 증가분↔가격변화 상관 {fmt(corr, '+.2f', '계산불가')}"
                   f"(구간 {pr['corr_n']}{', 표본부족' if pr['corr_n'] < MIN_SAMPLES else ''})")
    if bk:
        out.append(f"[호가] 단위 {bk['tick']}원 = 가격 {bk['price']:,.0f}원의 {bk['tick_pct']:.2f}% · 1% 안 호가 {bk['levels_in_1pct']:.1f}개"
                   f" · 10호가가 덮는 범위 약 {bk['ask_cover_pct']:.2f}%{' (1%를 다 못 봄)' if bk['ask_cover_pct'] < 1 else ''}")
        out.append(f"       평균 잔량 매도 {bk['avg_ask_qty']:,.0f}주({bk['ask_money'] / 1e8:.2f}억)·매수 {bk['avg_bid_qty']:,.0f}주({bk['bid_money'] / 1e8:.2f}억)"
                   f" · 빈 호가 매도 {bk['empty_ask']:.1f}·매수 {bk['empty_bid']:.1f}칸")
        out.append(f"       1% 올리는 데 필요한 금액 약 {bk['cost_up_1pct'] / 1e8:.2f}억 · 1% 내리는 데 필요한 물량 약 {bk['cost_down_1pct'] / 1e8:.2f}억 (10호가 안에서만 계산)")
    walls: list[dict] = []
    found = events(rows, prof, walls=walls, **limits)
    repeats = repeated_walls(walls)
    if repeats:
        out.append("[반복 패턴] 같은 쪽·같은 가격에 비슷한 수량(±10%)이 3번 이상 나타났다 사라짐")
        out += [f"  {side} {price:,}원 약 {qty:,.0f}주 × {n}회 ({first}~{last})" for side, price, qty, n, first, last in repeats]
    out.append(f"[이상 순간] {len(found)}건 (이 종목 평소 대비, 시험값 기준)")
    out += [f"  {t} {text}" for t, text in found[:40]]
    return "\n".join(out)


def main(argv: list[str]) -> int:
    args = [a for a in argv if not a.startswith("--")]
    opts = dict(a[2:].split("=", 1) for a in argv if a.startswith("--") and "=" in a)
    if not args:
        print(__doc__)
        return 1
    import time
    date = args[1] if len(args) > 1 else time.strftime("%Y%m%d")
    limits = {k: float(v) for k, v in opts.items() if k in ("trade_x", "program_x", "wall_x", "wall_min", "wall_seconds", "wall_traded")}
    print(report(args[0], date, opts.get("구간"), **limits))
    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except AttributeError:
        pass
    raise SystemExit(main(sys.argv[1:]))
