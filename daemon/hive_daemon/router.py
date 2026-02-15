"""Message routing by channel.

The router maps each channel to a handler coroutine. Channels that require
OC involvement (command, alert) are routed differently from deterministic
channels (heartbeat, sync).
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from typing import Any

from hive_daemon.envelope import Envelope

log = logging.getLogger(__name__)

# Type alias for channel handler coroutines.
# Each receives the envelope and the MQTT topic target (addressee), returns nothing.
ChannelHandler = Callable[[Envelope, str], Awaitable[None]]


class Router:
    """Routes inbound envelopes to registered channel handlers.

    Register handlers with ``register(channel, handler_coro)``.
    Dispatch with ``route(envelope, target=...)`` — calls the handler for
    the envelope's channel, passing along the topic target so handlers can
    route to the correct OC instance.
    """

    def __init__(self) -> None:
        self._handlers: dict[str, ChannelHandler] = {}

    def register(self, channel: str, handler: ChannelHandler) -> None:
        """Register a handler coroutine for a channel."""
        self._handlers[channel] = handler
        log.debug("registered handler for channel %r", channel)

    async def route(self, envelope: Envelope, *, target: str = "") -> None:
        """Route an envelope to the appropriate channel handler.

        Args:
            envelope: The message envelope.
            target: The MQTT topic target (e.g. instance name or "all").

        Logs a warning if no handler is registered for the channel.
        """
        handler = self._handlers.get(envelope.ch)
        if handler is None:
            log.warning("no handler registered for channel %r, dropping message %s", envelope.ch, envelope.id)
            return
        log.info("routing message %s on channel %r from %s", envelope.id, envelope.ch, envelope.from_)
        await handler(envelope, target)
