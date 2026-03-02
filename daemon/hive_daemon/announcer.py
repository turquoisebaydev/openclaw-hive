"""Optional Discord announcements for hive send/receive events.

Publishes compact one-line summaries to a Discord webhook when enabled.
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

from hive_daemon.config import AnnouncementsConfig
from hive_daemon.envelope import Envelope

log = logging.getLogger(__name__)

# Maximum text length included in announcements (avoid leaking large payloads).
_MAX_TEXT_LEN = 80


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


class Announcer:
    """Publishes hive event announcements to Discord.

    Safe to call even when disabled — methods return immediately.
    All publish errors are caught and logged as warnings.

    Args:
        config: The announcements configuration block.
    """

    def __init__(self, config: AnnouncementsConfig, *, node_id: str | None = None) -> None:
        self._config = config
        self._discord = config.discord
        self._node_id = node_id

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

    async def announce_send(self, envelope: Envelope) -> None:
        """Announce an outbound send event. Non-fatal on failure."""
        if not self.send_enabled:
            return
        if envelope.ch == "heartbeat":
            return
        text = format_send(envelope, gw=self._node_id)
        await self._publish_discord(text)

    async def announce_recv(self, envelope: Envelope) -> None:
        """Announce an inbound receive event. Non-fatal on failure."""
        if not self.recv_enabled:
            return
        if envelope.ch == "heartbeat":
            return
        text = format_recv(envelope, gw=self._node_id)
        await self._publish_discord(text)

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

    @staticmethod
    def _post_webhook(url: str, text: str) -> None:
        """Synchronous HTTP POST to Discord webhook (runs in executor)."""
        payload = json.dumps({"content": text}).encode()
        req = urllib.request.Request(
            url,
            data=payload,
            headers={
                "Content-Type": "application/json",
                # Discord/webhook edge can reject default Python urllib UA.
                # Use an explicit UA to align with successful curl/manual calls.
                "User-Agent": "openclaw-hive-daemon/0.1 (+https://github.com/turquoisebaydev/openclaw-hive)",
            },
            method="POST",
        )
        urllib.request.urlopen(req, timeout=10)
