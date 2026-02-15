"""Tests for the hive protocol v1 message envelope."""

import json
import time

import pytest

from hive_daemon.envelope import (
    SCHEMA_VERSION,
    Envelope,
    EnvelopeError,
    create_envelope,
    create_reply,
)


# --- fixtures ---

VALID_DATA = {
    "v": 1,
    "id": "abc-123",
    "ts": 1771143000,
    "from": "turq-18789",
    "to": "pg1-18890",
    "ch": "command",
    "urgency": "now",
    "text": "do something",
}


def _make(**overrides):
    """Build an Envelope from VALID_DATA with optional overrides."""
    d = {**VALID_DATA, **overrides}
    return Envelope.from_json(d)


# --- construction & validation ---


class TestEnvelopeValidation:
    def test_valid_envelope(self):
        env = _make()
        assert env.v == 1
        assert env.from_ == "turq-18789"
        assert env.to == "pg1-18890"
        assert env.ch == "command"

    def test_bad_version(self):
        with pytest.raises(EnvelopeError, match="unsupported schema version"):
            _make(v=99)

    def test_empty_id(self):
        with pytest.raises(EnvelopeError, match="id is required"):
            _make(id="")

    def test_bad_ts_zero(self):
        with pytest.raises(EnvelopeError, match="ts must be a positive integer"):
            _make(ts=0)

    def test_bad_ts_negative(self):
        with pytest.raises(EnvelopeError, match="ts must be a positive integer"):
            _make(ts=-1)

    def test_empty_from(self):
        data = {**VALID_DATA, "from": ""}
        with pytest.raises(EnvelopeError, match="from is required"):
            Envelope.from_json(data)

    def test_empty_to(self):
        with pytest.raises(EnvelopeError, match="to is required"):
            _make(to="")

    def test_invalid_channel(self):
        with pytest.raises(EnvelopeError, match="invalid channel"):
            _make(ch="bogus")

    def test_invalid_urgency(self):
        with pytest.raises(EnvelopeError, match="invalid urgency"):
            _make(urgency="critical")

    def test_empty_text(self):
        with pytest.raises(EnvelopeError, match="text is required"):
            _make(text="")

    def test_negative_ttl(self):
        with pytest.raises(EnvelopeError, match="ttl must be a non-negative integer"):
            _make(ttl=-5)

    def test_zero_ttl_is_valid(self):
        env = _make(ttl=0)
        assert env.ttl == 0

    def test_all_channels_accepted(self):
        for ch in ("command", "response", "sync", "heartbeat", "status", "alert"):
            env = _make(ch=ch)
            assert env.ch == ch

    def test_both_urgencies_accepted(self):
        for urg in ("now", "later"):
            env = _make(urgency=urg)
            assert env.urgency == urg

    def test_optional_fields_default_none(self):
        env = _make()
        assert env.corr is None
        assert env.reply_to is None
        assert env.ttl is None
        assert env.action is None

    def test_optional_fields_set(self):
        data = {**VALID_DATA, "corr": "corr-1", "replyTo": "msg-0", "ttl": 3600, "action": "git-sync"}
        env = Envelope.from_json(data)
        assert env.corr == "corr-1"
        assert env.reply_to == "msg-0"
        assert env.ttl == 3600
        assert env.action == "git-sync"

    def test_frozen(self):
        env = _make()
        with pytest.raises(AttributeError):
            env.text = "changed"  # type: ignore[misc]


# --- serialization ---


class TestEnvelopeSerialization:
    def test_roundtrip(self):
        original = _make()
        d = original.to_json()
        restored = Envelope.from_json(d)
        assert restored == original

    def test_roundtrip_with_optional_fields(self):
        data = {**VALID_DATA, "corr": "c1", "replyTo": "r1", "ttl": 60, "action": "deploy"}
        original = Envelope.from_json(data)
        d = original.to_json()
        restored = Envelope.from_json(d)
        assert restored == original

    def test_to_json_uses_from_not_from_(self):
        d = _make().to_json()
        assert "from" in d
        assert "from_" not in d

    def test_to_json_uses_replyTo_not_reply_to(self):
        data = {**VALID_DATA, "replyTo": "msg-0"}
        d = Envelope.from_json(data).to_json()
        assert "replyTo" in d
        assert "reply_to" not in d

    def test_to_json_omits_none_optionals(self):
        d = _make().to_json()
        assert "corr" not in d
        assert "replyTo" not in d
        assert "ttl" not in d
        assert "action" not in d

    def test_json_string_roundtrip(self):
        """Verify the dict is JSON-serializable and round-trips through json."""
        env = _make()
        s = json.dumps(env.to_json())
        d = json.loads(s)
        assert Envelope.from_json(d) == env

    def test_missing_required_field(self):
        for field in ("v", "id", "ts", "from", "to", "ch", "urgency", "text"):
            data = {**VALID_DATA}
            del data[field]
            with pytest.raises(EnvelopeError, match="missing required fields"):
                Envelope.from_json(data)


# --- factory functions ---


class TestCreateEnvelope:
    def test_auto_id_and_ts(self):
        before = int(time.time())
        env = create_envelope(from_="a", to="b", ch="command", text="hi")
        after = int(time.time())
        assert env.id  # non-empty UUID
        assert before <= env.ts <= after
        assert env.v == SCHEMA_VERSION

    def test_unique_ids(self):
        e1 = create_envelope(from_="a", to="b", ch="command", text="hi")
        e2 = create_envelope(from_="a", to="b", ch="command", text="hi")
        assert e1.id != e2.id

    def test_defaults(self):
        env = create_envelope(from_="a", to="b", ch="sync", text="x")
        assert env.urgency == "now"
        assert env.corr is None
        assert env.action is None

    def test_with_action(self):
        env = create_envelope(from_="a", to="all", ch="sync", text="updated", action="git-sync")
        assert env.action == "git-sync"


class TestCreateReply:
    def test_reply_sets_correlation(self):
        orig = _make()
        reply = create_reply(orig, from_="pg1-18890", text="done")
        assert reply.to == orig.from_
        assert reply.ch == "response"
        assert reply.corr == orig.id  # original had no corr, so uses id
        assert reply.reply_to == orig.id

    def test_reply_preserves_existing_corr(self):
        orig = _make(corr="existing-corr")
        reply = create_reply(orig, from_="pg1-18890", text="done")
        assert reply.corr == "existing-corr"

    def test_reply_has_unique_id(self):
        orig = _make()
        r1 = create_reply(orig, from_="pg1-18890", text="a")
        r2 = create_reply(orig, from_="pg1-18890", text="b")
        assert r1.id != r2.id
        assert r1.id != orig.id
