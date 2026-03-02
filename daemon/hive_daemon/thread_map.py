"""Persistent corr → Discord thread mapping store.

Maps correlation keys to Discord thread IDs so that related hive events
are grouped into one Discord thread.  Similar durability pattern to
session_map.py — file-based JSON with TTL pruning.

Default store path: ``~/.local/share/hive/announcement_threads.json``
(overridable via env var ``HIVE_THREAD_MAP``).

Record shape::

    {
        "discord:hive-announcements:corr:ab12...": {
            "threadId": "...",
            "parentMessageId": "...",
            "createdAt": 1772430000,
            "lastSeenAt": 1772433600
        }
    }
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass
from pathlib import Path

log = logging.getLogger(__name__)

_ENV_KEY = "HIVE_THREAD_MAP"
_DEFAULT_PATH = Path.home() / ".local" / "share" / "hive" / "announcement_threads.json"


@dataclass
class ThreadEntry:
    """A single thread mapping record."""

    thread_id: str
    parent_message_id: str
    created_at: float
    last_seen_at: float


class ThreadMap:
    """Persistent corr→thread mapping with TTL-based pruning.

    Args:
        max_age_hours: Entries older than this (by last_seen_at) are pruned.
        channel: Discord channel name used as part of the map key.
        path: Override the store file path (mainly for testing).
    """

    def __init__(
        self,
        *,
        max_age_hours: int = 168,
        channel: str = "hive-announcements",
        path: Path | None = None,
    ) -> None:
        self._max_age_secs = max_age_hours * 3600
        self._channel = channel
        self._path = path or _resolve_path()

    def _make_key(self, thread_key: str) -> str:
        """Build the composite store key."""
        return f"discord:{self._channel}:corr:{thread_key}"

    def get(self, thread_key: str) -> ThreadEntry | None:
        """Look up a thread entry by correlation key.

        Returns None if missing or expired.  Updates last_seen_at on hit.
        """
        data = self._load()
        key = self._make_key(thread_key)
        raw = data.get(key)
        if raw is None:
            return None
        entry = _parse_entry(raw)
        if entry is None:
            return None
        # Touch last_seen
        now = time.time()
        entry.last_seen_at = now
        data[key] = _serialize_entry(entry)
        self._save(data)
        return entry

    def put(self, thread_key: str, *, thread_id: str, parent_message_id: str) -> None:
        """Store or update a thread mapping."""
        data = self._load()
        key = self._make_key(thread_key)
        now = time.time()
        existing = data.get(key)
        created = now
        if existing and isinstance(existing, dict):
            created = existing.get("createdAt", now)
        entry = ThreadEntry(
            thread_id=thread_id,
            parent_message_id=parent_message_id,
            created_at=created,
            last_seen_at=now,
        )
        data[key] = _serialize_entry(entry)
        self._save(data)
        log.debug("thread_map: stored %s -> thread=%s", key, thread_id)

    def remove(self, thread_key: str) -> None:
        """Remove a mapping (e.g. when a thread is invalid/archived)."""
        data = self._load()
        key = self._make_key(thread_key)
        if key in data:
            del data[key]
            self._save(data)

    # -- internal --

    def _load(self) -> dict:
        """Load and prune the store."""
        if not self._path.exists():
            return {}
        try:
            data = json.loads(self._path.read_text())
        except (json.JSONDecodeError, OSError):
            return {}
        now = time.time()
        cutoff = now - self._max_age_secs
        return {
            k: v
            for k, v in data.items()
            if isinstance(v, dict) and v.get("lastSeenAt", 0) > cutoff
        }

    def _save(self, data: dict) -> None:
        """Atomically write the store."""
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, indent=2))
        tmp.replace(self._path)


def _resolve_path() -> Path:
    return Path(os.environ.get(_ENV_KEY, str(_DEFAULT_PATH)))


def _parse_entry(raw: dict) -> ThreadEntry | None:
    """Parse a raw dict into a ThreadEntry, or None if malformed."""
    try:
        return ThreadEntry(
            thread_id=raw["threadId"],
            parent_message_id=raw["parentMessageId"],
            created_at=raw["createdAt"],
            last_seen_at=raw["lastSeenAt"],
        )
    except (KeyError, TypeError):
        return None


def _serialize_entry(entry: ThreadEntry) -> dict:
    return {
        "threadId": entry.thread_id,
        "parentMessageId": entry.parent_message_id,
        "createdAt": entry.created_at,
        "lastSeenAt": entry.last_seen_at,
    }


def resolve_thread_key(corr: str | None, msg_id: str) -> str:
    """Determine the thread grouping key for an envelope.

    Uses ``corr`` when present, falls back to ``id``.
    """
    return corr if corr else msg_id
