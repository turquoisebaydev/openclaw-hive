"""Tests for handler discovery and dispatch."""

import json
import os
import stat
import sys
from pathlib import Path

import pytest

from hive_daemon.dispatcher import Dispatcher, DispatchResult, discover_handlers
from hive_daemon.envelope import Envelope


def _make_envelope(action: str | None = "test-action") -> Envelope:
    return Envelope(
        v=1, id="dispatch-1", ts=1000000, from_="node-a", to="node-b",
        ch="command", urgency="now", text="run test action", action=action,
    )


def _write_script(path: Path, body: str) -> Path:
    """Write an executable script file."""
    path.write_text(f"#!/usr/bin/env python3\n{body}")
    path.chmod(path.stat().st_mode | stat.S_IEXEC)
    return path


# --- discover_handlers ---


class TestDiscoverHandlers:
    def test_empty_dir(self, tmp_path: Path):
        handlers = discover_handlers(tmp_path)
        assert handlers == {}

    def test_nonexistent_dir(self, tmp_path: Path):
        handlers = discover_handlers(tmp_path / "nope")
        assert handlers == {}

    def test_finds_executable(self, tmp_path: Path):
        _write_script(tmp_path / "git-sync", "pass")
        handlers = discover_handlers(tmp_path)
        assert "git-sync" in handlers
        assert handlers["git-sync"] == tmp_path / "git-sync"

    def test_skips_non_executable(self, tmp_path: Path):
        script = tmp_path / "no-exec"
        script.write_text("#!/bin/bash\necho hi")
        # don't set executable bit
        handlers = discover_handlers(tmp_path)
        assert "no-exec" not in handlers

    def test_skips_dotfiles(self, tmp_path: Path):
        _write_script(tmp_path / ".hidden", "pass")
        handlers = discover_handlers(tmp_path)
        assert ".hidden" not in handlers

    def test_skips_directories(self, tmp_path: Path):
        subdir = tmp_path / "subdir"
        subdir.mkdir()
        handlers = discover_handlers(tmp_path)
        assert "subdir" not in handlers

    def test_multiple_handlers(self, tmp_path: Path):
        _write_script(tmp_path / "alpha", "pass")
        _write_script(tmp_path / "beta", "pass")
        _write_script(tmp_path / "gamma", "pass")
        handlers = discover_handlers(tmp_path)
        assert sorted(handlers.keys()) == ["alpha", "beta", "gamma"]


# --- Dispatcher ---


class TestDispatcher:
    def test_discover_and_list(self, tmp_path: Path):
        _write_script(tmp_path / "deploy", "pass")
        _write_script(tmp_path / "git-sync", "pass")

        d = Dispatcher(tmp_path)
        d.discover()
        assert d.available_handlers == ["deploy", "git-sync"]
        assert d.has_handler("deploy")
        assert not d.has_handler("nope")

    async def test_dispatch_success(self, tmp_path: Path):
        """Handler reads stdin, writes JSON to stdout, exits 0."""
        _write_script(tmp_path / "echo-action", """\
import sys, json
data = json.load(sys.stdin)
json.dump({"received_id": data["id"], "status": "ok"}, sys.stdout)
""")
        d = Dispatcher(tmp_path, timeout=10)
        d.discover()

        env = _make_envelope(action="echo-action")
        result = await d.dispatch(env)

        assert result.success
        assert result.exit_code == 0
        parsed = result.result_json()
        assert parsed["received_id"] == "dispatch-1"
        assert parsed["status"] == "ok"

    async def test_dispatch_failure(self, tmp_path: Path):
        """Handler exits non-zero with stderr."""
        _write_script(tmp_path / "fail-action", """\
import sys
print("error detail", file=sys.stderr)
sys.exit(1)
""")
        d = Dispatcher(tmp_path, timeout=10)
        d.discover()

        env = _make_envelope(action="fail-action")
        result = await d.dispatch(env)

        assert not result.success
        assert result.exit_code == 1
        assert "error detail" in result.stderr

    async def test_dispatch_timeout(self, tmp_path: Path):
        """Handler that takes too long gets killed."""
        _write_script(tmp_path / "slow-action", """\
import time
time.sleep(60)
""")
        d = Dispatcher(tmp_path, timeout=1)
        d.discover()

        env = _make_envelope(action="slow-action")
        result = await d.dispatch(env)

        assert not result.success
        assert result.exit_code is None
        assert "timed out" in result.stderr

    async def test_dispatch_missing_handler(self, tmp_path: Path):
        """Dispatching an action with no handler raises KeyError."""
        d = Dispatcher(tmp_path, timeout=10)
        d.discover()

        env = _make_envelope(action="missing")
        with pytest.raises(KeyError, match="no handler for action"):
            await d.dispatch(env)

    async def test_dispatch_no_action_field(self, tmp_path: Path):
        """Dispatching an envelope with no action raises ValueError."""
        d = Dispatcher(tmp_path, timeout=10)
        d.discover()

        env = _make_envelope(action=None)
        with pytest.raises(ValueError, match="no action field"):
            await d.dispatch(env)

    async def test_dispatch_passes_full_envelope(self, tmp_path: Path):
        """Handler receives the complete envelope JSON on stdin."""
        _write_script(tmp_path / "check-envelope", """\
import sys, json
data = json.load(sys.stdin)
# Verify all expected fields are present
required = ["v", "id", "ts", "from", "to", "ch", "urgency", "text", "action"]
missing = [f for f in required if f not in data]
if missing:
    print(json.dumps({"error": f"missing fields: {missing}"}))
    sys.exit(1)
json.dump({"all_fields_present": True, "action": data["action"]}, sys.stdout)
""")
        d = Dispatcher(tmp_path, timeout=10)
        d.discover()

        env = _make_envelope(action="check-envelope")
        result = await d.dispatch(env)

        assert result.success
        parsed = result.result_json()
        assert parsed["all_fields_present"] is True
        assert parsed["action"] == "check-envelope"


class TestDispatchResult:
    def test_result_json_valid(self):
        r = DispatchResult("test", True, '{"key": "val"}', "", 0)
        assert r.result_json() == {"key": "val"}

    def test_result_json_invalid_returns_raw(self):
        r = DispatchResult("test", True, "not json", "", 0)
        assert r.result_json() == "not json"

    def test_result_json_empty(self):
        r = DispatchResult("test", True, "", "", 0)
        assert r.result_json() == ""
