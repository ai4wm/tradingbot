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
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_SPACE_RE = re.compile(r"\s+")
_TRACKING_QUERY_KEYS = {
    "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
    "fbclid", "gclid", "nclick", "sm", "from",
}


def _plain_text(value: str) -> str:
    text = html.unescape(_HTML_TAG_RE.sub("", str(value or "")))
    return _SPACE_RE.sub(" ", text).strip()


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
            normalized_title = re.sub(
                r"[^0-9a-z가-힣]+", "", title.lower())
            duplicate_key = hashlib.sha256(
                normalized_title.encode("utf-8")).hexdigest()
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
