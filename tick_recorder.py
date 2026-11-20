# -*- coding: utf-8 -*-
"""웹소켓 실시간 틱(체결 0B·호가 0D·예상체결 0H·프로그램매매 0w)을 종목별 파일로 남긴다.
조건검색 편입/이탈(02)은 종목이 아니라 창 이벤트라 `_condition.jsonl` 한 파일에 모은다.

앱이 이미 받아 화면과 3단매도에 쓰고 버리던 데이터를 복사해 둘 뿐이다. 키움에
추가 요청은 없다. AI와 사람이 사후에 분봉·잔량 변화를 재구성하는 원본이다.

**수신 스레드를 막지 않는다.** `put`은 값을 복사해 대기열에 넣기만 한다. 파일은
별도 스레드가 쓴다. 대기열은 2만 건(최악 약 100MB)이고 가득 차면 기록을 버린다 — 점상 붕괴 순간 틱이
몰릴 때 저장이 느려져도 3단매도 판정이 밀리면 안 된다. 기록 오류도 삼킨다.

저장: `data/ticks/YYYYMMDD/<종목코드[접미사]>.jsonl`, 한 줄 한 틱.
`{"t": "17:52:03.412", "ty": "0D", "v": {원본 FID 전부}}`
접미사(`""` KRX, `_NX`, `_AL` 통합)는 파일 이름에 있다. FID는 골라 담지 않는다
(VI 기록 때 9068의 뜻을 몰랐던 일과 같다).
"""
from __future__ import annotations

import json
import logging
import os
import queue
import threading
import time

log = logging.getLogger(__name__)

# 체결·호가·예상체결·프로그램매매, 그리고 조건검색 편입/이탈(02, 종목이 아니라 창 전체의 이벤트).
TICK_TYPES = frozenset(("0B", "0D", "0H", "0w", "02"))
CONDITION_FILE = "_condition"
# 실행 폴더와 상관없이 프로젝트의 data/ticks. 메뉴·조회 스크립트가 같은 경로를 쓴다.
TICK_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "ticks")


class TickRecorder:
    def __init__(self, root: str = TICK_DIR, maxsize: int = 20_000,
                 flush_seconds: float = 1.0):
        self.root = root
        self.flush_seconds = flush_seconds
        self._queue: queue.Queue = queue.Queue(maxsize=maxsize)
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()
        self.enabled = True   # 끄면 put이 바로 돌아온다(체크박스가 바꾼다)
        self.written = 0
        self.dropped = 0
        self._last_drop_log = 0
        self._write_failed = False

    # --- 수신 스레드가 부르는 쪽 -------------------------------------------
    def put(self, item: dict) -> None:
        """틱 하나를 대기열에 넣는다. 절대 막지 않고 예외도 밖으로 내지 않는다."""
        if not self.enabled:
            return
        try:
            kind = item.get("type")
            if kind not in TICK_TYPES:
                return
            name = CONDITION_FILE if kind == "02" else str(item.get("item") or "")
            row = (time.time(), kind, name, dict(item.get("values") or {}))
            self._ensure_started()
            self._queue.put_nowait(row)
        except queue.Full:
            self.dropped += 1
        except Exception:  # noqa: BLE001 - 곁다리 기록이 수신을 깨면 안 된다
            self.dropped += 1

    def _ensure_started(self) -> None:
        if self._thread is not None:
            return
        with self._lock:
            if self._thread is None:
                self._thread = threading.Thread(
                    target=self._run, name="tick-recorder", daemon=True)
                self._thread.start()

    # --- 쓰는 스레드 -------------------------------------------------------
    def _run(self) -> None:
        while not self._stop.is_set():
            self._stop.wait(self.flush_seconds)
            self._flush()
        self._flush()

    def _flush(self) -> None:
        batch: dict[str, list[str]] = {}
        while True:
            try:
                stamp, kind, item, values = self._queue.get_nowait()
            except queue.Empty:
                break
            day = time.strftime("%Y%m%d", time.localtime(stamp))
            clock = time.strftime("%H:%M:%S", time.localtime(stamp))
            line = json.dumps({"t": f"{clock}.{int(stamp * 1000) % 1000:03d}",
                               "ty": kind, "v": values}, ensure_ascii=False)
            batch.setdefault(os.path.join(day, f"{item or 'unknown'}.jsonl"), []).append(line)
        for name, lines in batch.items():
            path = os.path.join(self.root, name)
            try:
                os.makedirs(os.path.dirname(path), exist_ok=True)
                with open(path, "a", encoding="utf-8") as file:
                    file.write("\n".join(lines) + "\n")
                self.written += len(lines)
            except Exception as error:  # noqa: BLE001
                self.dropped += len(lines)
                if not self._write_failed:
                    self._write_failed = True  # 틱마다 같은 경고를 쏟지 않는다
                    log.warning("tick record write failed: %s", error)
        if self.dropped > self._last_drop_log:
            log.warning("tick record dropped=%d written=%d", self.dropped, self.written)
            self._last_drop_log = self.dropped

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5)
