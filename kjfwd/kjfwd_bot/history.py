from __future__ import annotations

import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import List, Optional, Sequence, Tuple

from .models import ContextSnapshot, Conversation, StoredMessage, TriggerClaim


class HistoryStore:
    """仅保存机器人运行期间通过现有监听接口收到的增量消息。"""

    def __init__(self, database_path: Path, idle_timeout_seconds: int = 1800):
        self.database_path = Path(database_path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.idle_timeout_seconds = idle_timeout_seconds
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.database_path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA foreign_keys=ON")
            self._create_schema()

    def _create_schema(self) -> None:
        self._conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                group_name TEXT NOT NULL,
                role TEXT NOT NULL CHECK(role IN ('group', 'assistant')),
                content TEXT NOT NULL,
                observed_at REAL NOT NULL,
                session_id TEXT NOT NULL,
                conversation_id TEXT,
                source_key TEXT,
                created_at REAL NOT NULL DEFAULT (unixepoch())
            );
            CREATE UNIQUE INDEX IF NOT EXISTS uq_messages_source
                ON messages(group_name, source_key) WHERE source_key IS NOT NULL;
            CREATE INDEX IF NOT EXISTS ix_messages_session
                ON messages(session_id, id);
            CREATE TABLE IF NOT EXISTS conversations (
                id TEXT PRIMARY KEY,
                group_name TEXT NOT NULL,
                title TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'active',
                created_at REAL NOT NULL,
                updated_at REAL NOT NULL,
                last_trigger_at REAL,
                message_count INTEGER NOT NULL DEFAULT 0
            );
            CREATE INDEX IF NOT EXISTS ix_conversations_group_updated
                ON conversations(group_name, status, updated_at);

            CREATE TABLE IF NOT EXISTS triggers (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                trigger_key TEXT NOT NULL UNIQUE,
                fingerprint TEXT NOT NULL,
                group_name TEXT NOT NULL,
                message_id INTEGER NOT NULL REFERENCES messages(id),
                requested_at REAL NOT NULL,
                sent INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'pending',
                error TEXT,
                reply_message_id INTEGER REFERENCES messages(id),
                updated_at REAL NOT NULL
            );
            CREATE INDEX IF NOT EXISTS ix_triggers_group_time
                ON triggers(group_name, requested_at);
            """
        )
        self._ensure_column("messages", "conversation_id", "TEXT")
        self._conn.execute(
            "CREATE INDEX IF NOT EXISTS ix_messages_conversation ON messages(conversation_id, id)"
        )
        self._conn.commit()

    def _ensure_column(self, table: str, column: str, declaration: str) -> None:
        rows = self._conn.execute(f"PRAGMA table_info({table})").fetchall()
        if column not in {str(row["name"]) for row in rows}:
            self._conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {declaration}")

    def record_group_message(
        self,
        group_name: str,
        content: str,
        observed_at: float,
        source_key: Optional[str] = None,
    ) -> Tuple[StoredMessage, bool]:
        with self._lock:
            if source_key:
                existing = self._conn.execute(
                    "SELECT * FROM messages WHERE group_name=? AND source_key=?",
                    (group_name, source_key),
                ).fetchone()
                if existing:
                    return self._row_to_message(existing), False

            previous = self._conn.execute(
                "SELECT session_id, observed_at FROM messages WHERE group_name=? "
                "ORDER BY observed_at DESC, id DESC LIMIT 1",
                (group_name,),
            ).fetchone()
            if previous and observed_at - float(previous["observed_at"]) <= self.idle_timeout_seconds:
                session_id = str(previous["session_id"])
            else:
                session_id = uuid.uuid4().hex
            try:
                cursor = self._conn.execute(
                    "INSERT INTO messages(group_name, role, content, observed_at, session_id, source_key) "
                    "VALUES (?, 'group', ?, ?, ?, ?)",
                    (group_name, content, observed_at, session_id, source_key),
                )
                self._conn.commit()
            except sqlite3.IntegrityError:
                existing = self._conn.execute(
                    "SELECT * FROM messages WHERE group_name=? AND source_key=?",
                    (group_name, source_key),
                ).fetchone()
                if not existing:
                    raise
                return self._row_to_message(existing), False
            row = self._conn.execute("SELECT * FROM messages WHERE id=?", (cursor.lastrowid,)).fetchone()
            return self._row_to_message(row), True

    def record_assistant_message(
        self,
        group_name: str,
        session_id: str,
        content: str,
        observed_at: Optional[float] = None,
        conversation_id: Optional[str] = None,
    ) -> StoredMessage:
        observed_at = time.time() if observed_at is None else observed_at
        with self._lock:
            cursor = self._conn.execute(
                "INSERT INTO messages(group_name, role, content, observed_at, session_id, conversation_id) "
                "VALUES (?, 'assistant', ?, ?, ?, ?)",
                (group_name, content, observed_at, session_id, conversation_id),
            )
            if conversation_id:
                self._touch_conversation(conversation_id, observed_at, increment=True)
            self._conn.commit()
            row = self._conn.execute("SELECT * FROM messages WHERE id=?", (cursor.lastrowid,)).fetchone()
            return self._row_to_message(row)

    def start_new_session(self, message_id: int) -> StoredMessage:
        """把指定消息设为新会话的第一条消息。"""
        session_id = uuid.uuid4().hex
        with self._lock:
            self._conn.execute(
                "UPDATE messages SET session_id=? WHERE id=?",
                (session_id, message_id),
            )
            self._conn.commit()
            row = self._conn.execute("SELECT * FROM messages WHERE id=?", (message_id,)).fetchone()
            if row is None:
                raise KeyError(message_id)
            return self._row_to_message(row)

    def create_conversation(
        self,
        group_name: str,
        *,
        title: str,
        now: Optional[float] = None,
        status: str = "active",
    ) -> Conversation:
        observed_at = time.time() if now is None else now
        conversation_id = uuid.uuid4().hex
        title = (str(title or "").strip() or "新会话")[:80]
        with self._lock:
            self._conn.execute(
                "INSERT INTO conversations(id, group_name, title, status, created_at, updated_at, last_trigger_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?)",
                (conversation_id, group_name, title, status, observed_at, observed_at, observed_at),
            )
            self._conn.commit()
            return self.get_conversation(conversation_id)

    def get_conversation(self, conversation_id: str) -> Conversation:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM conversations WHERE id=?", (conversation_id,)
            ).fetchone()
            if row is None:
                raise KeyError(conversation_id)
            return self._row_to_conversation(row)

    def list_active_conversations(
        self,
        group_name: str,
        *,
        now: Optional[float] = None,
        ttl_seconds: int = 1800,
        limit: int = 5,
    ) -> Tuple[Conversation, ...]:
        cutoff = (time.time() if now is None else now) - ttl_seconds
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM conversations WHERE group_name=? AND status='active' AND updated_at>=? "
                "ORDER BY updated_at DESC LIMIT ?",
                (group_name, cutoff, limit),
            ).fetchall()
        return tuple(self._row_to_conversation(row) for row in rows)

    def conversation_recent_messages(
        self, conversation_id: str, *, limit: int = 6
    ) -> Tuple[StoredMessage, ...]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM messages WHERE conversation_id=? ORDER BY id DESC LIMIT ?",
                (conversation_id, limit),
            ).fetchall()
        messages = [self._row_to_message(row) for row in rows]
        messages.reverse()
        return tuple(messages)

    def bind_message_to_conversation(
        self,
        message_id: int,
        conversation_id: str,
        *,
        trigger_at: Optional[float] = None,
    ) -> StoredMessage:
        with self._lock:
            row = self._conn.execute("SELECT * FROM messages WHERE id=?", (message_id,)).fetchone()
            if row is None:
                raise KeyError(message_id)
            self._conn.execute(
                "UPDATE messages SET conversation_id=? WHERE id=?",
                (conversation_id, message_id),
            )
            observed_at = float(row["observed_at"])
            self._touch_conversation(
                conversation_id,
                trigger_at if trigger_at is not None else observed_at,
                increment=row["conversation_id"] != conversation_id,
                last_trigger_at=trigger_at if trigger_at is not None else observed_at,
            )
            self._conn.commit()
            updated = self._conn.execute("SELECT * FROM messages WHERE id=?", (message_id,)).fetchone()
            return self._row_to_message(updated)

    def claim_trigger(
        self,
        trigger_key: str,
        fingerprint: str,
        group_name: str,
        message_id: int,
        requested_at: float,
        dedupe_seconds: float,
    ) -> TriggerClaim:
        with self._lock:
            existing = self._conn.execute(
                "SELECT id, sent, status FROM triggers WHERE trigger_key=? OR "
                "(group_name=? AND fingerprint=? AND ABS(requested_at-?)<=?) "
                "ORDER BY id LIMIT 1",
                (trigger_key, group_name, fingerprint, requested_at, dedupe_seconds),
            ).fetchone()
            if existing:
                return TriggerClaim(
                    False, int(existing["id"]), bool(existing["sent"]), str(existing["status"])
                )
            try:
                cursor = self._conn.execute(
                    "INSERT INTO triggers(trigger_key, fingerprint, group_name, message_id, requested_at, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (trigger_key, fingerprint, group_name, message_id, requested_at, requested_at),
                )
                self._conn.commit()
                return TriggerClaim(True, int(cursor.lastrowid), False, "pending")
            except sqlite3.IntegrityError:
                row = self._conn.execute(
                    "SELECT id, sent, status FROM triggers WHERE trigger_key=?", (trigger_key,)
                ).fetchone()
                return TriggerClaim(False, int(row["id"]), bool(row["sent"]), str(row["status"]))

    def mark_trigger_sent(self, trigger_id: int, reply_message_id: int) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE triggers SET sent=1, status='sent', reply_message_id=?, error=NULL, updated_at=? "
                "WHERE id=?",
                (reply_message_id, time.time(), trigger_id),
            )
            self._conn.commit()

    def mark_trigger_failed(self, trigger_id: int, error: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE triggers SET status='failed', error=?, updated_at=? WHERE id=?",
                (str(error)[:1000], time.time(), trigger_id),
            )
            self._conn.commit()

    def get_trigger(self, trigger_id: int) -> sqlite3.Row:
        with self._lock:
            row = self._conn.execute("SELECT * FROM triggers WHERE id=?", (trigger_id,)).fetchone()
            if row is None:
                raise KeyError(trigger_id)
            return row

    def snapshot(
        self,
        trigger_message: StoredMessage,
        *,
        max_messages: int,
        max_characters: int,
    ) -> ContextSnapshot:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM messages WHERE session_id=? AND id<=? "
                "ORDER BY id DESC LIMIT ?",
                (trigger_message.session_id, trigger_message.id, max_messages),
            ).fetchall()
        newest_first = [self._row_to_message(row) for row in rows]
        selected: List[StoredMessage] = []
        used = 0
        for message in newest_first:
            cost = len(message.content)
            if selected and used + cost > max_characters:
                break
            selected.append(message)
            used += cost
        selected.reverse()
        return ContextSnapshot(
            group_name=trigger_message.group_name,
            session_id=trigger_message.session_id,
            trigger_message_id=trigger_message.id,
            messages=tuple(selected),
        )

    def conversation_snapshot(
        self,
        trigger_message: StoredMessage,
        conversation_id: str,
        *,
        max_messages: int,
        max_characters: int,
    ) -> ContextSnapshot:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM messages WHERE conversation_id=? AND id<=? "
                "ORDER BY id DESC LIMIT ?",
                (conversation_id, trigger_message.id, max_messages),
            ).fetchall()
        return self._snapshot_from_rows(trigger_message, rows, max_characters, conversation_id)

    def ambiguous_snapshot(
        self,
        trigger_message: StoredMessage,
        *,
        conversation_id: Optional[str],
        global_seconds: int,
        global_max_messages: int,
        max_characters: int,
    ) -> ContextSnapshot:
        cutoff = trigger_message.observed_at - global_seconds
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM messages WHERE group_name=? AND observed_at>=? AND id<=? "
                "ORDER BY id DESC LIMIT ?",
                (trigger_message.group_name, cutoff, trigger_message.id, global_max_messages),
            ).fetchall()
        global_messages = self._select_rows_with_budget(rows, max_characters)
        return ContextSnapshot(
            group_name=trigger_message.group_name,
            session_id=trigger_message.session_id,
            trigger_message_id=trigger_message.id,
            messages=(),
            conversation_id=conversation_id,
            global_messages=tuple(global_messages),
            ambiguous=True,
        )

    def _snapshot_from_rows(
        self,
        trigger_message: StoredMessage,
        rows: Sequence[sqlite3.Row],
        max_characters: int,
        conversation_id: Optional[str],
    ) -> ContextSnapshot:
        return ContextSnapshot(
            group_name=trigger_message.group_name,
            session_id=trigger_message.session_id,
            trigger_message_id=trigger_message.id,
            messages=tuple(self._select_rows_with_budget(rows, max_characters)),
            conversation_id=conversation_id,
        )

    def _select_rows_with_budget(
        self, rows: Sequence[sqlite3.Row], max_characters: int
    ) -> List[StoredMessage]:
        newest_first = [self._row_to_message(row) for row in rows]
        selected: List[StoredMessage] = []
        used = 0
        for message in newest_first:
            cost = len(message.content)
            if selected and used + cost > max_characters:
                break
            selected.append(message)
            used += cost
        selected.reverse()
        return selected

    def _touch_conversation(
        self,
        conversation_id: str,
        observed_at: float,
        *,
        increment: bool,
        last_trigger_at: Optional[float] = None,
    ) -> None:
        if increment:
            self._conn.execute(
                "UPDATE conversations SET updated_at=?, last_trigger_at=COALESCE(?, last_trigger_at), "
                "message_count=message_count+1 WHERE id=?",
                (observed_at, last_trigger_at, conversation_id),
            )
        else:
            self._conn.execute(
                "UPDATE conversations SET updated_at=?, last_trigger_at=COALESCE(?, last_trigger_at) "
                "WHERE id=?",
                (observed_at, last_trigger_at, conversation_id),
            )

    def prune(self, retention_days: int, now: Optional[float] = None) -> int:
        cutoff = (time.time() if now is None else now) - retention_days * 86400
        with self._lock:
            cursor = self._conn.execute(
                "DELETE FROM messages WHERE observed_at<? AND id NOT IN "
                "(SELECT message_id FROM triggers UNION SELECT reply_message_id FROM triggers "
                "WHERE reply_message_id IS NOT NULL)",
                (cutoff,),
            )
            self._conn.commit()
            return int(cursor.rowcount)

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    @staticmethod
    def _row_to_message(row: sqlite3.Row) -> StoredMessage:
        return StoredMessage(
            id=int(row["id"]),
            group_name=str(row["group_name"]),
            role=str(row["role"]),
            content=str(row["content"]),
            observed_at=float(row["observed_at"]),
            session_id=str(row["session_id"]),
            source_key=row["source_key"],
            conversation_id=row["conversation_id"],
        )

    @staticmethod
    def _row_to_conversation(row: sqlite3.Row) -> Conversation:
        return Conversation(
            id=str(row["id"]),
            group_name=str(row["group_name"]),
            title=str(row["title"]),
            status=str(row["status"]),
            created_at=float(row["created_at"]),
            updated_at=float(row["updated_at"]),
            last_trigger_at=None if row["last_trigger_at"] is None else float(row["last_trigger_at"]),
            message_count=int(row["message_count"]),
        )
