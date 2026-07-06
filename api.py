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

    async def watch_info(self, codes: list[str]) -> list[dict]:
        """ka10095 관심종목정보: 여러 종목을 한 번에 조회 -> gui 필드로 정규화.
        한 번의 호출로 현재가/등락률/거래량/매도·매수잔량을 채운다(장 마감 후에도 유효).
        codes는 '|'로 join. 응답 리스트는 요청 순서와 무관하므로 code로 매칭할 것."""
        if not codes:
            return []
        d = await self.request("ka10095", {"stk_cd": "|".join(codes)})
        out = []
        for r in d.get("atn_stk_infr", []):
            code = r.get("stk_cd", "")
            if not code:
                continue
            base = abs(_to_int(r.get("base_pric")))
            # 예상체결가/등락률은 여기(REST 스냅샷)서 안 채운다: 마감 후에도 마감동시호가 값이
            # 얼어붙어 계속 나오기 때문. 실시간(ws)에서 동시호가/VI 때만 들어오게 함.
            out.append({
                "code": code,
                "name": r.get("stk_nm", ""),
                "price": abs(_to_int(r.get("cur_prc"))),   # 부호 포함 -> abs
                "rate": _to_float(r.get("flu_rt")),        # 등락율 (부호 유지: 색)
                "vol": _to_int(r.get("trde_qty")),
                "ask_qty": _to_int(r.get("pri_sel_req")),  # 최우선 매도잔량
                "bid_qty": _to_int(r.get("pri_buy_req")),  # 최우선 매수잔량
                "open": abs(_to_int(r.get("open_pric"))),  # 시가 (L일봉H 몸통)
                "low": abs(_to_int(r.get("low_pric"))),    # 당일 저가 (심지)
                "high": abs(_to_int(r.get("high_pric"))),  # 당일 고가 (심지)
                "base": base,                              # 전일종가 (L일봉H 축 중심)
                "upper": abs(_to_int(r.get("upl_pric"))),  # 상한가 (축 오른쪽 끝)
                "lower": abs(_to_int(r.get("lst_pric"))),  # 하한가 (축 왼쪽 끝)
            })
        return out

    async def last_limit_entry(self, code: str, upper: int) -> str:
        """상한가 마지막 진입시각. ka10080 1분봉을 최신->과거로 스캔해 종가=상한가인
        연속 구간의 첫 봉 시각을 반환. 현재 상한가가 아니면 ''. 무너졌다 재진입하면
        가장 최근 연속구간이 잡혀 자동으로 '마지막 진입'이 된다. 반환 'HH:MM:SS'."""
        if not upper:
            return ""
        d = await self.request("ka10080", {"stk_cd": code, "tic_scope": "1", "upd_stkpc_tp": "1"},
                               path="/api/dostk/chart")
        bars = d.get("stk_min_pole_chart_qry", [])
        if not bars:
            return ""
        today = bars[0].get("cntr_tm", "")[:8]  # 당일 봉만 (전일 가격 오매칭 방지)
        entry = ""
        for b in bars:  # row0=최신 -> 과거
            t = b.get("cntr_tm", "")
            if t[:8] != today:
                break
            if abs(_to_int(b.get("cur_prc"))) == upper:
                entry = t  # 더 과거의 상한가 봉으로 계속 갱신 -> 연속구간 시작점
            else:
                break
        return f"{entry[8:10]}:{entry[10:12]}:{entry[12:14]}" if len(entry) >= 14 else ""

    async def close(self):
        await self._client.aclose()


def _to_int(v) -> int:
    """부호/콤마/공백 섞인 문자열 -> int. 빈값은 0."""
    try:
        return int(str(v).replace(",", "").replace("+", "").strip() or 0)
    except (ValueError, TypeError):
        return 0


def _to_float(v) -> float:
    """부호/콤마 섞인 문자열 -> float(부호 유지). 빈값은 0.0."""
    try:
        return float(str(v).replace(",", "").replace("+", "").strip() or 0)
    except (ValueError, TypeError):
        return 0.0


def _demo():
    """키 없이 도는 순수 로직 자가검증."""
    assert _to_int("+4,620") == 4620
    assert _to_int("-963") == -963
    assert _to_int("") == 0 and _to_int(None) == 0
    assert _to_float("+2.75") == 2.75 and _to_float("-1.5") == -1.5 and _to_float("") == 0.0
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
