"""Tests for the deterministic session presence registry."""

import time
from unittest.mock import AsyncMock, patch

import pytest

from hive_daemon.presence import (
    DELIVERY_ERROR_CODES,
    PRESENCE_VERSION,
    CacheEntry,
    PresenceCache,
    PresenceRecord,
    ResolveResult,
    TaskSummary,
    _list_sessions_via_api,
    _parse_api_session,
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


# ── PresenceRecord enrichment fields ───────────────────────────────

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


# ── Runtime API session listing ────────────────────────────────────

class TestListSessionsViaApi:
    """Tests for _list_sessions_via_api (runtime API RPC)."""

    @pytest.mark.asyncio
    async def test_successful_list_dict_format(self):
        """API returns {"sessions": [...]} dict format."""
        inst = OcInstance(name="turq")
        mock_sessions = [
            {"sessionId": "s1", "agent": "main", "model": "claude-sonnet"},
            {"sessionId": "s2", "agent": "main", "model": "claude-opus"},
        ]
        with patch(
            "hive_daemon.presence._run_openclaw_json",
            new_callable=AsyncMock,
            return_value=(True, {"sessions": mock_sessions}, ""),
        ):
            ok, sessions, err = await _list_sessions_via_api(inst)
        assert ok
        assert len(sessions) == 2
        assert err == ""

    @pytest.mark.asyncio
    async def test_successful_list_array_format(self):
        """API returns a flat list of sessions."""
        inst = OcInstance(name="turq")
        mock_sessions = [{"sessionId": "s1", "agent": "main"}]
        with patch(
            "hive_daemon.presence._run_openclaw_json",
            new_callable=AsyncMock,
            return_value=(True, mock_sessions, ""),
        ):
            ok, sessions, err = await _list_sessions_via_api(inst)
        assert ok
        assert len(sessions) == 1

    @pytest.mark.asyncio
    async def test_api_unavailable(self):
        inst = OcInstance(name="turq")
        with patch(
            "hive_daemon.presence._run_openclaw_json",
            new_callable=AsyncMock,
            return_value=(False, None, "connection refused"),
        ):
            ok, sessions, err = await _list_sessions_via_api(inst)
        assert not ok
        assert sessions == []
        assert "connection refused" in err

    @pytest.mark.asyncio
    async def test_unexpected_response_format(self):
        inst = OcInstance(name="turq")
        with patch(
            "hive_daemon.presence._run_openclaw_json",
            new_callable=AsyncMock,
            return_value=(True, "not-json-object", ""),
        ):
            ok, sessions, err = await _list_sessions_via_api(inst)
        assert not ok
        assert "unexpected response format" in err

    @pytest.mark.asyncio
    async def test_empty_sessions(self):
        inst = OcInstance(name="turq")
        with patch(
            "hive_daemon.presence._run_openclaw_json",
            new_callable=AsyncMock,
            return_value=(True, {"sessions": []}, ""),
        ):
            ok, sessions, err = await _list_sessions_via_api(inst)
        assert ok
        assert sessions == []

    @pytest.mark.asyncio
    async def test_uses_resolved_openclaw_cmd(self):
        inst = OcInstance(name="turq", openclaw_cmd="/opt/oc/bin/openclaw", profile="turq")
        with patch(
            "hive_daemon.presence._run_openclaw_json",
            new_callable=AsyncMock,
            return_value=(True, [], ""),
        ) as mock_run:
            await _list_sessions_via_api(inst)
        mock_run.assert_called_once_with(
            openclaw_cmd="/opt/oc/bin/openclaw",
            profile="turq",
            args=["sessions", "--all-agents", "--json"],
            timeout_s=10.0,
        )


# ── Parse API session ──────────────────────────────────────────────

class TestParseApiSession:
    def test_basic_fields(self):
        raw = {
            "sessionId": "sess-1",
            "agent": "main",
            "status": "idle",
            "model": "claude-sonnet-4-20250514",
            "thinking": "low",
            "title": "Working on tests",
        }
        parsed = _parse_api_session(raw)
        assert parsed["session"] == "sess-1"
        assert parsed["agent"] == "main"
        assert parsed["status"] == "idle"
        assert parsed["model"] == "claude-sonnet-4-20250514"
        assert parsed["thinking"] == "low"
        assert parsed["summary"] == "Working on tests"

    def test_alternative_field_names(self):
        raw = {
            "session_id": "sess-2",
            "agent_id": "dev",
            "model": "gpt-5",
        }
        parsed = _parse_api_session(raw)
        assert parsed["session"] == "sess-2"
        assert parsed["agent"] == "dev"

    def test_id_field_fallback(self):
        raw = {"id": "sess-3"}
        parsed = _parse_api_session(raw)
        assert parsed["session"] == "sess-3"

    def test_context_from_usage(self):
        raw = {
            "sessionId": "s1",
            "usage": {"inputTokens": 5000},
            "contextWindow": 200000,
        }
        parsed = _parse_api_session(raw)
        assert parsed["context_tokens"] == 5000
        assert parsed["context_window"] == 200000

    def test_context_alternative_keys(self):
        raw = {
            "sessionId": "s1",
            "usage": {"total_tokens": 7500},
            "context_window": 128000,
        }
        parsed = _parse_api_session(raw)
        assert parsed["context_tokens"] == 7500
        assert parsed["context_window"] == 128000

    def test_defaults_for_missing_fields(self):
        parsed = _parse_api_session({})
        assert parsed["session"] == ""
        assert parsed["agent"] == "main"
        assert parsed["model"] == "unknown"
        assert parsed["thinking"] == "unknown"
        assert parsed["status"] == "idle"
        assert parsed["context_tokens"] is None
        assert parsed["context_window"] is None
        assert parsed["summary"] == ""
        assert parsed["activity"] == ""
        assert parsed["cwd"] == ""
        assert parsed["cmdline"] == ""
        assert parsed["url"] == ""

    def test_truncates_long_values(self):
        raw = {
            "sessionId": "s1",
            "title": "x" * 500,
            "cwd": "y" * 500,
            "url": "z" * 1000,
        }
        parsed = _parse_api_session(raw)
        assert len(parsed["summary"]) == 200
        assert len(parsed["cwd"]) == 200
        assert len(parsed["url"]) == 500

    def test_summary_from_title_or_summary(self):
        assert _parse_api_session({"title": "From title"})["summary"] == "From title"
        assert _parse_api_session({"summary": "From summary"})["summary"] == "From summary"
        # title takes precedence
        assert _parse_api_session({"title": "T", "summary": "S"})["summary"] == "T"

    def test_task_fields(self):
        raw = {
            "sessionId": "s1",
            "activity": "generating response",
            "cwd": "/home/turq",
            "cmdline": "pytest tests/ -v",
            "url": "https://github.com/example",
        }
        parsed = _parse_api_session(raw)
        assert parsed["activity"] == "generating response"
        assert parsed["cwd"] == "/home/turq"
        assert parsed["cmdline"] == "pytest tests/ -v"
        assert parsed["url"] == "https://github.com/example"


# ── Build local presence records (runtime API) ─────────────────────

class TestBuildLocalPresenceRecords:

    @pytest.mark.asyncio
    async def test_api_returns_sessions(self):
        """API returns active sessions — one record per session."""
        cfg = _make_config(
            node_id="turq",
            oc_instances=[OcInstance(name="turq")],
            presence=PresenceConfig(ttl_sec=300),
        )
        mock_sessions = [
            {
                "sessionId": "s1", "agent": "main",
                "model": "claude-sonnet-4-20250514", "thinking": "low",
                "status": "idle", "title": "Task A",
            },
            {
                "sessionId": "s2", "agent": "main",
                "model": "claude-opus-4-20250514", "thinking": "high",
                "status": "running", "usage": {"inputTokens": 9999},
                "title": "Task B",
            },
        ]
        with patch(
            "hive_daemon.presence._list_sessions_via_api",
            new_callable=AsyncMock,
            return_value=(True, mock_sessions, ""),
        ):
            records = await build_local_presence_records(cfg)
        assert len(records) == 2
        by_session = {r.session: r for r in records}
        assert "s1" in by_session
        assert "s2" in by_session
        assert by_session["s1"].model == "claude-sonnet-4-20250514"
        assert by_session["s1"].thinking == "low"
        assert by_session["s2"].model == "claude-opus-4-20250514"
        assert by_session["s2"].context_tokens == 9999
        for r in records:
            assert r.gw == "turq"
            assert r.state == "active"
            assert r.ttl_sec == 300

    @pytest.mark.asyncio
    async def test_api_ok_no_sessions_emits_none(self):
        """API reachable but empty — emit no synthetic session records."""
        cfg = _make_config(
            node_id="turq",
            oc_instances=[OcInstance(name="turq", agent_id="main")],
        )
        with patch(
            "hive_daemon.presence._list_sessions_via_api",
            new_callable=AsyncMock,
            return_value=(True, [], ""),
        ):
            records = await build_local_presence_records(cfg)
        assert records == []

    @pytest.mark.asyncio
    async def test_api_unavailable_emits_none(self):
        """API failure — emit no synthetic records (runtime snapshot only)."""
        cfg = _make_config(
            node_id="turq",
            oc_instances=[OcInstance(name="turq", agent_id="main")],
        )
        with patch(
            "hive_daemon.presence._list_sessions_via_api",
            new_callable=AsyncMock,
            return_value=(False, [], "OpenClaw CLI not found: openclaw"),
        ):
            records = await build_local_presence_records(cfg)
        assert records == []

    @pytest.mark.asyncio
    async def test_standalone_daemon_no_api_call(self):
        """Standalone daemon (no OC instances) emits node-level record."""
        cfg = _make_config(node_id="standalone")
        records = await build_local_presence_records(cfg)
        assert len(records) == 1
        assert records[0].gw == "standalone"
        assert records[0].agent == "daemon"
        assert records[0].session == "default"

    @pytest.mark.asyncio
    async def test_api_unavailable_has_no_records_to_enrich(self):
        """No synthetic error records are emitted for unavailable API."""
        cfg = _make_config(
            node_id="turq",
            oc_instances=[OcInstance(name="turq")],
        )
        with patch(
            "hive_daemon.presence._list_sessions_via_api",
            new_callable=AsyncMock,
            return_value=(False, [], "timeout"),
        ):
            records = await build_local_presence_records(cfg)
        assert records == []

    @pytest.mark.asyncio
    async def test_multiple_gateways(self):
        """Multiple OC instances each get their own API call."""
        cfg = _make_config(
            node_id="turq",
            oc_instances=[
                OcInstance(name="turq"),
                OcInstance(name="mini1", profile="mini1"),
            ],
            presence=PresenceConfig(ttl_sec=300),
        )
        turq_sessions = [{"sessionId": "turq-s1", "agent": "main", "model": "claude-sonnet"}]
        mini1_sessions = [
            {"sessionId": "mini1-a", "agent": "main", "model": "claude-haiku"},
            {"sessionId": "mini1-b", "agent": "main", "model": "claude-opus"},
        ]

        call_count = 0

        async def mock_list(inst, *, timeout_s=10.0):
            nonlocal call_count
            call_count += 1
            if inst.name == "turq":
                return True, turq_sessions, ""
            return True, mini1_sessions, ""

        with patch("hive_daemon.presence._list_sessions_via_api", side_effect=mock_list):
            records = await build_local_presence_records(cfg)

        assert call_count == 2
        assert len(records) == 3
        gw_counts: dict[str, int] = {}
        for r in records:
            gw_counts[r.gw] = gw_counts.get(r.gw, 0) + 1
        assert gw_counts["turq"] == 1
        assert gw_counts["mini1"] == 2

    @pytest.mark.asyncio
    async def test_skips_sessions_with_empty_id(self):
        """Sessions with empty id field are skipped."""
        cfg = _make_config(
            node_id="turq",
            oc_instances=[OcInstance(name="turq")],
        )
        mock_sessions = [
            {"sessionId": "valid", "agent": "main"},
            {"agent": "main"},  # no session id
            {"sessionId": "", "agent": "main"},  # empty session id
        ]
        with patch(
            "hive_daemon.presence._list_sessions_via_api",
            new_callable=AsyncMock,
            return_value=(True, mock_sessions, ""),
        ):
            records = await build_local_presence_records(cfg)
        assert len(records) == 1
        assert records[0].session == "valid"

    @pytest.mark.asyncio
    async def test_skips_non_dict_entries(self):
        """Non-dict entries in API response are ignored."""
        cfg = _make_config(
            node_id="turq",
            oc_instances=[OcInstance(name="turq")],
        )
        mock_sessions = [
            {"sessionId": "valid", "agent": "main"},
            "not-a-dict",
            42,
            None,
        ]
        with patch(
            "hive_daemon.presence._list_sessions_via_api",
            new_callable=AsyncMock,
            return_value=(True, mock_sessions, ""),
        ):
            records = await build_local_presence_records(cfg)
        assert len(records) == 1
        assert records[0].session == "valid"

    @pytest.mark.asyncio
    async def test_ttl_from_config(self):
        """Records use ttl from presence config."""
        cfg = _make_config(
            node_id="turq",
            oc_instances=[OcInstance(name="turq")],
            presence=PresenceConfig(ttl_sec=120),
        )
        with patch(
            "hive_daemon.presence._list_sessions_via_api",
            new_callable=AsyncMock,
            return_value=(True, [{"sessionId": "s1", "agent": "main"}], ""),
        ):
            records = await build_local_presence_records(cfg)
        assert records[0].ttl_sec == 120


# ── MQTT topic builder ──────────────────────────────────────────────

class TestPresenceMqttTopic:
    def test_topic_format(self):
        r = _make_record(gw="pg1", agent="main", session="sess-1")
        topic = presence_mqtt_topic("turq/hive", r)
        assert topic == "turq/hive/presence/pg/gw/pg1/sess-1"

    def test_custom_prefix(self):
        r = _make_record(gw="x", agent="y", session="z")
        assert presence_mqtt_topic("my/prefix", r) == "my/prefix/presence/x/gw/x/z"
