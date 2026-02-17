"""Correlation → session mapping store.

File-based JSON store shared between hive-cli (writer) and hive-daemon (reader).
The CLI writes a corr_id → session_key mapping when --session is used;
the daemon reads it when delivering responses to route to the correct OC session.

The store file lives at ``~/.local/share/hive/session-map.json`` by default
(overridable via env var ``HIVE_SESSION_MAP``).

Format:
    {
        "<corr_id>": {"session": "<session_key>", "expires": <unix_timestamp>},
        ...
    }

Entries are pruned on every read/write to keep the file small.
"""

from __future__ import annotations

import json
import logging
import os
import time
from pathlib import Path

log = logging.getLogger(__name__)

DEFAULT_TTL = 3600  # 1 hour
_ENV_KEY = "HIVE_SESSION_MAP"
_DEFAULT_PATH = Path.home() / ".local" / "share" / "hive" / "session-map.json"


def _store_path() -> Path:
    """Resolve the session map file path."""
    return Path(os.environ.get(_ENV_KEY, str(_DEFAULT_PATH)))


def _load(path: Path) -> dict:
    """Load and prune the store, returning only non-expired entries."""
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return {}
    now = time.time()
    return {k: v for k, v in data.items() if isinstance(v, dict) and v.get("expires", 0) > now}


def _save(path: Path, data: dict) -> None:
    """Atomically write the store."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(data, indent=2))
    tmp.replace(path)


def put(corr_id: str, session_key: str, ttl: int = DEFAULT_TTL) -> None:
    """Register a corr → session mapping with a TTL."""
    path = _store_path()
    data = _load(path)
    data[corr_id] = {
        "session": session_key,
        "expires": int(time.time()) + ttl,
    }
    _save(path, data)
    log.debug("session_map: stored corr=%s -> session=%s (ttl=%ds)", corr_id, session_key, ttl)


def get(corr_id: str) -> str | None:
    """Look up the session key for a corr_id. Returns None if not found or expired."""
    path = _store_path()
    data = _load(path)
    entry = data.get(corr_id)
    if entry is None:
        return None
    session = entry.get("session")
    log.debug("session_map: found corr=%s -> session=%s", corr_id, session)
    return session


def pop(corr_id: str) -> str | None:
    """Look up and remove the session key for a corr_id."""
    path = _store_path()
    data = _load(path)
    entry = data.pop(corr_id, None)
    if entry is None:
        return None
    _save(path, data)
    session = entry.get("session")
    log.debug("session_map: popped corr=%s -> session=%s", corr_id, session)
    return session
