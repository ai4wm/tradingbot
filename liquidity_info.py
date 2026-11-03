# -*- coding: utf-8 -*-
"""한국투자증권 공개 종목마스터의 저유동성 지정 정보를 읽는다.

별도 계좌나 API 키는 필요하지 않다. 고정폭 명세는 한국투자증권 공식 예제:
https://github.com/koreainvestment/open-trading-api/tree/main/stocks_info
"""
from __future__ import annotations

import asyncio
import io
import re
import zipfile

import httpx


MASTER_URL = "https://new.real.download.dws.co.kr/common/master/{}_code.mst.zip"
RECORD_LENGTHS = {"kospi": 288, "kosdaq": 282}
# 공통: 단축코드 9 + ISIN 12 + 한글명 40바이트 뒤 고정폭 필드.
# 그룹 2 + 시총규모 1 + 업종 4*3 + 제조/벤처 1 다음이 저유동성 Y/N이다.
LOW_LIQUIDITY_OFFSET = 61 + 16
MAX_MASTER_BYTES = 8 * 1024 * 1024


def parse_low_liquidity(content: bytes, market: str) -> set[str]:
    """마스터 형식이 달라지면 추정하지 않고 실패시킨다."""
    codes: set[str] = set()
    rows = content.splitlines()
    if not rows:
        raise ValueError(f"{market} 종목마스터가 비어 있습니다")
    for row in rows:
        if len(row) != RECORD_LENGTHS[market]:
            raise ValueError(f"{market} 종목마스터 레코드 길이가 변경됐습니다")
        if row[61:63] != b"ST":  # 채권 등은 9자리 단축코드도 함께 내려온다
            continue
        code = row[:9].strip()
        flag = row[LOW_LIQUIDITY_OFFSET:LOW_LIQUIDITY_OFFSET + 1]
        if not re.fullmatch(rb"[0-9A-Z]{6}", code) or flag not in (b"Y", b"N"):
            raise ValueError(f"{market} 종목마스터 코드/저유동성 값이 올바르지 않습니다")
        if flag == b"Y":
            codes.add(code.decode("ascii"))
    return codes


async def fetch_low_liquidity() -> set[str]:
    """양 시장의 현재 공개 마스터를 조회한다. 파일은 메모리에서만 해제한다."""
    async with httpx.AsyncClient(timeout=15.0, follow_redirects=True) as client:
        async def one(market: str) -> set[str]:
            response = await client.get(MASTER_URL.format(market))
            response.raise_for_status()
            if len(response.content) > MAX_MASTER_BYTES:
                raise ValueError("종목마스터 압축파일이 너무 큽니다")
            with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
                member = archive.getinfo(f"{market}_code.mst")
                if member.file_size > MAX_MASTER_BYTES:
                    raise ValueError("종목마스터 파일이 너무 큽니다")
                content = archive.read(member)
            if len(content.splitlines()) < 500:
                raise ValueError(f"{market} 종목마스터가 불완전합니다")
            return parse_low_liquidity(content, market)

        results = await asyncio.gather(*(one(market) for market in RECORD_LENGTHS))
    return set().union(*results)
