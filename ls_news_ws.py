# -*- coding: utf-8 -*-
"""LS증권 NWS 실시간 뉴스 제목 스트림.

OAuth 접근토큰을 발급한 뒤 NWS/NWS001을 구독하고, 정상 뉴스 패킷만
``LSNewsItem``으로 정규화한다. UI와 분리해 재접속 및 파싱을 독립적으로
검증할 수 있도록 유지한다.
"""
import asyncio
import html
import json
import logging
import re
import time
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Callable
from urllib.parse import urlsplit

import httpx
import websockets

import config


log = logging.getLogger("ls_news_ws")

# LS NWS는 매체명을 보내지 않고 2자리 뉴스구분자(id)만 보낸다.
# t3102 상세 본문의 바이라인·기자 도메인·저작권 문구를 여러 건 대조해
# 공급처가 확인된 값만 고정한다. 제휴 기사의 원문 매체와 공급처는 다를 수 있다.
NEWS_SOURCE_NAMES = {
    "11": "연합뉴스",
    "14": "이데일리",
    "15": "한국거래소",
    "20": "머니투데이",
    "21": "인포스탁",
    "23": "아시아경제",
    "24": "뉴스핌",
    "25": "매일경제",
    "26": "한국경제",
    "27": "헤럴드경제",
    "28": "로이터",
    "29": "코리아헤럴드",
    "30": "파이낸셜뉴스",
    "31": "이투데이",
    "32": "조선비즈",
    "33": "데이터투자",
    "34": "연합인포맥스",
    "35": "서울경제",
    "37": "뉴스웨이",
}

# 본문에 남는 기자 이메일 도메인은 언론사 식별자로 오인 가능성이 낮다.
NEWS_SOURCE_DOMAINS = {
    "yna.co.kr": "연합뉴스",
    "newsis.com": "뉴시스",
    "news1.kr": "뉴스1",
    "krx.co.kr": "한국거래소",
    "hankyung.com": "한국경제",
    "wowtv.co.kr": "한국경제TV",
    "datatooza.com": "데이터투자",
    "infostock.co.kr": "인포스탁",
    "infostockdaily.co.kr": "인포스탁",
    "pharmnews.com": "팜뉴스",
    "mk.co.kr": "매일경제",
    "asiae.co.kr": "아시아경제",
    "einfomax.co.kr": "연합인포맥스",
    "edaily.co.kr": "이데일리",
    "reuters.com": "로이터",
    "thomsonreuters.com": "로이터",
    "koreaherald.com": "코리아헤럴드",
    "mt.co.kr": "머니투데이",
    "sedaily.com": "서울경제",
    "seadaily.com": "서울경제TV",
    "fnnews.com": "파이낸셜뉴스",
    "chosun.com": "조선일보",
    "chosunbiz.com": "조선비즈",
    "joongang.co.kr": "중앙일보",
    "donga.com": "동아일보",
    "hani.co.kr": "한겨레",
    "khan.co.kr": "경향신문",
    "heraldcorp.com": "헤럴드경제",
    "newspim.com": "뉴스핌",
    "newsway.co.kr": "뉴스웨이",
    "etnews.com": "전자신문",
    "dt.co.kr": "디지털타임스",
    "etoday.co.kr": "이투데이",
    "ajunews.com": "아주경제",
    "bizwatch.co.kr": "비즈워치",
}

NEWS_SOURCE_ALIASES = {
    "연합뉴스": ("연합뉴스",),
    "뉴시스": ("뉴시스",),
    "뉴스1": ("뉴스1",),
    "한국거래소": ("한국거래소", "KRX"),
    "한국경제": ("한국경제",),
    "한국경제TV": ("한국경제TV", "한경TV"),
    "데이터투자": ("데이터투자", "DataTooza"),
    "인포스탁": ("인포스탁", "인포스탁데일리"),
    "팜뉴스": ("팜뉴스",),
    "매일경제": ("매일경제",),
    "아시아경제": ("아시아경제",),
    "연합인포맥스": ("연합인포맥스", "인포맥스"),
    "이데일리": ("이데일리",),
    "로이터": ("로이터", "Reuters", "Thomson Reuters", "톰슨로이터"),
    "코리아헤럴드": ("코리아헤럴드", "The Korea Herald"),
    "머니투데이": ("머니투데이",),
    "서울경제": ("서울경제",),
    "서울경제TV": ("서울경제TV",),
    "파이낸셜뉴스": ("파이낸셜뉴스",),
    "조선일보": ("조선일보",),
    "조선비즈": ("조선비즈",),
    "중앙일보": ("중앙일보",),
    "동아일보": ("동아일보",),
    "한겨레": ("한겨레",),
    "경향신문": ("경향신문",),
    "헤럴드경제": ("헤럴드경제",),
    "뉴스핌": ("뉴스핌",),
    "뉴스웨이": ("뉴스웨이", "Newsway"),
    "전자신문": ("전자신문",),
    "디지털타임스": ("디지털타임스",),
    "이투데이": ("이투데이",),
    "아주경제": ("아주경제",),
    "비즈워치": ("비즈워치",),
}


@dataclass(frozen=True)
class LSNewsItem:
    date: str
    time: str
    title: str
    source_id: str
    realkey: str
    code: str = ""
    body_size: int = 0


@dataclass(frozen=True)
class LSNewsDetail:
    title: str
    body: str
    stock_codes: tuple[str, ...]


def parse_nws_message(message: dict) -> LSNewsItem | None:
    """LS 웹소켓 메시지 중 실제 NWS 뉴스 제목 패킷만 반환한다."""
    header = message.get("header")
    body = message.get("body")
    if not isinstance(header, dict) or not isinstance(body, dict):
        return None
    if str(header.get("tr_cd") or "").upper() != "NWS":
        return None
    title = " ".join(str(body.get("title") or "").split())
    if not title:
        return None
    try:
        body_size = int(str(body.get("bodysize") or "0").strip() or 0)
    except ValueError:
        body_size = 0
    return LSNewsItem(
        date=str(body.get("date") or "").strip(),
        time=str(body.get("time") or "").strip(),
        title=title,
        source_id=str(body.get("id") or "").strip(),
        realkey=str(body.get("realkey") or "").strip(),
        code=str(body.get("code") or "").strip(),
        body_size=body_size,
    )


def format_news_time(news_date: str, news_time: str) -> str:
    """오늘 뉴스는 시각만, 다른 날짜 뉴스는 월일까지 함께 표시한다."""
    digits = "".join(character for character in str(news_time) if character.isdigit())
    if len(digits) >= 6:
        clock = f"{digits[:2]}:{digits[2:4]}:{digits[4:6]}"
    elif digits:
        clock = digits
    else:
        clock = "-"
    news_date = "".join(
        character for character in str(news_date) if character.isdigit())
    today = time.strftime("%Y%m%d")
    if len(news_date) == 8 and news_date != today:
        return f"{news_date[4:6]}-{news_date[6:8]} {clock}"
    return clock


def normalize_news_title(value: str) -> str:
    """언론사별 문장부호 차이를 제거해 동일 기사 제목을 보수적으로 비교한다."""
    plain = html.unescape(re.sub(r"<[^>]+>", " ", str(value or "")))
    plain = plain.replace("\ufffd", "").lower()
    return re.sub(r"[^0-9a-z가-힣]+", "", plain)


def news_source_from_url(value: str) -> str:
    """검증된 언론사 도메인에 해당하는 URL만 매체명으로 변환한다."""
    try:
        host = str(urlsplit(str(value or "")).hostname or "").lower()
    except ValueError:
        return ""
    if not host:
        return ""
    for domain, source_name in sorted(
            NEWS_SOURCE_DOMAINS.items(), key=lambda entry: -len(entry[0])):
        if host == domain or host.endswith("." + domain):
            return source_name
    return ""


def infer_news_original_url(body: str) -> str:
    """본문의 검증 가능한 매체 경로에서 원문 주소를 보수적으로 복원한다."""
    text = html.unescape(str(body or ""))
    # 팜뉴스 이미지 주소의 첫 숫자는 같은 기사의 idxno다.
    # 예: /news/photo/202608/305339_206520_5950.jpg
    article_ids = {
        match.group(1)
        for match in re.finditer(
            r"(?:https?:)?//(?:www\.)?pharmnews\.com/news/photo/"
            r"\d{6}/([1-9]\d{0,11})_",
            text,
            flags=re.IGNORECASE,
        )
    }
    if len(article_ids) == 1:
        article_id = next(iter(article_ids))
        return (
            "https://www.pharmnews.com/news/articleView.html"
            f"?idxno={article_id}"
        )
    return ""


def clean_news_detail_title(value: str) -> str:
    """t3102 sTitle에 고정폭 바이너리가 섞인 응답을 걸러낸다."""
    title = re.sub(
        r"^\s*t3102OutBlock2\s*", "", str(value or ""), count=1)
    if "\ufffd" in title or re.search(
            r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", title):
        return ""
    return " ".join(title.split())


def source_label(source_id: str) -> str:
    """검증된 매체명은 이름으로, 미확인 식별자는 원본 코드로 표시한다."""
    source_id = str(source_id or "").strip()
    if not source_id:
        return "출처 미상"
    return NEWS_SOURCE_NAMES.get(source_id, f"매체 {source_id}")


def _repair_news_link_markup(body: str) -> str:
    """LS 고정폭 조각 경계에서 빠진 앵커 속성 공백을 복원한다."""
    text = str(body or "")
    attribute_names = r"(?:class|href|target|rel|title|id|name)"
    # 예: '<a' + 'class=...'가 '<aclass=...'로 합쳐진 경우.
    text = re.sub(
        rf"<a(?={attribute_names}\s*=)",
        "<a ", text, flags=re.IGNORECASE)

    def repair_anchor(match: re.Match) -> str:
        # 예: 'href="..."' + 'target=...'가 '"target='가 된 경우.
        return re.sub(
            rf"([\"'])(?={attribute_names}\s*=)",
            r"\1 ", match.group(0), flags=re.IGNORECASE)

    text = re.sub(
        r"<a\b[^>]*>", repair_anchor, text,
        flags=re.IGNORECASE | re.DOTALL)
    # 일부 t3102 본문은 마지막 100자 조각이 '</a'에서 끝나 닫는
    # 꺾쇠가 누락된다. 링크 파서와 본문 화면에서 태그로 처리되게 한다.
    return re.sub(r"</a\s*$", "</a>", text, flags=re.IGNORECASE)


def format_news_body(body: str) -> str:
    """t3102의 HTML 혼합 본문을 상세창용 일반 텍스트로 정리한다."""
    text = _repair_news_link_markup(body)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"^\s*t3102OutBlock1\s*", "", text, count=1)
    text = re.sub(
        r"<\s*(?:script|style)\b[^>]*>.*?<\s*/\s*(?:script|style)\s*>",
        "", text, flags=re.IGNORECASE | re.DOTALL)
    # t3102는 필드 고정 길이마다 CRLF를 넣는다. 한 번의
    # 줄바꿈은 '화\n재', 'newspi\nm.com'처럼 쪼개진 단어이므로 이어
    # 붙이고, 빈 줄로 구분된 실제 단락은 보존한다.
    wrapped_blocks = re.split(r"\n[ \t]*\n+", text)
    text = "\n\n".join(block.replace("\n", "")
                       for block in wrapped_blocks)
    text = re.sub(r"<\s*br\s*/?\s*>", "\n", text, flags=re.IGNORECASE)
    text = re.sub(
        r"<\s*/\s*(?:p|div|table|h[1-6])\s*>",
        "\n\n", text, flags=re.IGNORECASE)
    text = re.sub(
        r"<\s*/\s*(?:li|tr)\s*>",
        "\n", text, flags=re.IGNORECASE)
    text = re.sub(r"<\s*/\s*(?:td|th)\s*>", "\t", text,
                  flags=re.IGNORECASE)
    text = html.unescape(re.sub(r"<[^>]+>", "", text))
    text = text.replace("\xa0", " ")
    # 제휴사 원문에 이미 실려 온 U+FFFD와 그 사이의 깨진
    # 짧은 문자열은 웹뷰도 복구하지 못한다. 화면에서는 빈 깨짐 표시로
    # 남기지 않도록 해당 조각만 제거하고 원문은 DB에 보존한다.
    text = re.sub(r"\ufffd(?:[^\s\ufffd]{0,8}\ufffd)?", "", text)
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", text)
    text = re.sub(r"<\s*/?\s*[a-z][^<>]*\s*$", "", text,
                  flags=re.IGNORECASE)
    text = re.sub(r"<+\s*$", "", text)
    lines = [re.sub(r"[ \t]+", " ", line).strip()
             for line in text.split("\n")]
    text = "\n".join(lines)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


class _LSNewsLinkParser(HTMLParser):
    """sBody의 앵커 텍스트와 주소만 부작용 없이 추출한다."""

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.links: list[tuple[str, str]] = []
        self._href = ""
        self._text_parts: list[str] = []

    def _finish_anchor(self):
        if self._href:
            self.links.append((self._href, "".join(self._text_parts)))
        self._href = ""
        self._text_parts = []

    def handle_starttag(self, tag: str, attrs):
        if tag.lower() != "a":
            return
        self._finish_anchor()
        attributes = {
            str(name or "").lower(): str(value or "")
            for name, value in attrs
        }
        self._href = attributes.get("href", "").strip()

    def handle_data(self, data: str):
        if self._href:
            self._text_parts.append(str(data or ""))

    def handle_endtag(self, tag: str):
        if tag.lower() == "a":
            self._finish_anchor()

    def finish(self):
        self._finish_anchor()


def extract_news_links(body: str) -> tuple[tuple[str, str], ...]:
    """기사 HTML에서 화면에 표시할 안전한 텍스트 링크만 반환한다."""
    parser = _LSNewsLinkParser()
    try:
        parser.feed(_repair_news_link_markup(body))
        parser.close()
        parser.finish()
    except Exception:  # noqa: BLE001 - 깨진 제휴사 HTML은 링크만 포기한다.
        return ()

    links = []
    for url, raw_label in parser.links[:100]:
        url = html.unescape(str(url or "")).strip()
        if not url or len(url) > 2048:
            continue
        try:
            scheme = urlsplit(url).scheme.lower()
        except ValueError:
            continue
        if scheme not in ("http", "https", "mailto"):
            continue
        label = " ".join(format_news_body(raw_label).split())
        if not label or len(label) > 500:
            # 이미지 광고처럼 텍스트가 없는 앵커는 본문 뷰에서 제외한다.
            continue
        entry = (label, url)
        if entry not in links:
            links.append(entry)
    return tuple(links)


def infer_news_source(body: str) -> str:
    """본문 가장자리의 출처 표식과 기자 메일 도메인으로 언론사를 판별한다."""
    if not body:
        return ""
    plain = html.unescape(re.sub(r"<[^>]+>", " ", str(body)))
    plain = " ".join(plain.replace("\xa0", " ").split())
    # 제휴사명이 함께 남는 기사도 있으므로 본문 첫머리의 바이라인을 가장
    # 강한 근거로 쓴다. 예: [서울경제TV=기자] ... [ⓒ 서울경제]
    start = plain[:800]
    for source, aliases in sorted(
            NEWS_SOURCE_ALIASES.items(), key=lambda entry: -len(entry[0])):
        for alias in aliases:
            name = re.escape(alias)
            byline_patterns = (
                rf"\[\s*{name}\s*=",
                rf"[\[(]\s*(?:서울|세종|부산|대구|인천)?\s*=\s*{name}\s*[)\]]",
            )
            if any(re.search(pattern, start, re.IGNORECASE)
                   for pattern in byline_patterns):
                return source

    lowered = plain.lower()
    for domain, source in sorted(
            NEWS_SOURCE_DOMAINS.items(), key=lambda entry: -len(entry[0])):
        if domain in lowered:
            return source

    # 기자 메일이 없는 경우에는 본문 끝의 저작권 표식만 보조 근거로 쓴다.
    ending = plain[-1600:]
    for source, aliases in sorted(
            NEWS_SOURCE_ALIASES.items(), key=lambda entry: -len(entry[0])):
        for alias in aliases:
            if re.search(
                    rf"(?:ⓒ|©|copyright(?:\s*\(c\))?)\s*{re.escape(alias)}",
                    ending, re.IGNORECASE):
                return source
    return ""


NewsCallback = Callable[[LSNewsItem], None]
StatusCallback = Callable[[str, str], None]


class LSNewsStream:
    """토큰 발급, NWS 구독, 지수 백오프 재접속을 담당한다."""

    def __init__(self):
        self._token = ""
        self._token_valid_until = 0.0
        self._detail_lock = asyncio.Lock()
        self._last_detail_call = 0.0

    @property
    def configured(self) -> bool:
        return bool(config.LS_APPKEY.strip() and config.LS_APPSECRET.strip())

    async def run(self, on_news: NewsCallback, on_status: StatusCallback):
        if not self.configured:
            on_status(
                "missing",
                "LS_APPKEY·LS_APPSECRET이 없어 연결하지 않았습니다.",
            )
            return

        backoff = 1
        while True:
            try:
                on_status("connecting", "LS 실시간 뉴스 연결 중…")
                await self._connect_once(on_news, on_status)
                backoff = 1
            except asyncio.CancelledError:
                on_status("stopped", "LS 실시간 뉴스 연결 종료")
                raise
            except Exception as error:  # noqa: BLE001 - 끊김은 항상 재접속한다.
                log.warning(
                    "LS news websocket error (%s); reconnect in %ss",
                    type(error).__name__, backoff,
                )
                on_status(
                    "retrying",
                    f"LS 뉴스 연결 끊김 · {backoff}초 후 재연결",
                )
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)

    async def _access_token(self) -> str:
        # LS 토큰은 익일 07시까지 유효하지만 응답의 expires_in도 함께 존중한다.
        now = time.monotonic()
        if self._token and now < self._token_valid_until:
            return self._token
        payload = {
            "grant_type": "client_credentials",
            "appkey": config.LS_APPKEY.strip(),
            "appsecretkey": config.LS_APPSECRET.strip(),
            "scope": "oob",
        }
        headers = {"content-type": "application/x-www-form-urlencoded"}
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.post(
                f"{config.LS_HOST}/oauth2/token",
                headers=headers,
                params=payload,
            )
            response.raise_for_status()
            data = response.json()
        token = str(data.get("access_token") or "").strip()
        if not token:
            raise RuntimeError("LS 접근토큰 응답에 access_token이 없습니다.")
        try:
            expires_in = max(600, int(data.get("expires_in") or 12 * 60 * 60))
        except (TypeError, ValueError):
            expires_in = 12 * 60 * 60
        self._token = token
        self._token_valid_until = now + max(60, expires_in - 600)
        return token

    async def news_detail(self, realkey: str) -> LSNewsDetail:
        """t3102 본문과 연관 종목 배열을 초당 1건 제한에 맞춰 조회한다."""
        realkey = str(realkey or "").strip()
        if not realkey:
            return LSNewsDetail("", "", ())
        async with self._detail_lock:
            wait = 1.05 - (time.monotonic() - self._last_detail_call)
            if wait > 0:
                await asyncio.sleep(wait)
            data = await self._request_news_detail(realkey)
            self._last_detail_call = time.monotonic()

        response_code = str(data.get("rsp_cd") or "").strip()
        if response_code not in ("", "0", "00000"):
            raise RuntimeError(
                f"LS t3102 조회 실패: {response_code} "
                f"{str(data.get('rsp_msg') or '').strip()}")
        body_rows = data.get("t3102OutBlock1") or []
        if isinstance(body_rows, dict):
            body_rows = [body_rows]
        body = _repair_news_link_markup("".join(
            str(row.get("sBody") or "")
            for row in body_rows if isinstance(row, dict)))
        code_rows = data.get("t3102OutBlock") or []
        if isinstance(code_rows, dict):
            code_rows = [code_rows]
        codes = tuple(dict.fromkeys(
            str(row.get("sJongcode") or "").strip()
            for row in code_rows if isinstance(row, dict)
            and str(row.get("sJongcode") or "").strip()))
        title_block = data.get("t3102OutBlock2") or {}
        if isinstance(title_block, list):
            title_block = title_block[0] if title_block else {}
        title = clean_news_detail_title(
            str(title_block.get("sTitle") or "").strip()
            if isinstance(title_block, dict) else "")
        return LSNewsDetail(title, body, codes)

    async def _request_news_detail(self, realkey: str) -> dict:
        token = await self._access_token()
        headers = {
            "content-type": "application/json; charset=utf-8",
            "authorization": f"Bearer {token}",
            "tr_cd": "t3102",
            "tr_cont": "N",
            "tr_cont_key": "",
        }
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.post(
                f"{config.LS_HOST}/stock/investinfo",
                headers=headers,
                json={"t3102InBlock": {"sNewsno": realkey}},
            )
            if response.status_code == 401:
                self._token = ""
                self._token_valid_until = 0.0
                headers["authorization"] = (
                    f"Bearer {await self._access_token()}")
                response = await client.post(
                    f"{config.LS_HOST}/stock/investinfo",
                    headers=headers,
                    json={"t3102InBlock": {"sNewsno": realkey}},
                )
            response.raise_for_status()
            return response.json()

    async def _connect_once(
            self, on_news: NewsCallback, on_status: StatusCallback):
        token = await self._access_token()
        registration = {
            "header": {"token": token, "tr_type": "3"},
            "body": {"tr_cd": "NWS", "tr_key": "NWS001"},
        }
        async with websockets.connect(
                config.LS_WS_URL, ping_interval=30, ping_timeout=20,
                open_timeout=15) as websocket:
            await websocket.send(json.dumps(registration, ensure_ascii=False))
            on_status("connected", "LS 실시간 뉴스 연결됨")
            async for raw_message in websocket:
                try:
                    message = json.loads(raw_message)
                except (json.JSONDecodeError, TypeError):
                    log.warning("invalid LS news websocket message ignored")
                    continue
                self._raise_registration_error(message)
                item = parse_nws_message(message)
                if item is not None:
                    on_news(item)
        raise ConnectionError("LS 뉴스 웹소켓이 종료되었습니다.")

    def _raise_registration_error(self, message: dict):
        header = message.get("header")
        if not isinstance(header, dict):
            return
        response_code = str(header.get("rsp_cd") or "").strip()
        if response_code in ("", "0", "00000"):
            return
        # 만료·폐기된 토큰으로 재접속을 반복하지 않도록 다음 시도에서 재발급한다.
        self._token = ""
        self._token_valid_until = 0.0
        response_message = str(header.get("rsp_msg") or "등록 실패").strip()
        raise RuntimeError(f"LS NWS 등록 실패: {response_code} {response_message}")
