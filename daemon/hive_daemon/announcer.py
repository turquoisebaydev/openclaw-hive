"""Optional Discord announcements for hive send/receive events.

Publishes compact one-line summaries to a Discord webhook when enabled.
When threading is enabled, groups correlated events into Discord threads
using the Discord bot API, falling back to channel-level webhook posts
on failure.

Thread message bodies contain **message text only** — full metadata is
written to the local audit log.

All publish failures are non-fatal — logged as warnings, never breaking
the core send/receive flow.
"""

from __future__ import annotations

import asyncio
import json
import logging
import urllib.request
import urllib.error
from functools import partial

from hive_daemon.announcement_audit import AuditLogger
from hive_daemon.config import AnnouncementsConfig
from hive_daemon.envelope import Envelope
from hive_daemon.thread_map import ThreadMap, ThreadEntry, resolve_thread_key

log = logging.getLogger(__name__)

# Maximum text length included in announcements (avoid leaking large payloads).
_MAX_TEXT_LEN = 80

_USER_AGENT = "openclaw-hive-daemon/0.1 (+https://github.com/turquoisebaydev/openclaw-hive)"


def _text_preview(raw: str, max_len: int = _MAX_TEXT_LEN) -> str:
    """Return compact single-line preview text for announcements."""
    compact = " ".join(raw.split())
    if len(compact) <= max_len:
        return compact
    return compact[: max_len - 1] + "…"


def _format_common(prefix: str, envelope: Envelope, *, gw: str | None = None) -> str:
    """Format common announcement fields for send/recv events."""
    parts = [
        prefix,
        f"from={envelope.from_}",
        f"to={envelope.to}",
        f"type={envelope.ch}",
        f"urgency={envelope.urgency}",
        f"ts={envelope.ts}",
        f"text_len={len(envelope.text)}",
        f"text={_text_preview(envelope.text)!r}",
    ]
    if gw:
        parts.append(f"gw={gw}")
    if envelope.action:
        parts.append(f"action={envelope.action}")
    if envelope.corr:
        parts.append(f"corr={envelope.corr}")
    if envelope.reply_to:
        parts.append(f"reply_to={envelope.reply_to}")
    if envelope.ttl is not None:
        parts.append(f"ttl={envelope.ttl}")
    parts.append(f"id={envelope.id}")
    return " | ".join(parts)


def format_send(envelope: Envelope, *, gw: str | None = None) -> str:
    """Format a compact send announcement line."""
    return _format_common("HIVE SEND", envelope, gw=gw)


def format_recv(envelope: Envelope, *, gw: str | None = None) -> str:
    """Format a compact receive announcement line."""
    return _format_common("HIVE RECV", envelope, gw=gw)


def _format_thread_parent(envelope: Envelope) -> str:
    """Format the parent/root message for a new thread."""
    parts = [
        "HIVE FLOW",
        f"corr={envelope.corr or envelope.id}",
        f"from={envelope.from_}",
        f"to={envelope.to}",
    ]
    if envelope.action:
        parts.append(f"action={envelope.action}")
    return " | ".join(parts)


# Maximum text length for thread message bodies.
_MAX_THREAD_TEXT_LEN = 2000


def _thread_message_body(envelope: Envelope) -> str:
    """Return the text-only body for a thread message.

    Per spec, thread messages contain envelope.text only — no metadata.
    Applies deterministic truncation for very large payloads.
    """
    text = envelope.text
    if len(text) <= _MAX_THREAD_TEXT_LEN:
        return text
    return text[: _MAX_THREAD_TEXT_LEN - 1] + "…"


class Announcer:
    """Publishes hive event announcements to Discord.

    Safe to call even when disabled — methods return immediately.
    All publish errors are caught and logged as warnings.

    When threading is enabled, correlated events are grouped into
    Discord threads via the bot API.  Thread mappings are persisted
    on disk so they survive daemon restarts.

    Thread message bodies contain message text only; full metadata is
    written to the audit log.

    Args:
        config: The announcements configuration block.
        node_id: Local gateway identifier for gw= field.
        thread_map: Override the ThreadMap instance (mainly for testing).
        audit_logger: Override the AuditLogger instance (mainly for testing).
    """

    def __init__(
        self,
        config: AnnouncementsConfig,
        *,
        node_id: str | None = None,
        thread_map: ThreadMap | None = None,
        audit_logger: AuditLogger | None = None,
    ) -> None:
        self._config = config
        self._discord = config.discord
        self._node_id = node_id
        self._bot_token = config.discord.bot_token
        self._channel_id = config.discord.channel_id
        self._threading = config.discord.threading

        if self._threading.enabled and thread_map is not None:
            self._thread_map = thread_map
        elif self._threading.enabled:
            self._thread_map = ThreadMap(
                max_age_hours=self._threading.max_age_hours,
                channel=self._discord.channel,
            )
        else:
            self._thread_map = None

        if audit_logger is not None:
            self._audit = audit_logger
        else:
            self._audit = AuditLogger(config.audit, node_id=node_id)

    @property
    def send_enabled(self) -> bool:
        """Whether send announcements should be published."""
        return (
            self._config.enabled
            and self._discord.enabled
            and self._discord.publish_send
        )

    @property
    def recv_enabled(self) -> bool:
        """Whether receive announcements should be published."""
        return (
            self._config.enabled
            and self._discord.enabled
            and self._discord.publish_receive
        )

    @property
    def threading_enabled(self) -> bool:
        """Whether threaded posting is active."""
        return (
            self._threading.enabled
            and self._bot_token is not None
            and self._channel_id is not None
        )

    async def announce_send(self, envelope: Envelope) -> None:
        """Announce an outbound send event. Non-fatal on failure."""
        if not self.send_enabled:
            return
        if envelope.ch in ("heartbeat", "status", "presence"):
            return
        text = format_send(envelope, gw=self._node_id)
        await self._publish(text, envelope, event="send")

    async def announce_recv(self, envelope: Envelope) -> None:
        """Announce an inbound receive event. Non-fatal on failure."""
        if not self.recv_enabled:
            return
        if envelope.ch in ("heartbeat", "status", "presence"):
            return
        text = format_recv(envelope, gw=self._node_id)
        await self._publish(text, envelope, event="recv")

    async def _publish(self, text: str, envelope: Envelope, *, event: str) -> None:
        """Route to threaded or channel-level posting."""
        if self.threading_enabled:
            await self._publish_threaded(text, envelope, event=event)
        else:
            await self._publish_discord(text)
            self._audit.record(envelope, event=event)

    # -- Threaded posting (Discord bot API) --

    async def _publish_threaded(self, text: str, envelope: Envelope, *, event: str) -> None:
        """Post into a correlated Discord thread. Falls back on failure."""
        thread_key = resolve_thread_key(envelope.corr, envelope.id)
        assert self._thread_map is not None

        # Thread message body is text-only per spec.
        body = _thread_message_body(envelope)

        try:
            loop = asyncio.get_running_loop()
            entry = self._thread_map.get(thread_key)

            if entry is not None:
                # Try posting into existing thread
                try:
                    await loop.run_in_executor(
                        None,
                        partial(self._post_bot_message, entry.thread_id, body),
                    )
                    log.debug("threaded announcement posted to thread=%s", entry.thread_id)
                    self._audit.record(envelope, event=event, thread_id=entry.thread_id)
                    return
                except Exception:
                    # Thread may be archived/deleted — remove and recreate below
                    log.debug("existing thread %s failed, will recreate", entry.thread_id)
                    self._thread_map.remove(thread_key)

            # Create parent message + thread
            parent_text = _format_thread_parent(envelope)
            parent_id = await loop.run_in_executor(
                None,
                partial(self._post_bot_message, self._channel_id, parent_text),
            )
            thread_name = f"{self._threading.thread_name_prefix} | {thread_key[:8]}"
            thread_id = await loop.run_in_executor(
                None,
                partial(self._create_thread, self._channel_id, parent_id, thread_name),
            )
            self._thread_map.put(
                thread_key,
                thread_id=thread_id,
                parent_message_id=parent_id,
            )
            # Post the actual event message into the new thread (text-only body)
            await loop.run_in_executor(
                None,
                partial(self._post_bot_message, thread_id, body),
            )
            log.debug("threaded announcement: created thread=%s for key=%s", thread_id, thread_key)
            self._audit.record(envelope, event=event, thread_id=thread_id)

        except Exception as exc:
            log.warning("threaded discord announcement failed: %s", exc)
            self._audit.record(envelope, event=event, status="error", error=str(exc))
            if self._threading.fallback_to_channel:
                await self._publish_discord(text)

    # -- Channel-level posting (webhook) --

    async def _publish_discord(self, text: str) -> None:
        """POST a message to the Discord webhook. Non-fatal on any error."""
        webhook_url = self._discord.webhook_url
        if not webhook_url:
            log.debug("discord announcement skipped: no webhook_url configured")
            return

        try:
            loop = asyncio.get_running_loop()
            await loop.run_in_executor(None, partial(self._post_webhook, webhook_url, text))
            log.debug("discord announcement posted: %s", text[:120])
        except Exception as exc:
            log.warning(
                "discord announcement failed (channel=%s): %s",
                self._discord.channel,
                exc,
            )

    # -- Low-level HTTP helpers --

    @staticmethod
    def _post_webhook(url: str, text: str) -> None:
        """Synchronous HTTP POST to Discord webhook (runs in executor)."""
        payload = json.dumps({"content": text}).encode()
        req = urllib.request.Request(
            url,
            data=payload,
            headers={
                "Content-Type": "application/json",
                "User-Agent": _USER_AGENT,
            },
            method="POST",
        )
        urllib.request.urlopen(req, timeout=10)

    def _post_bot_message(self, channel_or_thread_id: str, text: str) -> str:
        """Post a message via Discord bot API. Returns the message ID."""
        url = f"https://discord.com/api/v10/channels/{channel_or_thread_id}/messages"
        payload = json.dumps({"content": text}).encode()
        req = urllib.request.Request(
            url,
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bot {self._bot_token}",
                "User-Agent": _USER_AGENT,
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        return data["id"]

    def _create_thread(self, channel_id: str, message_id: str, name: str) -> str:
        """Create a thread from an existing message. Returns the thread ID."""
        url = f"https://discord.com/api/v10/channels/{channel_id}/messages/{message_id}/threads"
        payload = json.dumps({"name": name[:100]}).encode()
        req = urllib.request.Request(
            url,
            data=payload,
            headers={
                "Content-Type": "application/json",
                "Authorization": f"Bot {self._bot_token}",
                "User-Agent": _USER_AGENT,
            },
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read())
        return data["id"]
