# -*- coding: utf-8 -*-
"""AI 대화만 저장한다. 시세 DB와 주문 설정은 변경하지 않는다."""
from __future__ import annotations

import json
import sqlite3
import uuid
from contextlib import closing
from datetime import datetime
from pathlib import Path


SESSION_DB = Path(__file__).resolve().parent / "data" / "ai_assistant.db"


def now_text() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


class SessionStore:
    def __init__(self, path: Path = SESSION_DB):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as db, db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS ai_conversations (
                    id TEXT PRIMARY KEY, title TEXT NOT NULL,
                    provider TEXT NOT NULL, model TEXT NOT NULL,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS ai_messages (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    conversation_id TEXT NOT NULL, role TEXT NOT NULL,
                    content TEXT NOT NULL, status TEXT NOT NULL,
                    context_json TEXT NOT NULL DEFAULT '{}',
                    metadata_json TEXT NOT NULL DEFAULT '{}',
                    created_at TEXT NOT NULL,
                    FOREIGN KEY(conversation_id) REFERENCES ai_conversations(id)
                        ON DELETE CASCADE
                );
            """)
            # 앱이 강제로 끝났던 작업을 다음 실행에서 진행 중으로 남기지 않는다.
            db.execute("UPDATE ai_messages SET status='interrupted' "
                       "WHERE status='running'")

    def _connect(self):
        db = sqlite3.connect(self.path, timeout=2)
        db.row_factory = sqlite3.Row
        db.execute("PRAGMA foreign_keys=ON")
        return db

    def create(self, provider: str, model: str, title: str = "새 대화") -> str:
        session_id, stamp = uuid.uuid4().hex, now_text()
        with closing(self._connect()) as db, db:
            db.execute("INSERT INTO ai_conversations VALUES(?,?,?,?,?,?)",
                       (session_id, title[:100], provider, model, stamp, stamp))
        return session_id

    def conversations(self) -> list[dict]:
        with closing(self._connect()) as db:
            return [dict(r) for r in db.execute(
                "SELECT * FROM ai_conversations ORDER BY updated_at DESC, rowid DESC")]

    def configure(self, session_id: str, provider: str, model: str):
        with closing(self._connect()) as db, db:
            db.execute("UPDATE ai_conversations SET provider=?,model=? WHERE id=?",
                       (provider, model, session_id))

    def messages(self, session_id: str, *, include_context=True, limit=60) -> list[dict]:
        context_column = "context_json" if include_context else "'{}' AS context_json"
        with closing(self._connect()) as db:
            rows = db.execute(
                f"SELECT id,conversation_id,role,content,status,{context_column},"
                "metadata_json,created_at FROM ai_messages WHERE conversation_id=? "
                "ORDER BY id DESC LIMIT ?", (session_id, max(1, min(200, int(limit)))))
            return list(reversed([dict(row) for row in rows]))

    def last_context(self, session_id: str) -> dict:
        with closing(self._connect()) as db:
            row = db.execute("SELECT context_json FROM ai_messages WHERE conversation_id=? "
                             "AND role='user' ORDER BY id DESC LIMIT 1", (session_id,)).fetchone()
            return json.loads(row[0]) if row else {}

    def add(self, session_id: str, role: str, content: str, *,
            status: str = "complete", context: dict | None = None,
            metadata: dict | None = None) -> int:
        with closing(self._connect()) as db, db:
            stamp = now_text()
            cursor = db.execute(
                "INSERT INTO ai_messages(conversation_id,role,content,status,"
                "context_json,metadata_json,created_at) VALUES(?,?,?,?,?,?,?)",
                (session_id, role, content, status,
                 json.dumps(context or {}, ensure_ascii=False),
                 json.dumps(metadata or {}, ensure_ascii=False), stamp))
            db.execute("UPDATE ai_conversations SET updated_at=? WHERE id=?",
                       (stamp, session_id))
            if role == "user":
                db.execute("UPDATE ai_conversations SET title=? "
                           "WHERE id=? AND title='새 대화'",
                           (content.replace("\n", " ")[:65], session_id))
            return int(cursor.lastrowid)

    def finish(self, message_id: int, content: str, status: str,
               metadata: dict | None = None):
        with closing(self._connect()) as db, db:
            db.execute("UPDATE ai_messages SET content=?,status=?,metadata_json=? "
                       "WHERE id=?", (content, status,
                       json.dumps(metadata or {}, ensure_ascii=False), message_id))
            db.execute("UPDATE ai_conversations SET updated_at=? WHERE id=("
                       "SELECT conversation_id FROM ai_messages WHERE id=?)",
                       (now_text(), message_id))

    def delete(self, session_id: str):
        with closing(self._connect()) as db, db:
            db.execute("DELETE FROM ai_conversations WHERE id=?", (session_id,))
