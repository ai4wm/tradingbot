# -*- coding: utf-8 -*-
"""해외 선행지표 공개 시세 조회.

Yahoo Finance의 공개 chart 응답을 소량 조회한다. 공식 보장 API가 아니므로
화면에는 출처·원본 시각·지연 상태를 반드시 함께 표시한다.
"""
import asyncio
from datetime import datetime, timezone
from urllib.parse import quote

import httpx


INDICATORS = {
    "SOX": ("^SOX", "필라델피아 반도체"),
    "NASDAQ_FUT": ("NQ=F", "나스닥100 선물"),
    "USDKRW": ("KRW=X", "원/달러"),
}


class GlobalMarketClient:
    BASE_URL = "https://query1.finance.yahoo.com/v8/finance/chart"

    async def fetch_all(self) -> tuple[list[dict], list[str]]:
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 Chrome/124 Safari/537.36"
            )
        }
        async with httpx.AsyncClient(
                timeout=12.0, headers=headers, follow_redirects=True) as client:
            results = await asyncio.gather(*(
                self._fetch_one(client, code, symbol, name)
                for code, (symbol, name) in INDICATORS.items()
            ), return_exceptions=True)
        rows, errors = [], []
        for code, result in zip(INDICATORS, results):
            if isinstance(result, Exception):
                errors.append(f"{code}: {result}")
            else:
                rows.append(result)
        return rows, errors

    async def _fetch_one(
        self, client: httpx.AsyncClient, code: str, symbol: str, name: str,
    ) -> dict:
        response = await client.get(
            f"{self.BASE_URL}/{quote(symbol, safe='')}",
            params={"interval": "1m", "range": "1d"},
        )
        response.raise_for_status()
        payload = response.json().get("chart") or {}
        if payload.get("error"):
            raise RuntimeError(str(payload["error"]))
        result = (payload.get("result") or [None])[0]
        if not result:
            raise RuntimeError("시세 응답이 비어 있습니다.")
        meta = result.get("meta") or {}
        timestamps = result.get("timestamp") or []
        quotes = (
            ((result.get("indicators") or {}).get("quote") or [{}])[0]
            .get("close") or []
        )
        latest = next((
            (timestamp, value)
            for timestamp, value in reversed(list(zip(timestamps, quotes)))
            if value is not None
        ), None)
        if latest:
            observed_epoch, value = latest
        else:
            observed_epoch = int(meta.get("regularMarketTime") or 0)
            value = meta.get("regularMarketPrice")
        if not observed_epoch or value is None:
            raise RuntimeError("현재값 또는 원본 시각이 없습니다.")
        previous_close = (
            meta.get("chartPreviousClose")
            or meta.get("previousClose")
        )
        value = float(value)
        previous_close = (
            float(previous_close) if previous_close not in (None, 0) else None)
        change_rate = (
            round((value - previous_close) * 100 / previous_close, 4)
            if previous_close else None
        )
        observed_at = datetime.fromtimestamp(
            int(observed_epoch), timezone.utc).astimezone().isoformat(
                timespec="seconds")
        return {
            "indicator_code": code,
            "indicator_name": name,
            "symbol": symbol,
            "value": value,
            "previous_close": previous_close,
            "change_rate": change_rate,
            "observed_at": observed_at,
            "currency": str(meta.get("currency") or ""),
            "exchange": str(
                meta.get("fullExchangeName")
                or meta.get("exchangeName") or ""),
            "source": "YAHOO_CHART",
        }
