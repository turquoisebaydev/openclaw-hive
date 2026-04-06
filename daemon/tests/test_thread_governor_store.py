"""Tests for durable thread governor SQLite state."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone

from hive_daemon.thread_governor_store import (
    ThreadGovernorState,
    ThreadGovernorStore,
)


def _ts(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 4, 6, hour, minute, tzinfo=timezone.utc)


class TestThreadGovernorStoreBootstrap:
    def test_bootstrap_creates_schema_and_index(self, tmp_path):
        path = tmp_path / "thread-governor.db"
        with ThreadGovernorStore(path=path) as store:
            tables = {
                row["name"]
                for row in store._conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'table'"
                ).fetchall()
            }
            index_names = {
                row["name"]
                for row in store._conn.execute(
                    "PRAGMA index_list('thread_governor_state')"
                ).fetchall()
            }
            user_version = store._conn.execute("PRAGMA user_version").fetchone()[0]

        assert "thread_governor_state" in tables
        assert "idx_thread_governor_locked_unlock_at" in index_names
        assert user_version == 1


class TestThreadGovernorStoreEnsureThread:
    def test_ensure_thread_creates_missing_row(self, tmp_path):
        with ThreadGovernorStore(path=tmp_path / "thread-governor.db") as store:
            state, created = store.ensure_thread(
                thread_id="1490216448679739526",
                parent_id="1490207637101613076",
                limit=12,
                now=_ts(3, 15),
            )

            fetched = store.get("1490216448679739526")

        assert created is True
        assert state.thread_id == "1490216448679739526"
        assert state.parent_id == "1490207637101613076"
        assert state.count == 0
        assert state.limit == 12
        assert state.locked is False
        assert state.created_at == _ts(3, 15)
        assert state.last_message_at == _ts(3, 15)
        assert fetched == state

    def test_ensure_thread_is_idempotent(self, tmp_path):
        with ThreadGovernorStore(path=tmp_path / "thread-governor.db") as store:
            state, created = store.ensure_thread(
                thread_id="thr-1",
                parent_id="parent-1",
                limit=12,
                now=_ts(4, 0),
            )
            store.save(
                replace(
                    state,
                    count=5,
                    last_message_at=_ts(4, 20),
                )
            )

            fetched, created_again = store.ensure_thread(
                thread_id="thr-1",
                parent_id="parent-2",
                limit=99,
                now=_ts(5, 0),
            )

        assert created is True
        assert created_again is False
        assert fetched.parent_id == "parent-1"
        assert fetched.limit == 12
        assert fetched.count == 5
        assert fetched.last_message_at == _ts(4, 20)


class TestThreadGovernorStorePersistence:
    def test_state_survives_restart(self, tmp_path):
        path = tmp_path / "thread-governor.db"
        created_at = _ts(6, 0)

        with ThreadGovernorStore(path=path) as store:
            state, _ = store.ensure_thread(
                thread_id="thr-2",
                parent_id="parent-1",
                limit=12,
                now=created_at,
            )
            store.save(
                replace(
                    state,
                    count=7,
                    limit=20,
                    locked=True,
                    last_owner_at=_ts(6, 10),
                    last_message_at=_ts(6, 30),
                    locked_at=_ts(6, 31),
                    unlock_at=_ts(6, 41),
                    closed_reason="manual_pause",
                )
            )

        with ThreadGovernorStore(path=path) as store:
            fetched = store.get("thr-2")

        assert fetched == ThreadGovernorState(
            thread_id="thr-2",
            parent_id="parent-1",
            count=7,
            limit=20,
            locked=True,
            created_at=created_at,
            last_owner_at=_ts(6, 10),
            last_message_at=_ts(6, 30),
            locked_at=_ts(6, 31),
            unlock_at=_ts(6, 41),
            closed_reason="manual_pause",
        )

    def test_limit_override_persists(self, tmp_path):
        path = tmp_path / "thread-governor.db"
        with ThreadGovernorStore(path=path) as store:
            state, _ = store.ensure_thread(
                thread_id="thr-3",
                parent_id="parent-1",
                limit=12,
                now=_ts(7, 0),
            )
            store.save(replace(state, limit=25))

        with ThreadGovernorStore(path=path) as store:
            fetched = store.get("thr-3")

        assert fetched is not None
        assert fetched.limit == 25


class TestThreadGovernorStoreQueries:
    def test_list_locked_due_returns_only_due_rows(self, tmp_path):
        with ThreadGovernorStore(path=tmp_path / "thread-governor.db") as store:
            due_state, _ = store.ensure_thread(
                thread_id="due-thread",
                parent_id="parent-1",
                limit=12,
                now=_ts(8, 0),
            )
            later_state, _ = store.ensure_thread(
                thread_id="later-thread",
                parent_id="parent-1",
                limit=12,
                now=_ts(8, 5),
            )
            unlocked_state, _ = store.ensure_thread(
                thread_id="open-thread",
                parent_id="parent-1",
                limit=12,
                now=_ts(8, 10),
            )
            store.save(
                replace(
                    due_state,
                    locked=True,
                    locked_at=_ts(8, 1),
                    unlock_at=_ts(8, 11),
                )
            )
            store.save(
                replace(
                    later_state,
                    locked=True,
                    locked_at=_ts(8, 6),
                    unlock_at=_ts(8, 30),
                )
            )
            store.save(unlocked_state)

            due = store.list_locked_due(_ts(8, 15))
            locked = store.list_locked()

        assert [row.thread_id for row in due] == ["due-thread"]
        assert [row.thread_id for row in locked] == ["due-thread", "later-thread"]

    def test_delete_removes_rows_idempotently(self, tmp_path):
        with ThreadGovernorStore(path=tmp_path / "thread-governor.db") as store:
            store.ensure_thread(
                thread_id="thr-4",
                parent_id="parent-1",
                limit=12,
                now=_ts(9, 0),
            )

            deleted = store.delete("thr-4")
            deleted_again = store.delete("thr-4")

        assert deleted is True
        assert deleted_again is False
