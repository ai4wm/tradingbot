# -*- coding: utf-8 -*-
"""OpenDART 기업코드와 공시 조회."""
from __future__ import annotations

import io
import xml.etree.ElementTree as ET
import zipfile

import httpx


class DartClient:
    BASE = "https://opendart.fss.or.kr/api"

    def __init__(self, api_key: str):
        self.api_key = api_key
        self._client = httpx.AsyncClient(timeout=30.0)

    async def corp_codes(self) -> dict[str, str]:
        response = await self._client.get(
            f"{self.BASE}/corpCode.xml", params={"crtfc_key": self.api_key})
        response.raise_for_status()
        with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
            xml_data = archive.read(archive.namelist()[0])
        root = ET.fromstring(xml_data)
        return {
            (item.findtext("stock_code") or "").strip():
            (item.findtext("corp_code") or "").strip()
            for item in root.findall("list")
            if (item.findtext("stock_code") or "").strip()
        }

    async def disclosures(
        self, corp_code: str, date_from: str, date_to: str,
    ) -> list[dict]:
        page = 1
        out = []
        while True:
            response = await self._client.get(
                f"{self.BASE}/list.json",
                params={
                    "crtfc_key": self.api_key,
                    "corp_code": corp_code,
                    "bgn_de": date_from,
                    "end_de": date_to,
                    "page_no": page,
                    "page_count": 100,
                },
            )
            response.raise_for_status()
            data = response.json()
            status = data.get("status")
            if status == "013":  # 조회된 데이터 없음
                return out
            if status != "000":
                raise RuntimeError(
                    f"OpenDART {status}: {data.get('message', '조회 실패')}")
            out.extend(data.get("list", []))
            if page >= int(data.get("total_page") or 1):
                return out
            page += 1

    async def largest_shareholders(
        self, corp_code: str, business_year: str,
    ) -> list[dict]:
        """사업보고서의 최대주주 현황을 가져온다.

        최대주주명이 상장 종목명과 일치할 때만 지주사·자회사 관계 후보로 쓴다.
        """
        response = await self._client.get(
            f"{self.BASE}/hyslrSttus.json",
            params={
                "crtfc_key": self.api_key,
                "corp_code": corp_code,
                "bsns_year": str(business_year),
                "reprt_code": "11011",  # 사업보고서
            },
        )
        response.raise_for_status()
        data = response.json()
        if data.get("status") == "013":
            return []
        if data.get("status") != "000":
            raise RuntimeError(
                f"OpenDART {data.get('status')}: "
                f"{data.get('message', '조회 실패')}")
        return list(data.get("list") or [])

    async def close(self):
        await self._client.aclose()
