"""Message envelope model for hive protocol v1."""

from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Any

SCHEMA_VERSION = 1

VALID_CHANNELS = frozenset({"command", "response", "sync", "heartbeat", "status", "alert"})
VALID_URGENCIES = frozenset({"now", "later"})


class Channel(str, Enum):
    """Logical message channels."""

    COMMAND = "command"
    RESPONSE = "response"
    SYNC = "sync"
    HEARTBEAT = "heartbeat"
    STATUS = "status"
    ALERT = "alert"


class Urgency(str, Enum):
    """Message urgency levels."""

    NOW = "now"
    LATER = "later"


class EnvelopeError(ValueError):
    """Raised when envelope validation fails."""


def _gen_id() -> str:
    return str(uuid.uuid4())


def _gen_ts() -> int:
    return int(time.time())


@dataclass(frozen=True, slots=True)
class Envelope:
    """Hive protocol v1 message envelope.

    Required fields: v, id, ts, from_, to, ch, urgency, text.
    Optional fields: corr, reply_to, ttl, action.

    The ``from_`` field maps to/from the JSON key ``"from"`` (a Python keyword).
    """

    v: int
    id: str
    ts: int
    from_: str
    to: str
    ch: str
    urgency: str
    text: str
    corr: str | None = None
    reply_to: str | None = None
    ttl: int | None = None
    action: str | None = None

    def __post_init__(self) -> None:
        if self.v != SCHEMA_VERSION:
            raise EnvelopeError(f"unsupported schema version: {self.v}")
        if not self.id:
            raise EnvelopeError("id is required")
        if not isinstance(self.ts, int) or self.ts <= 0:
            raise EnvelopeError("ts must be a positive integer")
        if not self.from_:
            raise EnvelopeError("from is required")
        if not self.to:
            raise EnvelopeError("to is required")
        if self.ch not in VALID_CHANNELS:
            raise EnvelopeError(f"invalid channel: {self.ch!r}, must be one of {sorted(VALID_CHANNELS)}")
        if self.urgency not in VALID_URGENCIES:
            raise EnvelopeError(f"invalid urgency: {self.urgency!r}, must be 'now' or 'later'")
        if not self.text:
            raise EnvelopeError("text is required")
        if self.ttl is not None and (not isinstance(self.ttl, int) or self.ttl < 0):
            raise EnvelopeError("ttl must be a non-negative integer")

    def to_json(self) -> dict[str, Any]:
        """Serialize to a JSON-compatible dict.

        Maps ``from_`` back to ``"from"`` and ``reply_to`` to ``"replyTo"``.
        Omits optional fields that are None.
        """
        d: dict[str, Any] = {
            "v": self.v,
            "id": self.id,
            "ts": self.ts,
            "from": self.from_,
            "to": self.to,
            "ch": self.ch,
            "urgency": self.urgency,
            "text": self.text,
        }
        if self.corr is not None:
            d["corr"] = self.corr
        if self.reply_to is not None:
            d["replyTo"] = self.reply_to
        if self.ttl is not None:
            d["ttl"] = self.ttl
        if self.action is not None:
            d["action"] = self.action
        return d

    @classmethod
    def from_json(cls, data: dict[str, Any]) -> Envelope:
        """Deserialize from a JSON-compatible dict.

        Maps ``"from"`` to ``from_`` and ``"replyTo"`` to ``reply_to``.
        Raises EnvelopeError on missing required fields or invalid data.
        """
        required = ("v", "id", "ts", "from", "to", "ch", "urgency", "text")
        missing = [f for f in required if f not in data]
        if missing:
            raise EnvelopeError(f"missing required fields: {missing}")

        return cls(
            v=data["v"],
            id=data["id"],
            ts=data["ts"],
            from_=data["from"],
            to=data["to"],
            ch=data["ch"],
            urgency=data["urgency"],
            text=data["text"],
            corr=data.get("corr"),
            reply_to=data.get("replyTo"),
            ttl=data.get("ttl"),
            action=data.get("action"),
        )


def create_envelope(
    *,
    from_: str,
    to: str,
    ch: str,
    text: str,
    urgency: str = "now",
    corr: str | None = None,
    ttl: int | None = None,
    action: str | None = None,
) -> Envelope:
    """Create a new envelope with auto-generated id and timestamp."""
    return Envelope(
        v=SCHEMA_VERSION,
        id=_gen_id(),
        ts=_gen_ts(),
        from_=from_,
        to=to,
        ch=ch,
        urgency=urgency,
        text=text,
        corr=corr,
        ttl=ttl,
        action=action,
    )


def create_reply(
    original: Envelope,
    *,
    from_: str,
    text: str,
    urgency: str = "now",
) -> Envelope:
    """Create a reply envelope for an existing message.

    Sets ``to`` to the original sender, ``corr`` to the original's corr (or id),
    and ``reply_to`` to the original's id.
    """
    return Envelope(
        v=SCHEMA_VERSION,
        id=_gen_id(),
        ts=_gen_ts(),
        from_=from_,
        to=original.from_,
        ch="response",
        urgency=urgency,
        text=text,
        corr=original.corr or original.id,
        reply_to=original.id,
    )
