"""Deterministic session presence registry.

Publishes session-level presence records to MQTT and maintains a local
cache of remote presence entries with TTL-based expiry.  Provides
deterministic session-target resolution and failure-response generation.

No LLM invocation anywhere in this module.
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
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
# Local session enumeration (deterministic filesystem reads)
# ---------------------------------------------------------------------------

def _state_dir_for_profile(profile: str | None) -> Path:
    """OpenClaw state directory for a given profile."""
    home = Path.home()
    return home / (f".openclaw-{profile}" if profile else ".openclaw")


def _read_last_jsonl_entry(path: Path, max_bytes: int = 32768) -> dict[str, Any] | None:
    """Best-effort: read last valid JSON object from a .jsonl file."""
    try:
        with path.open("rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            f.seek(max(0, size - max_bytes), os.SEEK_SET)
            data = f.read()
        for line in reversed(data.decode(errors="replace").splitlines()):
            line = line.strip()
            if line.startswith("{"):
                try:
                    return json.loads(line)
                except Exception:
                    continue
    except Exception:
        pass
    return None


def _extract_session_meta(
    entry: dict[str, Any],
    index_meta: dict[str, Any],
) -> dict[str, Any]:
    """Extract model/thinking/context from a jsonl entry + index metadata."""
    model = "unknown"
    thinking = "unknown"
    context_tokens: int | None = None
    context_window: int | None = None

    for src in (entry, index_meta):
        m = src.get("model")
        if isinstance(m, str) and m:
            model = m
            break

    t = entry.get("thinking")
    if isinstance(t, str) and t:
        thinking = t
    elif isinstance(t, dict):
        thinking = str(t.get("type", "unknown"))

    usage = entry.get("usage") or {}
    if isinstance(usage, dict):
        for key in ("inputTokens", "input_tokens", "totalTokens", "total_tokens"):
            val = usage.get(key)
            if isinstance(val, (int, float)):
                context_tokens = int(val)
                break

    for key in ("contextWindow", "context_window", "maxTokens", "max_tokens"):
        val = entry.get(key)
        if isinstance(val, (int, float)):
            context_window = int(val)
            break

    return {
        "model": model,
        "thinking": thinking,
        "context_tokens": context_tokens,
        "context_window": context_window,
    }


def _enumerate_active_sessions(
    state_dir: Path,
    active_window_s: int = 300,
) -> list[dict[str, Any]]:
    """Enumerate active sessions from local OC state directory.

    Reads sessions.json index files and best-effort session .jsonl files
    to collect per-session metadata.  All operations are deterministic
    (filesystem reads only, no LLM).
    """
    now_ms = int(time.time() * 1000)
    cutoff_ms = now_ms - (active_window_s * 1000)

    agents_dir = state_dir / "agents"
    if not agents_dir.is_dir():
        return []

    results: list[dict[str, Any]] = []

    try:
        agent_dirs = [d for d in agents_dir.iterdir() if d.is_dir()]
    except OSError:
        return []

    for agent_dir in agent_dirs:
        agent_name = agent_dir.name
        sessions_dir = agent_dir / "sessions"
        sessions_index = sessions_dir / "sessions.json"
        if not sessions_index.exists():
            continue
        try:
            idx = json.loads(sessions_index.read_text())
        except Exception:
            continue
        if not isinstance(idx, dict):
            continue

        for skey, meta in idx.items():
            if not isinstance(meta, dict):
                continue
            updated = meta.get("updatedAt")
            if not isinstance(updated, (int, float)):
                continue
            if updated < cutoff_ms:
                continue

            # Best-effort: read session jsonl for model/context info
            jsonl_entry: dict[str, Any] = {}
            jsonl_path = sessions_dir / f"{skey}.jsonl"
            if jsonl_path.exists():
                entry = _read_last_jsonl_entry(jsonl_path)
                if entry:
                    jsonl_entry = entry

            extracted = _extract_session_meta(jsonl_entry, meta)

            summary = ""
            for src in (meta, jsonl_entry):
                t = src.get("title")
                if isinstance(t, str) and t.strip():
                    summary = t.strip()[:200]
                    break

            activity = ""
            if jsonl_entry:
                sr = jsonl_entry.get("stopReason")
                if isinstance(sr, str) and sr:
                    activity = sr

            cwd = str(jsonl_entry.get("cwd", ""))[:200] if jsonl_entry else ""
            cmdline = str(jsonl_entry.get("cmdline", ""))[:200] if jsonl_entry else ""
            url = str(jsonl_entry.get("url", ""))[:500] if jsonl_entry else ""

            results.append({
                "agent": agent_name,
                "session": skey,
                "updated_at_ms": int(updated),
                "model": extracted["model"],
                "thinking": extracted["thinking"],
                "context_tokens": extracted["context_tokens"],
                "context_window": extracted["context_window"],
                "summary": summary,
                "activity": activity,
                "cmdline": cmdline,
                "cwd": cwd,
                "url": url,
            })

    return results


# ---------------------------------------------------------------------------
# Presence publisher (builds records for local sessions)
# ---------------------------------------------------------------------------

def build_local_presence_records(
    config: HiveConfig,
) -> list[PresenceRecord]:
    """Build presence records for all locally managed OC instances.

    Enumerates actual active sessions from local OC state directories and
    emits one PresenceRecord per session.  Falls back to a single synthetic
    record per instance when no active sessions are found.

    This is deterministic — reads only from config and local files, no LLM.
    """
    records: list[PresenceRecord] = []
    ttl = config.presence.ttl_sec

    for inst in config.oc_instances:
        state_dir = _state_dir_for_profile(inst.profile)
        active_sessions = _enumerate_active_sessions(state_dir, active_window_s=ttl)

        if active_sessions:
            for sess in active_sessions:
                records.append(PresenceRecord(
                    gw=inst.name,
                    agent=sess["agent"],
                    session=sess["session"],
                    state="active",
                    status="idle",
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
        else:
            # Fallback: synthetic record (backwards compat)
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
