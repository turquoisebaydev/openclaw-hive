"""Discord master action execution for hive-daemon.

Provides deterministic handlers for Discord thread/message operations using a
single bot token on a designated "master" node.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any
from urllib import error, parse, request

from hive_daemon.config import DiscordMasterConfig
from hive_daemon.envelope import Envelope


class DiscordMasterError(RuntimeError):
    """Raised when Discord master actions fail."""


@dataclass(slots=True)
class DiscordMasterService:
    config: DiscordMasterConfig

    def available(self) -> bool:
        return bool(self.config.enabled and self.config.guild_id and self.config.bot_token)

    def execute(self, envelope: Envelope) -> dict[str, Any]:
        """Execute a deterministic discord.* action.

        Payload convention: ``envelope.text`` should be JSON for complex args.
        """
        if not self.available():
            raise DiscordMasterError("discord_master is not enabled/configured")

        action = envelope.action or ""
        payload = self._parse_payload(envelope.text)

        if action == "discord.thread.resolve":
            return self._thread_resolve(payload)
        if action == "discord.thread.history":
            return self._thread_history(payload)
        if action == "discord.thread.send":
            return self._thread_send(payload)
        if action == "discord.mention.resolve":
            return self._mention_resolve(payload)

        raise DiscordMasterError(f"unsupported discord action: {action}")

    def _parse_payload(self, text: str) -> dict[str, Any]:
        try:
            value = json.loads(text)
            return value if isinstance(value, dict) else {"value": value}
        except json.JSONDecodeError:
            return {"text": text}

    def _request_json(self, method: str, path: str, data: dict[str, Any] | None = None) -> Any:
        base = self.config.api_base.rstrip("/")
        url = f"{base}{path}"
        body = None
        headers = {
            "Authorization": f"Bot {self.config.bot_token}",
            "Content-Type": "application/json",
            "User-Agent": "openclaw-hive-discord-master/1.0",
        }
        if data is not None:
            body = json.dumps(data).encode("utf-8")

        req = request.Request(url, data=body, method=method.upper(), headers=headers)
        timeout = max(int(self.config.request_timeout_sec), 1)
        try:
            with request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
            if not raw:
                return {}
            return json.loads(raw.decode("utf-8"))
        except error.HTTPError as exc:
            body_text = exc.read().decode("utf-8", errors="replace")
            raise DiscordMasterError(f"discord api {exc.code} {path}: {body_text}") from exc
        except error.URLError as exc:
            raise DiscordMasterError(f"discord api unavailable: {exc}") from exc

    def _thread_resolve(self, payload: dict[str, Any]) -> dict[str, Any]:
        thread_id = str(payload.get("thread_id") or payload.get("threadId") or "").strip()
        if thread_id:
            thread = self._request_json("GET", f"/channels/{thread_id}")
            return {
                "ok": True,
                "thread": {
                    "id": thread.get("id"),
                    "name": thread.get("name"),
                    "parent_id": thread.get("parent_id"),
                    "archived": bool((thread.get("thread_metadata") or {}).get("archived", False)),
                },
            }

        wanted = str(payload.get("thread_name") or payload.get("threadName") or "").strip()
        if not wanted:
            raise DiscordMasterError("discord.thread.resolve requires thread_id or thread_name")

        active = self._request_json("GET", f"/guilds/{self.config.guild_id}/threads/active")
        threads = active.get("threads", []) if isinstance(active, dict) else []
        wanted_norm = wanted.casefold()

        exact = next((t for t in threads if str(t.get("name", "")).casefold() == wanted_norm), None)
        match = exact or next((t for t in threads if wanted_norm in str(t.get("name", "")).casefold()), None)
        if match is None:
            return {"ok": False, "error": "thread_not_found", "thread_name": wanted}

        return {
            "ok": True,
            "thread": {
                "id": match.get("id"),
                "name": match.get("name"),
                "parent_id": match.get("parent_id"),
                "archived": bool((match.get("thread_metadata") or {}).get("archived", False)),
            },
        }

    def _thread_history(self, payload: dict[str, Any]) -> dict[str, Any]:
        thread_id = str(payload.get("thread_id") or payload.get("threadId") or "").strip()
        if not thread_id:
            raise DiscordMasterError("discord.thread.history requires thread_id")

        limit = int(payload.get("limit", 20))
        limit = max(1, min(limit, 100))
        qs = parse.urlencode({"limit": limit})
        messages = self._request_json("GET", f"/channels/{thread_id}/messages?{qs}")
        items: list[dict[str, Any]] = []
        for msg in messages if isinstance(messages, list) else []:
            author = msg.get("author") or {}
            items.append(
                {
                    "id": msg.get("id"),
                    "timestamp": msg.get("timestamp"),
                    "content": msg.get("content") or "",
                    "author": {
                        "id": author.get("id"),
                        "username": author.get("username"),
                        "bot": bool(author.get("bot", False)),
                    },
                }
            )

        return {"ok": True, "thread_id": thread_id, "count": len(items), "messages": items}

    def _thread_send(self, payload: dict[str, Any]) -> dict[str, Any]:
        thread_id = str(payload.get("thread_id") or payload.get("threadId") or "").strip()
        content = payload.get("content") or payload.get("message") or payload.get("text")
        if not thread_id:
            raise DiscordMasterError("discord.thread.send requires thread_id")
        if not isinstance(content, str) or not content.strip():
            raise DiscordMasterError("discord.thread.send requires non-empty content/message")

        sent = self._request_json("POST", f"/channels/{thread_id}/messages", data={"content": content})
        return {
            "ok": True,
            "thread_id": thread_id,
            "message": {
                "id": sent.get("id"),
                "channel_id": sent.get("channel_id"),
                "content": sent.get("content") or "",
            },
        }

    def _mention_from_target(self, target: str, mention_type: str = "auto") -> str | None:
        t = target.strip()
        if not t:
            return None
        if re.match(r"^<@&?\d+>$", t):
            return t
        if t.startswith("user:") and t[5:].isdigit():
            return f"<@{t[5:]}>"
        if t.startswith("role:") and t[5:].isdigit():
            return f"<@&{t[5:]}>"
        if t.isdigit():
            return f"<@&{t}>" if mention_type == "role" else f"<@{t}>"
        return None

    def _mention_resolve(self, payload: dict[str, Any]) -> dict[str, Any]:
        mention_type = str(payload.get("mention_type") or payload.get("mentionType") or "auto").strip() or "auto"

        channel_name = str(payload.get("channel") or "").strip()
        if channel_name:
            channel_cfg = next((c for c in self.config.channels if c.name == channel_name), None)
            if channel_cfg and channel_cfg.mention_target:
                mention = self._mention_from_target(channel_cfg.mention_target, channel_cfg.mention_type)
                if mention:
                    return {
                        "ok": True,
                        "source": "channel_config",
                        "mention": mention,
                        "mention_type": channel_cfg.mention_type,
                        "target": channel_cfg.mention_target,
                    }

        explicit = str(payload.get("mention_target") or payload.get("mention") or "").strip()
        if explicit:
            mention = self._mention_from_target(explicit, mention_type)
            if mention:
                return {
                    "ok": True,
                    "source": "explicit",
                    "mention": mention,
                    "mention_type": mention_type,
                    "target": explicit,
                }

        query = str(payload.get("query") or payload.get("name") or "").strip()
        if not query:
            raise DiscordMasterError("discord.mention.resolve requires mention_target, channel, or query")

        members = self._request_json(
            "GET",
            f"/guilds/{self.config.guild_id}/members/search?{parse.urlencode({'query': query, 'limit': 8})}",
        )
        if isinstance(members, list) and members:
            m = members[0]
            user = m.get("user") or {}
            user_id = user.get("id")
            if user_id:
                return {
                    "ok": True,
                    "source": "member_search",
                    "mention": f"<@{user_id}>",
                    "mention_type": "user",
                    "id": user_id,
                    "name": user.get("username") or m.get("nick") or query,
                }

        roles = self._request_json("GET", f"/guilds/{self.config.guild_id}/roles")
        if isinstance(roles, list):
            q = query.casefold()
            role = next((r for r in roles if str(r.get("name", "")).casefold() == q), None)
            role = role or next((r for r in roles if q in str(r.get("name", "")).casefold()), None)
            if role and role.get("id"):
                rid = role["id"]
                return {
                    "ok": True,
                    "source": "role_search",
                    "mention": f"<@&{rid}>",
                    "mention_type": "role",
                    "id": rid,
                    "name": role.get("name") or query,
                }

        return {"ok": False, "error": "mention_not_found", "query": query}
