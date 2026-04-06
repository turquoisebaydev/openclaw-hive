"""Discord bot client for the Hive thread governor."""

from __future__ import annotations

import asyncio
import logging
import math
from dataclasses import dataclass
from datetime import datetime, timezone

import discord
from discord import app_commands

from hive_daemon.config import HiveConfig, ThreadGovernorConfig
from hive_daemon.thread_governor import (
    GovernedThreadView,
    ThreadGovernor,
    ThreadGovernorError,
)
from hive_daemon.thread_governor_store import ThreadGovernorStore, resolve_thread_governor_db_path

log = logging.getLogger("hive_daemon.thread_governor_bot")

_LOCK_REASON = "Hive thread governor cooldown"
_UNLOCK_REASON = "Hive thread governor resume"
_LOCKED_THREAD_VIOLATION_NOTICE_COOLDOWN_SEC = 60


class ThreadGovernorBudgetGroup(app_commands.Group):
    """Slash-command group for thread budget overrides."""

    def __init__(self, client: DiscordThreadGovernorClient) -> None:
        super().__init__(name="budget", description="Thread governor budget commands")
        self._client = client

    @app_commands.command(name="set", description="Set the message budget for this thread")
    @app_commands.describe(limit="Allowed non-Hugh messages before cooldown")
    async def set_budget(self, interaction: discord.Interaction, limit: app_commands.Range[int, 1, 1000]) -> None:
        await self._client.handle_budget_set(interaction, int(limit))


@dataclass(slots=True)
class DiscordThreadGovernorActions:
    """Discord transport operations for the core governor."""

    client: DiscordThreadGovernorClient

    async def lock_thread(self, thread_id: str) -> None:
        thread = await self._require_thread(thread_id)
        await thread.edit(locked=True, archived=False, reason=_LOCK_REASON)

    async def unlock_thread(self, thread_id: str) -> None:
        thread = await self._require_thread(thread_id)
        await thread.edit(locked=False, archived=False, reason=_UNLOCK_REASON)

    async def send_notice(self, thread_id: str, content: str) -> None:
        thread = await self._require_thread(thread_id)
        await thread.send(content)

    async def fetch_thread(self, thread_id: str) -> GovernedThreadView | None:
        thread = await self._get_thread(thread_id)
        if thread is None:
            return None
        parent_id = str(thread.parent_id or (thread.parent.id if thread.parent else ""))
        archive_ts = getattr(thread, "archive_timestamp", None)
        return GovernedThreadView(
            thread_id=str(thread.id),
            parent_id=parent_id,
            locked=bool(thread.locked),
            archived=bool(thread.archived),
            archived_at=archive_ts,
        )

    async def _require_thread(self, thread_id: str) -> discord.Thread:
        thread = await self._get_thread(thread_id)
        if thread is None:
            raise ThreadGovernorError(f"thread {thread_id} is inaccessible")
        return thread

    async def _get_thread(self, thread_id: str) -> discord.Thread | None:
        channel = self.client.get_channel(int(thread_id))
        if isinstance(channel, discord.Thread):
            return channel
        try:
            fetched = await self.client.fetch_channel(int(thread_id))
        except (discord.NotFound, discord.Forbidden):
            return None
        return fetched if isinstance(fetched, discord.Thread) else None


class DiscordThreadGovernorClient(discord.Client):
    """Discord gateway client for thread governor events and slash commands."""

    def __init__(self, config: HiveConfig) -> None:
        governor_cfg = config.discord_master.thread_governor
        if governor_cfg is None:
            raise RuntimeError("thread governor config is required")
        if not config.discord_master.guild_id:
            raise RuntimeError("discord_master.guild_id is required for thread governor")
        if not config.discord_master.bot_token:
            raise RuntimeError("discord_master.bot_token is required for thread governor")

        intents = discord.Intents.none()
        intents.guilds = True
        intents.guild_messages = True
        intents.messages = True
        super().__init__(intents=intents)

        self._hive_config = config
        self._governor_config = governor_cfg
        self._guild_object = discord.Object(id=int(config.discord_master.guild_id))
        self._store = ThreadGovernorStore(path=resolve_thread_governor_db_path())
        self._actions = DiscordThreadGovernorActions(self)
        self._governor = ThreadGovernor(
            governor_cfg,
            store=self._store,
            actions=self._actions,
        )
        self.tree = app_commands.CommandTree(self)
        self._unlock_task: asyncio.Task | None = None
        self._cleanup_task: asyncio.Task | None = None
        self._background_started = False
        self._last_violation_notice_at: dict[str, datetime] = {}

        self.tree.add_command(
            app_commands.Command(
                name="unlock",
                description="Unlock and reset this governed thread",
                callback=self.unlock_command,
            ),
            guild=self._guild_object,
        )
        self.tree.add_command(
            app_commands.Command(
                name="pause",
                description="Pause this governed thread now",
                callback=self.pause_command,
            ),
            guild=self._guild_object,
        )
        self.tree.add_command(
            app_commands.Command(
                name="status",
                description="Show governor status for this thread",
                callback=self.status_command,
            ),
            guild=self._guild_object,
        )
        self.tree.add_command(ThreadGovernorBudgetGroup(self), guild=self._guild_object)

    async def setup_hook(self) -> None:
        await self.tree.sync(guild=self._guild_object)

    async def on_ready(self) -> None:
        if self._background_started:
            return

        overdue, pending = await self._governor.recover_locked_threads()
        self._unlock_task = asyncio.create_task(self._unlock_loop(), name="thread-governor-unlock")
        self._cleanup_task = asyncio.create_task(self._cleanup_loop(), name="thread-governor-cleanup")
        self._background_started = True
        log.info(
            "thread governor ready user=%s guild=%s db=%s overdue=%s pending=%s",
            self.user.id if self.user else None,
            self._guild_object.id,
            self._store.path,
            overdue,
            pending,
        )

    async def close(self) -> None:
        tasks = [task for task in (self._unlock_task, self._cleanup_task) if task is not None]
        for task in tasks:
            task.cancel()
        for task in tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._store.close()
        await super().close()

    async def on_message(self, message: discord.Message) -> None:
        if message.guild is None or message.guild.id != self._guild_object.id:
            return
        if not isinstance(message.channel, discord.Thread):
            return

        parent_id = str(message.channel.parent_id or (message.channel.parent.id if message.channel.parent else ""))
        state = await self._governor.handle_inbound_message(
            thread_id=str(message.channel.id),
            parent_id=parent_id,
            author_id=str(message.author.id),
            is_bot=bool(message.author.bot),
            is_system=message.is_system(),
        )
        if state is not None and state.locked:
            await self._enforce_locked_thread(message)

    async def _enforce_locked_thread(self, message: discord.Message) -> None:
        if self.user is not None and message.author.id == self.user.id:
            return
        if str(message.author.id) == self._governor_config.owner_id:
            return
        if message.is_system():
            return

        try:
            await message.delete()
        except (discord.NotFound, discord.Forbidden):
            return
        except discord.HTTPException:
            log.exception(
                "thread governor failed to delete locked-thread message thread=%s author=%s",
                message.channel.id,
                message.author.id,
            )
            return

        await self._maybe_send_locked_thread_violation_notice(message.channel)
        log.info(
            "thread governor blocked message in locked thread=%s author=%s bot=%s",
            message.channel.id,
            message.author.id,
            bool(message.author.bot),
        )

    async def _maybe_send_locked_thread_violation_notice(self, thread: discord.Thread) -> None:
        now = datetime.now(timezone.utc)
        thread_id = str(thread.id)
        last = self._last_violation_notice_at.get(thread_id)
        if last is not None and (now - last).total_seconds() < _LOCKED_THREAD_VIOLATION_NOTICE_COOLDOWN_SEC:
            return

        self._last_violation_notice_at[thread_id] = now
        try:
            await thread.send(_locked_thread_violation_message())
        except discord.HTTPException:
            log.exception("thread governor failed to send locked-thread violation notice thread=%s", thread.id)

    async def unlock_command(self, interaction: discord.Interaction) -> None:
        if not await self._require_authorized(interaction):
            return
        ctx = await self._require_governed_thread(interaction)
        if ctx is None:
            return
        thread, parent_id = ctx
        state = await self._governor.unlock_thread(
            thread_id=str(thread.id),
            parent_id=parent_id,
        )
        await self._respond(
            interaction,
            f"Unlocked. Count reset to {state.count}/{state.limit}.",
        )

    async def pause_command(self, interaction: discord.Interaction) -> None:
        if not await self._require_authorized(interaction):
            return
        ctx = await self._require_governed_thread(interaction)
        if ctx is None:
            return
        thread, parent_id = ctx
        state = await self._governor.pause_thread(
            thread_id=str(thread.id),
            parent_id=parent_id,
        )
        await self._respond(
            interaction,
            f"Paused for {self._governor_config.auto_unlock_minutes}m. "
            f"Count is {state.count}/{state.limit}.",
        )

    async def status_command(self, interaction: discord.Interaction) -> None:
        if not await self._require_authorized(interaction):
            return
        ctx = await self._require_governed_thread(interaction)
        if ctx is None:
            return
        thread, parent_id = ctx
        status = await self._governor.status(
            thread_id=str(thread.id),
            parent_id=parent_id,
        )
        lines = [
            f"state: {'locked' if status.locked else 'unlocked'}",
            f"count: {status.count}/{status.limit}",
            f"auto-unlock: {_format_unlock_eta(status.unlock_at)}",
            f"last Hugh activity: {_format_last_owner(status.last_owner_at)}",
        ]
        await self._respond(interaction, "\n".join(lines))

    async def handle_budget_set(self, interaction: discord.Interaction, limit: int) -> None:
        if not await self._require_authorized(interaction):
            return
        ctx = await self._require_governed_thread(interaction)
        if ctx is None:
            return
        thread, parent_id = ctx
        state = await self._governor.set_thread_limit(
            thread_id=str(thread.id),
            parent_id=parent_id,
            limit=limit,
        )
        await self._respond(interaction, f"Budget set to {state.limit}.")

    async def _unlock_loop(self) -> None:
        while not self.is_closed():
            try:
                await self._governor.unlock_due_threads()
            except Exception:
                log.exception("thread governor unlock loop failed")
            await asyncio.sleep(60)

    async def _cleanup_loop(self) -> None:
        interval = max(self._governor_config.cleanup.interval_minutes, 1) * 60
        while not self.is_closed():
            try:
                await self._governor.cleanup_threads()
            except Exception:
                log.exception("thread governor cleanup loop failed")
            await asyncio.sleep(interval)

    async def _require_authorized(self, interaction: discord.Interaction) -> bool:
        if self._is_authorized(interaction):
            return True
        await self._respond(interaction, "Only Hugh or a thread admin can use this command.", error=True)
        return False

    def _is_authorized(self, interaction: discord.Interaction) -> bool:
        if str(interaction.user.id) == self._governor_config.owner_id:
            return True
        perms = getattr(interaction.user, "guild_permissions", None)
        return bool(perms and (perms.administrator or perms.manage_threads))

    async def _require_governed_thread(
        self,
        interaction: discord.Interaction,
    ) -> tuple[discord.Thread, str] | None:
        channel = interaction.channel
        if not isinstance(channel, discord.Thread):
            await self._respond(interaction, "This command only works in watched threads.", error=True)
            return None

        parent_id = str(channel.parent_id or (channel.parent.id if channel.parent else ""))
        if not parent_id or not self._governor.is_watched_parent(parent_id):
            await self._respond(interaction, "This thread is not under governor control.", error=True)
            return None
        return channel, parent_id

    async def _respond(self, interaction: discord.Interaction, content: str, *, error: bool = False) -> None:
        if interaction.response.is_done():
            await interaction.followup.send(content, ephemeral=True)
            return
        await interaction.response.send_message(content, ephemeral=True)
        if error:
            log.debug("thread governor command error user=%s content=%s", interaction.user.id, content)


def create_thread_governor_client(config: HiveConfig) -> DiscordThreadGovernorClient:
    """Construct the Discord thread governor client."""
    return DiscordThreadGovernorClient(config)


def _locked_thread_violation_message() -> str:
    return (
        "Thread is locked by Hive governor cooldown. Message removed. "
        "Bots are not required to respond here while locked — they should choose NO_REPLY until Hugh unlocks the thread."
    )


def _format_unlock_eta(unlock_at: datetime | None) -> str:
    if unlock_at is None:
        return "-"
    now = datetime.now(timezone.utc)
    delta = max((unlock_at - now).total_seconds(), 0.0)
    minutes = max(1, math.ceil(delta / 60))
    return f"in {minutes}m"


def _format_last_owner(last_owner_at: datetime | None) -> str:
    if last_owner_at is None:
        return "never"
    now = datetime.now(timezone.utc)
    delta = max((now - last_owner_at).total_seconds(), 0.0)
    minutes = math.floor(delta / 60)
    if minutes < 60:
        return f"{minutes}m ago"
    hours = math.floor(minutes / 60)
    return f"{hours}h ago"
