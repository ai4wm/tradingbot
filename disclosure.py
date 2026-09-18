# -*- coding: utf-8 -*-
"""제3자배정 유상증자 공시 본문에서 강도를 재는 숫자를 뽑는다.

본문은 LS `t3102`가 주는 DART 원문 HTML이다. 제목만으로는 279억인지
100억인지, 유통주식의 3할인지 8할인지 알 수 없는데 그 차이가 결과를
가른다(2026-09: 미투온 희석 30.0% / 앤씨앤 79.7%).

**라벨 철자를 믿지 않는다.** 본문이 EUC-KR로 오는 과정에서 글자가
군데군데 깨지고, 깨지는 자리가 종목마다 다르다. 같은 날 두 공시에서
앤씨앤은 `4. 자금조달�� 목적`이고 미투온은 멀쩡했다. 그래서 큰 항목
번호가 아니라 안 깨진 하위 라벨(`운영자금 (원)` 등)에 건다.
"""
from __future__ import annotations

import html
import re

# 숫자 칸은 `1,234` 또는 `-`(해당없음)로 온다.
_NUMBER = re.compile(r"^-?[\d,]+$")
_DATE = re.compile(r"(\d{4})\s*년\s*(\d{1,2})\s*월\s*(\d{1,2})\s*일")

FUND_LABELS = {
    "시설자금": "facility",
    "영업양수자금": "business",
    "운영자금": "operating",
    "채무상환자금": "debt",
    "타법인 증권": "affiliate",
    "기타자금": "etc",
}


def flatten_disclosure(body: str) -> list[str]:
    """공시 HTML을 라벨·값이 한 줄씩 번갈아 오는 줄 목록으로 만든다."""
    text = re.sub(r"(?is)<(script|style|head).*?</\1>", " ", str(body or ""))
    text = html.unescape(re.sub(r"<[^>]+>", "\n", text)).replace("\xa0", " ")
    return [line.strip() for line in text.splitlines() if line.strip()]


def _int(value: str) -> int | None:
    value = str(value or "").strip()
    if not _NUMBER.match(value) or value == "-":
        return None
    try:
        return int(value.replace(",", ""))
    except ValueError:
        return None


# 해당없음 칸. 이걸 건너뛰면 다음 항목 금액을 제 것으로 집어온다
# (시설자금 `-` 다음이 운영자금 100억이라 시설투자 공시로 둔갑했다).
_EMPTY_CELL = {"-", "–", "—", "0"}


def _value_after(lines: list[str], start: int, within: int = 6) -> int | None:
    """라벨 줄 뒤 첫 숫자 칸. 표가 라벨-값 순서로 평평해진다.

    빈 칸(`-`)을 만나면 거기서 끝난다. 그 항목이 비었다는 뜻이다.
    """
    for line in lines[start + 1:start + 1 + within]:
        if line in _EMPTY_CELL:
            return None
        got = _int(line)
        if got is not None:
            return got
    return None


def _find(lines: list[str], *prefixes: str, start: int = 0) -> int:
    for index in range(start, len(lines)):
        if lines[index].startswith(prefixes):
            return index
    return -1


def parse_rights_offering(body: str) -> dict | None:
    """유상증자결정 공시에서 수량·가격·용도·최대주주 변경을 뽑는다.

    유상증자 공시가 아니면 None. 못 읽은 항목은 None으로 남긴다 — 0으로
    채우면 희석률이 0%로 계산돼 약한 공시로 둔갑한다.
    """
    lines = flatten_disclosure(body)
    if not lines:
        return None
    joined = "\n".join(lines)
    index = _find(lines, "1. 신주의 종류와 수")
    if index < 0:
        return None

    new_shares = _value_after(lines, index)
    # `3. 증자전` 아래에 `발행주식총수 (주)` → `보통주식 (주)` 순으로 온다.
    before_index = _find(lines, "3. 증자전")
    shares_before = None
    if before_index >= 0:
        total_index = _find(lines, "발행주식총수", start=before_index)
        if total_index >= 0:
            shares_before = _value_after(lines, total_index)

    price_index = _find(lines, "6. 신주 발행가액")
    price = _value_after(lines, price_index) if price_index >= 0 else None
    base_index = _find(lines, "7. 기준주가")
    base_price = _value_after(lines, base_index) if base_index >= 0 else None

    funds = {}
    for label, key in FUND_LABELS.items():
        found = _find(lines, label)
        funds[key] = _value_after(lines, found) if found >= 0 else None

    pay_index = _find(lines, "9. 납입일")
    pay_date = ""
    if pay_index >= 0:
        for line in lines[pay_index + 1:pay_index + 4]:
            matched = _DATE.search(line)
            if matched:
                pay_date = "%04d%02d%02d" % tuple(
                    int(part) for part in matched.groups())
                break

    return {
        "new_shares": new_shares,
        "shares_before": shares_before,
        "price": price,
        "base_price": base_price,
        "funds": funds,
        "pay_date": pay_date,
        # 「최대주주로 변경」은 본문 자유기술에만 있고 표에는 없다. 미투온은
        # 「최대주주는 ㈜카카오게임즈로 변경될 예정」, 앤씨앤은 「주식회사
        # 비투엔은 … 최대주주로 변경될 예정」이라 사이 글자수가 다르다.
        "owner_change": bool(re.search(r"최대주주[^\n]{0,60}변경", joined)),
        "third_party": "제3자배정" in joined,
    }


# ponytail: 구간과 배점은 손으로 잡은 값이다. 표본이 쌓이면 실제 연속
# 상한가 일수로 회귀해 다시 잡는다. 지금은 두 사례(미투온 63 / 앤씨앤 93)가
# 체감 순서와 맞는지만 본다.
DILUTION_STEPS = ((0.50, 40), (0.30, 30), (0.15, 20), (0.05, 10))
RAISE_STEPS = ((0.50, 25), (0.30, 18), (0.15, 10), (0.05, 5))
FUND_POINTS = {
    "affiliate": 10,  # 타법인 증권 취득 = 인수·지배구조
    "facility": 6,    # 시설자금 = 증설
    "business": 6,
    "operating": 3,
    "etc": 3,
    "debt": 0,        # 채무상환 = 돈이 회사 밖으로 나간다
}
OWNER_CHANGE_POINTS = 20
NEAR_PAYMENT_POINTS = 5
NEAR_PAYMENT_DAYS = 30


def _step(value: float | None, steps) -> int:
    if value is None:
        return 0
    for threshold, points in steps:
        if value >= threshold:
            return points
    return 0


def score_rights_offering(parsed: dict, close_price: int = 0,
                          today: str = "") -> dict:
    """희석률·조달비중·최대주주 변경·자금용도·납입시점을 0~100으로 묶는다.

    `close_price`는 공시 시점 종가다. 없으면 조달/시총 항목이 0점이 되고
    희석률만으로 센다 — 없는 값을 추정해 점수를 부풀리지 않는다.
    """
    new_shares = parsed.get("new_shares")
    shares_before = parsed.get("shares_before")
    price = parsed.get("price")

    dilution = (
        new_shares / shares_before
        if new_shares and shares_before else None)
    raised = new_shares * price if new_shares and price else None
    market_cap = shares_before * close_price if shares_before and close_price else None
    raise_ratio = raised / market_cap if raised and market_cap else None

    funds = parsed.get("funds") or {}
    fund_points = max(
        (FUND_POINTS.get(key, 0)
         for key, amount in funds.items() if amount),
        default=0)

    near_payment = False
    pay_date = str(parsed.get("pay_date") or "")
    if pay_date and today and pay_date >= today:
        import datetime
        try:
            gap = (
                datetime.date(
                    int(pay_date[:4]), int(pay_date[4:6]), int(pay_date[6:]))
                - datetime.date(
                    int(today[:4]), int(today[4:6]), int(today[6:]))
            ).days
            near_payment = gap <= NEAR_PAYMENT_DAYS
        except ValueError:
            near_payment = False

    points = {
        "희석률": _step(dilution, DILUTION_STEPS),
        "조달/시총": _step(raise_ratio, RAISE_STEPS),
        "최대주주변경": (
            OWNER_CHANGE_POINTS if parsed.get("owner_change") else 0),
        "자금용도": fund_points,
        "납입임박": NEAR_PAYMENT_POINTS if near_payment else 0,
    }
    return {
        "score": sum(points.values()),
        "points": points,
        "dilution": dilution,
        "raised": raised,
        "market_cap": market_cap,
        "raise_ratio": raise_ratio,
        "premium": (
            price / parsed["base_price"] - 1
            if price and parsed.get("base_price") else None),
    }
