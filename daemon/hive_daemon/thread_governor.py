"""Core thread-governor policy engine."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Protocol

from hive_daemon.config import ThreadGovernorConfig
from hive_daemon.thread_governor_store import ThreadGovernorState, ThreadGovernorStore

log = logging.getLogger("hive_daemon.thread_governor")


class ThreadGovernorError(RuntimeError):
    """Raised for invalid governor operations."""


@dataclass(frozen=True, slots=True)
class GovernedThreadView:
    """Minimal thread metadata needed for cleanup and command responses."""

    thread_id: str
    parent_id: str
    locked: bool
    archived: bool
    archived_at: datetime | None = None


class ThreadGovernorActions(Protocol):
    """Transport operations the governor needs from Discord."""

    async def lock_thread(self, thread_id: str) -> None: ...

    async def unlock_thread(self, thread_id: str) -> None: ...

    async def send_notice(self, thread_id: str, content: str) -> None: ...

    async def fetch_thread(self, thread_id: str) -> GovernedThreadView | None: ...


@dataclass(frozen=True, slots=True)
class ThreadGovernorStatus:
    """Concise state payload for `/status` and diagnostics."""

    thread_id: str
    parent_id: str
    count: int
    limit: int
    locked: bool
    unlock_at: datetime | None
    last_owner_at: datetime | None
    last_message_at: datetime


class ThreadGovernor:
    """Applies thread budgeting, cooldown locks, and cleanup."""

    def __init__(
        self,
        config: ThreadGovernorConfig,
        *,
        store: ThreadGovernorStore,
        actions: ThreadGovernorActions,
    ) -> None:
        self._config = config
        self._store = store
        self._actions = actions
        self._watched_parents = set(config.watched_parents)
        self._lock = asyncio.Lock()

    def is_watched_parent(self, parent_id: str) -> bool:
        return parent_id in self._watched_parents

    async def handle_inbound_message(
        self,
        *,
        thread_id: str,
        parent_id: str,
        author_id: str,
        is_bot: bool,
        is_system: bool = False,
        now: datetime | None = None,
    ) -> ThreadGovernorState | None:
        """Apply discovery and counting semantics for one inbound message."""
        if not self.is_watched_parent(parent_id):
            return None

        ts = _utcnow(now)
        async with self._lock:
            state, created = self._store.ensure_thread(
                thread_id=thread_id,
                parent_id=parent_id,
                limit=self._config.default_limit,
                now=ts,
            )
            if created:
                log.info(
                    "thread-governor discover thread=%s parent=%s limit=%s created_at=%s",
                    state.thread_id,
                    state.parent_id,
                    state.limit,
                    state.created_at.isoformat(),
                )

            if state.locked:
                return state

            if is_bot or is_system:
                return state

            if author_id == self._config.owner_id:
                updated = replace(
                    state,
                    count=0,
                    last_owner_at=ts,
                    last_message_at=ts,
                )
                self._store.save(updated)
                log.info(
                    "thread-governor reset thread=%s parent=%s count=%s limit=%s last_owner_at=%s",
                    updated.thread_id,
                    updated.parent_id,
                    updated.count,
                    updated.limit,
                    updated.last_owner_at.isoformat() if updated.last_owner_at else None,
                )
                return updated

            updated = replace(
                state,
                count=state.count + 1,
                last_message_at=ts,
            )
            if updated.count < updated.limit:
                self._store.save(updated)
                return updated

            unlock_at = ts + timedelta(minutes=self._config.auto_unlock_minutes)
            locked_state = replace(
                updated,
                locked=True,
                locked_at=ts,
                unlock_at=unlock_at,
                closed_reason="budget_exceeded",
            )
            await self._actions.lock_thread(thread_id)
            self._store.save(locked_state)
            await self._actions.send_notice(
                thread_id,
                self._config.notice_template.format(
                    minutes=self._config.auto_unlock_minutes,
                    limit=locked_state.limit,
                ),
            )
            log.info(
                "thread-governor lock thread=%s parent=%s count=%s limit=%s unlock_at=%s",
                locked_state.thread_id,
                locked_state.parent_id,
                locked_state.count,
                locked_state.limit,
                locked_state.unlock_at.isoformat() if locked_state.unlock_at else None,
            )
            return locked_state

    async def unlock_due_threads(self, *, now: datetime | None = None) -> int:
        """Unlock all cooldown-expired threads."""
        ts = _utcnow(now)
        async with self._lock:
            due = self._store.list_locked_due(ts)
            for state in due:
                await self._unlock_state(
                    state,
                    now=ts,
                    recovery=False,
                )
            return len(due)

    async def recover_locked_threads(self, *, now: datetime | None = None) -> tuple[int, int]:
        """Reconcile locked state on startup.

        Returns ``(overdue_unlocked, pending_locked)``.
        """
        ts = _utcnow(now)
        overdue = 0
        pending = 0
        async with self._lock:
            for state in self._store.list_locked():
                if state.unlock_at is None or state.unlock_at <= ts:
                    await self._unlock_state(
                        state,
                        now=ts,
                        recovery=True,
                    )
                    overdue += 1
                else:
                    pending += 1
        return overdue, pending

    async def pause_thread(
        self,
        *,
        thread_id: str,
        parent_id: str,
        now: datetime | None = None,
    ) -> ThreadGovernorState:
        """Lock a thread immediately with a fresh cooldown."""
        state = await self._require_thread_state(thread_id=thread_id, parent_id=parent_id, now=now)
        ts = _utcnow(now)
        unlock_at = ts + timedelta(minutes=self._config.auto_unlock_minutes)
        paused = replace(
            state,
            locked=True,
            locked_at=ts,
            unlock_at=unlock_at,
            closed_reason="manual_pause",
        )
        async with self._lock:
            await self._actions.lock_thread(thread_id)
            self._store.save(paused)
        log.info(
            "thread-governor lock thread=%s parent=%s count=%s limit=%s unlock_at=%s",
            paused.thread_id,
            paused.parent_id,
            paused.count,
            paused.limit,
            paused.unlock_at.isoformat() if paused.unlock_at else None,
        )
        return paused

    async def unlock_thread(
        self,
        *,
        thread_id: str,
        parent_id: str,
        now: datetime | None = None,
    ) -> ThreadGovernorState:
        """Unlock a thread immediately and reset its counter."""
        async with self._lock:
            state = self._ensure_thread_state(thread_id=thread_id, parent_id=parent_id, now=now)
            return await self._unlock_state(state, now=_utcnow(now), recovery=False)

    async def status(
        self,
        *,
        thread_id: str,
        parent_id: str,
        now: datetime | None = None,
    ) -> ThreadGovernorStatus:
        """Get current governor status for a thread."""
        state = await self._require_thread_state(thread_id=thread_id, parent_id=parent_id, now=now)
        return ThreadGovernorStatus(
            thread_id=state.thread_id,
            parent_id=state.parent_id,
            count=state.count,
            limit=state.limit,
            locked=state.locked,
            unlock_at=state.unlock_at,
            last_owner_at=state.last_owner_at,
            last_message_at=state.last_message_at,
        )

    async def set_thread_limit(
        self,
        *,
        thread_id: str,
        parent_id: str,
        limit: int,
        now: datetime | None = None,
    ) -> ThreadGovernorState:
        """Persist a per-thread budget override."""
        if limit < 1:
            raise ThreadGovernorError("thread budget must be >= 1")
        async with self._lock:
            state = self._ensure_thread_state(thread_id=thread_id, parent_id=parent_id, now=now)
            updated = replace(state, limit=limit)
            self._store.save(updated)
            return updated

    async def cleanup_threads(self, *, now: datetime | None = None) -> int:
        """Delete stale state rows without mutating Discord thread state."""
        ts = _utcnow(now)
        idle_cutoff = ts - timedelta(days=self._config.cleanup.idle_expiry_days)
        archived_cutoff = ts - timedelta(days=self._config.cleanup.archived_expiry_days)
        deleted = 0
        async with self._lock:
            for state in self._store.list_all():
                thread = await self._actions.fetch_thread(state.thread_id)
                reason = None
                if thread is None:
                    reason = "inaccessible"
                elif thread.archived and (thread.archived_at or state.last_message_at) <= archived_cutoff:
                    reason = "archived_expired"
                elif state.last_message_at <= idle_cutoff:
                    reason = "idle_expired"

                if reason is None:
                    continue

                if self._store.delete(state.thread_id):
                    deleted += 1
                    log.info(
                        "thread-governor cleanup thread=%s parent=%s reason=%s last_message_at=%s",
                        state.thread_id,
                        state.parent_id,
                        reason,
                        state.last_message_at.isoformat(),
                    )
        return deleted

    async def _require_thread_state(
        self,
        *,
        thread_id: str,
        parent_id: str,
        now: datetime | None = None,
    ) -> ThreadGovernorState:
        if not self.is_watched_parent(parent_id):
            raise ThreadGovernorError(f"thread parent {parent_id} is not governed")
        async with self._lock:
            return self._ensure_thread_state(thread_id=thread_id, parent_id=parent_id, now=now)

    def _ensure_thread_state(
        self,
        *,
        thread_id: str,
        parent_id: str,
        now: datetime | None = None,
    ) -> ThreadGovernorState:
        state, _ = self._store.ensure_thread(
            thread_id=thread_id,
            parent_id=parent_id,
            limit=self._config.default_limit,
            now=now,
        )
        return state

    async def _unlock_state(
        self,
        state: ThreadGovernorState,
        *,
        now: datetime,
        recovery: bool,
    ) -> ThreadGovernorState:
        await self._actions.unlock_thread(state.thread_id)
        updated = replace(
            state,
            count=0,
            locked=False,
            locked_at=None,
            unlock_at=None,
            closed_reason=None,
            last_message_at=max(state.last_message_at, now),
        )
        self._store.save(updated)
        if recovery:
            log.info(
                "thread-governor recovery thread=%s parent=%s count=%s limit=%s unlock_at=%s",
                state.thread_id,
                state.parent_id,
                state.count,
                state.limit,
                state.unlock_at.isoformat() if state.unlock_at else None,
            )
        log.info(
            "thread-governor unlock thread=%s parent=%s count=%s limit=%s",
            updated.thread_id,
            updated.parent_id,
            updated.count,
            updated.limit,
        )
        return updated


def _utcnow(value: datetime | None = None) -> datetime:
    ts = value or datetime.now(timezone.utc)
    if ts.tzinfo is None:
        return ts.replace(tzinfo=timezone.utc)
    return ts.astimezone(timezone.utc)
