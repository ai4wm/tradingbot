# -*- coding: utf-8 -*-
"""토큰 발급/갱신 + REST TR 호출.

TokenManager: 만료 10분 전 자동 재발급, 401 시 1회 재발급 후 재시도.
REST 호출은 초당 1건 rate limit (config.REST_RATE_LIMIT).
"""
import asyncio
import logging
from dataclasses import dataclass, field

import httpx

import config

log = logging.getLogger("api")

# 거래대금상위 등에서 ETF/ETN 제외용 발행사 접두(레버리지/인버스가 상위 독식).
# 일반주 종목명과 충돌하지 않는 브랜드만(예: '파워'는 파워로직스와 충돌 -> 제외).
ETF_PREFIXES = (
    "KODEX", "TIGER", "KBSTAR", "RISE", "ACE", "SOL", "PLUS", "ARIRANG",
    "HANARO", "KOSEF", "KINDEX", "TIMEFOLIO", "히어로즈", "마이티",
)


@dataclass
class MarketInfo:
    """ka10099 종목 분류셋 묶음 (시작 시 1회 조회해 gui 모델에 주입)."""
    kosdaq: set[str] = field(default_factory=set)  # 코스닥 (종목명 보라)
    single: set[str] = field(default_factory=set)  # 단일가 매매 (예상값 상시 표시)
    short_overheat: set[str] = field(default_factory=set)  # 단기과열 (30분 단일가)
    liquidation: set[str] = field(default_factory=set)  # 정리매매 (가격제한폭 없음)
    nxt: set[str] = field(default_factory=set)     # 넥스트레이드 거래가능 (좌상단 노랑)
    misu: set[str] = field(default_factory=set)    # 미수가능 (우상단 녹색)
    admin: set[str] = field(default_factory=set)   # 관리종목 (종목명 경고색)
    new_today: set[str] = field(default_factory=set)  # 상장 당일 (좌하단 마젠타)
    new15: set[str] = field(default_factory=set)      # 상장 15일 이내 (좌하단 하늘색)
    new30: set[str] = field(default_factory=set)      # 상장 16~30일 (좌하단 청회색)
    shares: dict = field(default_factory=dict)        # 상장주식수 (시가총액 = x현재가)


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
        self._order_sem = asyncio.Semaphore(1)
        self._last_order_call = 0.0
        # 시세 접미사: "" KRX, "_AL" 통합. watch_info 백필이 WS 통합시세를 KRX 종가로
        # 덮어쓰지 않게 ws.real_suffix와 함께 전환 (ka10095 _AL 실측: NXT 야간가 반영)
        self.suffix = ""

    async def _throttle(self):
        import time
        async with self._sem:
            wait = config.REST_RATE_LIMIT - (time.time() - self._last_call)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_call = time.time()

    async def _request_raw(self, api_id: str, body: dict, path: str,
                           cont: str = "") -> httpx.Response:
        await self._throttle()
        token = await self.tokens.token()
        headers = {"authorization": f"Bearer {token}", "api-id": api_id,
                   "Content-Type": "application/json;charset=UTF-8"}
        if cont:  # 연속조회: 이전 응답 헤더의 next-key로 다음 페이지
            headers["cont-yn"] = "Y"
            headers["next-key"] = cont
        r = await self._client.post(f"{config.HOST}{path}", json=body, headers=headers)
        if r.status_code == 401:  # 토큰 만료 -> 강제 재발급 1회 재시도
            self.tokens._token = ""
            token = await self.tokens.token()
            headers["authorization"] = f"Bearer {token}"
            r = await self._client.post(f"{config.HOST}{path}", json=body, headers=headers)
        r.raise_for_status()
        return r

    async def request(self, api_id: str, body: dict, path: str = "/api/dostk/stkinfo") -> dict:
        return (await self._request_raw(api_id, body, path)).json()

    async def _order_request(self, api_id: str, body: dict) -> dict:
        """주문 전용 5건/초 제한. 일반 조회 1초 제한과 큐를 분리한다."""
        import time
        async with self._order_sem:
            wait = 0.21 - (time.monotonic() - self._last_order_call)
            if wait > 0:
                await asyncio.sleep(wait)
            token = await self.tokens.token()
            headers = {
                "authorization": f"Bearer {token}", "api-id": api_id,
                "Content-Type": "application/json;charset=UTF-8",
            }
            r = await self._client.post(
                f"{config.HOST}/api/dostk/ordr", json=body, headers=headers)
            self._last_order_call = time.monotonic()
        r.raise_for_status()
        data = r.json()
        if str(data.get("return_code", "0")) not in ("0", ""):
            raise RuntimeError(data.get("return_msg") or f"{api_id} 주문 실패")
        return data

    async def buy_order(self, code: str, qty: int, price: int) -> dict:
        """KRX 보통 지정가 매수. 주문 화면은 상한가를 price로 전달한다."""
        price = int(price)
        if price <= 0:
            raise ValueError("매수 지정가는 0원보다 커야 합니다")
        data = await self._order_request("kt10000", {
            "dmst_stex_tp": "KRX",
            "stk_cd": code,
            "ord_qty": str(int(qty)),
            "ord_uv": str(price),
            "trde_tp": "0",  # 보통 지정가
            "cond_uv": "",
        })
        order_no = str(data.get("ord_no") or data.get("order_no") or "")
        if not order_no:
            raise RuntimeError("매수 주문번호가 응답에 없습니다")
        return {"order_no": order_no, "raw": data}

    async def sell_order(
            self, code: str, qty: int, price: int = 0, *,
            market: bool = False) -> dict:
        """KRX 지정가 또는 시장가 매도."""
        price = int(price)
        if not market and price <= 0:
            raise ValueError("매도 지정가는 0원보다 커야 합니다")
        data = await self._order_request("kt10001", {
            "dmst_stex_tp": "KRX",
            "stk_cd": code,
            "ord_qty": str(int(qty)),
            "ord_uv": "" if market else str(price),
            "trde_tp": "3" if market else "0",
            "cond_uv": "",
        })
        order_no = str(data.get("ord_no") or data.get("order_no") or "")
        if not order_no:
            raise RuntimeError("매도 주문번호가 응답에 없습니다")
        return {"order_no": order_no, "raw": data}

    async def cancel_order(
            self, code: str, original_order_no: str, qty: int,
            exchange: str = "KRX") -> dict:
        """원주문 1건을 취소한다. qty=0이면 그 주문의 잔량 전부 취소."""
        exchange = str(exchange or "KRX").strip().upper()
        if exchange in ("통합", "0"):
            exchange = "SOR"
        elif exchange == "1":
            exchange = "KRX"
        elif exchange == "2":
            exchange = "NXT"
        if exchange not in {"KRX", "NXT", "SOR"}:
            exchange = "KRX"
        data = await self._order_request("kt10003", {
            "dmst_stex_tp": exchange,
            "orig_ord_no": str(original_order_no),
            "stk_cd": code,
            "cncl_qty": str(int(qty)),
        })
        return {
            "order_no": str(data.get("ord_no") or data.get("order_no") or ""),
            "raw": data,
        }

    async def public_ip(self) -> str:
        """공인 IP (키움 REST는 IP 화이트리스트 -> 바뀌면 접속 차단. 감시용). 실패 시 ''."""
        r = await self._client.get("https://api.ipify.org", timeout=5.0)
        r.raise_for_status()
        return r.text.strip()

    async def watch_info(self, codes: list[str], exp: bool = None,
                         suffix: str = None) -> list[dict]:
        """ka10095 관심종목정보: 여러 종목을 한 번에 조회 -> gui 필드로 정규화.
        한 번의 호출로 현재가/등락률/거래량/매도·매수잔량을 채운다(장 마감 후에도 유효).
        codes는 '|'로 join. 응답 리스트는 요청 순서와 무관하므로 code로 매칭할 것."""
        if not codes:
            return []
        if exp is None:
            exp = _in_auction()
        real_suffix = self.suffix if suffix is None else suffix
        d = await self.request("ka10095", {"stk_cd": "|".join(c + real_suffix for c in codes)})
        out = []
        for r in d.get("atn_stk_infr", []):
            code = (r.get("stk_cd") or "").split("_")[0]  # _AL 응답 접미사 제거
            if not code:
                continue
            base = abs(_to_int(r.get("base_pric")))
            vol = _to_int(r.get("trde_qty"))
            # 전일거래량: ka10095엔 없지만 전일대비율(pred_trde_qty_pre=오늘/전일*100)로 역산.
            # 실측 대조 오차 <0.01% (비율이 소수2자리 반올림이라 몇 주 오차).
            ratio = _to_float(r.get("pred_trde_qty_pre"))
            prev_vol = round(vol / (abs(ratio) / 100)) if ratio else 0
            # 예상체결가/수량: 장중·마감후엔 얼어붙은 echo -> 기본은 동시호가 시간에만,
            # VI 발동 직후엔 exp=True로 강제 조회(전 컬럼 즉시 채움).
            # 예상값은 항상 전달 (표시 여부는 gui가 판정: 단일가 마킹/변화감지).
            # exp_hot=1은 국면 확정(동시호가/VI 조회)일 때만 -> 즉시 표시 허용.
            e = {"exp_price": abs(_to_int(r.get("exp_cntr_pric"))),
                 "exp_qty": _to_int(r.get("exp_cntr_qty"))}
            if exp:
                e["exp_hot"] = 1
            out.append({
                **e,
                "code": code,
                "name": r.get("stk_nm", ""),
                "price": abs(_to_int(r.get("cur_prc"))),   # 부호 포함 -> abs
                "rate": _to_float(r.get("flu_rt")),        # 등락율 (부호 유지: 색)
                "vol": vol,
                # ka10095 응답의 거래대금은 백만원 단위인 경우가 있어
                # 일봉 저장 계층과 동일하게 원 단위로 정규화한다.
                "trading_value": abs(_to_int(r.get("trde_prica"))) * 1_000_000
                    or abs(_to_int(r.get("cur_prc"))) * vol,
                "prev_vol": prev_vol,                      # 전일거래량 (역산)
                "ask_qty": _to_int(r.get("pri_sel_req")),  # 최우선 매도잔량
                "bid_qty": _to_int(r.get("pri_buy_req")),  # 최우선 매수잔량
                "ask_price": abs(_to_int(r.get("sel_1th_bid"))),  # 최우선 매도호가
                "bid_price": abs(_to_int(r.get("buy_1th_bid"))),  # 최우선 매수호가
                **{f"ask_price{n}": abs(_to_int(r.get(f"sel_{n}th_bid")))
                   for n in range(2, 6)},
                **{f"bid_price{n}": abs(_to_int(r.get(f"buy_{n}th_bid")))
                   for n in range(2, 6)},
                **{f"ask_qty{n}": _to_int(r.get(f"pri_sel_req{n}"))
                   for n in range(2, 6)},
                **{f"bid_qty{n}": _to_int(r.get(f"pri_buy_req{n}"))
                   for n in range(2, 6)},
                "open": abs(_to_int(r.get("open_pric"))),  # 시가 (L일봉H 몸통)
                "low": abs(_to_int(r.get("low_pric"))),    # 당일 저가 (심지)
                "high": abs(_to_int(r.get("high_pric"))),  # 당일 고가 (심지)
                "base": base,                              # 전일종가 (L일봉H 축 중심)
                "upper": abs(_to_int(r.get("upl_pric"))),  # 상한가 (축 오른쪽 끝)
                "lower": abs(_to_int(r.get("lst_pric"))),  # 하한가 (축 왼쪽 끝)
            })
        return out

    async def yesterday_limit_counts(self) -> dict[str, tuple[int, int]]:
        """어제 상한 마감 종목 -> (어제까지 연속 상한 일수, 어제 종가).
        연상 표시 = 일수 + (오늘 상한이면 1). 어제 종가는 휴장일 이중계산 방지용:
        휴장일엔 ka10095가 마지막 세션 그대로라 현재가==상한가인데 그 상한은 이미 일수에
        포함됨. 진짜 오늘 상한이면 상한가=전일종가x1.3이라 어제 종가와 절대 같을 수 없음.
        목록만 ka10017(updown_tp=6)에서 받고, 일수는 일봉으로 직접 계산.
        (서버 cnt는 장중에 오늘분이 섞여드는 시점이 불규칙 -> 신뢰 불가. 07-10 15:21 실측:
        마감 전인데 cnt에 오늘 상한 포함. 일봉 과거 행은 하루 종일 불변이라 결정적.)"""
        d = await self.request("ka10017", {
            "mrkt_tp": "000", "updown_tp": "6", "sort_tp": "1", "stk_cnd": "0",
            "trde_qty_tp": "00000", "crd_cnd": "0", "trde_gold_tp": "0", "stex_tp": "1"})
        out = {}
        for code in (r["stk_cd"] for r in d.get("updown_pric", []) if r.get("stk_cd")):
            try:
                out[code] = await self._yesterday_streak(code)
            except Exception as e:  # noqa: BLE001 - 개별 실패는 최소값 1 (목록에 있음 = 어제 상한)
                log.warning("yesterday_streak %s: %s", code, e)
                out[code] = (1, 0)
        return out

    async def _yesterday_streak(self, code: str) -> tuple[int, int]:
        """일봉에서 (어제까지 연속 상한 일수, 어제 종가): 종가 대비 +29.5% 이상 연속
        (gui.py LIMIT과 동일 판정)."""
        import datetime
        today = datetime.datetime.now().strftime("%Y%m%d")
        d = await self.request("ka10081",
                               {"stk_cd": code, "base_dt": today, "upd_stkpc_tp": "1"},
                               path="/api/dostk/chart")
        rows = [r for r in d.get("stk_dt_pole_chart_qry", []) if r.get("dt", "") < today]
        n = 0
        for a, b in zip(rows, rows[1:]):  # 최신(어제) -> 과거
            c0, c1 = abs(_to_int(a.get("cur_prc"))), abs(_to_int(b.get("cur_prc")))
            if not c1 or (c0 - c1) / c1 * 100 < 29.5:
                break
            n += 1
        return n, abs(_to_int(rows[0].get("cur_prc"))) if rows else 0

    async def prev_volume(self, code: str) -> int:
        """전일(직전 거래일) 절대 거래량 = ka10081 일봉의 첫 dt<오늘 행.
        동시호가엔 오늘 체결이 없어 ka10095 역산(오늘거래량÷전일대비율)이 0이 됨.
        이 절대값으로 채운다. 정적값이라 종목당 1회만 조회하면 됨."""
        import datetime
        today = datetime.datetime.now().strftime("%Y%m%d")
        d = await self.request("ka10081",
                               {"stk_cd": code, "base_dt": today, "upd_stkpc_tp": "1"},
                               path="/api/dostk/chart")
        for r in d.get("stk_dt_pole_chart_qry", []):
            if r.get("dt", "") < today:            # 오늘 행(부분체결) 건너뛰고 전일
                return abs(_to_int(r.get("trde_qty")))
        return 0

    async def market_info(self) -> "MarketInfo":
        """ka10099 양시장 1회 조회 -> 종목 분류셋 묶음(MarketInfo).
        단일가: orderWarning 2=정리매매 3=단기과열 (30분 단일가)
              + 상장주식수 50만주 미만 우선주 = 상시 단일가 (2020.7 저유동성 규제,
                orderWarning엔 안 잡힘. 실측: 진흥기업2우B/금호건설우).
        4=투자위험은 단일가 아님 (실측 079650: 장중 연속체결. 정지 1일 후 일반매매).
        NXT: nxtEnable='Y' = 넥스트레이드(대체거래소) 거래가능.
        미수가능: state에 증거금 있고 100% 아님(=일부 현금). 증거금100%는 미수 불가.
        관리종목: state 토큰 '관리종목' (거래정지 겸하면 auditInfo엔 안 잡혀 state로 판정)."""
        import datetime
        today = datetime.date.today()
        m = MarketInfo()
        for mrkt in ("0", "10"):
            d = await self.request("ka10099", {"mrkt_tp": mrkt})
            for r in d.get("list", []):
                code = r.get("code")
                if not code:
                    continue
                state = r.get("state") or ""
                shares = _to_int(r.get("listCount"))
                if shares:
                    m.shares[code] = shares
                reg = r.get("regDay") or ""  # 상장일 yyyyMMdd -> 신규 3단계 (당일/15일/30일, 달력일)
                if len(reg) == 8:
                    try:
                        days = (today - datetime.date(int(reg[:4]), int(reg[4:6]), int(reg[6:]))).days
                        if days == 0:
                            m.new_today.add(code)
                        elif days <= 15:
                            m.new15.add(code)
                        elif days <= 30:
                            m.new30.add(code)
                    except ValueError:
                        pass
                if mrkt == "10":
                    m.kosdaq.add(code)
                if r.get("nxtEnable") == "Y":
                    m.nxt.add(code)
                if "증거금" in state and "증거금100%" not in state:
                    m.misu.add(code)
                if "관리종목" in state:
                    m.admin.add(code)
                order_warning = str(r.get("orderWarning") or "").strip()
                if order_warning in ("2", "3"):
                    m.single.add(code)
                    if order_warning == "2":
                        m.liquidation.add(code)
                    else:
                        m.short_overheat.add(code)
                elif (r.get("marketCode") in ("0", "10") and not code.endswith("0")
                        and 0 < shares < 500_000):
                    m.single.add(code)  # 저유동성 우선주
        return m

    async def stock_universe(self) -> list[dict]:
        """KOSPI/KOSDAQ 전 종목 기본정보. 분석 DB 최초 적재용."""
        out = []
        for market_code, market_name in (("0", "KOSPI"), ("10", "KOSDAQ")):
            d = await self.request("ka10099", {"mrkt_tp": market_code})
            for row in d.get("list", []):
                code = (row.get("code") or "").removesuffix("_AL")
                if not code:
                    continue
                item_market_code = str(row.get("marketCode") or market_code)
                item_market_name = str(row.get("marketName") or "")
                company_class = str(row.get("companyClassName") or "")
                if item_market_code == "8":
                    stock_type = "ETF"
                elif item_market_code in ("60", "70", "90"):
                    stock_type = "ETN"
                elif item_market_code == "6":
                    stock_type = "REIT"
                elif item_market_code == "2":
                    stock_type = "INFRA"
                elif item_market_code == "4":
                    stock_type = "MUTUAL_FUND"
                elif company_class == "스팩":
                    stock_type = "SPAC"
                elif company_class == "외국기업":
                    stock_type = "FOREIGN"
                elif (row.get("name") or "").endswith(("우", "우B")):
                    stock_type = "PREFERRED"
                else:
                    stock_type = "COMMON"
                out.append({
                    "code": code,
                    "name": row.get("name") or row.get("stk_nm") or "",
                    "market": market_name,
                    "stock_type": stock_type,
                    "sector_name": company_class or item_market_name,
                    "listed_date": row.get("regDay") or "",
                    "shares": _to_int(row.get("listCount")),
                })
        return out

    async def theme_groups(self) -> list[dict]:
        """키움 테마 목록(ka90001)을 반환한다."""
        return await self._theme_pages(
            "ka90001",
            {
                "qry_tp": "0",
                "stk_cd": "",
                "date_tp": "1",
                "thema_nm": "",
                "flu_pl_amt_tp": "1",
                "stex_tp": "1",
            },
            "thema_grp",
        )

    async def theme_members(self, theme_code: str) -> list[dict]:
        """키움 테마 하나의 구성 종목(ka90002)을 반환한다."""
        return await self._theme_pages(
            "ka90002",
            {
                "date_tp": "1",
                "thema_grp_cd": str(theme_code),
                "stex_tp": "1",
            },
            "thema_comp_stk",
        )

    async def _theme_pages(self, api_id: str, body: dict,
                           list_key: str) -> list[dict]:
        """키움 테마 TR의 연속조회 페이지를 모두 합친다."""
        rows = []
        continuation = ""
        while True:
            response = await self._request_raw(
                api_id, body, "/api/dostk/thme", continuation)
            data = response.json()
            if str(data.get("return_code", "0")) not in ("0", ""):
                raise RuntimeError(
                    data.get("return_msg") or f"{api_id} 조회 실패")
            rows.extend(data.get(list_key) or [])
            if response.headers.get("cont-yn", "").upper() != "Y":
                break
            continuation = response.headers.get("next-key", "")
            if not continuation:
                break
        return rows

    async def daily_bars(self, code: str, base_date: str) -> list[dict]:
        """ka10081 수정주가 일봉을 저장 계층에서 쓰는 공통 필드로 정규화."""
        d = await self.request(
            "ka10081",
            {"stk_cd": code, "base_dt": base_date, "upd_stkpc_tp": "1"},
            path="/api/dostk/chart",
        )
        return [{
            "date": row.get("dt") or "",
            "open": abs(_to_int(row.get("open_pric"))),
            "high": abs(_to_int(row.get("high_pric"))),
            "low": abs(_to_int(row.get("low_pric"))),
            "close": abs(_to_int(row.get("cur_prc"))),
            "volume": abs(_to_int(row.get("trde_qty"))),
            # ka10081 거래대금은 백만원 단위이므로 DB 공통 단위인 원으로 변환한다.
            "trading_value": abs(_to_int(row.get("trde_prica"))) * 1_000_000,
        } for row in d.get("stk_dt_pole_chart_qry", []) if row.get("dt")]

    async def market_index_daily(
        self, index_code: str, date_from: str, date_to: str
    ) -> list[dict]:
        """ka20006 코스피·코스닥 지수 일봉을 날짜 범위로 조회한다.

        index_code는 001(KOSPI 종합) 또는 101(KOSDAQ 종합)이다. 키움이
        지수값을 소수점 제거 100배 정수로 주므로 실제 지수값으로 환산한다.
        """
        markets = {"001": "KOSPI", "101": "KOSDAQ"}
        if index_code not in markets:
            raise ValueError(f"지원하지 않는 시장 지수 코드: {index_code}")
        body = {"inds_cd": index_code, "base_dt": date_to}
        rows: list[dict] = []
        continuation = ""
        seen_keys: set[str] = set()
        while True:
            response = await self._request_raw(
                "ka20006", body, "/api/dostk/chart", continuation)
            data = response.json()
            if str(data.get("return_code", "0")) not in ("0", ""):
                raise RuntimeError(
                    data.get("return_msg") or "ka20006 지수 일봉 조회 실패")
            page = data.get("inds_dt_pole_qry") or []
            for row in page:
                trade_date = str(row.get("dt") or "")
                if not date_from <= trade_date <= date_to:
                    continue
                rows.append({
                    "date": trade_date,
                    "index_code": index_code,
                    "market": markets[index_code],
                    "open": abs(_to_int(row.get("open_pric"))) / 100.0,
                    "high": abs(_to_int(row.get("high_pric"))) / 100.0,
                    "low": abs(_to_int(row.get("low_pric"))) / 100.0,
                    "close": abs(_to_int(row.get("cur_prc"))) / 100.0,
                    "volume": abs(_to_int(row.get("trde_qty"))),
                    "trading_value": (
                        abs(_to_int(row.get("trde_prica"))) * 1_000_000
                    ),
                    "source": "KIWOOM",
                })
            oldest = min(
                (str(row.get("dt") or "") for row in page
                 if row.get("dt")),
                default="",
            )
            next_key = response.headers.get("next-key", "")
            if (
                not page
                or (oldest and oldest <= date_from)
                or response.headers.get("cont-yn", "").upper() != "Y"
                or not next_key
                or next_key in seen_keys
            ):
                break
            seen_keys.add(next_key)
            continuation = next_key
        return sorted(
            {row["date"]: row for row in rows}.values(),
            key=lambda row: row["date"],
        )

    async def market_breadth(self) -> list[dict]:
        """ka20001 조회시점의 코스피·코스닥 지수와 등락 종목 수를 반환한다."""
        rows = []
        for index_code, market in (("001", "KOSPI"), ("101", "KOSDAQ")):
            data = await self.request(
                "ka20001",
                {"mrkt_tp": "0", "inds_cd": index_code},
                "/api/dostk/sect",
            )
            if str(data.get("return_code", "0")) not in ("0", ""):
                raise RuntimeError(
                    data.get("return_msg") or f"{market} 현재시장 조회 실패")
            rising = abs(_to_int(data.get("rising")))
            falling = abs(_to_int(data.get("fall")))
            unchanged = abs(_to_int(data.get("stdns")))
            upper = abs(_to_int(data.get("upl")))
            lower = abs(_to_int(data.get("lst")))
            stock_count = abs(_to_int(data.get("trde_frmatn_stk_num")))
            if not stock_count:
                stock_count = rising + falling + unchanged
            rows.append({
                "market": market,
                "index_code": index_code,
                "close_value": abs(_to_float(data.get("cur_prc"))),
                "change_rate": _to_float(data.get("flu_rt")),
                "volume": abs(_to_int(data.get("trde_qty"))),
                "trading_value": (
                    abs(_to_int(data.get("trde_prica"))) * 1_000_000
                ),
                "stock_count": stock_count,
                "rising": rising,
                "falling": falling,
                "unchanged": unchanged,
                "limit_up_count": upper,
                "limit_down_count": lower,
            })
        return rows

    async def market_investor_flows(
        self, market: str, trade_date: str
    ) -> list[dict]:
        """ka10051 시장·업종별 투자자 순매수 금액을 조회한다.

        금액 조회 응답은 억원 단위이므로 기존 DB 수급 단위인 백만원으로
        환산한다. market은 KOSPI 또는 KOSDAQ이다.
        """
        market_codes = {"KOSPI": "0", "KOSDAQ": "1"}
        market = str(market or "").upper()
        if market not in market_codes:
            raise ValueError(f"지원하지 않는 시장: {market}")
        body = {
            "mrkt_tp": market_codes[market],
            "amt_qty_tp": "0",
            "base_dt": trade_date,
            "stex_tp": "1",
        }
        key_map = {
            "sc_netprps": "securities",
            "insrnc_netprps": "insurance",
            "invtrt_netprps": "investment_trust",
            "bank_netprps": "bank",
            "jnsinkm_netprps": "merchant_bank",
            "endw_netprps": "fund",
            "etc_corp_netprps": "other_corporation",
            "ind_netprps": "individual",
            "frgnr_netprps": "foreign",
            "native_trmt_frgnr_netprps": "domestic_foreign",
            "natn_netprps": "national",
            "samo_fund_netprps": "private_fund",
            "orgn_netprps": "institution",
        }
        rows: list[dict] = []
        continuation = ""
        seen_keys: set[str] = set()
        while True:
            response = await self._request_raw(
                "ka10051", body, "/api/dostk/sect", continuation)
            data = response.json()
            if str(data.get("return_code", "0")) not in ("0", ""):
                raise RuntimeError(
                    data.get("return_msg")
                    or "ka10051 시장수급 조회 실패")
            for row in data.get("inds_netprps") or []:
                industry_code = str(row.get("inds_cd") or "").strip()
                if not industry_code:
                    continue
                normalized = {
                    "date": trade_date,
                    "market": market,
                    "industry_code": industry_code,
                    "industry_name": str(
                        row.get("inds_nm") or "").strip(),
                    "change_rate": _to_float(row.get("flu_rt")),
                    "volume": abs(_to_int(row.get("trde_qty"))),
                    "source": "KIWOOM",
                }
                for source_key, field in key_map.items():
                    normalized[f"{field}_net_amount_million"] = (
                        _to_int(row.get(source_key)) * 100
                    )
                rows.append(normalized)
            next_key = response.headers.get("next-key", "")
            if (
                response.headers.get("cont-yn", "").upper() != "Y"
                or not next_key
                or next_key in seen_keys
            ):
                break
            seen_keys.add(next_key)
            continuation = next_key
        return list({
            row["industry_code"]: row for row in rows
        }.values())

    async def investor_flows(
        self, code: str, date_from: str, date_to: str
    ) -> list[dict]:
        """ka10060: 종목별 일별 투자자 순매수(금액, 백만원)."""
        body = {
            "dt": date_to,
            "stk_cd": code,
            "amt_qty_tp": "1",
            "trde_tp": "0",
            "unit_tp": "1",
        }
        rows: list[dict] = []
        cont = ""
        while True:
            response = await self._request_raw(
                "ka10060", body, "/api/dostk/chart", cont)
            data = response.json()
            page = data.get("stk_invsr_orgn_chart", [])
            rows.extend(row for row in page if row.get("dt"))
            oldest = min(
                (str(row.get("dt") or "") for row in page),
                default="",
            )
            next_key = response.headers.get("next-key", "")
            if (
                not page
                or (oldest and oldest <= date_from)
                or response.headers.get("cont-yn", "").upper() != "Y"
                or not next_key
            ):
                break
            cont = next_key
        return [
            row for row in rows
            if date_from <= str(row.get("dt") or "") <= date_to
        ]

    async def inquiry_rank(self, qry_tp: str = "5") -> list[dict]:
        """ka00198 실시간 종목조회순위 -> rank.py 필드로 정규화.
        qry_tp: 1=1분 2=10분 3=1시간 4=당일누적 5=30초 (기준 집계기간)."""
        # ka00198은 순위 성격의 TR이지만 실제 REST 제공 경로는 stkinfo이다.
        d = await self.request("ka00198", {"qry_tp": qry_tp})
        return [{
            "rank": _to_int(r.get("bigd_rank")),
            "code": r.get("stk_cd", ""),
            "name": r.get("stk_nm", ""),
            "price": abs(_to_int(r.get("past_curr_prc"))),
            "rate": _to_float(r.get("base_comp_chgr")),
            "prev_rate": _to_float(r.get("prev_base_chgr")),
            "rank_chg": _to_int(r.get("rank_chg")),
            "time": r.get("tm", ""),
        } for r in d.get("item_inq_rank", [])]

    async def holdings(self) -> list[dict]:
        """kt00018 계좌평가잔고내역의 보유수량이 있는 국내주식 목록."""
        return [
            {"code": row["code"], "name": row["name"]}
            for row in await self.holding_positions()
        ]

    async def holding_positions(self) -> list[dict]:
        """kt00018의 실제 보유수량과 현재 매매가능수량을 반환한다."""
        body = {"qry_tp": "1", "dmst_stex_tp": "KRX"}
        out = []
        cont = ""
        while True:
            r = await self._request_raw("kt00018", body, "/api/dostk/acnt", cont)
            d = r.json()
            # 국내주식 REST 실응답 컨테이너명. 구 명세의 이름도 호환한다.
            rows = d.get("acnt_evlt_remn_indv_tot", d.get("stk_acnt_evlt_prst", []))
            for item in rows:
                if _to_int(item.get("rmnd_qty")) <= 0:
                    continue
                code = (item.get("stk_cd") or "").strip()
                if code.startswith("A") and len(code) == 7:
                    code = code[1:]
                code = code.split("_")[0]
                if code:
                    held = max(0, _to_int(item.get("rmnd_qty")))
                    # 매매가능수량은 기존 미체결 매도분을 제외하므로 청산 주문에는
                    # 보유수량보다 이 값을 우선한다.
                    sellable = max(0, _to_int(item.get("trde_able_qty")))
                    out.append({
                        "code": code,
                        "name": item.get("stk_nm", ""),
                        "held_qty": held,
                        "sellable_qty": sellable,
                    })
            if r.headers.get("cont-yn", "N").upper() != "Y":
                break
            cont = r.headers.get("next-key", "")
            if not cont:
                break
        return out

    async def holding_position(self, code: str) -> dict:
        code = str(code).strip().split("_")[0].removeprefix("A")
        return next((
            row for row in await self.holding_positions()
            if row["code"] == code
        ), {"code": code, "name": "", "held_qty": 0, "sellable_qty": 0})

    async def open_buy_orders(self, code: str) -> list[dict]:
        """ka10075로 해당 종목의 계좌 전체 미체결 매수주문을 조회한다."""
        code = str(code).strip().split("_")[0].removeprefix("A")
        body = {
            "all_stk_tp": "1",  # 종목
            "trde_tp": "2",     # 매수
            "stk_cd": code,
            "stex_tp": "0",     # 통합
        }
        out = []
        cont = ""
        while True:
            response = await self._request_raw(
                "ka10075", body, "/api/dostk/acnt", cont)
            data = response.json()
            for item in data.get("oso", []):
                remaining = max(0, _to_int(item.get("oso_qty")))
                order_no = str(item.get("ord_no") or "").strip()
                row_code = str(item.get("stk_cd") or code)
                row_code = row_code.split("_")[0].removeprefix("A")
                if order_no and remaining > 0 and row_code == code:
                    ordered = max(0, _to_int(item.get("ord_qty")))
                    exchange = str(
                        item.get("stex_tp_txt") or "").strip().upper()
                    if exchange == "통합" or str(
                            item.get("sor_yn") or "").upper() == "Y":
                        exchange = "SOR"
                    if exchange not in {"KRX", "NXT", "SOR"}:
                        exchange = {
                            "0": "SOR", "1": "KRX", "2": "NXT",
                        }.get(str(item.get("stex_tp") or ""), "KRX")
                    out.append({
                        "code": code,
                        "order_no": order_no,
                        "order_qty": ordered,
                        "remaining_qty": remaining,
                        "filled_qty": max(0, ordered - remaining),
                        "exchange": exchange,
                    })
            if response.headers.get("cont-yn", "N").upper() != "Y":
                break
            cont = response.headers.get("next-key", "")
            if not cont:
                break
        return out

    async def cancel_open_buy_orders(self, code: str) -> tuple[int, int]:
        """주문 출처와 무관하게 해당 종목의 계좌 미체결 매수를 전부 취소한다."""
        orders = await self.open_buy_orders(code)
        sent = 0
        qty = 0
        for order in orders:
            await self.cancel_order(
                order["code"], order["order_no"], 0, order["exchange"])
            sent += 1
            qty += order["remaining_qty"]
        return sent, qty

    async def cancel_open_buy_order(
            self, code: str, order_no: str) -> tuple[int, int]:
        """해당 주문번호의 남은 계좌 미체결 매수만 취소한다."""
        target_order_no = str(order_no or "").strip()
        if not target_order_no:
            return 0, 0
        for order in await self.open_buy_orders(code):
            if order["order_no"] == target_order_no:
                qty = order["remaining_qty"]
                await self.cancel_order(
                    order["code"], target_order_no, 0, order["exchange"])
                return 1, qty
        return 0, 0

    async def cancel_filled_buy_orders(
            self, code: str, minimum_filled: int = 100) -> tuple[int, int]:
        """실제 계좌 누적체결량이 기준 이상인 미체결 매수주문만 취소한다."""
        orders = await self.open_buy_orders(code)
        targets = [
            order for order in orders
            if order["filled_qty"] >= max(1, int(minimum_filled))
        ]
        qty = 0
        for order in targets:
            await self.cancel_order(
                order["code"], order["order_no"], 0, order["exchange"])
            qty += order["remaining_qty"]
        return len(targets), qty

    async def account_summary(self) -> dict:
        """주문 화면용 실계좌 요약.

        kt00018: 추정예탁자산, kt00001: 증거금률별 주문가능금액.
        미수 미사용 기본값은 100% 증거금 주문가능금액을 사용한다.
        """
        evlt = await self._request_raw(
            "kt00018", {"qry_tp": "1", "dmst_stex_tp": "KRX"}, "/api/dostk/acnt")
        deposit = await self._request_raw("kt00001", {"qry_tp": "3"}, "/api/dostk/acnt")
        evlt_data = evlt.json()
        deposit_data = deposit.json()

        def optional_amount(*keys):
            """실제 응답에 포함된 금액만 반환해 누락값을 0원으로 오인하지 않는다."""
            for key in keys:
                if key in deposit_data and deposit_data.get(key) not in (None, ""):
                    return _to_int(deposit_data.get(key))
            return None

        return {
            "estimated_assets": _to_int(evlt_data.get("prsm_dpst_aset_amt")),
            "cash_orderable": _to_int(deposit_data.get("100stk_ord_alow_amt")),
            "cash_deposit": _to_int(deposit_data.get("entr")),
            "withdrawable": optional_amount("pymn_alow_amt"),
            # 계좌/서비스에 따라 매도담보대출 인출가능금액 필드가 생략될 수
            # 있으므로 확인된 응답 키만 사용하고 없으면 UI에 '-'로 표시한다.
            "loan_withdrawable": optional_amount(
                "lnd_pymn_alow_amt",
                "loan_pymn_alow_amt",
                "sell_coll_lnd_alow_amt",
            ),
            "orderable_by_margin": {
                rate: _to_int(deposit_data.get(f"{rate}stk_ord_alow_amt"))
                for rate in (20, 30, 40, 50, 60, 100)
            },
        }

    async def orderable_quantity(self, code: str, price: int) -> dict:
        """kt00011: 선택 종목·주문가격 기준 현금/미수 주문가능수량.

        kt00011은 증거금률별 주문가능수량 조회다. kt00010(주문인출가능금액)을
        쓰면 수량 응답 필드가 없어 주문가능수량을 표시할 수 없다.
        """
        r = await self._request_raw(
            "kt00011",
            {
                "stk_cd": code,
                "uv": str(int(price)),
            },
            "/api/dostk/acnt",
        )
        data = r.json()
        if str(data.get("return_code", "0")) not in ("0", ""):
            raise RuntimeError(
                data.get("return_msg") or "kt00010 주문가능수량 조회 실패")

        def first_value(*keys):
            for key in keys:
                if key in data and data[key] not in (None, ""):
                    return data[key]
            return None

        cash_amount = first_value(
            "profa_100ord_alow_amt", "100ord_alow_amt",
            "100stk_ord_alow_amt",
        )
        cash_qty = first_value(
            "profa_100ord_alowq", "100ord_alowq", "100stk_ord_alowq",
        )
        if cash_amount is None or cash_qty is None:
            missing = []
            if cash_amount is None:
                missing.append("100% 주문가능금액")
            if cash_qty is None:
                missing.append("100% 주문가능수량")
            raise ValueError(
                f"kt00010 주문가능수량 응답 필드 누락: {', '.join(missing)}")
        applied_text = str(first_value("aplc_rt", "stk_profa_rt") or "100")
        rate_digits = "".join(character for character in applied_text
                              if character.isdigit())
        applied_rate = int(rate_digits or 100)
        if applied_rate not in (20, 30, 40, 50, 60, 100):
            applied_rate = 100
        prefix = f"profa_{applied_rate}"
        margin_amount = first_value(
            prefix + "ord_alow_amt",
            f"{applied_rate}ord_alow_amt",
            f"{applied_rate}stk_ord_alow_amt",
        )
        margin_qty = first_value(
            prefix + "ord_alowq",
            f"{applied_rate}ord_alowq",
            f"{applied_rate}stk_ord_alowq",
        )
        return {
            "code": code,
            "price": int(price),
            "stock_margin_rate": str(data.get("stk_profa_rt", "")),
            "applied_margin_rate": applied_rate,
            "cash_amount": _to_int(cash_amount),
            "cash_qty": _to_int(cash_qty),
            "margin_amount": _to_int(
                margin_amount if margin_amount is not None else cash_amount),
            "margin_qty": _to_int(
                margin_qty if margin_qty is not None else cash_qty),
        }

    async def volume_surge(self, tm: str = "60", stex_tp: str = "3",
                           drop_etf: bool = True) -> list[dict]:
        """ka10023 거래량급증 -> 조회순위와 같은 필드로 정규화 (순위=목록순, 변동 없음).
        tm: 집계 구간(분) — 직전 tm분 대비 급증. stex_tp: 1=KRX 2=NXT 3=통합(애프터마켓 포함).
        drop_etf: 종목명 접두로 ETF/ETN 제외 -> 코스피·코스닥 일반주는 유지.
        필드명 07-10 실측 확정: cur_prc/flu_rt/stk_cd/stk_nm, 컨테이너 trde_qty_sdnin."""
        d = await self.request("ka10023", {
            "mrkt_tp": "000", "sort_tp": "1", "tm_tp": "2", "trde_qty_tp": "0",
            "tm": tm, "stk_cnd": "0", "pric_tp": "0", "stex_tp": stex_tp,
        }, path="/api/dostk/rkinfo")
        rows = d.get("trde_qty_sdnin", [])
        out = []
        rank = 0
        for r in rows:
            code = (r.get("stk_cd") or "").split("_")[0]
            name = r.get("stk_nm", "")
            if not code or (drop_etf and name.startswith(ETF_PREFIXES)):
                continue
            rank += 1
            out.append({
                "rank": rank, "code": code, "name": name,
                "price": abs(_to_int(r.get("cur_prc"))),
                "rate": _to_float(r.get("flu_rt")),
                "prev_rate": 0.0, "rank_chg": 0, "time": "",
            })
        return out

    async def trade_value_rank(self, stex_tp: str = "3", drop_etf: bool = True) -> list[dict]:
        """ka10032 거래대금상위 -> 조회순위와 같은 필드로 정규화. 순위=now_rank, 변동=pred-now.
        stex_tp: 1=KRX 2=NXT 3=통합(애프터마켓 포함).
        drop_etf: 종목명 접두로 ETF/ETN 제외(응답에 종목구분 필드가 없어 이름으로 거름).
        메이저 발행사 접두만 커버(레버리지/인버스가 거래대금 상위 독식) -> 코스피·코스닥 일반주는 유지."""
        d = await self.request("ka10032", {
            "mrkt_tp": "000", "mang_stk_incls": "1", "stex_tp": stex_tp,
        }, path="/api/dostk/rkinfo")
        rows = d.get("trde_prica_upper", [])
        out = []
        rank = 0
        for r in rows:
            code = (r.get("stk_cd") or "").split("_")[0]
            name = r.get("stk_nm", "")
            if not code or (drop_etf and name.startswith(ETF_PREFIXES)):
                continue
            rank += 1
            out.append({
                "rank": rank, "code": code, "name": name,
                "price": abs(_to_int(r.get("cur_prc"))),
                "rate": _to_float(r.get("flu_rt")),
                "volume": abs(_to_int(r.get("now_trde_qty"))),
                "trading_value": (
                    abs(_to_int(r.get("trde_prica"))) * 1_000_000),
                "prev_rate": 0.0,
                "rank_chg": _to_int(r.get("pred_rank")) - _to_int(r.get("now_rank")),
                "time": "",
            })
        return out

    async def change_rate_rank(self, stex_tp: str = "2") -> list[dict]:
        """ka10027 전일대비등락률상위 -> 상승률순. stex_tp=2면 NXT 전용.
        NXT 서버가 매매체결 대상만 반환하므로 ETF/ETN 등의 별도 필터는 적용하지 않는다."""
        d = await self.request("ka10027", {
            "mrkt_tp": "000", "sort_tp": "1", "trde_qty_cnd": "0000",
            "stk_cnd": "0", "crd_cnd": "0", "updown_incls": "1",
            "pric_cnd": "0", "trde_prica_cnd": "0", "stex_tp": stex_tp,
        }, path="/api/dostk/rkinfo")
        rows = d.get("pred_pre_flu_rt_upper", [])
        out = []
        for rank, r in enumerate(rows, 1):
            code = (r.get("stk_cd") or "").split("_")[0]
            if not code:
                continue
            out.append({
                "rank": rank, "code": code, "name": r.get("stk_nm", ""),
                "price": abs(_to_int(r.get("cur_prc"))),
                "rate": _to_float(r.get("flu_rt")),
                "prev_rate": 0.0, "rank_chg": 0, "time": "",
            })
        return out

    async def last_limit_entry(self, code: str, upper: int) -> str:
        """상한가 마지막 진입시각(초단위). ka10079 틱차트를 최신->과거로 스캔해 현재가=상한가인
        연속 구간의 첫 틱 시각을 반환. 영웅문과 동일한 초단위. 현재 상한가가 아니면 ''.
        무너졌다 재진입하면 가장 최근 연속구간이 잡혀 '마지막 진입'이 된다. 반환 'HH:MM:SS'.
        상한 구간이 900틱(1페이지)보다 길면 헤더 연속조회로 페이징(활발한 상한: 실측 3페이지).
        페이지 상한 초과(초활발 상한)면 틀린 초 대신 분봉 폴백(분단위, 초는 00)."""
        if not upper:
            return ""
        entry, cont, today = "", "", ""
        for _ in range(config.TICK_MAX_PAGES):  # 무한 페이징 방지
            r = await self._request_raw(
                "ka10079", {"stk_cd": code, "tic_scope": "1", "upd_stkpc_tp": "1"},
                "/api/dostk/chart", cont=cont)
            ticks = r.json().get("stk_tic_chart_qry", [])
            if not ticks:
                break
            today = today or ticks[0].get("cntr_tm", "")[:8]  # 첫 페이지 최신틱 = 당일
            for b in ticks:  # 최신 -> 과거
                t = b.get("cntr_tm", "")
                if t[:8] != today or abs(_to_int(b.get("cur_prc"))) != upper:
                    return _hms(entry)  # 상한 무너진 틱 = 진입 확정 (초단위)
                entry = t  # 더 과거의 상한 틱으로 계속 갱신 = 연속구간 시작점
            if r.headers.get("cont-yn") != "Y":
                return _hms(entry)  # 데이터 소진 = 첫 틱까지 다 봄
            cont = r.headers.get("next-key", "")
            if not cont:
                return _hms(entry)
        # 페이지 상한 도달(진입 못 찾음) -> 분봉으로 분단위라도 정확히
        return await self._limit_entry_minute(code, upper)

    async def last_limit_entry_on_date(
            self, code: str, upper: int, trade_date: str) -> str:
        """틱의 실제 거래일이 지정일과 일치할 때만 마지막 진입시각을 반환한다."""
        if not upper:
            return ""
        entry, cont = "", ""
        for _ in range(config.TICK_MAX_PAGES):
            response = await self._request_raw(
                "ka10079",
                {"stk_cd": code, "tic_scope": "1", "upd_stkpc_tp": "1"},
                "/api/dostk/chart", cont=cont)
            ticks = response.json().get("stk_tic_chart_qry", [])
            if not ticks:
                return ""
            actual_date = ticks[0].get("cntr_tm", "")[:8]
            if actual_date != trade_date:
                return ""
            for tick in ticks:
                tick_time = tick.get("cntr_tm", "")
                if (tick_time[:8] != trade_date
                        or abs(_to_int(tick.get("cur_prc"))) != upper):
                    return _hms(entry)
                entry = tick_time
            if response.headers.get("cont-yn") != "Y":
                return _hms(entry)
            cont = response.headers.get("next-key", "")
            if not cont:
                return _hms(entry)
        return _hms(entry)

    async def _limit_entry_minute(self, code: str, upper: int) -> str:
        """분봉(ka10080) 폴백: 초활발 상한이라 틱 페이징이 안 끝날 때 분단위 진입시각."""
        d = await self.request("ka10080", {"stk_cd": code, "tic_scope": "1", "upd_stkpc_tp": "1"},
                               path="/api/dostk/chart")
        bars = d.get("stk_min_pole_chart_qry", [])
        if not bars:
            return ""
        today = bars[0].get("cntr_tm", "")[:8]
        entry = ""
        for b in bars:
            t = b.get("cntr_tm", "")
            if t[:8] != today or abs(_to_int(b.get("cur_prc"))) != upper:
                break
            entry = t
        return _hms(entry)

    async def close(self):
        await self._client.aclose()


def _in_auction() -> bool:
    """개장/마감 동시호가 시간대(로컬=KST) 여부."""
    import time
    hm = time.strftime("%H%M")
    return "0830" <= hm < "0900" or "1520" <= hm < "1530"


def _hms(tm: str) -> str:
    """'yyyyMMddHHmmss' -> 'HH:MM:SS'. 14자 미만이면 ''."""
    return f"{tm[8:10]}:{tm[10:12]}:{tm[12:14]}" if len(tm) >= 14 else ""


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
    assert _hms("20260707145940") == "14:59:40"
    assert _hms("") == "" and _hms("2026") == ""
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
