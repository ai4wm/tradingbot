# -*- coding: utf-8 -*-
"""웹소켓: 접속/LOGIN/PING/재접속 + 조건검색(CNSRLST/CNSRREQ/CNSRCLR) + 실시간 시세(REG/REMOVE).

콜백 3개를 main.py가 연결한다:
    on_condition_event(code, is_insert, time_str)   # 편입/이탈
    on_real(code, fields)                           # 시세 갱신 (gui on_tick 필드로 정규화됨)
    on_condition_list(list[(seq, name)])            # CNSRLST 결과

재접속: 지수 백오프, 성공 시 등록돼 있던 조건검색+시세 자동 재등록.
"""
import asyncio
import json
import logging

import websockets

import config

log = logging.getLogger("ws")

# 실시간 FID -> gui 필드 매핑. ⚠️ 문서 확인: 키움 '실시간 항목표'와 대조 필수.
# 0B(주식체결), 0D(주식호가잔량). 값은 부호/콤마 섞인 문자열로 옴 -> _num()로 정규화.
FID = {
    "10": "price",     # 현재가 (부호 포함 -> abs)
    "12": "rate",      # 등락율
    "13": "vol",       # 누적거래량
    "61": "ask_qty",   # 최우선 매도잔량 (0D 매도호가1 잔량)
    "71": "bid_qty",   # 최우선 매수잔량 (0D 매수호가1 잔량)
    # ⚠️ 예상체결가: 동시호가/VI 때만 실시간 수신 -> 이때만 예상 컬럼이 채워지고 평시엔 빈칸.
    #    FID 번호(예상체결가)는 개장 동시호가에서 실제 REAL 메시지로 확인해 아래 값을 채울 것.
    "예상체결가_FID_확인필요": "exp_price",
}


def _num(v):
    try:
        return float(str(v).replace(",", "").replace("+", "").strip() or 0)
    except (ValueError, TypeError):
        return 0.0


# --- 순수 함수: 메시지 빌드/파싱 (소켓 없이 테스트 가능) --------------------

def build_reg(codes: list[str], types: list[str], grp_no: str = "1", refresh: str = "1") -> dict:
    return {"trnm": "REG", "grp_no": grp_no, "refresh": refresh,
            "data": [{"item": codes, "type": types}]}


def build_remove(codes: list[str], types: list[str], grp_no: str = "1") -> dict:
    return {"trnm": "REMOVE", "grp_no": grp_no,
            "data": [{"item": codes, "type": types}]}


def parse_real_item(item: dict) -> tuple[str, dict]:
    """REAL data 원소 하나 -> (종목코드, {gui필드: 값}). 매핑 없는 FID는 무시."""
    code = item.get("item", "")
    values = item.get("values", {})
    out = {}
    for fid, raw in values.items():
        field = FID.get(fid)
        if not field:
            continue
        n = _num(raw)
        if field in ("price", "exp_price"):   # 가격류: 부호 제거 + 정수
            out[field] = int(abs(n))
        elif field in ("vol", "ask_qty", "bid_qty"):
            out[field] = int(n)
        else:
            out[field] = n
    return code, out


def parse_condition_list(data: list) -> list[tuple[str, str]]:
    """CNSRLST data([[seq, name], ...]) -> [(seq, name)].
    서버는 seq를 문자열 정렬(0,1,10,100,...,11)로 주므로 숫자로 재정렬해 영웅문 순서를 맞춘다."""
    rows = [(str(row[0]), row[1]) for row in data if len(row) >= 2]
    return sorted(rows, key=lambda r: int(r[0]) if r[0].isdigit() else 1 << 30)


class WSClient:
    def __init__(self):
        self.on_condition_event = None   # (code, is_insert, time_str)
        self.on_real = None              # (code, fields)
        self.on_condition_list = None    # (list[(seq, name)])
        self._ws = None
        self._token_fn = None            # async () -> token
        self._active_seq = None          # 등록된 조건식 일련번호 (재등록용)
        self._reg_codes: set[str] = set()  # 실시간 등록 종목 (재등록용)
        self._connected = asyncio.Event()
        self._seen_fids: set = set()       # 처음 본 (type,fid)만 로그 (FID 발굴용)

    # --- 외부 API -------------------------------------------------------
    async def run(self, token_fn):
        """무한 접속 루프. token_fn: 매 재접속마다 새 토큰을 주는 async 콜러블."""
        self._token_fn = token_fn
        backoff = 1
        while True:
            try:
                await self._connect_once()
                backoff = 1  # 정상 접속했으니 리셋
            except Exception as e:  # noqa: BLE001 - 어떤 예외든 재접속
                self._connected.clear()
                log.warning("ws error: %s -> reconnect in %ss", e, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 30)

    async def list_conditions(self):
        await self._send({"trnm": "CNSRLST"})

    async def register_condition(self, seq: str):
        self._active_seq = seq
        await self._send({"trnm": "CNSRREQ", "seq": seq, "search_type": "1", "stex_tp": "K"})

    async def clear_condition(self):
        if self._active_seq is not None:
            await self._send({"trnm": "CNSRCLR", "seq": self._active_seq})
            self._active_seq = None

    async def register_real(self, code: str):
        if code in self._reg_codes:
            return
        if len(self._reg_codes) >= config.REAL_REG_LIMIT:
            log.warning("real-time reg limit %d reached, skip %s", config.REAL_REG_LIMIT, code)
            return
        self._reg_codes.add(code)
        await self._send(build_reg([code], ["0B", "0D"]))

    async def remove_real(self, code: str):
        if code not in self._reg_codes:
            return
        self._reg_codes.discard(code)
        await self._send(build_remove([code], ["0B", "0D"]))

    # --- 내부 ----------------------------------------------------------
    async def _connect_once(self):
        token = await self._token_fn()
        async with websockets.connect(config.WS_URL, ping_interval=None) as ws:
            self._ws = ws
            await ws.send(json.dumps({"trnm": "LOGIN", "token": token}))
            login = json.loads(await ws.recv())
            if login.get("return_code") not in (0, "0", None):
                raise RuntimeError(f"LOGIN failed: {login}")
            log.info("ws connected + LOGIN ok")
            self._connected.set()
            await self._resubscribe()
            async for raw in ws:
                await self._dispatch(json.loads(raw))
            raise ConnectionError("socket closed")  # -> 재접속

    async def _resubscribe(self):
        """재접속 시 조건검색 + 실시간 시세 자동 재등록."""
        await self.list_conditions()
        if self._active_seq is not None:
            await self._send({"trnm": "CNSRREQ", "seq": self._active_seq,
                              "search_type": "1", "stex_tp": "K"})
        if self._reg_codes:
            await self._send(build_reg(list(self._reg_codes), ["0B", "0D"]))

    async def _send(self, msg: dict):
        if self._ws is None:
            log.warning("send while disconnected: %s", msg.get("trnm"))
            return
        await self._ws.send(json.dumps(msg))
        log.debug("send %s", msg)

    async def _dispatch(self, msg: dict):
        trnm = msg.get("trnm")
        if trnm == "PING":
            await self._ws.send(json.dumps(msg))  # 받은 그대로 echo
            return
        log.debug("recv %s", msg)
        if trnm == "CNSRLST" and self.on_condition_list:
            self.on_condition_list(parse_condition_list(msg.get("data", [])))
        elif trnm == "CNSRREQ":
            self._handle_condition(msg)
        elif trnm == "REAL":
            for item in msg.get("data", []):
                self._discover_fids(item)  # 처음 본 FID 로그 (개장 동시호가 때 예상체결가 FID 확인용)
                code, fields = parse_real_item(item)
                if code and fields and self.on_real:
                    self.on_real(code, fields)

    def _discover_fids(self, item: dict):
        """처음 보는 (type,fid)를 한 번씩 로그. 매핑여부+샘플값 포함.
        개장 동시호가(08:50~) 때 bot.log 열면 예상체결가 등 UNMAPPED FID를 바로 찾는다."""
        typ = item.get("type", "")
        for fid, val in item.get("values", {}).items():
            key = (typ, fid)
            if key in self._seen_fids:
                continue
            self._seen_fids.add(key)
            log.info("FID discover: type=%s fid=%s -> %s  sample=%r",
                     typ, fid, FID.get(fid, "UNMAPPED"), val)

    def _handle_condition(self, msg: dict):
        """CNSRREQ 응답/실시간 편입·이탈. ⚠️ 문서 확인: 편입/이탈 필드명(I/D)."""
        if not self.on_condition_event:
            return
        for item in msg.get("data", []):
            code = (item.get("9001") or item.get("jmcode") or item.get("item") or "").lstrip("A")
            if not code:
                continue
            # type '1'/'I'=편입, '2'/'D'=이탈. 초기 리스트는 편입으로 취급.
            t = str(item.get("type", item.get("841", "I")))
            is_insert = t in ("1", "I", "insert")
            time_str = item.get("time") or item.get("843") or ""
            self.on_condition_event(code, is_insert, time_str)


def _demo():
    """소켓 없이 순수 로직 자가검증."""
    # REG/REMOVE 빌드
    assert build_reg(["005930"], ["0B"])["data"][0]["item"] == ["005930"]
    assert build_remove(["005930"], ["0B"])["trnm"] == "REMOVE"
    # REAL 파싱: 부호/콤마 정규화 + price abs
    code, f = parse_real_item({"item": "005930", "values": {"10": "-4,620", "12": "+29.96", "13": "19687"}})
    assert code == "005930", code
    assert f["price"] == 4620 and f["rate"] == 29.96 and f["vol"] == 19687, f
    # 매핑 없는 FID 무시
    _, f2 = parse_real_item({"item": "x", "values": {"9999": "1"}})
    assert f2 == {}, f2
    # CNSRLST 파싱
    assert parse_condition_list([["0", "상한근접"], ["1", "급등주"]]) == [("0", "상한근접"), ("1", "급등주")]
    print("ws self-check OK")


if __name__ == "__main__":
    _demo()
