"""Tests for the deterministic session presence registry."""

import json
import time
from pathlib import Path
from unittest.mock import patch

import pytest

from hive_daemon.presence import (
    DELIVERY_ERROR_CODES,
    PRESENCE_VERSION,
    CacheEntry,
    PresenceCache,
    PresenceRecord,
    ResolveResult,
    TaskSummary,
    _enumerate_active_sessions,
    _extract_session_meta,
    _extract_text_from_content,
    _derive_status_and_activity,
    _read_last_jsonl_entry,
    build_local_presence_records,
    make_delivery_error,
    presence_mqtt_topic,
    resolve_session_target,
)
from hive_daemon.config import HiveConfig, MqttConfig, OcInstance, PresenceConfig


# ── helpers ─────────────────────────────────────────────────────────

def _make_config(
    node_id: str = "turq",
    oc_instances: list[OcInstance] | None = None,
    presence: PresenceConfig | None = None,
) -> HiveConfig:
    return HiveConfig(
        node_id=node_id,
        topic_prefix="turq/hive",
        mqtt=MqttConfig(),
        oc_instances=oc_instances or [],
        presence=presence or PresenceConfig(),
    )


def _make_record(
    gw: str = "pg1",
    agent: str = "main",
    session: str = "hive-pg1-20260305",
    state: str = "active",
    status: str = "idle",
    ttl_sec: int = 300,
) -> PresenceRecord:
    return PresenceRecord(
        gw=gw,
        agent=agent,
        session=session,
        state=state,
        status=status,
        updated_ts=int(time.time()),
        ttl_sec=ttl_sec,
    )


def _make_state_dir(
    tmp_path: Path,
    agent: str = "main",
    sessions: dict | None = None,
    jsonl_files: dict[str, list[dict]] | None = None,
) -> Path:
    """Create a mock OC state directory with sessions.json and optional jsonl."""
    sessions_dir = tmp_path / "agents" / agent / "sessions"
    sessions_dir.mkdir(parents=True)
    (sessions_dir / "sessions.json").write_text(json.dumps(sessions or {}))
    for name, entries in (jsonl_files or {}).items():
        lines = [json.dumps(e) for e in entries]
        (sessions_dir / f"{name}.jsonl").write_text("\n".join(lines) + "\n")
    return tmp_path


# ── TaskSummary ─────────────────────────────────────────────────────

class TestTaskSummary:
    def test_to_dict_empty(self):
        t = TaskSummary()
        assert t.to_dict() == {}

    def test_to_dict_populated(self):
        t = TaskSummary(summary="testing", cmdline="pytest", cwd="/tmp", url="https://x.com")
        d = t.to_dict()
        assert d["summary"] == "testing"
        assert d["cmdline"] == "pytest"
        assert d["cwd"] == "/tmp"
        assert d["url"] == "https://x.com"

    def test_to_dict_truncates(self):
        t = TaskSummary(summary="x" * 300)
        d = t.to_dict()
        assert len(d["summary"]) == 200

    def test_from_dict(self):
        t = TaskSummary.from_dict({"summary": "hello", "cmdline": "ls"})
        assert t.summary == "hello"
        assert t.cmdline == "ls"
        assert t.cwd == ""
        assert t.url == ""

    def test_from_dict_empty(self):
        t = TaskSummary.from_dict({})
        assert t.summary == ""

    def test_activity_field(self):
        t = TaskSummary(summary="testing", activity="running tests")
        d = t.to_dict()
        assert d["activity"] == "running tests"

    def test_activity_from_dict(self):
        t = TaskSummary.from_dict({"summary": "x", "activity": "compiling"})
        assert t.activity == "compiling"

    def test_activity_default_empty(self):
        t = TaskSummary.from_dict({"summary": "x"})
        assert t.activity == ""


# ── PresenceRecord ──────────────────────────────────────────────────

class TestPresenceRecord:
    def test_key(self):
        r = _make_record(gw="pg1", agent="main", session="sess-1")
        assert r.key == "pg1/main/sess-1"

    def test_to_dict(self):
        r = _make_record()
        d = r.to_dict()
        assert d["v"] == PRESENCE_VERSION
        assert d["kind"] == "session_presence"
        assert d["gw"] == "pg1"
        assert d["agent"] == "main"
        assert d["state"] == "active"
        assert d["ttlSec"] == 300
        assert "updatedTs" in d

    def test_to_dict_with_task(self):
        r = PresenceRecord(
            gw="pg1", agent="main", session="s1",
            task=TaskSummary(summary="testing"),
            updated_ts=1000, ttl_sec=300,
        )
        d = r.to_dict()
        assert d["task"]["summary"] == "testing"

    def test_to_dict_omits_empty_task(self):
        r = _make_record()
        d = r.to_dict()
        assert "task" not in d

    def test_from_dict(self):
        data = {
            "kind": "session_presence",
            "gw": "pg1",
            "agent": "main",
            "session": "sess-1",
            "state": "active",
            "status": "busy",
            "updatedTs": 1000,
            "ttlSec": 120,
        }
        r = PresenceRecord.from_dict(data)
        assert r.gw == "pg1"
        assert r.agent == "main"
        assert r.session == "sess-1"
        assert r.status == "busy"
        assert r.ttl_sec == 120

    def test_from_dict_with_task(self):
        data = {
            "kind": "session_presence",
            "gw": "pg1",
            "agent": "main",
            "session": "s1",
            "task": {"summary": "debugging"},
            "updatedTs": 1000,
            "ttlSec": 300,
        }
        r = PresenceRecord.from_dict(data)
        assert r.task.summary == "debugging"

    def test_from_dict_wrong_kind(self):
        with pytest.raises(ValueError, match="unexpected kind"):
            PresenceRecord.from_dict({"kind": "wrong"})

    def test_roundtrip(self):
        r = PresenceRecord(
            gw="turq", agent="main", session="test-session",
            state="active", status="idle",
            task=TaskSummary(summary="roundtrip test"),
            updated_ts=12345, ttl_sec=600,
        )
        d = r.to_dict()
        r2 = PresenceRecord.from_dict(d)
        assert r2.gw == r.gw
        assert r2.agent == r.agent
        assert r2.session == r.session
        assert r2.task.summary == r.task.summary
        assert r2.ttl_sec == r.ttl_sec


# ── PresenceRecord enrichment fields (Phase 5) ────────────────────

class TestPresenceRecordEnrichment:
    """Tests for model/thinking/context enrichment fields."""

    def test_to_dict_includes_model_thinking_context(self):
        r = PresenceRecord(
            gw="pg1", agent="main", session="s1",
            model="claude-sonnet-4-20250514",
            thinking="low",
            context_tokens=5000,
            context_window=200000,
            updated_ts=1000, ttl_sec=300,
        )
        d = r.to_dict()
        assert d["model"] == "claude-sonnet-4-20250514"
        assert d["thinking"] == "low"
        assert d["context"]["tokens"] == 5000
        assert d["context"]["window"] == 200000

    def test_to_dict_unknown_defaults(self):
        r = _make_record()
        d = r.to_dict()
        assert d["model"] == "unknown"
        assert d["thinking"] == "unknown"
        assert d["context"]["tokens"] == "unknown"
        assert d["context"]["window"] == "unknown"

    def test_from_dict_with_enrichment_fields(self):
        data = {
            "kind": "session_presence",
            "gw": "pg1",
            "agent": "main",
            "session": "s1",
            "model": "claude-opus-4-20250514",
            "thinking": "high",
            "context": {"tokens": 10000, "window": 200000},
            "updatedTs": 1000,
            "ttlSec": 300,
        }
        r = PresenceRecord.from_dict(data)
        assert r.model == "claude-opus-4-20250514"
        assert r.thinking == "high"
        assert r.context_tokens == 10000
        assert r.context_window == 200000

    def test_from_dict_missing_enrichment_defaults(self):
        """Old-format payloads without enrichment fields parse with defaults."""
        data = {
            "kind": "session_presence",
            "gw": "pg1",
            "agent": "main",
            "session": "s1",
            "updatedTs": 1000,
            "ttlSec": 300,
        }
        r = PresenceRecord.from_dict(data)
        assert r.model == "unknown"
        assert r.thinking == "unknown"
        assert r.context_tokens is None
        assert r.context_window is None

    def test_from_dict_context_unknown_strings(self):
        """Context values that are 'unknown' strings deserialize as None."""
        data = {
            "kind": "session_presence",
            "gw": "pg1",
            "agent": "main",
            "session": "s1",
            "context": {"tokens": "unknown", "window": "unknown"},
            "updatedTs": 1000,
            "ttlSec": 300,
        }
        r = PresenceRecord.from_dict(data)
        assert r.context_tokens is None
        assert r.context_window is None

    def test_roundtrip_with_enrichment(self):
        r = PresenceRecord(
            gw="turq", agent="main", session="test",
            model="claude-sonnet-4-20250514",
            thinking="low",
            context_tokens=8000,
            context_window=200000,
            updated_ts=12345, ttl_sec=300,
        )
        d = r.to_dict()
        r2 = PresenceRecord.from_dict(d)
        assert r2.model == r.model
        assert r2.thinking == r.thinking
        assert r2.context_tokens == r.context_tokens
        assert r2.context_window == r.context_window


# ── PresenceCache ───────────────────────────────────────────────────

class TestPresenceCache:
    def test_empty_cache(self):
        cache = PresenceCache()
        assert len(cache) == 0
        assert cache.all_fresh() == []

    def test_update_and_get(self):
        cache = PresenceCache()
        r = _make_record()
        cache.update(r)
        assert len(cache) == 1
        got = cache.get(r.key)
        assert got is not None
        assert got.gw == "pg1"

    def test_update_overwrites(self):
        cache = PresenceCache()
        r1 = _make_record(status="idle")
        cache.update(r1)
        r2 = _make_record(status="busy")
        cache.update(r2)
        assert len(cache) == 1
        assert cache.get(r1.key).status == "busy"

    def test_get_missing(self):
        cache = PresenceCache()
        assert cache.get("nonexistent/key/here") is None

    def test_get_expired(self):
        cache = PresenceCache()
        r = _make_record(ttl_sec=0)  # expires immediately
        cache.update(r)
        # Manipulate entry to be expired
        entry = cache._entries[r.key]
        entry.expiry_ts = time.monotonic() - 1
        assert cache.get(r.key) is None
        assert len(cache) == 0  # cleaned up on access

    def test_resolve(self):
        cache = PresenceCache()
        r = _make_record(gw="pg1", agent="main", session="sess-1")
        cache.update(r)
        got = cache.resolve("pg1", "main", "sess-1")
        assert got is not None
        assert got.key == "pg1/main/sess-1"

    def test_resolve_miss(self):
        cache = PresenceCache()
        assert cache.resolve("pg1", "main", "nonexistent") is None

    def test_resolve_shorthand_unique(self):
        cache = PresenceCache()
        r = _make_record(gw="pg1", agent="main", session="sess-1")
        cache.update(r)
        result = cache.resolve_shorthand("pg1", "sess-1")
        assert isinstance(result, PresenceRecord)
        assert result.key == "pg1/main/sess-1"

    def test_resolve_shorthand_ambiguous(self):
        cache = PresenceCache()
        r1 = _make_record(gw="pg1", agent="main", session="sess-1")
        r2 = _make_record(gw="pg1", agent="alt", session="sess-1")
        cache.update(r1)
        cache.update(r2)
        result = cache.resolve_shorthand("pg1", "sess-1")
        assert result == "ambiguous"

    def test_resolve_shorthand_miss(self):
        cache = PresenceCache()
        assert cache.resolve_shorthand("pg1", "nonexistent") is None

    def test_prune(self):
        cache = PresenceCache()
        r1 = _make_record(gw="pg1", agent="main", session="fresh")
        r2 = _make_record(gw="pg1", agent="main", session="stale")
        cache.update(r1)
        cache.update(r2)
        # Make r2 expired
        cache._entries[r2.key].expiry_ts = time.monotonic() - 1
        pruned = cache.prune()
        assert pruned == 1
        assert len(cache) == 1
        assert cache.get(r1.key) is not None
        assert cache.get(r2.key) is None

    def test_prune_empty(self):
        cache = PresenceCache()
        assert cache.prune() == 0

    def test_all_fresh(self):
        cache = PresenceCache()
        cache.update(_make_record(gw="a", agent="m", session="s1"))
        cache.update(_make_record(gw="b", agent="m", session="s2"))
        assert len(cache.all_fresh()) == 2

    def test_all_fresh_excludes_expired(self):
        cache = PresenceCache()
        r1 = _make_record(gw="a", agent="m", session="fresh")
        r2 = _make_record(gw="b", agent="m", session="stale")
        cache.update(r1)
        cache.update(r2)
        cache._entries[r2.key].expiry_ts = time.monotonic() - 1
        fresh = cache.all_fresh()
        assert len(fresh) == 1
        assert fresh[0].gw == "a"

    def test_ignores_empty_identity_fields(self):
        cache = PresenceCache()
        r = PresenceRecord(gw="", agent="main", session="s1")
        cache.update(r)
        assert len(cache) == 0

        r2 = PresenceRecord(gw="pg1", agent="", session="s1")
        cache.update(r2)
        assert len(cache) == 0

    def test_prune_preserves_enriched_records(self):
        """Enriched records with model/context survive pruning when fresh."""
        cache = PresenceCache()
        r = PresenceRecord(
            gw="pg1", agent="main", session="s1",
            model="claude-sonnet-4-20250514", thinking="low",
            context_tokens=5000, context_window=200000,
            updated_ts=int(time.time()), ttl_sec=300,
        )
        cache.update(r)
        cache.prune()
        got = cache.get(r.key)
        assert got is not None
        assert got.model == "claude-sonnet-4-20250514"
        assert got.context_tokens == 5000


# ── Delivery error ──────────────────────────────────────────────────

class TestMakeDeliveryError:
    def test_basic_error(self):
        err = make_delivery_error(
            code="SESSION_NOT_FOUND",
            target_gw="pg1",
            target_agent="main",
            target_session="sess-1",
            detail="No fresh presence record within ttl window",
            corr="corr-123",
            reply_to="msg-456",
        )
        assert err["v"] == PRESENCE_VERSION
        assert err["kind"] == "delivery_error"
        assert err["code"] == "SESSION_NOT_FOUND"
        assert err["target"]["gw"] == "pg1"
        assert err["target"]["agent"] == "main"
        assert err["target"]["session"] == "sess-1"
        assert err["corr"] == "corr-123"
        assert err["replyTo"] == "msg-456"

    def test_all_valid_codes(self):
        for code in DELIVERY_ERROR_CODES:
            err = make_delivery_error(
                code=code,
                target_gw="x", target_agent="y", target_session="z",
                detail="test",
            )
            assert err["code"] == code

    def test_invalid_code_raises(self):
        with pytest.raises(ValueError, match="unknown delivery error code"):
            make_delivery_error(
                code="INVALID_CODE",
                target_gw="x", target_agent="y", target_session="z",
                detail="test",
            )

    def test_optional_fields_none(self):
        err = make_delivery_error(
            code="SESSION_STALE",
            target_gw="pg1", target_agent="main", target_session="s1",
            detail="expired",
        )
        assert err["corr"] is None
        assert err["replyTo"] is None


# ── Session target resolver ─────────────────────────────────────────

class TestResolveSessionTarget:
    def test_resolve_full_target_found(self):
        cache = PresenceCache()
        cache.update(_make_record(gw="pg1", agent="main", session="sess-1"))
        result = resolve_session_target(cache, gw="pg1", agent="main", session="sess-1")
        assert result.ok
        assert result.record.key == "pg1/main/sess-1"
        assert result.error is None

    def test_resolve_full_target_not_found(self):
        cache = PresenceCache()
        result = resolve_session_target(cache, gw="pg1", agent="main", session="nonexistent")
        assert not result.ok
        assert result.error is not None
        assert result.error["code"] == "SESSION_NOT_FOUND"
        assert result.error["target"]["gw"] == "pg1"

    def test_resolve_shorthand_found(self):
        cache = PresenceCache()
        cache.update(_make_record(gw="pg1", agent="main", session="sess-1"))
        result = resolve_session_target(cache, gw="pg1", session="sess-1")
        assert result.ok
        assert result.record.agent == "main"

    def test_resolve_shorthand_ambiguous(self):
        cache = PresenceCache()
        cache.update(_make_record(gw="pg1", agent="main", session="sess-1"))
        cache.update(_make_record(gw="pg1", agent="alt", session="sess-1"))
        result = resolve_session_target(cache, gw="pg1", session="sess-1")
        assert not result.ok
        assert result.error["code"] == "TARGET_AMBIGUOUS"

    def test_resolve_shorthand_not_found(self):
        cache = PresenceCache()
        result = resolve_session_target(cache, gw="pg1", session="nonexistent")
        assert not result.ok
        assert result.error["code"] == "SESSION_NOT_FOUND"

    def test_resolve_preserves_corr_and_reply_to(self):
        cache = PresenceCache()
        result = resolve_session_target(
            cache, gw="pg1", agent="main", session="x",
            corr="c-1", reply_to="r-1",
        )
        assert result.error["corr"] == "c-1"
        assert result.error["replyTo"] == "r-1"

    def test_resolve_expired_entry(self):
        cache = PresenceCache()
        r = _make_record(gw="pg1", agent="main", session="sess-1")
        cache.update(r)
        cache._entries[r.key].expiry_ts = time.monotonic() - 1
        result = resolve_session_target(cache, gw="pg1", agent="main", session="sess-1")
        assert not result.ok
        assert result.error["code"] == "SESSION_NOT_FOUND"


# ── Session enumeration (Phase 5) ─────────────────────────────────

class TestReadLastJsonlEntry:
    def test_reads_last_entry(self, tmp_path):
        p = tmp_path / "test.jsonl"
        p.write_text(
            json.dumps({"line": 1}) + "\n"
            + json.dumps({"line": 2, "model": "claude"}) + "\n"
        )
        entry = _read_last_jsonl_entry(p)
        assert entry is not None
        assert entry["line"] == 2
        assert entry["model"] == "claude"

    def test_returns_none_for_missing_file(self, tmp_path):
        assert _read_last_jsonl_entry(tmp_path / "nonexistent.jsonl") is None

    def test_returns_none_for_empty_file(self, tmp_path):
        p = tmp_path / "empty.jsonl"
        p.write_text("")
        assert _read_last_jsonl_entry(p) is None

    def test_skips_invalid_json_lines(self, tmp_path):
        p = tmp_path / "mixed.jsonl"
        p.write_text(
            json.dumps({"valid": True}) + "\n"
            + "not json\n"
        )
        entry = _read_last_jsonl_entry(p)
        assert entry is not None
        assert entry["valid"] is True


class TestExtractSessionMeta:
    def test_extracts_model_from_entry(self):
        result = _extract_session_meta(
            {"model": "claude-sonnet-4-20250514"},
            {},
        )
        assert result["model"] == "claude-sonnet-4-20250514"

    def test_extracts_model_from_nested_message(self):
        result = _extract_session_meta(
            {"message": {"model": "gpt-5.3-codex"}},
            {},
        )
        assert result["model"] == "gpt-5.3-codex"

    def test_falls_back_to_index_model(self):
        result = _extract_session_meta(
            {},
            {"model": "claude-haiku-4-5-20251001"},
        )
        assert result["model"] == "claude-haiku-4-5-20251001"

    def test_defaults_to_unknown(self):
        result = _extract_session_meta({}, {})
        assert result["model"] == "unknown"
        assert result["thinking"] == "unknown"
        assert result["context_tokens"] is None
        assert result["context_window"] is None

    def test_extracts_thinking_string(self):
        result = _extract_session_meta({"thinking": "low"}, {})
        assert result["thinking"] == "low"

    def test_extracts_thinking_dict(self):
        result = _extract_session_meta({"thinking": {"type": "enabled"}}, {})
        assert result["thinking"] == "enabled"

    def test_extracts_context_tokens(self):
        result = _extract_session_meta(
            {"usage": {"inputTokens": 5000}},
            {},
        )
        assert result["context_tokens"] == 5000

    def test_extracts_context_window(self):
        result = _extract_session_meta(
            {"contextWindow": 200000},
            {},
        )
        assert result["context_window"] == 200000


class TestPresenceContentExtraction:
    def test_extract_text_from_content_list(self):
        content = [
            {"type": "thinking", "thinking": "Analyzing"},
            {"type": "text", "text": "Generating response now"},
        ]
        out = _extract_text_from_content(content)
        assert "Analyzing" in out
        assert "Generating response now" in out

    def test_derive_status_running_for_assistant_without_stop_reason(self):
        now_ms = int(time.time() * 1000)
        entry = {"message": {"role": "assistant", "content": [{"type": "text", "text": "Working"}]}}
        status, activity = _derive_status_and_activity(entry, now_ms)
        assert status == "running"
        assert activity == "generating response"

    def test_derive_status_waiting_for_recent_user(self):
        now_ms = int(time.time() * 1000)
        entry = {"message": {"role": "user", "content": [{"type": "text", "text": "please check logs"}]}}
        status, activity = _derive_status_and_activity(entry, now_ms)
        assert status == "waiting"
        assert "awaiting response" in activity


class TestEnumerateActiveSessions:
    def test_no_agents_dir(self, tmp_path):
        assert _enumerate_active_sessions(tmp_path) == []

    def test_empty_sessions_index(self, tmp_path):
        _make_state_dir(tmp_path, sessions={})
        assert _enumerate_active_sessions(tmp_path) == []

    def test_all_stale_sessions(self, tmp_path):
        old_ms = int(time.time() * 1000) - 999999000
        _make_state_dir(tmp_path, sessions={"old-sess": {"updatedAt": old_ms}})
        assert _enumerate_active_sessions(tmp_path, active_window_s=300) == []

    def test_active_sessions_returned(self, tmp_path):
        now_ms = int(time.time() * 1000)
        _make_state_dir(tmp_path, sessions={
            "sess-alpha": {"updatedAt": now_ms - 1000},
            "sess-beta": {"updatedAt": now_ms - 2000},
            "sess-old": {"updatedAt": now_ms - 999999000},
        })
        results = _enumerate_active_sessions(tmp_path, active_window_s=300)
        assert len(results) == 2
        keys = {r["session"] for r in results}
        assert keys == {"sess-alpha", "sess-beta"}

    def test_extracts_model_from_jsonl(self, tmp_path):
        now_ms = int(time.time() * 1000)
        _make_state_dir(
            tmp_path,
            sessions={"my-session": {"updatedAt": now_ms}},
            jsonl_files={"my-session": [
                {"message": {"role": "assistant", "model": "claude-haiku-4-5-20251001", "thinking": "none", "content": [{"type":"text","text":"done"}] }},
            ]},
        )
        results = _enumerate_active_sessions(tmp_path, active_window_s=300)
        assert len(results) == 1
        assert results[0]["model"] == "claude-haiku-4-5-20251001"
        assert results[0]["thinking"] == "none"
        assert results[0]["status"] in {"running", "idle"}

    def test_extracts_context_from_jsonl(self, tmp_path):
        now_ms = int(time.time() * 1000)
        _make_state_dir(
            tmp_path,
            sessions={"ctx-session": {"updatedAt": now_ms}},
            jsonl_files={"ctx-session": [
                {"model": "claude-opus-4-20250514", "usage": {"inputTokens": 12000}, "contextWindow": 200000},
            ]},
        )
        results = _enumerate_active_sessions(tmp_path, active_window_s=300)
        assert results[0]["context_tokens"] == 12000
        assert results[0]["context_window"] == 200000

    def test_missing_jsonl_defaults_to_unknown(self, tmp_path):
        now_ms = int(time.time() * 1000)
        _make_state_dir(tmp_path, sessions={"no-jsonl": {"updatedAt": now_ms}})
        results = _enumerate_active_sessions(tmp_path, active_window_s=300)
        assert len(results) == 1
        assert results[0]["model"] == "unknown"
        assert results[0]["thinking"] == "unknown"
        assert results[0]["context_tokens"] is None
        assert results[0]["context_window"] is None

    def test_multiple_agents(self, tmp_path):
        """Sessions from different agents are all enumerated."""
        now_ms = int(time.time() * 1000)
        # Agent "main"
        _make_state_dir(tmp_path, agent="main", sessions={"sess-1": {"updatedAt": now_ms}})
        # Agent "alt" — create a second agent dir
        alt_dir = tmp_path / "agents" / "alt" / "sessions"
        alt_dir.mkdir(parents=True)
        (alt_dir / "sessions.json").write_text(json.dumps({"sess-2": {"updatedAt": now_ms}}))
        results = _enumerate_active_sessions(tmp_path, active_window_s=300)
        assert len(results) == 2
        agents = {r["agent"] for r in results}
        assert agents == {"main", "alt"}


# ── Build local presence records (Phase 5) ─────────────────────────

class TestBuildLocalPresenceRecords:
    def test_with_oc_instances(self):
        cfg = _make_config(
            node_id="turq",
            oc_instances=[
                OcInstance(name="turq", agent_id="main"),
                OcInstance(name="mini1", agent_id="default"),
            ],
            presence=PresenceConfig(ttl_sec=120),
        )
        records = build_local_presence_records(cfg)
        assert len(records) == 2
        gws = {r.gw for r in records}
        assert gws == {"turq", "mini1"}
        for r in records:
            assert r.ttl_sec == 120
            assert r.state == "active"

    def test_without_oc_instances(self):
        cfg = _make_config(node_id="standalone")
        records = build_local_presence_records(cfg)
        assert len(records) == 1
        assert records[0].gw == "standalone"
        assert records[0].agent == "daemon"
        assert records[0].session == "default"

    def test_agent_defaults_to_main(self):
        cfg = _make_config(
            node_id="turq",
            oc_instances=[OcInstance(name="turq")],
        )
        records = build_local_presence_records(cfg)
        assert records[0].agent == "main"

    def test_real_sessions_produce_multiple_records(self, tmp_path):
        """When real active sessions exist, one record per session is emitted."""
        now_ms = int(time.time() * 1000)
        _make_state_dir(
            tmp_path,
            sessions={
                "sess-1": {"updatedAt": now_ms - 1000},
                "sess-2": {"updatedAt": now_ms - 2000},
            },
            jsonl_files={
                "sess-1": [{"model": "claude-sonnet-4-20250514", "thinking": "low"}],
                "sess-2": [{"model": "claude-opus-4-20250514", "usage": {"inputTokens": 9999}}],
            },
        )
        cfg = _make_config(
            node_id="turq",
            oc_instances=[OcInstance(name="turq")],
            presence=PresenceConfig(ttl_sec=300),
        )
        with patch("hive_daemon.presence._state_dir_for_profile", return_value=tmp_path):
            records = build_local_presence_records(cfg)
        assert len(records) == 2
        by_session = {r.session: r for r in records}
        assert "sess-1" in by_session
        assert "sess-2" in by_session
        assert by_session["sess-1"].model == "claude-sonnet-4-20250514"
        assert by_session["sess-1"].thinking == "low"
        assert by_session["sess-2"].model == "claude-opus-4-20250514"
        assert by_session["sess-2"].context_tokens == 9999
        for r in records:
            assert r.gw == "turq"
            assert r.state == "active"
            assert r.ttl_sec == 300

    def test_fallback_synthetic_when_no_sessions(self):
        """Falls back to synthetic record when no active sessions found."""
        cfg = _make_config(
            node_id="turq",
            oc_instances=[OcInstance(name="turq", agent_id="main")],
        )
        records = build_local_presence_records(cfg)
        assert len(records) == 1
        assert records[0].gw == "turq"
        assert records[0].agent == "main"
        assert records[0].session.startswith("hive-turq-")

    def test_enrichment_fields_default_on_fallback(self):
        """Fallback synthetic records have explicit unknown defaults."""
        cfg = _make_config(
            node_id="turq",
            oc_instances=[OcInstance(name="turq")],
        )
        records = build_local_presence_records(cfg)
        r = records[0]
        assert r.model == "unknown"
        assert r.thinking == "unknown"
        assert r.context_tokens is None
        assert r.context_window is None

    def test_multiple_gateways_with_real_sessions(self, tmp_path):
        """Multiple OC instances each discover their own sessions."""
        now_ms = int(time.time() * 1000)
        # Create two separate state dirs
        turq_dir = tmp_path / "turq_state"
        mini1_dir = tmp_path / "mini1_state"

        _make_state_dir(turq_dir, sessions={"turq-sess": {"updatedAt": now_ms}})
        _make_state_dir(mini1_dir, sessions={
            "mini1-sess-a": {"updatedAt": now_ms},
            "mini1-sess-b": {"updatedAt": now_ms},
        })

        cfg = _make_config(
            node_id="turq",
            oc_instances=[
                OcInstance(name="turq"),
                OcInstance(name="mini1", profile="mini1"),
            ],
            presence=PresenceConfig(ttl_sec=300),
        )

        def _mock_state_dir(profile):
            if profile == "mini1":
                return mini1_dir
            return turq_dir

        with patch("hive_daemon.presence._state_dir_for_profile", side_effect=_mock_state_dir):
            records = build_local_presence_records(cfg)

        assert len(records) == 3
        gw_counts = {}
        for r in records:
            gw_counts[r.gw] = gw_counts.get(r.gw, 0) + 1
        assert gw_counts["turq"] == 1
        assert gw_counts["mini1"] == 2


# ── MQTT topic builder ──────────────────────────────────────────────

class TestPresenceMqttTopic:
    def test_topic_format(self):
        r = _make_record(gw="pg1", agent="main", session="sess-1")
        topic = presence_mqtt_topic("turq/hive", r)
        assert topic == "turq/hive/presence/pg1/main/sess-1"

    def test_custom_prefix(self):
        r = _make_record(gw="x", agent="y", session="z")
        assert presence_mqtt_topic("my/prefix", r) == "my/prefix/presence/x/y/z"
