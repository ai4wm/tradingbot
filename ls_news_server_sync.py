# -*- coding: utf-8 -*-
"""우분투 24시간 수집 DB에서 로컬에 없는 LS 뉴스 제목을 증분 동기화한다.

서버의 ``news.id``를 커서로 사용하고, 시작 시점의 ``MAX(id)``까지만
오름차순으로 스트리밍한다. 본문은 전송하지 않으며 로컬 DB는 500건 단위로
커밋해 UI와 실시간 웹소켓 저장을 오래 막지 않는다.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import shutil
import subprocess
from pathlib import Path
from typing import Callable

import config
from analysis_db import (
    DB_PATH, ls_news_server_cursor, merge_ls_news_server_rows,
)
from ls_news_ws import source_label

log = logging.getLogger("ls_news_server_sync")

ProgressCallback = Callable[[dict], None]
_SYNC_LOCK = asyncio.Lock()


_REMOTE_READER = r'''# -*- coding: utf-8 -*-
import json
import sqlite3
import sys

db_path = sys.argv[1]
after_id = max(0, int(sys.argv[2]))
connection = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
connection.row_factory = sqlite3.Row
connection.execute("PRAGMA query_only=ON")
upper_id = int(connection.execute(
    "SELECT COALESCE(MAX(id), 0) FROM news"
).fetchone()[0])
print(json.dumps({"type": "meta", "upper_id": upper_id}), flush=True)

rows = connection.execute(
    """SELECT id, title, published_at, raw_payload, created_at
         FROM news
        WHERE id>? AND id<=?
        ORDER BY id""",
    (after_id, upper_id),
)
for row in rows:
    try:
        payload = json.loads(row["raw_payload"] or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        payload = {}
    body = payload.get("body") if isinstance(payload, dict) else {}
    if not isinstance(body, dict):
        body = {}
    item = {
        "type": "news",
        "server_id": int(row["id"]),
        "realkey": str(body.get("realkey") or "").strip(),
        "date": str(body.get("date") or "").strip(),
        "time": str(body.get("time") or "").strip(),
        "source_id": str(body.get("id") or "").strip(),
        "title": " ".join(str(
            body.get("title") or row["title"] or "").split()),
        "code": str(body.get("code") or "").strip(),
        "body_size": str(body.get("bodysize") or "0").strip(),
        "published_at": str(row["published_at"] or "").strip(),
        "received_at": str(row["created_at"] or "").strip(),
    }
    print(json.dumps(item, ensure_ascii=False, separators=(",", ":")),
          flush=True)
connection.close()
'''


class LSNewsServerSync:
    """고정된 SSH 대상에서 뉴스 누락분을 스트리밍해 로컬 DB에 병합한다."""

    def __init__(self, db_path: Path = DB_PATH):
        self.db_path = Path(db_path)
        self.host = config.LS_NEWS_SYNC_SSH_HOST
        self.remote_db = config.LS_NEWS_SYNC_DB_PATH
        self.batch_size = max(100, int(config.LS_NEWS_SYNC_BATCH_SIZE))

    @property
    def source(self) -> str:
        return f"{self.host}:{self.remote_db}"

    @property
    def configured(self) -> bool:
        return bool(
            config.LS_NEWS_SYNC_ENABLED and self.host and self.remote_db)

    async def sync(
            self, on_progress: ProgressCallback | None = None) -> dict:
        """마지막 커서 다음 행부터 시작 시점 상한까지 한 번 동기화한다."""
        if not self.configured:
            return {
                "status": "disabled", "upper_id": 0, "cursor": 0,
                "processed": 0, "inserted": 0, "updated": 0,
            }
        async with _SYNC_LOCK:
            return await self._sync_locked(on_progress)

    async def _sync_locked(
            self, on_progress: ProgressCallback | None) -> dict:
        ssh = shutil.which("ssh")
        if not ssh:
            raise RuntimeError("Windows OpenSSH의 ssh 명령을 찾지 못했습니다.")
        cursor = await asyncio.to_thread(
            ls_news_server_cursor, self.db_path)
        creationflags = (
            subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        process = await asyncio.create_subprocess_exec(
            ssh,
            "-T",
            "-o", "BatchMode=yes",
            "-o", "ConnectTimeout=10",
            self.host,
            "python3", "-", self.remote_db, str(cursor),
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            creationflags=creationflags,
        )
        assert process.stdin is not None
        assert process.stdout is not None
        assert process.stderr is not None
        process.stdin.write(_REMOTE_READER.encode("utf-8"))
        try:
            await process.stdin.drain()
        finally:
            process.stdin.close()

        totals = {
            "status": "running",
            "upper_id": cursor,
            "cursor": cursor,
            "processed": 0,
            "inserted": 0,
            "updated": 0,
        }
        batch: list[dict] = []
        meta_received = False

        async def commit_batch():
            nonlocal batch
            if not batch:
                return
            result = await asyncio.to_thread(
                merge_ls_news_server_rows,
                batch,
                self.source,
                self.db_path,
            )
            totals["processed"] += int(result["processed"])
            totals["inserted"] += int(result["inserted"])
            totals["updated"] += int(result["updated"])
            totals["cursor"] = int(result["cursor"])
            batch = []
            if on_progress is not None:
                on_progress(dict(totals))
            await asyncio.sleep(0)

        try:
            async for raw_line in process.stdout:
                line = raw_line.decode("utf-8", errors="strict").strip()
                if not line:
                    continue
                item = json.loads(line)
                if item.get("type") == "meta":
                    totals["upper_id"] = max(
                        cursor, int(item.get("upper_id") or 0))
                    meta_received = True
                    if on_progress is not None:
                        on_progress(dict(totals))
                    continue
                if item.get("type") != "news":
                    continue
                item.pop("type", None)
                source_name = source_label(item.get("source_id"))
                # 미확인 코드의 임시 표시명으로 이미 확인된 DB 매체명을 덮지 않는다.
                item["source_name"] = (
                    "" if source_name == "출처 미상"
                    or source_name.startswith("매체 ") else source_name
                )
                batch.append(item)
                if len(batch) >= self.batch_size:
                    await commit_batch()
            await commit_batch()
            return_code = await process.wait()
            stderr = (await process.stderr.read()).decode(
                "utf-8", errors="replace").strip()
            if return_code != 0:
                raise RuntimeError(
                    f"서버 뉴스 SSH 조회 실패({return_code}): "
                    f"{stderr or '상세 오류 없음'}")
            if not meta_received:
                raise RuntimeError("서버 뉴스 조회가 상한 ID를 반환하지 않았습니다.")
        except asyncio.CancelledError:
            if process.returncode is None:
                process.kill()
                await process.wait()
            raise
        except Exception:
            if process.returncode is None:
                process.kill()
                await process.wait()
            raise

        totals["status"] = "completed"
        log.info(
            "LS server gap sync complete: source=%s upper=%d cursor=%d "
            "processed=%d inserted=%d updated=%d",
            self.source, totals["upper_id"], totals["cursor"],
            totals["processed"], totals["inserted"], totals["updated"],
        )
        return totals
