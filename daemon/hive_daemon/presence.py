"""Deterministic session presence registry.

Publishes session-level presence records to MQTT and maintains a local
cache of remote presence entries with TTL-based expiry.  Provides
deterministic session-target resolution and failure-response generation.

No LLM invocation anywhere in this module.
"""

from __future__ import annotations

import json
import logging
import time
import uuid
from dataclasses import dataclass, field
from typing import Any

from hive_daemon.config import HiveConfig, PresenceConfig

log = logging.getLogger(__name__)

PRESENCE_VERSION = 1


# ---------------------------------------------------------------------------
# Presence payload model
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class TaskSummary:
    """Lightweight, sanitised task metadata."""

    summary: str = ""
    cmdline: str = ""
    cwd: str = ""
    url: str = ""

    def to_dict(self) -> dict[str, str]:
        d: dict[str, str] = {}
        if self.summary:
            d["summary"] = self.summary[:200]
        if self.cmdline:
            d["cmdline"] = self.cmdline[:200]
        if self.cwd:
            d["cwd"] = self.cwd[:200]
        if self.url:
            d["url"] = self.url[:500]
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> TaskSummary:
        return cls(
            summary=str(data.get("summary", ""))[:200],
            cmdline=str(data.get("cmdline", ""))[:200],
            cwd=str(data.get("cwd", ""))[:200],
            url=str(data.get("url", ""))[:500],
        )


@dataclass(frozen=True, slots=True)
class PresenceRecord:
    """A single session presence record."""

    gw: str
    agent: str
    session: str
    state: str = "active"
    status: str = "idle"
    task: TaskSummary = field(default_factory=TaskSummary)
    updated_ts: int = 0
    ttl_sec: int = 300

    @property
    def key(self) -> str:
        """Canonical composite key: gw/agent/session."""
        return f"{self.gw}/{self.agent}/{self.session}"

    def to_dict(self) -> dict[str, Any]:
        d: dict[str, Any] = {
            "v": PRESENCE_VERSION,
            "kind": "session_presence",
            "gw": self.gw,
            "agent": self.agent,
            "session": self.session,
            "state": self.state,
            "status": self.status,
            "updatedTs": self.updated_ts or int(time.time()),
            "ttlSec": self.ttl_sec,
        }
        task_d = self.task.to_dict()
        if task_d:
            d["task"] = task_d
        return d

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> PresenceRecord:
        """Parse a presence payload dict.  Raises ValueError on invalid data."""
        if data.get("kind") != "session_presence":
            raise ValueError(f"unexpected kind: {data.get('kind')}")
        task_raw = data.get("task")
        task = TaskSummary.from_dict(task_raw) if isinstance(task_raw, dict) else TaskSummary()
        return cls(
            gw=str(data.get("gw", "")),
            agent=str(data.get("agent", "")),
            session=str(data.get("session", "")),
            state=str(data.get("state", "active")),
            status=str(data.get("status", "idle")),
            task=task,
            updated_ts=int(data.get("updatedTs", 0)),
            ttl_sec=int(data.get("ttlSec", 300)),
        )


# ---------------------------------------------------------------------------
# Presence cache
# ---------------------------------------------------------------------------

@dataclass
class CacheEntry:
    """A cached presence record with local timing."""

    record: PresenceRecord
    received_ts: float  # time.monotonic() when we received it
    expiry_ts: float    # monotonic deadline


class PresenceCache:
    """In-memory cache of presence entries keyed by gw/agent/session.

    All operations are deterministic and synchronous.
    """

    def __init__(self) -> None:
        self._entries: dict[str, CacheEntry] = {}

    def update(self, record: PresenceRecord) -> None:
        """Insert or update a presence record."""
        if not record.gw or not record.agent or not record.session:
            log.warning("ignoring presence record with missing identity fields")
            return
        now = time.monotonic()
        self._entries[record.key] = CacheEntry(
            record=record,
            received_ts=now,
            expiry_ts=now + record.ttl_sec,
        )
        log.debug("presence cache: updated %s (ttl=%ds)", record.key, record.ttl_sec)

    def get(self, key: str) -> PresenceRecord | None:
        """Look up a fresh (non-expired) entry by composite key."""
        entry = self._entries.get(key)
        if entry is None:
            return None
        if time.monotonic() > entry.expiry_ts:
            del self._entries[key]
            return None
        return entry.record

    def resolve(self, gw: str, agent: str, session: str) -> PresenceRecord | None:
        """Resolve a specific session target."""
        return self.get(f"{gw}/{agent}/{session}")

    def resolve_shorthand(self, gw: str, session: str) -> PresenceRecord | None | str:
        """Resolve gw/session shorthand (agent inferred).

        Returns:
            PresenceRecord if exactly one match found.
            None if no match.
            "ambiguous" string if multiple agents match.
        """
        matches: list[PresenceRecord] = []
        self.prune()
        for key, entry in self._entries.items():
            r = entry.record
            if r.gw == gw and r.session == session:
                matches.append(r)
        if len(matches) == 1:
            return matches[0]
        if len(matches) > 1:
            return "ambiguous"
        return None

    def prune(self) -> int:
        """Remove expired entries.  Returns count of pruned entries."""
        now = time.monotonic()
        expired = [k for k, v in self._entries.items() if now > v.expiry_ts]
        for k in expired:
            del self._entries[k]
        if expired:
            log.debug("presence cache: pruned %d expired entries", len(expired))
        return len(expired)

    def all_fresh(self) -> list[PresenceRecord]:
        """Return all non-expired records."""
        self.prune()
        return [e.record for e in self._entries.values()]

    def __len__(self) -> int:
        return len(self._entries)


# ---------------------------------------------------------------------------
# Deterministic delivery-error response
# ---------------------------------------------------------------------------

DELIVERY_ERROR_CODES = frozenset({
    "SESSION_NOT_FOUND",
    "SESSION_STALE",
    "SESSION_UNREACHABLE",
    "AGENT_MISMATCH",
    "TARGET_AMBIGUOUS",
})


def make_delivery_error(
    *,
    code: str,
    target_gw: str,
    target_agent: str,
    target_session: str,
    detail: str,
    corr: str | None = None,
    reply_to: str | None = None,
) -> dict[str, Any]:
    """Build a deterministic delivery_error response dict.

    This is NOT an Envelope — it is a lightweight error payload returned
    to the caller (CLI or daemon) without involving any LLM.
    """
    if code not in DELIVERY_ERROR_CODES:
        raise ValueError(f"unknown delivery error code: {code}")
    return {
        "v": PRESENCE_VERSION,
        "kind": "delivery_error",
        "code": code,
        "target": {
            "gw": target_gw,
            "agent": target_agent,
            "session": target_session,
        },
        "detail": detail,
        "corr": corr,
        "replyTo": reply_to,
    }


# ---------------------------------------------------------------------------
# Session target resolver (deterministic)
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class ResolveResult:
    """Outcome of a session-target resolution attempt."""

    record: PresenceRecord | None = None
    error: dict[str, Any] | None = None

    @property
    def ok(self) -> bool:
        return self.record is not None and self.error is None


def resolve_session_target(
    cache: PresenceCache,
    *,
    gw: str,
    agent: str | None = None,
    session: str,
    corr: str | None = None,
    reply_to: str | None = None,
) -> ResolveResult:
    """Deterministically resolve a session target from the presence cache.

    Returns a ResolveResult with either a fresh record or a delivery_error dict.
    """
    if agent:
        record = cache.resolve(gw, agent, session)
        if record is not None:
            return ResolveResult(record=record)
        return ResolveResult(error=make_delivery_error(
            code="SESSION_NOT_FOUND",
            target_gw=gw,
            target_agent=agent,
            target_session=session,
            detail="No fresh presence record within ttl window",
            corr=corr,
            reply_to=reply_to,
        ))

    # Shorthand: agent not specified — infer from cache
    result = cache.resolve_shorthand(gw, session)
    if isinstance(result, PresenceRecord):
        return ResolveResult(record=result)
    if result == "ambiguous":
        return ResolveResult(error=make_delivery_error(
            code="TARGET_AMBIGUOUS",
            target_gw=gw,
            target_agent="",
            target_session=session,
            detail="Multiple agents have active sessions matching this target",
            corr=corr,
            reply_to=reply_to,
        ))
    return ResolveResult(error=make_delivery_error(
        code="SESSION_NOT_FOUND",
        target_gw=gw,
        target_agent="",
        target_session=session,
        detail="No fresh presence record within ttl window",
        corr=corr,
        reply_to=reply_to,
    ))


# ---------------------------------------------------------------------------
# Presence publisher (builds records for local sessions)
# ---------------------------------------------------------------------------

def build_local_presence_records(
    config: HiveConfig,
) -> list[PresenceRecord]:
    """Build presence records for all locally managed OC instances.

    This is deterministic — reads only from config, no LLM.
    """
    records: list[PresenceRecord] = []
    ttl = config.presence.ttl_sec

    for inst in config.oc_instances:
        # Each OC instance gets a presence record with its daily session id
        from hive_daemon.oc_bridge import OcBridge
        session_id = OcBridge._session_id_for_instance(inst)
        records.append(PresenceRecord(
            gw=inst.name,
            agent=inst.agent_id or "main",
            session=session_id,
            state="active",
            status="idle",
            updated_ts=int(time.time()),
            ttl_sec=ttl,
        ))

    if not config.oc_instances:
        # Standalone daemon — publish a single node-level record
        records.append(PresenceRecord(
            gw=config.node_id,
            agent="daemon",
            session="default",
            state="active",
            status="idle",
            updated_ts=int(time.time()),
            ttl_sec=ttl,
        ))

    return records


def presence_mqtt_topic(prefix: str, record: PresenceRecord) -> str:
    """Build the MQTT topic for a presence record."""
    return f"{prefix}/presence/{record.gw}/{record.agent}/{record.session}"
