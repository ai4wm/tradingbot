# -*- coding: utf-8 -*-
"""네이버 공식 뉴스 검색 API 클라이언트.

네이버 금융 페이지나 종목토론실 HTML은 읽지 않는다. 이 모듈은 공식
Search API가 반환한 제목·요약·링크·게시시각만 정규화한다.
"""
from __future__ import annotations

import hashlib
import html
import re
from datetime import datetime
from email.utils import parsedate_to_datetime
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import aiohttp


NEWS_SEARCH_URL = "https://openapi.naver.com/v1/search/news.json"
# 네이버 증권 종목뉴스. 검색 API와 달리 **네이버가 그 종목에 직접 매핑한**
# 기사만 온다. 「[특징주] 전기장비株, 아마존 AI 전력장비 계약」처럼 제목에
# 종목명이 없는 섹터 기사도 여기 걸려 있으면 그 종목 재료가 맞다.
# 검색 API는 종목명으로 찾는 것이라 이 판정을 못 한다.
STOCK_NEWS_URL = "https://m.stock.naver.com/api/news/stock"
STOCK_NEWS_HEADERS = {
    "User-Agent": "Mozilla/5.0",
    "Referer": "https://m.stock.naver.com/",
}
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_SPACE_RE = re.compile(r"\s+")
_TRACKING_QUERY_KEYS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "fbclid", "gclid", "nclick", "sm", "from",
}


def _plain_text(value: str) -> str:
    text = html.unescape(_HTML_TAG_RE.sub("", str(value or "")))
    return _SPACE_RE.sub(" ", text).strip()


def title_key(title: str) -> str:
    """제목만으로 같은 기사를 알아보는 열쇠.

    검색 API와 종목뉴스 API는 기사 식별자가 서로 다르다(URL 대 officeId·
    articleId). 둘 다 주는 것은 제목뿐이라 그것으로 맞춘다.
    """
    normalized = re.sub(
        r"[^0-9a-z가-힣]+", "", _plain_text(title).lower())
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()


def _canonical_url(value: str) -> str:
    value = str(value or "").strip()
    if not value:
        return ""
    try:
        parts = urlsplit(value)
        query = [
            (key, item) for key, item in parse_qsl(
                parts.query, keep_blank_values=True)
            if key.lower() not in _TRACKING_QUERY_KEYS
        ]
        return urlunsplit((
            parts.scheme.lower(), parts.netloc.lower(), parts.path,
            urlencode(query, doseq=True), "",
        ))
    except ValueError:
        return value


def _published_at(value: str) -> str:
    try:
        parsed = parsedate_to_datetime(str(value or ""))
        if parsed.tzinfo is None:
            parsed = parsed.astimezone()
        return parsed.astimezone().isoformat(timespec="seconds")
    except (TypeError, ValueError, OverflowError):
        return ""


def _material_type(text: str) -> tuple[str, float]:
    rules = (
        ("수주·공급계약", ("수주", "공급계약", "납품", "계약 체결")),
        ("실적·전망", ("실적", "영업이익", "매출", "흑자", "적자", "전망")),
        ("자금조달", ("유상증자", "무상증자", "전환사채", "cb", "bw",
                    "신주인수권", "자금조달")),
        ("M&A·지분", ("인수", "합병", "m&a", "지분 취득", "최대주주")),
        ("정책·규제", ("정부", "정책", "규제", "법안", "국회", "관세")),
        ("임상·허가", ("임상", "품목허가", "식약처", "fda", "승인")),
        ("기술·제품", ("신제품", "신기술", "특허", "개발", "출시")),
        ("배당·자사주", ("배당", "자사주", "주식 소각")),
        ("경영권·인사", ("경영권", "대표이사", "임원", "인사")),
        ("소송·제재", ("소송", "제재", "과징금", "압수수색", "수사")),
        ("원자재·공급망", ("원자재", "공급망", "유가", "희토류")),
        ("산업·테마", ("테마", "관련주", "수혜주", "업종")),
        ("루머·미확인", ("설", "루머", "미확인", "조회공시")),
    )
    lowered = text.lower()
    for label, keywords in rules:
        if any(keyword in lowered for keyword in keywords):
            return label, 0.75
    return "기타", 0.35


class NaverNewsClient:
    def __init__(self, client_id: str, client_secret: str):
        self.client_id = str(client_id or "").strip()
        self.client_secret = str(client_secret or "").strip()

    async def search(self, query: str, display: int = 100) -> list[dict]:
        if not self.client_id or not self.client_secret:
            raise RuntimeError("네이버 뉴스 API 키가 없습니다.")
        headers = {
            "X-Naver-Client-Id": self.client_id,
            "X-Naver-Client-Secret": self.client_secret,
        }
        params = {
            "query": str(query or "").strip(),
            "display": max(1, min(100, int(display))),
            "start": 1,
            "sort": "date",
        }
        timeout = aiohttp.ClientTimeout(total=15)
        async with aiohttp.ClientSession(
                timeout=timeout, headers=headers) as session:
            async with session.get(NEWS_SEARCH_URL, params=params) as response:
                text = await response.text()
                if response.status != 200:
                    raise RuntimeError(
                        f"네이버 뉴스 API HTTP {response.status}: "
                        f"{text[:300]}")
                payload = await response.json(content_type=None)

        result = []
        for raw in payload.get("items") or []:
            title = _plain_text(raw.get("title"))
            summary = _plain_text(raw.get("description"))
            original_url = str(raw.get("originallink") or "").strip()
            naver_url = str(raw.get("link") or "").strip()
            canonical = _canonical_url(original_url or naver_url)
            if not canonical:
                continue
            source_key = hashlib.sha256(
                canonical.encode("utf-8")).hexdigest()
            current_hash = hashlib.sha256(
                f"{title}\n{summary}\n{canonical}".encode("utf-8")
            ).hexdigest()
            duplicate_key = title_key(title)
            host = urlsplit(original_url or naver_url).netloc.lower()
            material, confidence = _material_type(f"{title} {summary}")
            result.append({
                "source_item_key": source_key,
                "canonical_url": canonical,
                "original_url": original_url,
                "naver_url": naver_url,
                "publisher": host,
                "published_at_source": _published_at(raw.get("pubDate")),
                "title": title,
                "summary": summary,
                "current_hash": current_hash,
                "duplicate_key": duplicate_key,
                "material_type": material,
                "material_confidence": confidence,
            })
        return result

    async def stock_news_keys(self, stock_code: str,
                              page_size: int = 40) -> set[str]:
        """네이버가 그 종목에 걸어 둔 기사들의 제목 열쇠.

        인증이 필요 없는 공개 화면 API다. 실패해도 예외를 올리지 않는다 —
        이것이 없으면 제목에 종목명이 있는 기사만 붙던 예전 동작으로
        돌아갈 뿐이라, 뉴스 수집 전체를 멈출 이유가 없다.
        """
        stock_code = str(stock_code or "").strip()
        if not stock_code:
            return set()
        url = f"{STOCK_NEWS_URL}/{stock_code}?pageSize={int(page_size)}&page=1"
        timeout = aiohttp.ClientTimeout(total=10)
        try:
            async with aiohttp.ClientSession(
                    timeout=timeout, headers=STOCK_NEWS_HEADERS) as session:
                async with session.get(url) as response:
                    if response.status != 200:
                        return set()
                    payload = await response.json(content_type=None)
        except Exception:  # noqa: BLE001 - 보조 신호라 조용히 포기한다.
            return set()
        # 응답은 [{"total":n,"items":[...]}, ...] 꼴로 블록이 여럿 온다.
        keys = set()
        for block in payload if isinstance(payload, list) else [payload]:
            if not isinstance(block, dict):
                continue
            for item in block.get("items") or []:
                title = _plain_text(item.get("title"))
                if title:
                    keys.add(title_key(title))
        return keys
