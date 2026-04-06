"""Tests for core thread governor policy."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone

import pytest

from hive_daemon.config import ThreadGovernorCleanupConfig, ThreadGovernorConfig
from hive_daemon.thread_governor import (
    GovernedThreadView,
    ThreadGovernor,
    ThreadGovernorError,
)
from hive_daemon.thread_governor_store import ThreadGovernorStore


def _ts(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 4, 6, hour, minute, tzinfo=timezone.utc)


class FakeActions:
    def __init__(self) -> None:
        self.threads: dict[str, GovernedThreadView | None] = {}
        self.lock_calls: list[str] = []
        self.unlock_calls: list[str] = []
        self.notices: list[tuple[str, str]] = []

    async def lock_thread(self, thread_id: str) -> None:
        self.lock_calls.append(thread_id)
        thread = self.threads.get(thread_id)
        if thread is not None:
            self.threads[thread_id] = replace(thread, locked=True)

    async def unlock_thread(self, thread_id: str) -> None:
        self.unlock_calls.append(thread_id)
        thread = self.threads.get(thread_id)
        if thread is not None:
            self.threads[thread_id] = replace(thread, locked=False)

    async def send_notice(self, thread_id: str, content: str) -> None:
        self.notices.append((thread_id, content))

    async def fetch_thread(self, thread_id: str) -> GovernedThreadView | None:
        return self.threads.get(thread_id)


def _config(**overrides) -> ThreadGovernorConfig:
    base = dict(
        owner_id="737554625577746492",
        watched_parents=["1490207637101613076"],
        default_limit=3,
        auto_unlock_minutes=10,
        cleanup=ThreadGovernorCleanupConfig(
            interval_minutes=60,
            idle_expiry_days=7,
            archived_expiry_days=2,
        ),
    )
    base.update(overrides)
    return ThreadGovernorConfig(**base)


def _thread(thread_id: str, *, archived: bool = False, archived_at: datetime | None = None) -> GovernedThreadView:
    return GovernedThreadView(
        thread_id=thread_id,
        parent_id="1490207637101613076",
        locked=False,
        archived=archived,
        archived_at=archived_at,
    )


@pytest.mark.asyncio
class TestThreadGovernorInbound:
    async def test_discovers_counts_and_resets_on_owner_message(self, tmp_path, caplog):
        actions = FakeActions()
        actions.threads["thr-1"] = _thread("thr-1")
        governor = ThreadGovernor(
            _config(),
            store=ThreadGovernorStore(path=tmp_path / "thread-governor.db"),
            actions=actions,
        )

        with caplog.at_level("INFO", logger="hive_daemon.thread_governor"):
            first = await governor.handle_inbound_message(
                thread_id="thr-1",
                parent_id="1490207637101613076",
                author_id="user-1",
                is_bot=False,
                now=_ts(3, 0),
            )
            reset = await governor.handle_inbound_message(
                thread_id="thr-1",
                parent_id="1490207637101613076",
                author_id="737554625577746492",
                is_bot=False,
                now=_ts(3, 2),
            )

        assert first is not None
        assert first.count == 1
        assert reset is not None
        assert reset.count == 0
        assert reset.last_owner_at == _ts(3, 2)
        assert "thread-governor discover" in caplog.text
        assert "thread-governor reset" in caplog.text

    async def test_ignores_unwatched_and_bot_messages(self, tmp_path):
        actions = FakeActions()
        actions.threads["thr-1"] = _thread("thr-1")
        governor = ThreadGovernor(
            _config(),
            store=ThreadGovernorStore(path=tmp_path / "thread-governor.db"),
            actions=actions,
        )

        ignored = await governor.handle_inbound_message(
            thread_id="thr-1",
            parent_id="unwatched-parent",
            author_id="user-1",
            is_bot=False,
            now=_ts(4, 0),
        )
        bot_state = await governor.handle_inbound_message(
            thread_id="thr-1",
            parent_id="1490207637101613076",
            author_id="bot-1",
            is_bot=True,
            now=_ts(4, 1),
        )

        assert ignored is None
        assert bot_state is not None
        assert bot_state.count == 0

    async def test_locks_at_threshold_and_stops_duplicate_notices(self, tmp_path, caplog):
        actions = FakeActions()
        actions.threads["thr-1"] = _thread("thr-1")
        governor = ThreadGovernor(
            _config(default_limit=2),
            store=ThreadGovernorStore(path=tmp_path / "thread-governor.db"),
            actions=actions,
        )

        await governor.handle_inbound_message(
            thread_id="thr-1",
            parent_id="1490207637101613076",
            author_id="user-1",
            is_bot=False,
            now=_ts(5, 0),
        )
        with caplog.at_level("INFO", logger="hive_daemon.thread_governor"):
            locked = await governor.handle_inbound_message(
                thread_id="thr-1",
                parent_id="1490207637101613076",
                author_id="user-2",
                is_bot=False,
                now=_ts(5, 1),
            )
            still_locked = await governor.handle_inbound_message(
                thread_id="thr-1",
                parent_id="1490207637101613076",
                author_id="user-3",
                is_bot=False,
                now=_ts(5, 2),
            )

        assert locked is not None
        assert locked.locked is True
        assert locked.count == 2
        assert locked.unlock_at == _ts(5, 11)
        assert still_locked == locked
        assert actions.lock_calls == ["thr-1"]
        assert len(actions.notices) == 1
        assert "thread-governor lock" in caplog.text


@pytest.mark.asyncio
class TestThreadGovernorLifecycle:
    async def test_unlock_due_resets_state(self, tmp_path, caplog):
        actions = FakeActions()
        actions.threads["thr-1"] = _thread("thr-1")
        store = ThreadGovernorStore(path=tmp_path / "thread-governor.db")
        governor = ThreadGovernor(_config(default_limit=1), store=store, actions=actions)

        await governor.handle_inbound_message(
            thread_id="thr-1",
            parent_id="1490207637101613076",
            author_id="user-1",
            is_bot=False,
            now=_ts(6, 0),
        )
        with caplog.at_level("INFO", logger="hive_daemon.thread_governor"):
            unlocked_count = await governor.unlock_due_threads(now=_ts(6, 11))

        state = store.get("thr-1")
        assert unlocked_count == 1
        assert state is not None
        assert state.locked is False
        assert state.count == 0
        assert state.unlock_at is None
        assert actions.unlock_calls == ["thr-1"]
        assert "thread-governor unlock" in caplog.text

    async def test_recover_locked_threads_unlocks_overdue_only(self, tmp_path, caplog):
        actions = FakeActions()
        actions.threads["due"] = _thread("due")
        actions.threads["pending"] = _thread("pending")
        store = ThreadGovernorStore(path=tmp_path / "thread-governor.db")
        governor = ThreadGovernor(_config(), store=store, actions=actions)

        due_state, _ = store.ensure_thread(
            thread_id="due",
            parent_id="1490207637101613076",
            limit=3,
            now=_ts(7, 0),
        )
        pending_state, _ = store.ensure_thread(
            thread_id="pending",
            parent_id="1490207637101613076",
            limit=3,
            now=_ts(7, 0),
        )
        store.save(
            replace(
                due_state,
                locked=True,
                count=3,
                locked_at=_ts(7, 1),
                unlock_at=_ts(7, 5),
            )
        )
        store.save(
            replace(
                pending_state,
                locked=True,
                count=2,
                locked_at=_ts(7, 2),
                unlock_at=_ts(7, 30),
            )
        )

        with caplog.at_level("INFO", logger="hive_daemon.thread_governor"):
            overdue, pending = await governor.recover_locked_threads(now=_ts(7, 10))

        assert overdue == 1
        assert pending == 1
        assert store.get("due").locked is False
        assert store.get("pending").locked is True
        assert "thread-governor recovery" in caplog.text

    async def test_pause_unlock_status_and_budget_override(self, tmp_path):
        actions = FakeActions()
        actions.threads["thr-2"] = _thread("thr-2")
        store = ThreadGovernorStore(path=tmp_path / "thread-governor.db")
        governor = ThreadGovernor(_config(), store=store, actions=actions)

        paused = await governor.pause_thread(
            thread_id="thr-2",
            parent_id="1490207637101613076",
            now=_ts(8, 0),
        )
        status = await governor.status(
            thread_id="thr-2",
            parent_id="1490207637101613076",
            now=_ts(8, 1),
        )
        overridden = await governor.set_thread_limit(
            thread_id="thr-2",
            parent_id="1490207637101613076",
            limit=9,
            now=_ts(8, 2),
        )
        unlocked = await governor.unlock_thread(
            thread_id="thr-2",
            parent_id="1490207637101613076",
            now=_ts(8, 3),
        )

        assert paused.locked is True
        assert status.locked is True
        assert status.count == 0
        assert overridden.limit == 9
        assert unlocked.locked is False
        assert unlocked.count == 0

    async def test_rejects_commands_for_unwatched_parent(self, tmp_path):
        governor = ThreadGovernor(
            _config(),
            store=ThreadGovernorStore(path=tmp_path / "thread-governor.db"),
            actions=FakeActions(),
        )

        with pytest.raises(ThreadGovernorError, match="not governed"):
            await governor.status(
                thread_id="thr-x",
                parent_id="nope",
                now=_ts(9, 0),
            )


@pytest.mark.asyncio
class TestThreadGovernorCleanup:
    async def test_cleanup_removes_inaccessible_archived_and_idle_rows(self, tmp_path, caplog):
        old_ts = datetime(2026, 3, 20, 1, 0, tzinfo=timezone.utc)
        now = datetime(2026, 4, 10, 10, 0, tzinfo=timezone.utc)
        actions = FakeActions()
        actions.threads["archived"] = _thread(
            "archived",
            archived=True,
            archived_at=old_ts,
        )
        actions.threads["active"] = _thread("active")
        actions.threads["missing"] = None
        store = ThreadGovernorStore(path=tmp_path / "thread-governor.db")
        governor = ThreadGovernor(_config(), store=store, actions=actions)

        archived_state, _ = store.ensure_thread(
            thread_id="archived",
            parent_id="1490207637101613076",
            limit=3,
            now=old_ts,
        )
        idle_state, _ = store.ensure_thread(
            thread_id="active",
            parent_id="1490207637101613076",
            limit=3,
            now=old_ts,
        )
        missing_state, _ = store.ensure_thread(
            thread_id="missing",
            parent_id="1490207637101613076",
            limit=3,
            now=old_ts,
        )
        store.save(replace(archived_state, last_message_at=old_ts))
        store.save(replace(idle_state, last_message_at=old_ts))
        store.save(replace(missing_state, last_message_at=old_ts))

        with caplog.at_level("INFO", logger="hive_daemon.thread_governor"):
            deleted = await governor.cleanup_threads(now=now)

        assert deleted == 3
        assert store.get("archived") is None
        assert store.get("active") is None
        assert store.get("missing") is None
        assert "thread-governor cleanup" in caplog.text
