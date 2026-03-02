"""Tests for persistent corr → Discord thread mapping store."""

import json
import time

import pytest

from hive_daemon.thread_map import ThreadMap, ThreadEntry, resolve_thread_key


class TestResolveThreadKey:
    def test_uses_corr_when_present(self):
        assert resolve_thread_key("abc123", "msg-001") == "abc123"

    def test_falls_back_to_id_when_corr_none(self):
        assert resolve_thread_key(None, "msg-001") == "msg-001"

    def test_falls_back_to_id_when_corr_empty(self):
        assert resolve_thread_key("", "msg-001") == "msg-001"


class TestThreadMapPutGet:
    def test_put_and_get(self, tmp_path):
        path = tmp_path / "threads.json"
        tm = ThreadMap(path=path, channel="test-ch")
        tm.put("corr-1", thread_id="t-100", parent_message_id="p-100")
        entry = tm.get("corr-1")
        assert entry is not None
        assert entry.thread_id == "t-100"
        assert entry.parent_message_id == "p-100"

    def test_get_missing_returns_none(self, tmp_path):
        path = tmp_path / "threads.json"
        tm = ThreadMap(path=path)
        assert tm.get("nonexistent") is None

    def test_get_updates_last_seen(self, tmp_path):
        path = tmp_path / "threads.json"
        tm = ThreadMap(path=path)
        tm.put("corr-1", thread_id="t-1", parent_message_id="p-1")
        # Read raw file to check lastSeenAt before get
        raw_before = json.loads(path.read_text())
        key = list(raw_before.keys())[0]
        seen_before = raw_before[key]["lastSeenAt"]

        # Small delay to ensure time difference
        time.sleep(0.01)
        entry = tm.get("corr-1")
        raw_after = json.loads(path.read_text())
        seen_after = raw_after[key]["lastSeenAt"]
        assert seen_after >= seen_before

    def test_put_overwrites_thread_id(self, tmp_path):
        path = tmp_path / "threads.json"
        tm = ThreadMap(path=path)
        tm.put("corr-1", thread_id="t-old", parent_message_id="p-old")
        tm.put("corr-1", thread_id="t-new", parent_message_id="p-new")
        entry = tm.get("corr-1")
        assert entry is not None
        assert entry.thread_id == "t-new"
        assert entry.parent_message_id == "p-new"


class TestThreadMapPersistence:
    def test_survives_reload(self, tmp_path):
        """Data persists across ThreadMap instances (simulates restart)."""
        path = tmp_path / "threads.json"
        tm1 = ThreadMap(path=path, channel="ch")
        tm1.put("corr-1", thread_id="t-1", parent_message_id="p-1")

        tm2 = ThreadMap(path=path, channel="ch")
        entry = tm2.get("corr-1")
        assert entry is not None
        assert entry.thread_id == "t-1"

    def test_different_channels_different_keys(self, tmp_path):
        path = tmp_path / "threads.json"
        tm_a = ThreadMap(path=path, channel="ch-a")
        tm_b = ThreadMap(path=path, channel="ch-b")
        tm_a.put("corr-1", thread_id="t-a", parent_message_id="p-a")
        tm_b.put("corr-1", thread_id="t-b", parent_message_id="p-b")
        assert tm_a.get("corr-1").thread_id == "t-a"
        assert tm_b.get("corr-1").thread_id == "t-b"


class TestThreadMapPrune:
    def test_expired_entries_pruned_on_load(self, tmp_path):
        """Entries older than max_age_hours are pruned."""
        path = tmp_path / "threads.json"
        old_time = time.time() - 200 * 3600  # 200 hours ago
        data = {
            "discord:ch:corr:old-key": {
                "threadId": "t-old",
                "parentMessageId": "p-old",
                "createdAt": old_time,
                "lastSeenAt": old_time,
            },
            "discord:ch:corr:new-key": {
                "threadId": "t-new",
                "parentMessageId": "p-new",
                "createdAt": time.time(),
                "lastSeenAt": time.time(),
            },
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(data))

        tm = ThreadMap(path=path, max_age_hours=168, channel="ch")
        # Old entry should be gone
        assert tm.get("old-key") is None
        # New entry should still be there
        assert tm.get("new-key") is not None

    def test_zero_max_age_prunes_all(self, tmp_path):
        """max_age_hours=0 means all entries expire immediately."""
        path = tmp_path / "threads.json"
        tm = ThreadMap(path=path, max_age_hours=0)
        tm.put("corr-1", thread_id="t-1", parent_message_id="p-1")
        # With 0 hours max age, entry is immediately stale on next load
        assert tm.get("corr-1") is None


class TestThreadMapRemove:
    def test_remove_existing(self, tmp_path):
        path = tmp_path / "threads.json"
        tm = ThreadMap(path=path)
        tm.put("corr-1", thread_id="t-1", parent_message_id="p-1")
        tm.remove("corr-1")
        assert tm.get("corr-1") is None

    def test_remove_nonexistent_no_error(self, tmp_path):
        path = tmp_path / "threads.json"
        tm = ThreadMap(path=path)
        tm.remove("nonexistent")  # Should not raise


class TestThreadMapCorruptFile:
    def test_corrupt_json_returns_empty(self, tmp_path):
        path = tmp_path / "threads.json"
        path.write_text("not json at all")
        tm = ThreadMap(path=path)
        assert tm.get("any") is None

    def test_malformed_entry_returns_none(self, tmp_path):
        path = tmp_path / "threads.json"
        data = {"discord:ch:corr:k": {"threadId": "t-1"}}  # missing fields
        path.write_text(json.dumps(data))
        tm = ThreadMap(path=path, channel="ch")
        assert tm.get("k") is None
