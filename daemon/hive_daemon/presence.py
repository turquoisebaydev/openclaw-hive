"""Deterministic session presence registry.

Publishes session-level presence records to MQTT and maintains a local
cache of remote presence entries with TTL-based expiry.  Provides
deterministic session-target resolution and failure-response generation.

No LLM invocation anywhere in this module.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any

from hive_daemon.config import HiveConfig, OcInstance, PresenceConfig
from hive_daemon.probe import _run_openclaw_json

log = logging.getLogger(__name__)

PRESENCE_VERSION = 1


# ---------------------------------------------------------------------------
# Presence payload model
# ---------------------------------------------------------------------------

@dataclass(frozen=True, slots=True)
class TaskSummary:
    """Lightweight, sanitised task metadata."""

    summary: str = ""
    activity: str = ""
    cmdline: str = ""
    cwd: str = ""
    url: str = ""

    def to_dict(self) -> dict[str, str]:
        d: dict[str, str] = {}
        if self.summary:
            d["summary"] = self.summary[:200]
        if self.activity:
            d["activity"] = self.activity[:200]
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
            activity=str(data.get("activity", ""))[:200],
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
    model: str = "unknown"
    thinking: str = "unknown"
    context_tokens: int | None = None
    context_window: int | None = None
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
            "model": self.model,
            "thinking": self.thinking,
            "context": {
                "tokens": self.context_tokens if self.context_tokens is not None else "unknown",
                "window": self.context_window if self.context_window is not None else "unknown",
            },
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
        ctx = data.get("context") if isinstance(data.get("context"), dict) else {}
        ctx_tokens = ctx.get("tokens") if isinstance(ctx, dict) else None
        ctx_window = ctx.get("window") if isinstance(ctx, dict) else None
        return cls(
            gw=str(data.get("gw", "")),
            agent=str(data.get("agent", "")),
            session=str(data.get("session", "")),
            state=str(data.get("state", "active")),
            status=str(data.get("status", "idle")),
            model=str(data.get("model", "unknown")),
            thinking=str(data.get("thinking", "unknown")),
            context_tokens=int(ctx_tokens) if isinstance(ctx_tokens, (int, float)) else None,
            context_window=int(ctx_window) if isinstance(ctx_window, (int, float)) else None,
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
# Runtime API session enumeration (deterministic RPC, no file scraping)
# ---------------------------------------------------------------------------

async def _list_sessions_via_api(
    inst: OcInstance,
    *,
    timeout_s: float = 10.0,
) -> tuple[bool, list[dict[str, Any]], str]:
    """List active sessions via OpenClaw runtime API.

    Calls ``openclaw sessions list --json`` for the given instance.
    Returns: (ok, sessions_list, error_string).
    """
    ok, data, err = await _run_openclaw_json(
        openclaw_cmd=inst.resolved_openclaw_cmd,
        profile=inst.profile,
        args=["sessions", "list", "--json"],
        timeout_s=timeout_s,
    )
    if not ok:
        return False, [], err
    if not isinstance(data, (dict, list)):
        return False, [], "unexpected response format"

    sessions = data if isinstance(data, list) else data.get("sessions", [])
    if not isinstance(sessions, list):
        return False, [], "sessions field is not a list"

    return True, sessions, ""


def _parse_api_session(raw: dict[str, Any]) -> dict[str, Any]:
    """Parse a single session record from the runtime API response.

    Handles multiple field-name conventions used by different OC versions.
    """
    session_id = str(
        raw.get("sessionId") or raw.get("session_id") or raw.get("id") or ""
    )
    agent = str(raw.get("agent") or raw.get("agentId") or raw.get("agent_id") or "main")
    status = str(raw.get("status") or "idle")
    model = str(raw.get("model") or "unknown")
    thinking = str(raw.get("thinking") or "unknown")

    # Context tokens / window
    context_tokens: int | None = None
    context_window: int | None = None

    usage = raw.get("usage") if isinstance(raw.get("usage"), dict) else {}
    for key in ("inputTokens", "input_tokens", "totalTokens", "total_tokens"):
        val = usage.get(key)
        if isinstance(val, (int, float)):
            context_tokens = int(val)
            break

    for key in ("contextWindow", "context_window"):
        val = raw.get(key)
        if isinstance(val, (int, float)):
            context_window = int(val)
            break

    # Task / activity fields
    summary = str(raw.get("title") or raw.get("summary") or "")[:200]
    activity = str(raw.get("activity") or "")[:200]
    cwd = str(raw.get("cwd") or "")[:200]
    cmdline = str(raw.get("cmdline") or "")[:200]
    url = str(raw.get("url") or "")[:500]

    return {
        "session": session_id,
        "agent": agent,
        "status": status,
        "model": model,
        "thinking": thinking,
        "context_tokens": context_tokens,
        "context_window": context_window,
        "summary": summary,
        "activity": activity,
        "cwd": cwd,
        "cmdline": cmdline,
        "url": url,
    }


# ---------------------------------------------------------------------------
# Presence publisher (builds records for local sessions)
# ---------------------------------------------------------------------------

async def build_local_presence_records(
    config: HiveConfig,
) -> list[PresenceRecord]:
    """Build presence records for all locally managed OC instances.

    Calls the OpenClaw runtime API (``openclaw sessions list --json``) to
    enumerate active sessions and emits one PresenceRecord per session.

    When the API is unavailable, emits an explicit error record with
    ``state="error"`` — never silently falls back to file scraping.

    This is deterministic — runtime API / CLI JSON only, no LLM.
    """
    records: list[PresenceRecord] = []
    ttl = config.presence.ttl_sec

    for inst in config.oc_instances:
        ok, api_sessions, err = await _list_sessions_via_api(inst, timeout_s=10.0)

        if ok and api_sessions:
            for raw in api_sessions:
                if not isinstance(raw, dict):
                    continue
                sess = _parse_api_session(raw)
                if not sess["session"]:
                    continue
                records.append(PresenceRecord(
                    gw=inst.name,
                    agent=sess["agent"],
                    session=sess["session"],
                    state="active",
                    status=sess["status"],
                    model=sess["model"],
                    thinking=sess["thinking"],
                    context_tokens=sess["context_tokens"],
                    context_window=sess["context_window"],
                    task=TaskSummary(
                        summary=sess["summary"],
                        activity=sess["activity"],
                        cmdline=sess["cmdline"],
                        cwd=sess["cwd"],
                        url=sess["url"],
                    ),
                    updated_ts=int(time.time()),
                    ttl_sec=ttl,
                ))
        elif ok:
            # API reachable but returned no sessions — emit idle record
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
        else:
            # API unavailable — emit explicit error record (no silent fallback)
            from hive_daemon.oc_bridge import OcBridge
            session_id = OcBridge._session_id_for_instance(inst)
            records.append(PresenceRecord(
                gw=inst.name,
                agent=inst.agent_id or "main",
                session=session_id,
                state="error",
                status=f"api_unavailable: {err}"[:200],
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
