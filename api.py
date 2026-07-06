# -*- coding: utf-8 -*-
"""토큰 발급/갱신 + REST TR 호출.

TokenManager: 만료 10분 전 자동 재발급, 401 시 1회 재발급 후 재시도.
REST 호출은 초당 1건 rate limit (config.REST_RATE_LIMIT).
"""
import asyncio
import logging

import httpx

import config

log = logging.getLogger("api")


def _parse_expires(dt: str) -> float:
    """'yyyyMMddHHmmss' -> epoch seconds. 파싱 실패 시 0(=즉시 만료 취급)."""
    import calendar
    import time
    try:
        return calendar.timegm(time.strptime(dt, "%Y%m%d%H%M%S")) - time.timezone
    except (ValueError, TypeError):
        return 0.0


class TokenManager:
    def __init__(self, client: httpx.AsyncClient):
        self._client = client
        self._token = ""
        self._expires_at = 0.0
        self._lock = asyncio.Lock()

    async def token(self) -> str:
        import time
        async with self._lock:
            if not self._token or time.time() > self._expires_at - 600:  # 10분 전
                await self._issue()
            return self._token

    async def _issue(self):
        # au10001: 접근토큰 발급. ⚠️ 문서 확인: 경로/필드명.
        r = await self._client.post(
            f"{config.HOST}/oauth2/token",
            json={"grant_type": "client_credentials",
                  "appkey": config.APPKEY, "secretkey": config.SECRETKEY},
        )
        r.raise_for_status()
        d = r.json()
        self._token = d["token"]
        self._expires_at = _parse_expires(d.get("expires_dt", ""))
        log.info("token issued, expires_dt=%s", d.get("expires_dt"))


class RestClient:
    """TR 호출 공통 계층. api-id 헤더 방식(키움 REST 표준)."""

    def __init__(self):
        self._client = httpx.AsyncClient(timeout=10.0)
        self.tokens = TokenManager(self._client)
        self._sem = asyncio.Semaphore(1)  # 동시 1건
        self._last_call = 0.0

    async def _throttle(self):
        import time
        async with self._sem:
            wait = config.REST_RATE_LIMIT - (time.time() - self._last_call)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_call = time.time()

    async def request(self, api_id: str, body: dict, path: str = "/api/dostk/stkinfo") -> dict:
        await self._throttle()
        token = await self.tokens.token()
        headers = {"authorization": f"Bearer {token}", "api-id": api_id,
                   "Content-Type": "application/json;charset=UTF-8"}
        r = await self._client.post(f"{config.HOST}{path}", json=body, headers=headers)
        if r.status_code == 401:  # 토큰 만료 -> 강제 재발급 1회 재시도
            self.tokens._token = ""
            token = await self.tokens.token()
            headers["authorization"] = f"Bearer {token}"
            r = await self._client.post(f"{config.HOST}{path}", json=body, headers=headers)
        r.raise_for_status()
        return r.json()

    async def stock_info(self, code: str) -> dict:
        """ka10001 주식기본정보. 반환: gui가 쓰는 필드로 정규화.
        ⚠️ 문서 확인: 응답 필드명(stk_nm, upname, pred_trde_qty 등)."""
        d = await self.request("ka10001", {"stk_cd": code})
        return {
            "name": d.get("stk_nm", ""),
            "sector": d.get("upname", d.get("sctr_nm", "")),
            "prev_vol": _to_int(d.get("pred_trde_qty") or d.get("pre_trde_qty")),
        }

    async def close(self):
        await self._client.aclose()


def _to_int(v) -> int:
    """부호/콤마/공백 섞인 문자열 -> int. 빈값은 0."""
    try:
        return int(str(v).replace(",", "").replace("+", "").strip() or 0)
    except (ValueError, TypeError):
        return 0


def _demo():
    """키 없이 도는 순수 로직 자가검증."""
    assert _to_int("+4,620") == 4620
    assert _to_int("-963") == -963
    assert _to_int("") == 0 and _to_int(None) == 0
    assert _parse_expires("20260706153000") > 0
    assert _parse_expires("bad") == 0.0
    print("api self-check OK")


if __name__ == "__main__":
    import sys
    if config.APPKEY and config.SECRETKEY:
        async def main():
            c = RestClient()
            tok = await c.tokens.token()
            print("token OK:", tok[:12], "...")
            await c.close()
        asyncio.run(main())
    else:
        print("(.env 없음 -> 순수 로직만 검증)")
        _demo()
        sys.exit(0)
