"""Tests for hive-cli commands: send, reply, status, roster."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from click.testing import CliRunner

from hive_daemon.config import HiveConfig, MqttConfig
from hive_daemon.envelope import Envelope, create_envelope
from hive_cli.main import cli


@pytest.fixture
def runner():
    return CliRunner()


@pytest.fixture
def config_file(tmp_path: Path) -> Path:
    """Write a minimal hive.toml and return its path."""
    cfg = tmp_path / "hive.toml"
    cfg.write_text(
        '[node]\n'
        'id = "test-node-1"\n'
        'topic_prefix = "turq/hive"\n'
        '\n'
        '[mqtt]\n'
        'host = "localhost"\n'
        'port = 1883\n'
    )
    return cfg


# ── helpers ─────────────────────────────────────────────────────────

def _make_mock_client():
    """Create a mock aiomqtt.Client that supports async context manager."""
    client = AsyncMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=False)
    client.publish = AsyncMock()
    return client


def _sample_envelope_json() -> dict:
    """A valid envelope dict for testing reply."""
    return {
        "v": 1,
        "id": "orig-uuid-1234",
        "ts": 1700000000,
        "from": "sender-node",
        "to": "test-node-1",
        "ch": "command",
        "urgency": "now",
        "text": "do something",
    }


# ── send command ────────────────────────────────────────────────────

class TestSendCommand:

    @patch("hive_cli.commands._mqtt_client")
    def test_send_basic(self, mock_client_fn, runner, config_file):
        client = _make_mock_client()
        mock_client_fn.return_value = client

        result = runner.invoke(cli, [
            "--config", str(config_file),
            "send",
            "--to", "peer-node",
            "--ch", "command",
            "--text", "hello world",
        ])

        assert result.exit_code == 0, result.output
        assert "sent" in result.output
        assert "turq/hive/peer-node/command" in result.output

        # Verify publish was called
        client.publish.assert_called_once()
        call_args = client.publish.call_args
        topic = call_args.args[0] if call_args.args else call_args.kwargs.get("topic")
        assert topic == "turq/hive/peer-node/command"

        # Verify payload is a valid envelope
        payload_bytes = call_args.args[1] if len(call_args.args) > 1 else call_args.kwargs.get("payload")
        payload = json.loads(payload_bytes.decode())
        assert payload["from"] == "test-node-1"
        assert payload["to"] == "peer-node"
        assert payload["ch"] == "command"
        assert payload["text"] == "hello world"
        assert payload["urgency"] == "now"
        assert payload["v"] == 1
        assert "id" in payload
        assert "ts" in payload

    @patch("hive_cli.commands._mqtt_client")
    def test_send_with_action(self, mock_client_fn, runner, config_file):
        client = _make_mock_client()
        mock_client_fn.return_value = client

        result = runner.invoke(cli, [
            "--config", str(config_file),
            "send",
            "--to", "peer-node",
            "--ch", "command",
            "--text", "run sync",
            "--action", "git-sync",
        ])

        assert result.exit_code == 0, result.output
        payload = json.loads(client.publish.call_args.args[1].decode())
        assert payload["action"] == "git-sync"

    @patch("hive_cli.commands._mqtt_client")
    def test_send_with_urgency_and_ttl(self, mock_client_fn, runner, config_file):
        client = _make_mock_client()
        mock_client_fn.return_value = client

        result = runner.invoke(cli, [
            "--config", str(config_file),
            "send",
            "--to", "peer-node",
            "--ch", "sync",
            "--text", "file update",
            "--urgency", "later",
            "--ttl", "60",
        ])

        assert result.exit_code == 0, result.output
        payload = json.loads(client.publish.call_args.args[1].decode())
        assert payload["urgency"] == "later"
        assert payload["ttl"] == 60

    @patch("hive_cli.commands._mqtt_client")
    def test_send_to_all(self, mock_client_fn, runner, config_file):
        client = _make_mock_client()
        mock_client_fn.return_value = client

        result = runner.invoke(cli, [
            "--config", str(config_file),
            "send",
            "--to", "all",
            "--ch", "command",
            "--text", "broadcast",
        ])

        assert result.exit_code == 0, result.output
        topic = client.publish.call_args.args[0]
        assert topic == "turq/hive/all/command"

    def test_send_invalid_channel(self, runner, config_file):
        result = runner.invoke(cli, [
            "--config", str(config_file),
            "send",
            "--to", "peer-node",
            "--ch", "invalid",
            "--text", "bad",
        ])
        assert result.exit_code != 0
        assert "invalid" in result.output.lower() or "Invalid value" in result.output

    def test_send_missing_required(self, runner, config_file):
        result = runner.invoke(cli, [
            "--config", str(config_file),
            "send",
            "--to", "peer-node",
        ])
        assert result.exit_code != 0


# ── reply command ───────────────────────────────────────────────────

class TestReplyCommand:

    @patch("hive_cli.commands._mqtt_client")
    def test_reply_from_json_string(self, mock_client_fn, runner, config_file):
        client = _make_mock_client()
        mock_client_fn.return_value = client

        original = json.dumps(_sample_envelope_json())
        result = runner.invoke(cli, [
            "--config", str(config_file),
            "reply",
            "--to-msg", original,
            "--text", "ack",
        ])

        assert result.exit_code == 0, result.output
        assert "reply" in result.output
        assert "corr=" in result.output

        payload = json.loads(client.publish.call_args.args[1].decode())
        assert payload["ch"] == "response"
        assert payload["to"] == "sender-node"
        assert payload["from"] == "test-node-1"
        assert payload["corr"] == "orig-uuid-1234"
        assert payload["replyTo"] == "orig-uuid-1234"
        assert payload["text"] == "ack"

    @patch("hive_cli.commands._mqtt_client")
    def test_reply_from_file(self, mock_client_fn, runner, config_file, tmp_path):
        client = _make_mock_client()
        mock_client_fn.return_value = client

        msg_file = tmp_path / "msg.json"
        msg_file.write_text(json.dumps(_sample_envelope_json()))

        result = runner.invoke(cli, [
            "--config", str(config_file),
            "reply",
            "--to-msg", str(msg_file),
            "--text", "done",
        ])

        assert result.exit_code == 0, result.output
        payload = json.loads(client.publish.call_args.args[1].decode())
        assert payload["to"] == "sender-node"
        assert payload["text"] == "done"

    @patch("hive_cli.commands._mqtt_client")
    def test_reply_uses_corr_from_original(self, mock_client_fn, runner, config_file):
        """When original has a corr field, reply preserves it."""
        client = _make_mock_client()
        mock_client_fn.return_value = client

        env = _sample_envelope_json()
        env["corr"] = "conversation-123"

        result = runner.invoke(cli, [
            "--config", str(config_file),
            "reply",
            "--to-msg", json.dumps(env),
            "--text", "noted",
        ])

        assert result.exit_code == 0, result.output
        payload = json.loads(client.publish.call_args.args[1].decode())
        assert payload["corr"] == "conversation-123"

    def test_reply_invalid_json(self, runner, config_file):
        result = runner.invoke(cli, [
            "--config", str(config_file),
            "reply",
            "--to-msg", "not valid json",
            "--text", "ack",
        ])
        assert result.exit_code != 0

    @patch("hive_cli.commands._mqtt_client")
    def test_reply_publishes_to_response_channel(self, mock_client_fn, runner, config_file):
        client = _make_mock_client()
        mock_client_fn.return_value = client

        result = runner.invoke(cli, [
            "--config", str(config_file),
            "reply",
            "--to-msg", json.dumps(_sample_envelope_json()),
            "--text", "ok",
        ])

        assert result.exit_code == 0, result.output
        topic = client.publish.call_args.args[0]
        assert topic == "turq/hive/sender-node/response"

    @patch("hive_cli.commands._mqtt_client")
    @patch("hive_daemon.session_map.put")
    def test_reply_with_session_flag(self, mock_session_put, mock_client_fn, runner, config_file):
        """When --session is provided, reply stores mapping keyed by corr (for response routing)."""
        client = _make_mock_client()
        mock_client_fn.return_value = client

        original = _sample_envelope_json()
        original["ttl"] = 7200  # 2 hours

        result = runner.invoke(cli, [
            "--config", str(config_file),
            "reply",
            "--to-msg", json.dumps(original),
            "--text", "acknowledged",
            "--session", "my-session-key",
        ])

        assert result.exit_code == 0, result.output
        assert "session map:" in result.output
        assert "my-session-key" in result.output

        # Verify session_map.put was called with the corr field (what future responses will carry)
        mock_session_put.assert_called_once()
        call_args = mock_session_put.call_args
        corr_id = call_args.args[0]
        session = call_args.args[1]
        ttl = call_args.kwargs.get("ttl")

        # The mapping key should be the corr field from the reply envelope
        # (which equals original.id since original has no corr)
        assert corr_id == "orig-uuid-1234"
        assert session == "my-session-key"
        assert ttl == 7200  # inherited from original envelope's ttl

    @patch("hive_cli.commands._mqtt_client")
    @patch("hive_daemon.session_map.put")
    def test_reply_with_session_no_ttl_in_original(self, mock_session_put, mock_client_fn, runner, config_file):
        """When --session is provided but original has no ttl, default to 3600."""
        client = _make_mock_client()
        mock_client_fn.return_value = client

        original = _sample_envelope_json()  # no ttl field

        result = runner.invoke(cli, [
            "--config", str(config_file),
            "reply",
            "--to-msg", json.dumps(original),
            "--text", "done",
            "--session", "test-session",
        ])

        assert result.exit_code == 0, result.output
        mock_session_put.assert_called_once()
        ttl = mock_session_put.call_args.kwargs.get("ttl")
        assert ttl == 3600  # default ttl


# ── status command ──────────────────────────────────────────────────

class TestStatusCommand:

    @patch("hive_cli.commands._read_retained")
    def test_status_with_nodes(self, mock_read, runner, config_file):
        mock_read.return_value = [
            {"node_id": "turq-box", "status": "online", "last_seen": "2026-02-15T10:00:00Z"},
            {"node_id": "pg1-box", "status": "online", "last_seen": "2026-02-15T09:59:55Z"},
        ]

        result = runner.invoke(cli, [
            "--config", str(config_file),
            "status",
        ])

        assert result.exit_code == 0, result.output
        assert "turq-box" in result.output
        assert "pg1-box" in result.output
        assert "online" in result.output
        assert "NODE" in result.output  # header

    @patch("hive_cli.commands._read_retained")
    def test_status_empty(self, mock_read, runner, config_file):
        mock_read.return_value = []

        result = runner.invoke(cli, [
            "--config", str(config_file),
            "status",
        ])

        assert result.exit_code == 0
        assert "no nodes reporting status" in result.output

    @patch("hive_cli.commands._read_retained")
    def test_status_calls_correct_topic(self, mock_read, runner, config_file):
        mock_read.return_value = []

        runner.invoke(cli, [
            "--config", str(config_file),
            "status",
        ])

        mock_read.assert_called_once()
        args = mock_read.call_args
        cfg = args.args[0] if args.args else args.kwargs.get("cfg")
        topic = args.args[1] if len(args.args) > 1 else args.kwargs.get("topic_filter")
        assert topic == "turq/hive/meta/+/state"

    @patch("hive_cli.commands._read_retained")
    @patch("hive_cli.commands.time")
    def test_status_detects_stale_nodes(self, mock_time, mock_read, runner, config_file):
        """Nodes with last_seen > 30s ago should be marked as 'stale'."""
        current_time = 1700000100.0
        mock_time.time.return_value = current_time

        mock_read.return_value = [
            {"node_id": "fresh-node", "status": "online", "last_seen": int(current_time - 10)},
            {"node_id": "stale-node", "status": "online", "last_seen": int(current_time - 60)},
        ]

        result = runner.invoke(cli, [
            "--config", str(config_file),
            "status",
        ])

        assert result.exit_code == 0, result.output
        # Fresh node should keep its status
        assert "fresh-node" in result.output
        # Stale node should be marked as stale regardless of reported status
        assert "stale-node" in result.output
        assert "stale" in result.output

    @patch("hive_cli.commands._read_retained")
    @patch("hive_cli.commands.time")
    def test_status_human_readable_age_seconds(self, mock_time, mock_read, runner, config_file):
        """Recent timestamps should show as 'Xs ago'."""
        current_time = 1700000100.0
        mock_time.time.return_value = current_time

        mock_read.return_value = [
            {"node_id": "recent-node", "status": "online", "last_seen": int(current_time - 15)},
        ]

        result = runner.invoke(cli, [
            "--config", str(config_file),
            "status",
        ])

        assert result.exit_code == 0, result.output
        assert "15s ago" in result.output

    @patch("hive_cli.commands._read_retained")
    @patch("hive_cli.commands.time")
    def test_status_human_readable_age_minutes(self, mock_time, mock_read, runner, config_file):
        """Timestamps < 1h ago should show as 'Xm ago'."""
        current_time = 1700000100.0
        mock_time.time.return_value = current_time

        mock_read.return_value = [
            {"node_id": "old-node", "status": "online", "last_seen": int(current_time - 300)},
        ]

        result = runner.invoke(cli, [
            "--config", str(config_file),
            "status",
        ])

        assert result.exit_code == 0, result.output
        assert "5m ago" in result.output

    @patch("hive_cli.commands._read_retained")
    @patch("hive_cli.commands.time")
    def test_status_human_readable_age_hours(self, mock_time, mock_read, runner, config_file):
        """Timestamps < 1d ago should show as 'Xh ago'."""
        current_time = 1700000100.0
        mock_time.time.return_value = current_time

        mock_read.return_value = [
            {"node_id": "older-node", "status": "online", "last_seen": int(current_time - 7200)},
        ]

        result = runner.invoke(cli, [
            "--config", str(config_file),
            "status",
        ])

        assert result.exit_code == 0, result.output
        assert "2h ago" in result.output

    @patch("hive_cli.commands._read_retained")
    @patch("hive_cli.commands.time")
    def test_status_human_readable_age_days(self, mock_time, mock_read, runner, config_file):
        """Timestamps >= 1d ago should show as 'Xd ago'."""
        current_time = 1700000100.0
        mock_time.time.return_value = current_time

        mock_read.return_value = [
            {"node_id": "ancient-node", "status": "online", "last_seen": int(current_time - 172800)},
        ]

        result = runner.invoke(cli, [
            "--config", str(config_file),
            "status",
        ])

        assert result.exit_code == 0, result.output
        assert "2d ago" in result.output

    @patch("hive_cli.commands._read_retained")
    def test_status_non_integer_last_seen(self, mock_read, runner, config_file):
        """Non-integer last_seen values should display as-is without crashing."""
        mock_read.return_value = [
            {"node_id": "weird-node", "status": "online", "last_seen": "some-string"},
        ]

        result = runner.invoke(cli, [
            "--config", str(config_file),
            "status",
        ])

        assert result.exit_code == 0, result.output
        assert "weird-node" in result.output
        assert "some-string" in result.output

    @patch("hive_cli.commands._read_retained")
    def test_status_shows_openclaw_command_column(self, mock_read, runner, config_file):
        mock_read.return_value = [
            {
                "node_id": "mini1",
                "status": "online",
                "last_seen": 1700000100,
                "oc": {"gw": {"rpcOk": True, "openclawCmd": "/opt/mini1/bin/openclaw"}},
            },
        ]

        result = runner.invoke(cli, [
            "--config", str(config_file),
            "status",
        ])

        assert result.exit_code == 0, result.output
        assert "OC_CMD" in result.output
        assert "openclaw" in result.output


# ── roster command ──────────────────────────────────────────────────

class TestRosterCommand:

    @patch("hive_cli.commands._read_retained")
    def test_roster_with_handlers(self, mock_read, runner, config_file):
        mock_read.return_value = [
            {"node_id": "turq-box", "handlers": ["git-sync", "health-check"]},
            {"node_id": "pg1-box", "handlers": ["git-sync"]},
        ]

        result = runner.invoke(cli, [
            "--config", str(config_file),
            "roster",
        ])

        assert result.exit_code == 0, result.output
        assert "turq-box" in result.output
        assert "git-sync" in result.output
        assert "health-check" in result.output
        assert "pg1-box" in result.output

    @patch("hive_cli.commands._read_retained")
    def test_roster_empty(self, mock_read, runner, config_file):
        mock_read.return_value = []

        result = runner.invoke(cli, [
            "--config", str(config_file),
            "roster",
        ])

        assert result.exit_code == 0
        assert "no roster data available" in result.output

    @patch("hive_cli.commands._read_retained")
    def test_roster_node_with_no_handlers(self, mock_read, runner, config_file):
        mock_read.return_value = [
            {"node_id": "bare-node", "handlers": []},
        ]

        result = runner.invoke(cli, [
            "--config", str(config_file),
            "roster",
        ])

        assert result.exit_code == 0, result.output
        assert "bare-node" in result.output
        assert "(no handlers)" in result.output

    @patch("hive_cli.commands._read_retained")
    def test_roster_calls_correct_topic(self, mock_read, runner, config_file):
        mock_read.return_value = []

        runner.invoke(cli, [
            "--config", str(config_file),
            "roster",
        ])

        mock_read.assert_called_once()
        args = mock_read.call_args
        topic = args.args[1] if len(args.args) > 1 else args.kwargs.get("topic_filter")
        assert topic == "turq/hive/meta/+/roster"


# ── config loading ──────────────────────────────────────────────────

class TestConfigLoading:

    def test_missing_config_file(self, runner, tmp_path):
        result = runner.invoke(cli, [
            "--config", str(tmp_path / "nonexistent.toml"),
            "status",
        ])
        assert result.exit_code != 0

    @patch("hive_cli.commands._read_retained")
    def test_custom_topic_prefix(self, mock_read, runner, tmp_path):
        cfg = tmp_path / "custom.toml"
        cfg.write_text(
            '[node]\n'
            'id = "custom-node"\n'
            'topic_prefix = "myorg/hive"\n'
            '\n'
            '[mqtt]\n'
            'host = "localhost"\n'
        )
        mock_read.return_value = []

        runner.invoke(cli, [
            "--config", str(cfg),
            "status",
        ])

        args = mock_read.call_args
        topic = args.args[1]
        assert topic == "myorg/hive/meta/+/state"


# ── envelope construction ──────────────────────────────────────────

class TestEnvelopeConstruction:

    @patch("hive_cli.commands._mqtt_client")
    def test_send_auto_generates_id_and_ts(self, mock_client_fn, runner, config_file):
        client = _make_mock_client()
        mock_client_fn.return_value = client

        result = runner.invoke(cli, [
            "--config", str(config_file),
            "send",
            "--to", "peer",
            "--ch", "command",
            "--text", "test",
        ])

        assert result.exit_code == 0
        payload = json.loads(client.publish.call_args.args[1].decode())
        assert len(payload["id"]) == 36  # UUID4 format
        assert isinstance(payload["ts"], int)
        assert payload["ts"] > 0

    @patch("hive_cli.commands._mqtt_client")
    def test_send_omits_none_optional_fields(self, mock_client_fn, runner, config_file):
        client = _make_mock_client()
        mock_client_fn.return_value = client

        runner.invoke(cli, [
            "--config", str(config_file),
            "send",
            "--to", "peer",
            "--ch", "command",
            "--text", "test",
        ])

        payload = json.loads(client.publish.call_args.args[1].decode())
        assert "action" not in payload
        assert "ttl" not in payload
        assert "corr" not in payload
        assert "replyTo" not in payload


# ── session-targeted send ──────────────────────────────────────────

class TestSessionTargetSend:

    @patch("hive_cli.commands._mqtt_client")
    @patch("hive_cli.commands._read_presence")
    def test_to_session_resolved(self, mock_read_presence, mock_client_fn, runner, config_file):
        """--to-session with a fresh presence record resolves and sends."""
        from hive_daemon.presence import PresenceCache, PresenceRecord

        cache = PresenceCache()
        cache.update(PresenceRecord(
            gw="pg1", agent="main", session="sess-1",
            state="active", status="idle",
            updated_ts=1000, ttl_sec=300,
        ))
        mock_read_presence.return_value = cache

        client = _make_mock_client()
        mock_client_fn.return_value = client

        result = runner.invoke(cli, [
            "--config", str(config_file),
            "send",
            "--to-session", "pg1/main/sess-1",
            "--ch", "command",
            "--text", "hello",
        ])

        assert result.exit_code == 0, result.output
        assert "sent" in result.output
        # Should route to the resolved gateway
        assert "turq/hive/pg1/command" in result.output
        payload = json.loads(client.publish.call_args.args[1].decode())
        assert payload["targetSession"] == "sess-1"

    @patch("hive_cli.commands._read_presence")
    def test_to_session_not_found(self, mock_read_presence, runner, config_file):
        """--to-session with missing session produces delivery_error."""
        from hive_daemon.presence import PresenceCache

        mock_read_presence.return_value = PresenceCache()

        result = runner.invoke(cli, [
            "--config", str(config_file),
            "send",
            "--to-session", "pg1/main/nonexistent",
            "--ch", "command",
            "--text", "hello",
        ])

        assert result.exit_code != 0
        assert "SESSION_NOT_FOUND" in result.output

    @patch("hive_cli.commands._mqtt_client")
    @patch("hive_cli.commands._read_presence")
    def test_to_session_shorthand(self, mock_read_presence, mock_client_fn, runner, config_file):
        """--to-session with gw/session shorthand resolves agent automatically."""
        from hive_daemon.presence import PresenceCache, PresenceRecord

        cache = PresenceCache()
        cache.update(PresenceRecord(
            gw="pg1", agent="main", session="sess-1",
            updated_ts=1000, ttl_sec=300,
        ))
        mock_read_presence.return_value = cache

        client = _make_mock_client()
        mock_client_fn.return_value = client

        result = runner.invoke(cli, [
            "--config", str(config_file),
            "send",
            "--to-session", "pg1/sess-1",
            "--ch", "command",
            "--text", "hello",
        ])

        assert result.exit_code == 0, result.output

    @patch("hive_cli.commands._read_presence")
    def test_to_session_ambiguous(self, mock_read_presence, runner, config_file):
        """Ambiguous shorthand produces TARGET_AMBIGUOUS error."""
        from hive_daemon.presence import PresenceCache, PresenceRecord

        cache = PresenceCache()
        cache.update(PresenceRecord(
            gw="pg1", agent="main", session="sess-1",
            updated_ts=1000, ttl_sec=300,
        ))
        cache.update(PresenceRecord(
            gw="pg1", agent="alt", session="sess-1",
            updated_ts=1000, ttl_sec=300,
        ))
        mock_read_presence.return_value = cache

        result = runner.invoke(cli, [
            "--config", str(config_file),
            "send",
            "--to-session", "pg1/sess-1",
            "--ch", "command",
            "--text", "hello",
        ])

        assert result.exit_code != 0
        assert "TARGET_AMBIGUOUS" in result.output

    def test_to_session_and_to_mutual_exclusion(self, runner, config_file):
        """Cannot use both --to and --to-session."""
        result = runner.invoke(cli, [
            "--config", str(config_file),
            "send",
            "--to", "pg1",
            "--to-session", "pg1/main/sess-1",
            "--ch", "command",
            "--text", "hello",
        ])

        assert result.exit_code != 0
        assert "Cannot use both" in result.output

    def test_neither_to_nor_to_session(self, runner, config_file):
        """Must provide at least one of --to or --to-session."""
        result = runner.invoke(cli, [
            "--config", str(config_file),
            "send",
            "--ch", "command",
            "--text", "hello",
        ])

        assert result.exit_code != 0
        assert "required" in result.output.lower() or "Either" in result.output


# ── sessions command ───────────────────────────────────────────────

class TestSessionsCommand:

    @patch("hive_cli.commands._read_presence")
    def test_sessions_json(self, mock_read_presence, runner, config_file):
        from hive_daemon.presence import PresenceCache, PresenceRecord, RecentMessage

        cache = PresenceCache()
        cache.update(PresenceRecord(
            gw="pg1",
            agent="hermes",
            session="sess-1",
            session_type="hermes",
            history_available=True,
            recent_messages=(RecentMessage(role="user", text="hello"),),
            updated_ts=1000,
            ttl_sec=300,
        ))
        mock_read_presence.return_value = cache

        result = runner.invoke(cli, [
            "--config", str(config_file),
            "sessions",
            "--json",
        ])

        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload[0]["gw"] == "pg1"
        assert payload[0]["agent"] == "hermes"
        assert payload[0]["sessionType"] == "hermes"
        assert payload[0]["historyAvailable"] is True
        assert payload[0]["recentMessages"][0]["text"] == "hello"

    @patch("hive_cli.commands._read_presence")
    def test_sessions_human_output(self, mock_read_presence, runner, config_file):
        from hive_daemon.presence import PresenceCache, PresenceRecord, RecentMessage, TaskSummary

        cache = PresenceCache()
        cache.update(PresenceRecord(
            gw="pg1",
            agent="main",
            session="sess-1",
            session_type="claude",
            model="claude-sonnet-4-6",
            status="busy",
            task=TaskSummary(summary="Investigating history"),
            recent_messages=(RecentMessage(role="assistant", text="latest reply"),),
            updated_ts=1000,
            ttl_sec=300,
        ))
        mock_read_presence.return_value = cache

        result = runner.invoke(cli, [
            "--config", str(config_file),
            "sessions",
        ])

        assert result.exit_code == 0, result.output
        assert "SESSION" in result.output
        assert "claude" in result.output
        assert "Investigating history" in result.output
        assert "assistant: latest reply" in result.output


# ── session-history command ───────────────────────────────────────

class TestSessionHistoryCommand:

    @patch("hive_cli.commands.read_local_session_history")
    @patch("hive_cli.commands._read_presence")
    def test_session_history_json(self, mock_read_presence, mock_history, runner, config_file):
        from hive_daemon.presence import PresenceCache, PresenceRecord

        cache = PresenceCache()
        cache.update(PresenceRecord(
            gw="pg1",
            agent="hermes",
            session="sess-1",
            updated_ts=1000,
            ttl_sec=300,
        ))
        mock_read_presence.return_value = cache
        mock_history.return_value = [{"role": "user", "text": "hello", "ts": "2026-03-31T00:00:00Z"}]

        result = runner.invoke(cli, [
            "--config", str(config_file),
            "session-history",
            "pg1/hermes/sess-1",
            "--json",
        ])

        assert result.exit_code == 0, result.output
        payload = json.loads(result.output)
        assert payload[0]["role"] == "user"
        mock_history.assert_called_once()

    @patch("hive_cli.commands.read_local_session_history")
    @patch("hive_cli.commands._read_presence")
    def test_session_history_missing_local_data(self, mock_read_presence, mock_history, runner, config_file):
        from hive_daemon.presence import PresenceCache, PresenceRecord

        cache = PresenceCache()
        cache.update(PresenceRecord(
            gw="pg1",
            agent="main",
            session="sess-1",
            updated_ts=1000,
            ttl_sec=300,
        ))
        mock_read_presence.return_value = cache
        mock_history.return_value = []

        result = runner.invoke(cli, [
            "--config", str(config_file),
            "session-history",
            "pg1/main/sess-1",
        ])

        assert result.exit_code != 0
        assert "not available locally" in result.output


# ── discord command group ───────────────────────────────────────────

class TestDiscordCommands:

    @patch("hive_cli.commands._publish_and_wait")
    def test_discord_thread_send(self, mock_wait, runner, config_file):
        response = create_envelope(
            from_="pg1",
            to="test-node-1",
            ch="response",
            text=json.dumps({"ok": True, "message": {"id": "m1"}}),
            urgency="now",
            corr="c1",
        )
        mock_wait.return_value = response

        result = runner.invoke(cli, [
            "--config", str(config_file),
            "discord", "thread-send",
            "--to", "pg1",
            "--thread-id", "123",
            "--content", "hello",
            "--wait", "15",
        ])

        assert result.exit_code == 0, result.output
        assert '"ok": true' in result.output.lower()
        assert mock_wait.called
        args, kwargs = mock_wait.call_args
        assert kwargs == {}
        assert args[4] == 15.0

    @patch("hive_cli.commands._publish_and_wait")
    def test_discord_mention_resolve_requires_input(self, mock_wait, runner, config_file):
        result = runner.invoke(cli, [
            "--config", str(config_file),
            "discord", "mention-resolve",
            "--to", "pg1",
        ])

        assert result.exit_code != 0
        assert "Provide at least one" in result.output
        assert not mock_wait.called

    @patch("hive_cli.commands._publish_and_wait")
    def test_hive_thread_list_defaults(self, mock_wait, runner, config_file):
        response = create_envelope(
            from_="pg1",
            to="test-node-1",
            ch="response",
            text=json.dumps({
                "ok": True,
                "parent_suffix": "-hive",
                "thread_suffix": "-proj",
                "threads": [
                    {"id": "t1", "name": "qmd-proj", "parent_name": "qmd-hive", "mention_user_id": "123"}
                ],
            }),
            urgency="now",
            corr="c2",
        )
        mock_wait.return_value = response

        result = runner.invoke(cli, [
            "--config", str(config_file),
            "hive-thread-list",
            "--to", "pg1",
            "--wait", "10",
        ])
        assert result.exit_code == 0, result.output
        assert "qmd-proj" in result.output
        assert "123" in result.output

    @patch("hive_cli.commands._publish_and_wait")
    def test_hive_thread_send_auto_mentions(self, mock_wait, runner, config_file):
        resolve = create_envelope(
            from_="pg1",
            to="test-node-1",
            ch="response",
            text=json.dumps({"ok": True, "thread": {"mention": "<@123>"}}),
            urgency="now",
            corr="r1",
        )
        sent = create_envelope(
            from_="pg1",
            to="test-node-1",
            ch="response",
            text=json.dumps({"ok": True, "thread_id": "t1", "message": {"id": "m1", "content": "<@123> hi"}}),
            urgency="now",
            corr="r2",
        )
        mock_wait.side_effect = [resolve, sent]

        result = runner.invoke(cli, [
            "--config", str(config_file),
            "hive-thread-send",
            "--to", "pg1",
            "--thread-id", "t1",
            "--message", "hi",
            "--wait", "10",
        ])
        assert result.exit_code == 0, result.output

        # second call payload should include auto mention prefix
        args2, _ = mock_wait.call_args_list[1]
        payload_json = json.loads(args2[2])
        assert payload_json["text"]
        body = json.loads(payload_json["text"])
        assert body["content"].startswith("<@123>")
