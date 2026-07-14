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
import time

import websockets

import config

log = logging.getLogger("ws")

# 실시간 FID -> gui 필드 매핑. ⚠️ 문서 확인: 키움 '실시간 항목표'와 대조 필수.
# 0B(주식체결), 0D(주식호가잔량). 값은 부호/콤마 섞인 문자열로 옴 -> _num()로 정규화.
FID = {
    "10": "price",     # 현재가 (부호 포함 -> abs)
    "12": "rate",      # 등락율
    "13": "vol",       # 누적거래량
    "15": "tick_qty",  # 개별 체결량 (+매수체결 / -매도체결, 부호 보존)
    "61": "ask_qty",   # 최우선 매도잔량 (0D 매도호가1 잔량)
    "71": "bid_qty",   # 최우선 매수잔량 (0D 매수호가1 잔량)
    # 예상가: 0D 23/24는 장중에도 값이 바뀌며 옴(상한가 종목 등) -> 표시 ON 신호로 못 씀.
    # gui가 0H/단일가/VI/동시호가REST로 켠 상태에서만 갱신용으로 반영한다.
    "23": "exp_price",
    "24": "exp_qty",
}

# 0H(주식예상체결): 단일가 국면(동시호가/VI/단일가종목)에만 서버가 보냄 = 자기게이트.
# 같은 FID 번호가 0B와 다른 의미(10=예상체결가, 13=예상체결량).
FID_0H = {
    "10": "exp_price",
    "13": "exp_qty",
}

REAL_TYPES = ["0B", "0D", "0H", "1h"]  # 체결 / 호가잔량 / 예상체결 / VI발동해제


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
    """REAL data 원소 하나 -> (종목코드, {gui필드: 값}). 매핑 없는 FID는 무시.
    통합(_AL)/NXT(_NX) 등록 시 item에 접미사가 붙어 옴 -> 떼서 순수코드로."""
    code = (item.get("item") or "").split("_")[0]
    values = item.get("values", {})
    table = FID_0H if item.get("type") == "0H" else FID
    out = {}
    for fid, raw in values.items():
        field = table.get(fid)
        if not field:
            continue
        n = _num(raw)
        if field in ("price", "exp_price"):   # 가격류: 부호 제거 + 정수
            out[field] = int(abs(n))
        elif field in ("vol", "tick_qty", "ask_qty", "bid_qty", "exp_qty"):
            out[field] = int(n)
        else:
            out[field] = n
    if table is FID_0H and out:
        out["exp_hot"] = 1  # 0H는 단일가/VI 국면에만 옴 -> gui가 판정 없이 표시
    return code, out


def parse_condition_list(data: list) -> list[tuple[str, str]]:
    """CNSRLST data([[seq, name], ...]) -> [(seq, name)].
    서버는 seq를 문자열 정렬(0,1,10,100,...,11)로 주므로 숫자로 재정렬해 영웅문 순서를 맞춘다."""
    rows = [(str(row[0]), row[1]) for row in data if len(row) >= 2]
    return sorted(rows, key=lambda r: int(r[0]) if r[0].isdigit() else 1 << 30)


class WSClient:
    def __init__(self):
        self.on_condition_event = None    # (seq, code, is_insert) - 실시간 편입/이탈
        self.on_condition_snapshot = None  # (seq, list[code]) - CNSRREQ 초기 목록
        self.on_real = None               # (code, fields)
        self.on_vi = None                 # (code, active, 발동가) - VI 발동/해제
        self.on_condition_list = None     # (list[(seq, name)])
        self._ws = None
        self._token_fn = None            # async () -> token
        self._active_seqs: set[str] = set()  # 등록된 조건식들 (창마다 1개, 재등록용)
        # (순수코드, 명시 접미사) -> 참조수. suffix=None은 전역 KRX/통합 설정을 따르고,
        # "_NX"는 NXT 전용 창이라 전역 설정이 바뀌어도 그대로 유지한다.
        self._reg_codes: dict[tuple[str, str | None], int] = {}
        # 시세 접미사: "" = KRX 전용, "_AL" = KRX+NXT 통합 (REG 코드에만 붙임, 실측 확인).
        # 조건검색(CNSRREQ)은 stex_tp "K"만 허용이라 편입/이탈은 항상 KRX 기준.
        self.real_suffix = ""
        self._connected = asyncio.Event()
        self._seen_fids: set = set()       # 처음 본 (type,fid)만 로그 (FID 발굴용)
        self._real_stats: dict = {}        # 5초 단위 REAL 수신 빈도 (예상값 갱신속도 진단용)
        self._stats_t = 0.0

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
        self._active_seqs.add(str(seq))
        await self._send({"trnm": "CNSRREQ", "seq": seq, "search_type": "1", "stex_tp": "K"})

    async def clear_condition(self, seq: str):
        if str(seq) in self._active_seqs:
            self._active_seqs.discard(str(seq))
            await self._send({"trnm": "CNSRCLR", "seq": seq})

    def _registered_item(self, key: tuple[str, str | None]) -> str:
        code, suffix = key
        return code + (self.real_suffix if suffix is None else suffix)

    async def register_real(self, code: str, suffix: str = None):
        await self.register_real_many([code], suffix)

    async def register_real_many(self, codes: list[str], suffix: str = None):
        """여러 종목을 REG 1건으로 등록(연사하면 서버 105110 거부, 2026-07-07 실측).
        참조수 관리: 여러 창이 같은 종목을 등록하면 카운트만 올리고 REG는 1회."""
        todo = []
        for c in codes:  # 중복 포함 리스트 허용: 발생 횟수만큼 참조수 증가
            key = (c, suffix)
            if self._reg_codes.get(key, 0) == 0 and key not in todo:
                if len(self._reg_codes) + len(todo) >= config.REAL_REG_LIMIT:
                    log.warning("real-time reg limit %d, skip %s", config.REAL_REG_LIMIT, c)
                    continue
                todo.append(key)
            self._reg_codes[key] = self._reg_codes.get(key, 0) + 1
        if todo:
            await self._send(build_reg([self._registered_item(k) for k in todo], REAL_TYPES))

    async def remove_real(self, code: str, suffix: str = None):
        await self.remove_real_many([code], suffix)

    async def remove_real_many(self, codes: list[str], suffix: str = None):
        todo = []
        for c in codes:
            key = (c, suffix)
            if key not in self._reg_codes:
                continue
            self._reg_codes[key] -= 1
            if self._reg_codes[key] <= 0:  # 마지막 창이 뺄 때만 실제 REMOVE
                del self._reg_codes[key]
                todo.append(key)
        if todo:
            await self._send(build_remove([self._registered_item(k) for k in todo], REAL_TYPES))

    async def set_real_suffix(self, suffix: str):
        """KRX 전용("") <-> 통합("_AL") 런타임 전환: 기존 등록 전부 갈아끼움."""
        if suffix == self.real_suffix:
            return
        default_keys = [k for k in self._reg_codes if k[1] is None]
        if default_keys:
            await self._send(build_remove([self._registered_item(k) for k in default_keys], REAL_TYPES))
        self.real_suffix = suffix
        log.info("real suffix -> %r", suffix)
        if default_keys:
            await self._send(build_reg([self._registered_item(k) for k in default_keys], REAL_TYPES))

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
        for seq in self._active_seqs:
            await self._send({"trnm": "CNSRREQ", "seq": seq,
                              "search_type": "1", "stex_tp": "K"})
        if self._reg_codes:
            await self._send(build_reg([self._registered_item(k) for k in self._reg_codes], REAL_TYPES))

    async def _send(self, msg: dict):
        if self._ws is None:
            log.warning("send while disconnected: %s", msg.get("trnm"))
            return
        try:
            await self._ws.send(json.dumps(msg))
        except websockets.exceptions.ConnectionClosed:
            # 재접속 창 사이의 send: _active_seqs/_reg_codes로 _resubscribe가 복구함
            log.warning("send on closed socket: %s", msg.get("trnm"))
            return
        log.debug("send %s", msg)

    async def _dispatch(self, msg: dict):
        trnm = msg.get("trnm")
        if trnm == "PING":
            await self._ws.send(json.dumps(msg))  # 받은 그대로 echo
            return
        log.debug("recv %s", msg)
        if trnm in ("REG", "REMOVE"):
            if str(msg.get("return_code", "0")) not in ("0", ""):
                log.warning("%s failed: %s", trnm, msg)
            return
        if trnm == "CNSRLST" and self.on_condition_list:
            self.on_condition_list(parse_condition_list(msg.get("data", [])))
        elif trnm == "CNSRREQ":
            self._handle_condition(msg)
        elif trnm == "REAL":
            for item in msg.get("data", []):
                self._discover_fids(item)  # 처음 본 FID 로그 (FID 발굴용)
                self._count_real(item)
                if item.get("type") == "02":  # 조건검색 실시간 편입/이탈
                    self._on_real_condition(item)
                    continue
                if item.get("type") == "1h":
                    self._on_vi(item)
                    continue
                code, fields = parse_real_item(item)
                if code and fields and self.on_real:
                    raw_item = item.get("item") or ""
                    fields["_real_suffix"] = ("_NX" if raw_item.endswith("_NX") else
                                              "_AL" if raw_item.endswith("_AL") else "")
                    self.on_real(code, fields)

    def _count_real(self, item: dict):
        """REAL 수신 빈도 5초 단위 로그. 예상값 갱신이 영웅문보다 느린 게 서버 송신
        간격 탓인지 확인용: 'exp:종목코드' 카운트 = 그 종목의 예상값 수신 횟수.
        ponytail: 원인 확정되면 이 메서드와 호출부 삭제."""
        typ = item.get("type", "?")
        self._real_stats[typ] = self._real_stats.get(typ, 0) + 1
        v = item.get("values", {})
        if typ == "0H" or "23" in v:
            k = ("expH:" if typ == "0H" else "expD:") + item.get("item", "?")
            self._real_stats[k] = self._real_stats.get(k, 0) + 1
            t = v.get("21", "")  # 호가시간(HHMMSS) 대비 수신 지연(초) = 피드 지연 실측
            if len(t) == 6 and t.isdigit():
                lt = time.localtime()
                lag = (lt.tm_hour * 3600 + lt.tm_min * 60 + lt.tm_sec
                       - int(t[:2]) * 3600 - int(t[2:4]) * 60 - int(t[4:]))
                if 0 <= lag < 3600:
                    self._real_stats["lag_max"] = max(self._real_stats.get("lag_max", 0), lag)
                    self._real_stats["lag_min"] = min(self._real_stats.get("lag_min", 9999), lag)
        now = time.monotonic()
        if not self._stats_t:
            self._stats_t = now
        elif now - self._stats_t >= 5:
            log.info("REAL recv/%.0fs: %s", now - self._stats_t, self._real_stats)
            self._real_stats = {}
            self._stats_t = now

    def _discover_fids(self, item: dict):
        """처음 보는 (type,fid)를 한 번씩 로그. 매핑여부+샘플값 포함.
        개장 동시호가(08:50~) 때 bot.log 열면 예상체결가 등 UNMAPPED FID를 바로 찾는다."""
        typ = item.get("type", "")
        table = FID_0H if typ == "0H" else FID
        for fid, val in item.get("values", {}).items():
            key = (typ, fid)
            if key in self._seen_fids:
                continue
            self._seen_fids.add(key)
            log.info("FID discover: type=%s fid=%s -> %s  sample=%r",
                     typ, fid, table.get(fid, "UNMAPPED"), val)

    def _on_real_condition(self, item: dict):
        """REAL type=02 (2026-07-07 실수신으로 확정): values에
        841=조건seq, 9001=종목코드, 843='I'(편입)/'D'(이탈), 20=시각."""
        v = item.get("values", {})
        seq = str(v.get("841"))
        if seq not in self._active_seqs:
            return  # 등록 안 한 조건식 이벤트 무시
        code = (v.get("9001") or "").lstrip("A")
        if code and self.on_condition_event:
            self.on_condition_event(seq, code, v.get("843") == "I")

    def _on_vi(self, item: dict):
        # 1h fid: 9001=코드(_AL 접미사), 9068=1 발동/2 해제, 1221=발동가격 (2026-07-07 실수신 확정)
        # 의심: 07-09 007390 해제가 하루종일 미처리(expOFF zero 0건). 9068이 발동/해제가 아니라
        # 정적/동적 구분(1225와 동일)일 가능성 -> 우리 종목 raw 전체를 남겨 다음 VI에서 확정
        v = item.get("values", {})
        code = (v.get("9001") or "").split("_")[0].lstrip("A")
        if any(c == code for c, _ in self._reg_codes):
            log.info("VI raw %s: %s", code, v)
        if code and self.on_vi:
            self.on_vi(code, v.get("9068") == "1", int(abs(_num(v.get("1221")))))

    def _handle_condition(self, msg: dict):
        """CNSRREQ 응답 = 현재 편입 전체 스냅샷. 통째로 넘겨 diff는 main이 한다
        (행 삭제/재생성 없이 편입/이탈만 반영 -> 예상값 등 실시간 상태 유지).
        실시간 편입/이탈은 REAL type=02(_on_real_condition)로 따로 옴."""
        if not self.on_condition_snapshot:
            return
        seq = str(msg.get("seq", ""))
        if not seq and len(self._active_seqs) == 1:  # 응답에 seq 없으면 단일 등록으로 판정
            seq = next(iter(self._active_seqs))
        codes = []
        for item in msg.get("data") or []:
            code = (item.get("9001") or item.get("jmcode") or item.get("item") or "").lstrip("A")
            if code:
                codes.append(code)
        self.on_condition_snapshot(seq, codes)


def _demo():
    """소켓 없이 순수 로직 자가검증."""
    # REG/REMOVE 빌드
    assert build_reg(["005930"], ["0B"])["data"][0]["item"] == ["005930"]
    assert build_remove(["005930"], ["0B"])["trnm"] == "REMOVE"
    # REAL 파싱: 부호/콤마 정규화 + price abs
    code, f = parse_real_item({"item": "005930", "values": {
        "10": "-4,620", "12": "+29.96", "13": "19687", "15": "-125",
    }})
    assert code == "005930", code
    assert f["price"] == 4620 and f["rate"] == 29.96 and f["vol"] == 19687, f
    assert f["tick_qty"] == -125, f
    _, fe = parse_real_item({"item": "x", "type": "0D", "values": {"23": "+1215", "24": "8,151"}})
    assert fe == {"exp_price": 1215, "exp_qty": 8151}, fe
    # 0H(주식예상체결)는 같은 FID가 다른 의미: 10=예상체결가, 13=예상체결량
    _, fh = parse_real_item({"item": "x", "type": "0H", "values": {"10": "-1215", "13": "8151", "12": "+21.02"}})
    assert fh == {"exp_price": 1215, "exp_qty": 8151, "exp_hot": 1}, fh
    # 매핑 없는 FID 무시
    _, f2 = parse_real_item({"item": "x", "values": {"9999": "1"}})
    assert f2 == {}, f2
    # 실시간 편입/이탈 (REAL type=02): seq 라우팅 (창=조건 여러 개)
    c = WSClient()
    got = []
    c.on_condition_event = lambda seq, code, ins: got.append((seq, code, ins))
    c._active_seqs = {"2", "3"}
    c._on_real_condition({"type": "02", "values": {"841": "2", "9001": "A294140", "843": "I", "20": "090010"}})
    c._on_real_condition({"type": "02", "values": {"841": "3", "9001": "011230", "843": "D", "20": "090430"}})
    c._on_real_condition({"type": "02", "values": {"841": "9", "9001": "005930", "843": "I"}})  # 미등록 조건 무시
    assert got == [("2", "294140", True), ("3", "011230", False)], got
    # VI 발동/해제 (1h)
    vi = []
    c.on_vi = lambda code, active, price: vi.append((code, active, price))
    c._on_vi({"type": "1h", "values": {"9001": "109610_AL", "9068": "1", "1221": "2165"}})
    c._on_vi({"type": "1h", "values": {"9001": "760006_AL", "9068": "2", "1221": "8260"}})
    assert vi == [("109610", True, 2165), ("760006", False, 8260)], vi
    # CNSRREQ 스냅샷 -> (seq, 코드 리스트)
    snap = []
    c.on_condition_snapshot = lambda seq, codes: snap.append((seq, codes))
    c._handle_condition({"trnm": "CNSRREQ", "seq": "2",
                         "data": [{"9001": "A005930"}, {"jmcode": "002995"}]})
    assert snap == [("2", ["005930", "002995"])], snap
    c._active_seqs = {"7"}  # 응답에 seq 없고 단일 등록이면 그 seq로 판정
    c._handle_condition({"trnm": "CNSRREQ", "data": [{"9001": "011230"}]})
    c._handle_condition({"trnm": "CNSRREQ", "data": None})
    assert snap[-2:] == [("7", ["011230"]), ("7", [])], snap
    # CNSRLST 파싱
    assert parse_condition_list([["0", "상한근접"], ["1", "급등주"]]) == [("0", "상한근접"), ("1", "급등주")]
    # REG 묶음 전송 + 참조수: 두 창이 같은 종목이면 REG 1회, 마지막 창이 뺄 때만 REMOVE
    sent = []
    c2 = WSClient()

    async def _fake(m):
        sent.append(m)
    c2._send = _fake
    asyncio.run(c2.register_real_many(["1", "2"]))
    asyncio.run(c2.register_real_many(["2", "3"]))   # 2는 참조수만 2로
    asyncio.run(c2.remove_real_many(["1", "9"]))
    asyncio.run(c2.remove_real_many(["2"]))          # 참조수 2->1, REMOVE 안 나감
    assert sent[0]["data"][0]["item"] == ["1", "2"], sent
    assert sent[1]["data"][0]["item"] == ["3"], sent
    assert sent[2]["trnm"] == "REMOVE" and sent[2]["data"][0]["item"] == ["1"], sent
    assert len(sent) == 3, sent
    assert c2._reg_codes == {("2", None): 1, ("3", None): 1}, c2._reg_codes
    asyncio.run(c2.remove_real_many(["2"]))          # 참조수 0 -> 이제 REMOVE
    assert sent[3]["data"][0]["item"] == ["2"], sent
    # 통합(_AL) 접미사: REG/REMOVE에만 붙고 _reg_codes는 순수코드 유지, 수신은 접미사 떼서 매칭
    asyncio.run(c2.register_real_many(["4"]))        # 잔여 등록 = {3, 4}
    asyncio.run(c2.set_real_suffix("_AL"))           # 전환: REMOVE(구) + REG(_AL)
    assert sent[5]["trnm"] == "REMOVE" and sent[5]["data"][0]["item"] == ["3", "4"], sent
    assert sent[6]["trnm"] == "REG" and sent[6]["data"][0]["item"] == ["3_AL", "4_AL"], sent
    asyncio.run(c2.register_real_many(["5"]))
    assert sent[7]["data"][0]["item"] == ["5_AL"], sent
    assert c2._reg_codes == {("3", None): 1, ("4", None): 1, ("5", None): 1}, c2._reg_codes
    asyncio.run(c2.remove_real_many(["5"]))
    assert sent[8]["trnm"] == "REMOVE" and sent[8]["data"][0]["item"] == ["5_AL"], sent
    # NXT 명시 등록은 전역 KRX/통합 전환과 분리된다.
    asyncio.run(c2.register_real_many(["6"], "_NX"))
    assert sent[9]["data"][0]["item"] == ["6_NX"], sent
    asyncio.run(c2.set_real_suffix(""))
    assert sent[10]["data"][0]["item"] == ["3_AL", "4_AL"], sent
    assert sent[11]["data"][0]["item"] == ["3", "4"], sent
    assert ("6", "_NX") in c2._reg_codes
    code, f = parse_real_item({"item": "005930_AL", "values": {"10": "-4620"}})
    assert code == "005930" and f["price"] == 4620, (code, f)
    print("ws self-check OK")


if __name__ == "__main__":
    _demo()
