"""Tests for the deterministic session presence registry."""

import time
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


# ── Build local presence records ────────────────────────────────────

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


# ── MQTT topic builder ──────────────────────────────────────────────

class TestPresenceMqttTopic:
    def test_topic_format(self):
        r = _make_record(gw="pg1", agent="main", session="sess-1")
        topic = presence_mqtt_topic("turq/hive", r)
        assert topic == "turq/hive/presence/pg1/main/sess-1"

    def test_custom_prefix(self):
        r = _make_record(gw="x", agent="y", session="z")
        assert presence_mqtt_topic("my/prefix", r) == "my/prefix/presence/x/y/z"
