"""Local JSONL audit log for hive announcement events.

Writes one JSON line per announcement event containing full metadata and
publish outcome.  Replaces the need for metadata-heavy Discord message bodies.

Default path (macOS): ``~/Library/Logs/hive-announcements.log``
Default path (Linux): ``~/.local/state/hive/hive-announcements.log``

Configurable via ``[announcements.audit]`` in hive.toml.
"""

from __future__ import annotations

import json
import logging
import time
from pathlib import Path

from hive_daemon.config import AuditConfig
from hive_daemon.envelope import Envelope

log = logging.getLogger(__name__)


class AuditLogger:
    """Append-only JSONL audit logger for announcement events.

    Safe to call even when disabled — methods return immediately.
    All write errors are caught and logged as warnings.

    Args:
        config: The audit configuration block.
        node_id: Local gateway identifier for gw= field.
    """

    def __init__(self, config: AuditConfig, *, node_id: str | None = None) -> None:
        self._enabled = config.enabled
        self._node_id = node_id
        if self._enabled:
            self._path = Path(config.path).expanduser()
        else:
            self._path = None

    @property
    def enabled(self) -> bool:
        return self._enabled

    def record(
        self,
        envelope: Envelope,
        *,
        event: str,
        status: str = "ok",
        thread_id: str | None = None,
        error: str | None = None,
    ) -> None:
        """Write one audit record. Non-fatal on any error."""
        if not self._enabled or self._path is None:
            return
        entry: dict = {
            "ts": time.time(),
            "event": event,
            "id": envelope.id,
            "from": envelope.from_,
            "to": envelope.to,
            "ch": envelope.ch,
            "action": envelope.action,
            "corr": envelope.corr,
            "replyTo": envelope.reply_to,
            "gw": self._node_id,
            "status": status,
        }
        if thread_id:
            entry["threadId"] = thread_id
        if error:
            entry["error"] = error
        try:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            with open(self._path, "a") as f:
                f.write(json.dumps(entry, separators=(",", ":")) + "\n")
        except OSError as exc:
            log.warning("audit log write failed: %s", exc)
