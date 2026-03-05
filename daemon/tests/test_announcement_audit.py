"""Tests for local JSONL audit log writer."""

import json

import pytest

from hive_daemon.announcement_audit import AuditLogger
from hive_daemon.config import AuditConfig
from hive_daemon.envelope import Envelope


def _make_envelope(
    corr: str | None = "corr-1",
    action: str | None = "ping",
) -> Envelope:
    return Envelope(
        v=1, id="msg-001", ts=1000000, from_="turq", to="pg1",
        ch="command", urgency="now", text="hello", corr=corr, action=action,
    )


class TestAuditLoggerDisabled:
    def test_noop_when_disabled(self, tmp_path):
        cfg = AuditConfig(enabled=False)
        logger = AuditLogger(cfg)
        assert not logger.enabled
        # Should not raise or write anything
        logger.record(_make_envelope(), event="send")

    def test_no_file_created_when_disabled(self, tmp_path):
        path = tmp_path / "audit.log"
        cfg = AuditConfig(enabled=False, path=str(path))
        logger = AuditLogger(cfg)
        logger.record(_make_envelope(), event="send")
        assert not path.exists()


class TestAuditLoggerEnabled:
    def test_writes_jsonl_line(self, tmp_path):
        path = tmp_path / "audit.log"
        cfg = AuditConfig(enabled=True, path=str(path))
        logger = AuditLogger(cfg, node_id="turq")
        logger.record(_make_envelope(), event="send")

        assert path.exists()
        lines = path.read_text().strip().splitlines()
        assert len(lines) == 1
        record = json.loads(lines[0])
        assert record["event"] == "send"
        assert record["id"] == "msg-001"
        assert record["from"] == "turq"
        assert record["to"] == "pg1"
        assert record["ch"] == "command"
        assert record["action"] == "ping"
        assert record["corr"] == "corr-1"
        assert record["gw"] == "turq"
        assert record["status"] == "ok"
        assert "ts" in record

    def test_appends_multiple_records(self, tmp_path):
        path = tmp_path / "audit.log"
        cfg = AuditConfig(enabled=True, path=str(path))
        logger = AuditLogger(cfg, node_id="turq")
        logger.record(_make_envelope(), event="send")
        logger.record(_make_envelope(), event="recv")

        lines = path.read_text().strip().splitlines()
        assert len(lines) == 2
        assert json.loads(lines[0])["event"] == "send"
        assert json.loads(lines[1])["event"] == "recv"

    def test_includes_thread_id(self, tmp_path):
        path = tmp_path / "audit.log"
        cfg = AuditConfig(enabled=True, path=str(path))
        logger = AuditLogger(cfg)
        logger.record(_make_envelope(), event="send", thread_id="t-100")

        record = json.loads(path.read_text().strip())
        assert record["threadId"] == "t-100"

    def test_includes_error(self, tmp_path):
        path = tmp_path / "audit.log"
        cfg = AuditConfig(enabled=True, path=str(path))
        logger = AuditLogger(cfg)
        logger.record(
            _make_envelope(), event="send",
            status="error", error="API down",
        )

        record = json.loads(path.read_text().strip())
        assert record["status"] == "error"
        assert record["error"] == "API down"

    def test_omits_thread_id_when_none(self, tmp_path):
        path = tmp_path / "audit.log"
        cfg = AuditConfig(enabled=True, path=str(path))
        logger = AuditLogger(cfg)
        logger.record(_make_envelope(), event="send")

        record = json.loads(path.read_text().strip())
        assert "threadId" not in record

    def test_handles_none_optional_fields(self, tmp_path):
        path = tmp_path / "audit.log"
        cfg = AuditConfig(enabled=True, path=str(path))
        logger = AuditLogger(cfg)
        logger.record(_make_envelope(corr=None, action=None), event="recv")

        record = json.loads(path.read_text().strip())
        assert record["corr"] is None
        assert record["action"] is None

    def test_creates_parent_dirs(self, tmp_path):
        path = tmp_path / "deep" / "nested" / "audit.log"
        cfg = AuditConfig(enabled=True, path=str(path))
        logger = AuditLogger(cfg)
        logger.record(_make_envelope(), event="send")
        assert path.exists()

    def test_expands_tilde(self, tmp_path, monkeypatch):
        """Path with ~ is expanded to user home."""
        monkeypatch.setenv("HOME", str(tmp_path))
        cfg = AuditConfig(enabled=True, path="~/hive-audit.log")
        logger = AuditLogger(cfg)
        logger.record(_make_envelope(), event="send")
        assert (tmp_path / "hive-audit.log").exists()


class TestAuditLoggerWriteFailure:
    def test_write_failure_is_nonfatal(self, tmp_path):
        """If the path is unwritable, record() does not raise."""
        cfg = AuditConfig(enabled=True, path="/proc/nonexistent/audit.log")
        logger = AuditLogger(cfg)
        # Must NOT raise
        logger.record(_make_envelope(), event="send")
