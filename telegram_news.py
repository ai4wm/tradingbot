# -*- coding: utf-8 -*-
"""텔레그램 채널 뉴스 수집.

Telethon 사용자 API로 로그인해 `TG_CHANNELS` 채널의 과거 누락분을 먼저
소급 수집한 뒤 새 메시지를 실시간으로 받는다. UI와 분리해 두어 인증과
파싱을 따로 검증할 수 있다.
"""
from __future__ import annotations

import asyncio
import logging
import re
from datetime import datetime, timezone
from typing import Awaitable, Callable

from telethon import TelegramClient, events, utils
from telethon.errors import SessionPasswordNeededError

import config
from analysis_db import stock_name_index, telegram_news_cursors


log = logging.getLogger("telegram_news")

_CODE_RE = re.compile(r"(?<!\d)(\d{6})(?!\d)")
_SPACE_RE = re.compile(r"[^\S\n]+")
_WORD_CHAR = re.compile(r"[0-9A-Za-z가-힣]")
# 채널이 직접 붙인 종목 목록. 본문 추측보다 항상 우선한다.
_RELATED_RE = re.compile(r"\[관련\s*종목\]\s*([^\n]+)")
# 첫 줄에 오는 채널 말머리(예: "🟦 하나증권(중국)", "🟦 [대신증권 리서치]").
_HEADER_MARK = "🟦"
# 종목명 뒤에 바로 붙는 한 글자 조사는 낱말 경계로 인정한다.
_JOSA = frozenset("이가은는을를의에도와과로만서요랑")


def _has_standalone(text: str, name: str) -> bool:
    """2글자 이름은 더 긴 낱말의 일부(트레이딩→레이, CSP→CS)를 걸러낸다."""
    start = text.find(name)
    while start >= 0:
        after = start + len(name)
        before_ok = start == 0 or not _WORD_CHAR.match(text[start - 1])
        after_ok = (
            after >= len(text)
            or not _WORD_CHAR.match(text[after])
            or (text[after] in _JOSA
                and (after + 1 >= len(text)
                     or not _WORD_CHAR.match(text[after + 1])))
        )
        if before_ok and after_ok:
            return True
        start = text.find(name, start + 1)
    return False


def extract_stocks(text: str, name_index: dict[str, str]
                   ) -> tuple[tuple[str, ...], str]:
    """본문에서 종목코드와 종목명을 뽑는다. (코드 튜플, 표시용 종목명)"""
    codes: list[str] = []
    names: list[str] = []
    related = _RELATED_RE.search(text)
    if related is not None:
        for token in re.split(r"[|,·/]", related.group(1)):
            code = name_index.get(token.strip())
            if code and code not in codes:
                codes.append(code)
                names.append(token.strip())
        if codes:
            return tuple(codes), ", ".join(names)
    code_to_name = {code: name for name, code in name_index.items()}
    for code in _CODE_RE.findall(text):
        if code in code_to_name and code not in codes:
            codes.append(code)
            names.append(code_to_name[code])
    # ponytail: 긴 이름은 부분문자열, 2글자 이름만 경계 검사. 오탐이 더 나오면
    # 3글자 이름에도 같은 경계 규칙을 적용한다.
    matched = sorted(
        (name for name in name_index
         if len(name) >= 3 and name in text
         or len(name) == 2 and _has_standalone(text, name)),
        key=len, reverse=True)
    for name in matched:
        code = name_index[name]
        if code in codes or any(name in other for other in names):
            continue
        codes.append(code)
        names.append(name)
        if len(codes) >= 5:
            break
    return tuple(codes), ", ".join(names)


def _message_row(channel: str, title: str, message,
                 name_index: dict[str, str]) -> dict | None:
    raw = _SPACE_RE.sub(
        " ", str(getattr(message, "message", "") or "").replace("\xa0", " "))
    lines = [line.strip() for line in raw.splitlines() if line.strip()]
    if not lines:
        return None
    # 말머리는 채널 소분류일 뿐이라 제목·종목 추출 대상에서 뺀다.
    header = ""
    if lines[0].startswith(_HEADER_MARK):
        header = lines[0].lstrip(_HEADER_MARK).strip(" []")
        lines = lines[1:]
        if not lines:
            return None
    text = "\n".join(lines)
    published = getattr(message, "date", None)
    if isinstance(published, datetime):
        published_at = published.replace(
            tzinfo=published.tzinfo or timezone.utc
        ).astimezone().isoformat(timespec="seconds")
    else:
        published_at = datetime.now().astimezone().isoformat(timespec="seconds")
    codes, names = extract_stocks(text, name_index)
    if header:
        # 발행처(예: "대신증권 리서치")는 본문 서명에도 다시 나오므로 뺀다.
        kept = [(code, name) for code, name in zip(codes, names.split(", "))
                if name not in header]
        codes = tuple(code for code, _ in kept)
        names = ", ".join(name for _, name in kept)
    link_name = channel.lstrip("@")
    return {
        "channel": channel,
        "channel_title": header or title or channel,
        "message_id": int(message.id),
        "published_at": published_at,
        "title": lines[0][:200],
        "body": text,
        "stock_codes": codes,
        "stock_names": names,
        "url": (
            f"https://t.me/{link_name}/{message.id}"
            if not link_name.lstrip("-").isdigit() else ""
        ),
    }


class TelegramNewsStream:
    """채널 소급 수집 뒤 실시간 수신을 유지한다."""

    def __init__(self, code_callback: Callable[[], Awaitable[str]] | None = None,
                 password_callback: Callable[[], Awaitable[str]] | None = None,
                 phone_callback: Callable[[], Awaitable[str]] | None = None):
        self._code_callback = code_callback
        self._password_callback = password_callback
        self._phone_callback = phone_callback
        self._client: TelegramClient | None = None
        self._stopped = False

    async def _phone(self) -> str:
        if config.TG_PHONE:
            return config.TG_PHONE
        if self._phone_callback is None:
            raise RuntimeError("TG_PHONE이 없고 전화번호 입력 경로도 없습니다.")
        return await self._phone_callback()

    async def _code(self) -> str:
        if self._code_callback is None:
            raise RuntimeError("텔레그램 인증번호 입력 경로가 없습니다.")
        return await self._code_callback()

    async def _password(self) -> str:
        if self._password_callback is None:
            raise SessionPasswordNeededError(None)
        return await self._password_callback()

    async def stop(self):
        self._stopped = True
        if self._client is not None:
            await self._client.disconnect()

    async def run(self, on_item: Callable[[dict, bool], None],
                  on_status: Callable[[str, str], None]):
        if not (config.TG_API_ID and config.TG_API_HASH):
            on_status("off", "TG_API_ID·TG_API_HASH 없음")
            return
        if not config.TG_CHANNELS:
            on_status("off", "TG_CHANNELS 없음")
            return
        while not self._stopped:
            try:
                await self._session(on_item, on_status)
            except asyncio.CancelledError:
                raise
            except Exception as error:  # noqa: BLE001 - 재접속을 유지한다.
                log.exception("telegram news session failed")
                on_status("error", f"오류: {error}")
            if self._stopped:
                return
            await asyncio.sleep(30)

    async def _session(self, on_item, on_status):
        on_status("connecting", "연결 중")
        client = TelegramClient(
            config.TG_SESSION_PATH, int(config.TG_API_ID), config.TG_API_HASH)
        self._client = client
        try:
            await client.start(
                phone=self._phone,
                code_callback=self._code,
                password=self._password,
            )
            name_index = stock_name_index()
            cursors = telegram_news_cursors()
            entities = []
            for channel in config.TG_CHANNELS:
                try:
                    entities.append((channel, await client.get_entity(channel)))
                except Exception as error:  # noqa: BLE001 - 나머지 채널은 계속한다.
                    log.warning("telegram channel skipped: %s (%s)",
                                channel, error)
            if not entities:
                on_status("error", "접근 가능한 채널 없음")
                return
            on_status("live", f"연결 · 채널 {len(entities)}")
            for channel, entity in entities:
                await self._backfill(
                    client, channel, entity, cursors.get(channel, 0),
                    name_index, on_item)

            watched = [entity for _, entity in entities]
            titles = {}
            for channel, entity in entities:
                label = (channel, getattr(entity, "title", channel))
                # 실시간 이벤트의 chat_id는 -100 접두 형식이라 두 키를 모두 넣는다.
                titles[entity.id] = label
                titles[utils.get_peer_id(entity)] = label

            @client.on(events.NewMessage(chats=watched))
            async def _on_new(event):  # noqa: ANN001
                channel, title = titles.get(
                    event.chat_id, (str(event.chat_id), str(event.chat_id)))
                row = _message_row(channel, title, event.message, name_index)
                if row is not None:
                    on_item(row, True)

            await client.run_until_disconnected()
            on_status("error", "연결 끊김")
        finally:
            self._client = None
            try:
                await client.disconnect()
            except Exception:  # noqa: BLE001
                pass

    async def _backfill(self, client, channel: str, entity, last_id: int,
                        name_index: dict[str, str], on_item):
        """마지막 저장 ID 이후 글만 오래된 순서로 채운다."""
        limit = (config.TG_BACKFILL_LIMIT if last_id
                 else config.TG_FIRST_RUN_LIMIT)
        title = getattr(entity, "title", channel)
        rows = []
        async for message in client.iter_messages(
                entity, limit=limit, min_id=last_id):
            row = _message_row(channel, title, message, name_index)
            if row is not None:
                rows.append(row)
        for row in reversed(rows):
            on_item(row, False)
        if rows:
            log.info("telegram backfill %s: %d", channel, len(rows))


async def _demo_backfill():
    """소급 수집이 마지막 저장 ID 이후만, 오래된 순서로 넘기는지 확인한다."""
    class _Msg:
        def __init__(self, mid):
            self.id = mid
            self.message = f"메시지 {mid}"
            self.date = datetime(2026, 8, 2, 9, 0, tzinfo=timezone.utc)

    class _Client:
        def __init__(self):
            self.calls = []

        def iter_messages(self, entity, limit=None, min_id=0):
            self.calls.append({"limit": limit, "min_id": min_id})
            # 텔레그램과 같이 최신순으로 돌려준다.
            picked = [m for m in reversed(entity.messages) if m.id > min_id]

            async def _gen():
                for message in picked[:limit]:
                    yield message
            return _gen()

    entity = type("E", (), {"title": "채널", "messages": [_Msg(i) for i in range(1, 9)]})()
    stream = TelegramNewsStream()
    client = _Client()

    seen = []
    await stream._backfill(client, "@c", entity, 0, {}, lambda row, new: seen.append(row))
    assert client.calls[-1]["limit"] == config.TG_FIRST_RUN_LIMIT, client.calls
    assert [row["message_id"] for row in seen] == list(range(1, 9)), seen

    seen.clear()
    await stream._backfill(client, "@c", entity, 6, {}, lambda row, new: seen.append(row))
    assert client.calls[-1]["min_id"] == 6, client.calls
    assert [row["message_id"] for row in seen] == [7, 8], seen

    seen.clear()
    await stream._backfill(client, "@c", entity, 8, {}, lambda row, new: seen.append(row))
    assert seen == [], seen


def _demo_dedupe():
    """같은 채널·메시지 ID는 다시 저장되지 않고 커서가 마지막 ID를 가리킨다."""
    import tempfile
    from pathlib import Path

    from analysis_db import save_telegram_news, telegram_news_cursors

    with tempfile.TemporaryDirectory() as folder:
        db = Path(folder) / "test.db"
        row = {
            "channel": "@c", "message_id": 5, "channel_title": "채널",
            "published_at": "2026-08-02T09:00:00+09:00", "title": "제목",
            "body": "본문", "stock_codes": ("005930",),
            "stock_names": "삼성전자", "url": "https://t.me/c/5",
        }
        assert save_telegram_news(row, db) is True
        assert save_telegram_news(row, db) is False
        assert save_telegram_news({**row, "message_id": 9}, db) is True
        assert telegram_news_cursors(db) == {"@c": 9}


def _demo():
    index = {"삼성전자": "005930", "한샘": "009240", "에코프로": "086520"}
    codes, names = extract_stocks("삼성전자(005930) 강세, 에코프로도 상승", index)
    assert codes == ("005930", "086520"), codes
    assert "삼성전자" in names and "에코프로" in names, names
    assert extract_stocks("특징주 없음", index) == ((), "")

    short = {"레이": "228670", "SK": "034730", "두산": "000150"}
    assert extract_stocks("트레이딩 재개, CSP CapEx, SK실트론 인수", short) == ((), "")
    assert extract_stocks("#두산 SK실트론 인수", short)[0] == ("000150",)
    assert extract_stocks("두산이 급등", short)[0] == ("000150",)
    assert extract_stocks("레이 신고가", short)[0] == ("228670",)

    # 채널이 붙인 [관련 종목] 목록은 본문 추측보다 우선한다.
    related = {"삼성전자": "005930", "SK하이닉스": "000660", "네이버": "035420"}
    codes, names = extract_stocks(
        "코스피 하락폭 만회\n\n[관련 종목] SK하이닉스 | 삼성전자", related)
    assert codes == ("000660", "005930"), codes

    class _Message:
        id = 7
        message = "  한샘  급등 "
        date = datetime(2026, 8, 2, 9, 30, tzinfo=timezone.utc)

    row = _message_row("@stockinfojji", "지지뉴스", _Message(), index)
    assert row["stock_codes"] == ("009240",), row
    assert row["title"] == "한샘 급등", row
    assert row["url"] == "https://t.me/stockinfojji/7", row

    # 말머리(발행처)는 제목과 종목 추출에서 빠지고 채널 표시로만 남는다.
    header_message = type("M", (), {
        "id": 9,
        "message": "🟦 [대신증권 리서치]\n\n두산, 잘 사왔다\n\n본문 이어짐",
        "date": _Message.date,
    })()
    row = _message_row(
        "@stockinfojji", "지지뉴스", header_message,
        {"대신증권": "003540", "두산": "000150"})
    assert row["channel_title"] == "대신증권 리서치", row
    assert row["title"] == "두산, 잘 사왔다", row
    assert row["stock_codes"] == ("000150",), row

    # 본문 서명에 다시 나오는 발행처도 종목으로 잡지 않는다.
    signed = type("M", (), {
        "id": 11,
        "message": "🟦 [대신증권 리서치]\n\n대덕전자: 2Q 호조\n\n[대신증권 이경연]",
        "date": _Message.date,
    })()
    row = _message_row("@stockinfojji", "지지뉴스", signed,
                       {"대신증권": "003540", "대덕전자": "353200"})
    assert row["stock_names"] == "대덕전자", row
    assert _message_row("@c", "c", type("E", (), {
        "id": 1, "message": "", "date": None})(), index) is None

    asyncio.run(_demo_backfill())
    _demo_dedupe()
    print("ok")


if __name__ == "__main__":
    _demo()
