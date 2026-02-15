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
