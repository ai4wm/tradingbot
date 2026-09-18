# -*- coding: utf-8 -*-
"""WICS 및 KRX 상장법인 분류 조회."""
from __future__ import annotations

import asyncio
import html
import re
from html.parser import HTMLParser

import httpx


_WICS_RE = re.compile(r"WICS\s*:\s*([^<\r\n]+)", re.IGNORECASE)
_TAG_RE = re.compile(r"<[^>]+>")

# 새 네이버 증권 화면이 쓰는 JSON. 옛 `sise/theme.naver`는 여기로
# 리다이렉트되고 옛 DOM은 남아 있지 않다(2026-09-18 확인).
NAVER_THEME_LIST_URL = "https://m.stock.naver.com/api/stocks/theme"
NAVER_THEME_DETAIL_URL = "https://m.stock.naver.com/api/stocks/theme"
NAVER_PAGE_SIZE = 100
NAVER_API_HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Referer": "https://m.stock.naver.com/",
}


class _TableParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.rows: list[list[str]] = []
        self._row: list[str] | None = None
        self._cell: list[str] | None = None

    def handle_starttag(self, tag, _attrs):
        if tag == "tr":
            self._row = []
        elif tag in ("td", "th") and self._row is not None:
            self._cell = []

    def handle_data(self, data):
        if self._cell is not None:
            self._cell.append(data)

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self._cell is not None:
            self._row.append(" ".join("".join(self._cell).split()))
            self._cell = None
        elif tag == "tr" and self._row is not None:
            if self._row:
                self.rows.append(self._row)
            self._row = None


class ClassificationClient:
    USER_AGENT = "Mozilla/5.0 (compatible; trading-bot/1.0)"

    def __init__(self):
        self._client = httpx.AsyncClient(
            timeout=20.0, follow_redirects=True,
            headers={"User-Agent": self.USER_AGENT},
        )

    async def close(self):
        await self._client.aclose()

    async def wics_for_stocks(
        self, stock_codes: list[str], concurrency: int = 5, progress=None
    ) -> dict[str, str]:
        """네이버 기업정보에 공개된 WICS 분류를 종목별로 조회한다.

        종목 수가 많아 수 분이 걸린다. progress(done, total)로 진행을 알린다.
        """
        semaphore = asyncio.Semaphore(concurrency)
        total = len(stock_codes)
        done = 0

        async def one(code: str):
            nonlocal done
            async with semaphore:
                url = (
                    "https://navercomp.wisereport.co.kr/v2/company/"
                    f"c1010001.aspx?cmp_cd={code}&cn="
                )
                try:
                    response = await self._client.get(url)
                    response.raise_for_status()
                    match = _WICS_RE.search(response.text)
                finally:
                    done += 1
                    if progress is not None:
                        progress(done, total)
                if not match:
                    return code, ""
                return code, html.unescape(match.group(1)).strip()

        results = await asyncio.gather(
            *(one(code) for code in stock_codes), return_exceptions=True)
        return {
            code: label for result in results
            if not isinstance(result, Exception)
            for code, label in [result] if label
        }

    async def krx_classifications(self) -> dict[str, str]:
        """KIND 상장법인 목록의 KRX 업종 분류를 반환한다."""
        response = await self._client.get(
            "https://kind.krx.co.kr/corpgeneral/corpList.do"
            "?method=download&searchType=13"
        )
        response.raise_for_status()
        parser = _TableParser()
        parser.feed(response.content.decode("euc-kr", "replace"))
        mapping: dict[str, str] = {}
        for row in parser.rows[1:]:
            if len(row) < 5:
                continue
            code, industry = row[2].strip(), row[3].strip()
            if code and industry:
                mapping[code] = industry
        return mapping

    async def naver_themes(
        self, progress=None, cancelled=None, concurrency: int = 4,
        known_codes: set[str] | None = None,
    ) -> list[dict]:
        """네이버 금융 테마를 수집하며 known_codes는 상세조회를 건너뛴다.

        2026-09-18. `finance.naver.com/sise/theme.naver`가 새 화면
        (`stock.naver.com/market/stock/kr/theme`)으로 넘어가면서 옛 DOM을
        긁던 정규식이 0개를 돌려줬다. 조용히 0개였지 예외가 아니라서
        09-15 수집이 `COMPLETED · 테마 0개`로 끝났고 기존 6,421건이
        통째로 만료됐다. 이제 새 화면이 쓰는 JSON을 그대로 받는다.
        """
        themes: dict[str, str] = {}
        page, pages = 1, 1
        while page <= pages:
            response = await self._client.get(
                f"{NAVER_THEME_LIST_URL}?page={page}&pageSize={NAVER_PAGE_SIZE}",
                headers=NAVER_API_HEADERS)
            response.raise_for_status()
            payload = response.json()
            groups = payload.get("groups") or []
            if not groups:
                break
            # 마지막 다음 페이지는 빈 목록이 아니라 404다. 총건수로 끊는다.
            total = int(payload.get("totalCount") or 0)
            if total:
                pages = -(-total // NAVER_PAGE_SIZE)
            for group in groups:
                number = str(group.get("no") or "").strip()
                name = html.unescape(str(group.get("name") or "")).strip()
                if number and name:
                    themes.setdefault(number, name)
            page += 1
        if not themes:
            # 0개를 정상으로 넘기면 저장 단계가 기존 연결을 전부 만료시킨다.
            raise RuntimeError(
                "네이버 테마 목록이 비어 있습니다. 원문 화면이 또 바뀐 것입니다.")

        if known_codes is not None:
            themes = {
                number: name for number, name in themes.items()
                if number not in known_codes
            }

        semaphore = asyncio.Semaphore(concurrency)
        total = len(themes)
        completed = 0

        async def one(number: str, name: str):
            nonlocal completed
            if cancelled and cancelled():
                return None
            async with semaphore:
                response = await self._client.get(
                    f"{NAVER_THEME_DETAIL_URL}/{number}"
                    "?page=1&pageSize=100",
                    headers=NAVER_API_HEADERS)
                response.raise_for_status()
                members = list(dict.fromkeys(
                    str(stock.get("itemCode") or "").strip()
                    for stock in (response.json().get("stocks") or [])
                    if str(stock.get("itemCode") or "").strip()
                ))
            completed += 1
            if progress:
                progress(completed, total, name, len(members))
            return {"code": number, "name": name, "members": members}

        results = await asyncio.gather(
            *(one(number, name) for number, name in themes.items()),
            return_exceptions=True,
        )
        errors = [result for result in results if isinstance(result, Exception)]
        if errors:
            raise errors[0]
        return [result for result in results if result is not None]
