"""Durable SQLite store for Discord thread governor state."""

from __future__ import annotations

import os
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

_ENV_KEY = "HIVE_THREAD_GOVERNOR_DB"
_DEFAULT_PATH = Path.home() / ".local" / "state" / "hive" / "thread-governor.db"
_SCHEMA_VERSION = 2


@dataclass(frozen=True, slots=True)
class ThreadGovernorState:
    """Durable state for a governed Discord thread."""

    thread_id: str
    parent_id: str
    count: int
    limit: int
    locked: bool
    created_at: datetime
    last_owner_at: datetime | None
    last_message_at: datetime
    locked_at: datetime | None
    unlock_at: datetime | None
    closed_reason: str | None = None
    last_bot_author_id: str | None = None


class ThreadGovernorStore:
    """SQLite-backed thread governor state store."""

    def __init__(self, *, path: Path | None = None) -> None:
        self.path = path or resolve_thread_governor_db_path()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._bootstrap()

    def close(self) -> None:
        self._conn.close()

    def __enter__(self) -> ThreadGovernorStore:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        self.close()

    def ensure_thread(
        self,
        *,
        thread_id: str,
        parent_id: str,
        limit: int,
        now: datetime | None = None,
    ) -> tuple[ThreadGovernorState, bool]:
        """Load or create thread state on first observation."""
        existing = self.get(thread_id)
        if existing is not None:
            return existing, False

        ts = _coerce_utc(now)
        state = ThreadGovernorState(
            thread_id=thread_id,
            parent_id=parent_id,
            count=0,
            limit=limit,
            locked=False,
            created_at=ts,
            last_owner_at=None,
            last_message_at=ts,
            locked_at=None,
            unlock_at=None,
            closed_reason=None,
            last_bot_author_id=None,
        )
        self.save(state)
        return state, True

    def get(self, thread_id: str) -> ThreadGovernorState | None:
        row = self._conn.execute(
            "SELECT * FROM thread_governor_state WHERE thread_id = ?",
            (thread_id,),
        ).fetchone()
        return _row_to_state(row) if row is not None else None

    def save(self, state: ThreadGovernorState) -> None:
        """Insert or replace thread state."""
        self._conn.execute(
            """
            INSERT INTO thread_governor_state (
                thread_id, parent_id, count, message_limit, locked, created_at,
                last_owner_at, last_message_at, locked_at, unlock_at, closed_reason,
                last_bot_author_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(thread_id) DO UPDATE SET
                parent_id = excluded.parent_id,
                count = excluded.count,
                message_limit = excluded.message_limit,
                locked = excluded.locked,
                created_at = excluded.created_at,
                last_owner_at = excluded.last_owner_at,
                last_message_at = excluded.last_message_at,
                locked_at = excluded.locked_at,
                unlock_at = excluded.unlock_at,
                closed_reason = excluded.closed_reason,
                last_bot_author_id = excluded.last_bot_author_id
            """,
            (
                state.thread_id,
                state.parent_id,
                state.count,
                state.limit,
                int(state.locked),
                _serialize_dt(state.created_at),
                _serialize_dt(state.last_owner_at),
                _serialize_dt(state.last_message_at),
                _serialize_dt(state.locked_at),
                _serialize_dt(state.unlock_at),
                state.closed_reason,
                state.last_bot_author_id,
            ),
        )
        self._conn.commit()

    def list_locked(self) -> list[ThreadGovernorState]:
        rows = self._conn.execute(
            """
            SELECT * FROM thread_governor_state
            WHERE locked = 1
            ORDER BY unlock_at ASC, thread_id ASC
            """
        ).fetchall()
        return [_row_to_state(row) for row in rows]

    def list_locked_due(self, now: datetime) -> list[ThreadGovernorState]:
        rows = self._conn.execute(
            """
            SELECT * FROM thread_governor_state
            WHERE locked = 1
              AND unlock_at IS NOT NULL
              AND unlock_at <= ?
            ORDER BY unlock_at ASC, thread_id ASC
            """,
            (_serialize_dt(_coerce_utc(now)),),
        ).fetchall()
        return [_row_to_state(row) for row in rows]

    def list_all(self) -> list[ThreadGovernorState]:
        rows = self._conn.execute(
            "SELECT * FROM thread_governor_state ORDER BY last_message_at DESC, thread_id ASC"
        ).fetchall()
        return [_row_to_state(row) for row in rows]

    def delete(self, thread_id: str) -> bool:
        cur = self._conn.execute(
            "DELETE FROM thread_governor_state WHERE thread_id = ?",
            (thread_id,),
        )
        self._conn.commit()
        return cur.rowcount > 0

    def _bootstrap(self) -> None:
        version = int(self._conn.execute("PRAGMA user_version").fetchone()[0])
        if version < 1:
            self._conn.executescript(
                """
                CREATE TABLE IF NOT EXISTS thread_governor_state (
                    thread_id TEXT PRIMARY KEY,
                    parent_id TEXT NOT NULL,
                    count INTEGER NOT NULL,
                    message_limit INTEGER NOT NULL,
                    locked INTEGER NOT NULL DEFAULT 0,
                    created_at TEXT NOT NULL,
                    last_owner_at TEXT,
                    last_message_at TEXT NOT NULL,
                    locked_at TEXT,
                    unlock_at TEXT,
                    closed_reason TEXT
                );

                CREATE INDEX IF NOT EXISTS idx_thread_governor_locked_unlock_at
                ON thread_governor_state (locked, unlock_at);
                """
            )
            version = 1

        if version < 2:
            self._conn.execute(
                "ALTER TABLE thread_governor_state ADD COLUMN last_bot_author_id TEXT"
            )
            version = 2

        self._conn.execute(f"PRAGMA user_version = {version}")
        self._conn.commit()


def resolve_thread_governor_db_path() -> Path:
    """Resolve the thread governor DB path from env or default."""
    return Path(os.environ.get(_ENV_KEY, str(_DEFAULT_PATH))).expanduser()


def _coerce_utc(value: datetime | None) -> datetime:
    ts = value or datetime.now(timezone.utc)
    if ts.tzinfo is None:
        return ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc)


def _serialize_dt(value: datetime | None) -> str | None:
    return _coerce_utc(value).isoformat() if value is not None else None


def _deserialize_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    ts = datetime.fromisoformat(value)
    if ts.tzinfo is None:
        return ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc)


def _row_to_state(row: sqlite3.Row) -> ThreadGovernorState:
    return ThreadGovernorState(
        thread_id=str(row["thread_id"]),
        parent_id=str(row["parent_id"]),
        count=int(row["count"]),
        limit=int(row["message_limit"]),
        locked=bool(row["locked"]),
        created_at=_deserialize_dt(row["created_at"]) or datetime.now(timezone.utc),
        last_owner_at=_deserialize_dt(row["last_owner_at"]),
        last_message_at=_deserialize_dt(row["last_message_at"]) or datetime.now(timezone.utc),
        locked_at=_deserialize_dt(row["locked_at"]),
        unlock_at=_deserialize_dt(row["unlock_at"]),
        closed_reason=row["closed_reason"],
        last_bot_author_id=row["last_bot_author_id"],
    )
