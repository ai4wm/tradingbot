# -*- coding: utf-8 -*-
"""KRX Data Marketplace 일별 주식 매매정보 클라이언트."""
from __future__ import annotations

import httpx


class KrxClient:
    BASE = "https://data-dbg.krx.co.kr/svc/apis/sto"
    PATHS = {"KOSPI": "stk_bydd_trd", "KOSDAQ": "ksq_bydd_trd"}

    def __init__(self, api_key: str):
        self._client = httpx.AsyncClient(
            headers={"AUTH_KEY": api_key}, timeout=30.0)

    async def daily_market(self, trade_date: str) -> list[dict]:
        rows = []
        for market, path in self.PATHS.items():
            response = await self._client.get(
                f"{self.BASE}/{path}", params={"basDd": trade_date})
            if response.status_code == 401:
                raise RuntimeError(
                    "KRX 인증 실패(401): 유가증권·코스닥 일별매매정보 "
                    "API 이용신청 및 승인 상태를 확인해 주세요.")
            response.raise_for_status()
            data = response.json()
            if data.get("respCode") not in (None, "200"):
                raise RuntimeError(
                    f"KRX {data.get('respCode')}: "
                    f"{data.get('respMsg', '조회 실패')}")
            for row in data.get("OutBlock_1", []):
                row["_market"] = market
                rows.append(row)
        return rows

    async def close(self):
        await self._client.aclose()
