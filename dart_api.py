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

    async def close(self):
        await self._client.aclose()
