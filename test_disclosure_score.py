# -*- coding: utf-8 -*-
"""제3자배정 유상증자 공시 본문 파싱과 강도 점수를 확인한다.

본문 줄 순서는 2026-09-15 미투온(201490)과 09-16 앤씨앤(092600) 실수신분을
그대로 옮겼다. 앤씨앤 쪽 `4. 자금조달�� 목적`은 오타가 아니라 실제로 그렇게
들어온다 — 본문이 EUC-KR로 오면서 종목마다 다른 자리가 깨진다. 파서가 큰
항목 번호에 걸면 안 되는 이유가 이것이다.
"""
from disclosure import (
    FUND_LABELS, flatten_disclosure, parse_rights_offering,
    score_rights_offering)

ME2ON = """
1. 신주의 종류와 수
보통주식 (주)
9,817,118
기타주식 (주)
-
2. 1주당 액면가액 (원)
500
3. 증자전
발행주식총수 (주)
보통주식 (주)
32,723,726
기타주식 (주)
-
4. 자금조달의 목적
시설자금 (원)
-
영업양수자금 (원)
-
운영자금 (원)
19,249,970,356
채무상환자금 (원)
8,650,279,000
타법인 증권
취득자금 (원)
-
기타자금 (원)
-
5. 증자방식
제3자배정증자
6. 신주 발행가액
보통주식 (원)
2,842
기타주식 (원)
-
7. 기준주가
보통주식 (원)
2,842
기타주식 (원)
-
9. 납입일
2026년 11월 16일
마. 본 계약에 따른 거래종료 시 최대주주는 ㈜카카오게임즈로 변경될 예정입니다.
"""

NCAND = """
1. 신주의 종류와 수
보통주식 (주)
4,000,000
기타주식 (주)
-
2. 1주당 액면�±� (원)
2,500
3. 증자전
발행주식총수 (주)
보통주식 (주)
5,016,703
기타주식 (주)
-
4. 자금조달�� 목적
시설자금 (원)
-
영업양수자금 (원)
-
운영자금 (원)
10,000,000,000
채무상환자금 (원)
-
타법인 증권
취득자금 (원)
-
기타자금 (원)
-
5. 증자방식
제3자배정증자
6. 신주 발행가액
보통주식 (원)
2,500
기타주식 (원)
-
7. 기준주가
보통주식 (원)
2,180
기타주식 (원)
-
9. 납입일
2026년 09월 28일
마. 최대주주 변경에 관한 사항
본 유상증자가 예정대로 완료될 경우, 주식회사 비투엔은 보통주식 2,500,000주를
취득하여 당사의 최대주주로 변경될 예정입니다.
"""


def demo_parse():
    me2on = parse_rights_offering(ME2ON)
    assert me2on["new_shares"] == 9_817_118, me2on
    assert me2on["shares_before"] == 32_723_726, me2on
    assert me2on["price"] == 2_842, me2on
    assert me2on["base_price"] == 2_842, me2on
    assert me2on["pay_date"] == "20261116", me2on
    assert me2on["owner_change"] is True
    assert me2on["third_party"] is True

    ncand = parse_rights_offering(NCAND)
    # 라벨이 깨진 종목에서도 같은 값이 나와야 한다.
    assert ncand["new_shares"] == 4_000_000, ncand
    assert ncand["shares_before"] == 5_016_703, ncand
    assert ncand["price"] == 2_500, ncand
    assert ncand["base_price"] == 2_180, ncand
    assert ncand["pay_date"] == "20260928", ncand
    assert ncand["owner_change"] is True
    print("ok (본문 파싱)")


def demo_empty_cells():
    """`-` 칸에서 멈춰야 한다. 안 멈추면 다음 항목 금액을 제 것으로 집는다."""
    me2on = parse_rights_offering(ME2ON)["funds"]
    assert me2on["facility"] is None, me2on      # 시설자금 `-`
    assert me2on["business"] is None, me2on      # 영업양수 `-`
    assert me2on["operating"] == 19_249_970_356, me2on
    assert me2on["debt"] == 8_650_279_000, me2on
    assert me2on["affiliate"] is None, me2on     # 타법인 `-`
    ncand = parse_rights_offering(NCAND)["funds"]
    assert ncand["operating"] == 10_000_000_000, ncand
    # 앤씨앤은 채무상환이 `-`다. 여기서 안 멈추면 시설자금 100억이 된다.
    assert ncand["debt"] is None, ncand
    assert set(ncand) == set(FUND_LABELS.values())
    print("ok (해당없음 칸)")


def demo_score():
    """앤씨앤이 미투온보다 세다. 희석률과 조달비중이 갈랐다."""
    me2on = score_rights_offering(
        parse_rights_offering(ME2ON), close_price=2955, today="20260915")
    ncand = score_rights_offering(
        parse_rights_offering(NCAND), close_price=2175, today="20260916")

    assert abs(me2on["dilution"] - 0.300) < 0.001, me2on["dilution"]
    assert abs(ncand["dilution"] - 0.797) < 0.001, ncand["dilution"]
    assert abs(me2on["raise_ratio"] - 0.289) < 0.002, me2on["raise_ratio"]
    assert abs(ncand["raise_ratio"] - 0.916) < 0.002, ncand["raise_ratio"]
    # 미투온은 할인율 0%, 앤씨앤은 액면가 제약으로 14.7% 할증이다.
    assert abs(me2on["premium"]) < 0.001, me2on["premium"]
    assert abs(ncand["premium"] - 0.1468) < 0.001, ncand["premium"]

    assert ncand["score"] > me2on["score"], (ncand["score"], me2on["score"])
    assert me2on["score"] == 63, me2on["points"]
    assert ncand["score"] == 93, ncand["points"]
    # 납입일: 미투온 62일 뒤(공정위 심사), 앤씨앤 12일 뒤.
    assert me2on["points"]["납입임박"] == 0
    assert ncand["points"]["납입임박"] == 5
    print(f"ok (강도 점수) 미투온 {me2on['score']} · 앤씨앤 {ncand['score']}")


def demo_guards():
    # 유상증자 공시가 아니면 None. 다른 공시 본문에 붙으면 안 된다.
    assert parse_rights_offering("") is None
    assert parse_rights_offering(
        "타인에대한채무보증결정\n보증금액 (원)\n1,000,000,000") is None
    # 종가를 모르면 조달/시총만 0점이고 나머지는 센다. 추정해 채우지 않는다.
    blind = score_rights_offering(parse_rights_offering(NCAND))
    assert blind["market_cap"] is None
    assert blind["raise_ratio"] is None
    assert blind["points"]["조달/시총"] == 0
    assert blind["points"]["희석률"] == 40
    # HTML로 와도 같은 줄이 나온다.
    assert flatten_disclosure("<p>가<br>나</p>") == ["가", "나"]
    print("ok (방어)")


if __name__ == "__main__":
    demo_parse()
    demo_empty_cells()
    demo_score()
    demo_guards()
