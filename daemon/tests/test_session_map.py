"""Tests for session_map module."""

import json
import time
from pathlib import Path

from hive_daemon import session_map


def test_put_and_get(tmp_path, monkeypatch):
    store = tmp_path / "session-map.json"
    monkeypatch.setenv("HIVE_SESSION_MAP", str(store))

    session_map.put("corr-1", "my-session-key", ttl=60)
    assert session_map.get("corr-1") == "my-session-key"


def test_get_missing(tmp_path, monkeypatch):
    store = tmp_path / "session-map.json"
    monkeypatch.setenv("HIVE_SESSION_MAP", str(store))

    assert session_map.get("nonexistent") is None


def test_pop_removes(tmp_path, monkeypatch):
    store = tmp_path / "session-map.json"
    monkeypatch.setenv("HIVE_SESSION_MAP", str(store))

    session_map.put("corr-2", "sess-abc", ttl=60)
    assert session_map.pop("corr-2") == "sess-abc"
    assert session_map.get("corr-2") is None


def test_expired_entry(tmp_path, monkeypatch):
    store = tmp_path / "session-map.json"
    monkeypatch.setenv("HIVE_SESSION_MAP", str(store))

    # Write an already-expired entry
    data = {"corr-old": {"session": "dead", "expires": int(time.time()) - 10}}
    store.write_text(json.dumps(data))

    assert session_map.get("corr-old") is None
