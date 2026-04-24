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
        if action == "discord.thread.rename":
            return self._thread_rename(payload)
        if action == "discord.thread.create":
            return self._thread_create(payload)
        if action == "discord.thread.list":
            return self._thread_list(payload)
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


    def _user_id_from_target(self, target: str) -> str | None:
        t = (target or "").strip()
        if t.startswith("user:") and t[5:].isdigit():
            return t[5:]
        m = re.match(r"^<@(\d+)>$", t)
        if m:
            return m.group(1)
        if t.isdigit():
            return t
        return None

    def _base_channel_name(self, parent_name: str, parent_suffix: str) -> str:
        if parent_suffix and parent_name.endswith(parent_suffix):
            return parent_name[: -len(parent_suffix)]
        return parent_name

    def _iter_alias_candidates(self, value: str | None) -> list[str]:
        raw = (value or "").strip()
        if not raw:
            return []

        out: list[str] = []

        def add(v: str | None) -> None:
            vv = (v or "").strip()
            if vv and vv not in out:
                out.append(vv)

        add(raw)

        suffixes = {
            self.config.default_parent_suffix,
            self.config.default_thread_suffix,
            "-proj",
            "-hive",
            "-init",
        }
        for sx in suffixes:
            if sx and raw.endswith(sx):
                add(raw[: -len(sx)])

        for cand in list(out):
            if "-" in cand:
                add(cand.rsplit("-", 1)[-1])

        return out

    def _resolve_alias_mention(self, *values: str | None) -> dict[str, Any] | None:
        aliases = {a.name.casefold(): a for a in self.config.aliases}
        if not aliases:
            return None

        seen: set[str] = set()
        for value in values:
            for cand in self._iter_alias_candidates(value):
                key = cand.casefold()
                if key in seen:
                    continue
                seen.add(key)
                alias_cfg = aliases.get(key)
                if not alias_cfg:
                    continue
                mention = self._mention_from_target(alias_cfg.mention_target, alias_cfg.mention_type)
                if mention:
                    return {
                        "mention": mention,
                        "mention_user_id": self._user_id_from_target(alias_cfg.mention_target),
                        "mention_target": alias_cfg.mention_target,
                        "mention_source": f"alias:{alias_cfg.name}",
                    }
        return None

    def _channel_mention_info(self, parent_name: str, parent_suffix: str, thread_name: str | None = None, thread_suffix: str | None = None) -> dict[str, Any]:
        candidates = []
        parent_base = self._base_channel_name(parent_name, parent_suffix)
        if parent_base:
            candidates.append(parent_base)
        if thread_name:
            tsfx = thread_suffix if thread_suffix is not None else self.config.default_thread_suffix
            thread_base = thread_name
            if tsfx and thread_base.endswith(tsfx):
                thread_base = thread_base[: -len(tsfx)]
            if thread_base:
                candidates.append(thread_base)

        alias_info = self._resolve_alias_mention(parent_name, parent_base, thread_name, *(candidates or []))
        if alias_info:
            return alias_info

        channel_cfg = next((c for c in self.config.channels if c.name.casefold() in {x.casefold() for x in candidates}), None)
        if channel_cfg and channel_cfg.mention_target:
            mention = self._mention_from_target(channel_cfg.mention_target, channel_cfg.mention_type)
            mention_user_id = self._user_id_from_target(channel_cfg.mention_target)
            return {
                "mention": mention,
                "mention_user_id": mention_user_id,
                "mention_target": channel_cfg.mention_target,
                "mention_source": f"channel:{channel_cfg.name}",
            }

        # Convention fallback: use parent base name (text before -hive),
        # e.g. "hermes-hive" -> query "hermes".
        if parent_base:
            try:
                resolved = self._mention_resolve({"query": parent_base, "mention_type": "auto"})
            except Exception:
                resolved = {"ok": False}
            if isinstance(resolved, dict) and resolved.get("ok") is True:
                return {
                    "mention": resolved.get("mention"),
                    "mention_user_id": resolved.get("id"),
                    "mention_target": f"auto:query:{parent_base}",
                    "mention_source": "query_fallback",
                }

        return {"mention": None, "mention_user_id": None, "mention_target": None, "mention_source": None}


    def _thread_resolve(self, payload: dict[str, Any]) -> dict[str, Any]:
        thread_id = str(payload.get("thread_id") or payload.get("threadId") or "").strip()
        if thread_id:
            thread = self._request_json("GET", f"/channels/{thread_id}")
            parent_id = thread.get("parent_id")
            parent = self._request_json("GET", f"/channels/{parent_id}") if parent_id else {}
            parent_name = str(parent.get("name") or "")
            mention_info = self._channel_mention_info(parent_name, self.config.default_parent_suffix, thread.get("name"), self.config.default_thread_suffix)
            return {
                "ok": True,
                "thread": {
                    "id": thread.get("id"),
                    "name": thread.get("name"),
                    "parent_id": parent_id,
                    "parent_name": parent_name,
                    "archived": bool((thread.get("thread_metadata") or {}).get("archived", False)),
                    **mention_info,
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

        parent_id = match.get("parent_id")
        parent = self._request_json("GET", f"/channels/{parent_id}") if parent_id else {}
        parent_name = str(parent.get("name") or "")
        mention_info = self._channel_mention_info(parent_name, self.config.default_parent_suffix, match.get("name"), self.config.default_thread_suffix)
        return {
            "ok": True,
            "thread": {
                "id": match.get("id"),
                "name": match.get("name"),
                "parent_id": parent_id,
                "parent_name": parent_name,
                "archived": bool((match.get("thread_metadata") or {}).get("archived", False)),
                **mention_info,
            },
        }


    def _thread_list(self, payload: dict[str, Any]) -> dict[str, Any]:
        parent_suffix = str(payload.get("parent_suffix") or payload.get("parentSuffix") or self.config.default_parent_suffix)
        thread_suffix = str(payload.get("thread_suffix") or payload.get("threadSuffix") or self.config.default_thread_suffix)
        include_channels = payload.get("include_channels", True)

        guild_channels = self._request_json("GET", f"/guilds/{self.config.guild_id}/channels")
        channel_by_id = {str(c.get("id")): c for c in guild_channels if isinstance(c, dict) and c.get("id")}

        items: list[dict[str, Any]] = []

        # Top-level guild channels matching thread_suffix (e.g. *-proj channels).
        if include_channels:
            _TEXT_CHANNEL_TYPE = 0
            for c in guild_channels:
                if not isinstance(c, dict) or c.get("type") != _TEXT_CHANNEL_TYPE:
                    continue
                cname = str(c.get("name") or "")
                if thread_suffix and not cname.endswith(thread_suffix):
                    continue
                mention_info = self._channel_mention_info(cname, thread_suffix)
                items.append(
                    {
                        "id": c.get("id"),
                        "name": cname,
                        "parent_id": None,
                        "parent_name": None,
                        "type": "channel",
                        "archived": False,
                        **mention_info,
                    }
                )

        # Active threads within parent channels matching parent_suffix.
        active = self._request_json("GET", f"/guilds/{self.config.guild_id}/threads/active")
        threads = active.get("threads", []) if isinstance(active, dict) else []

        for t in threads:
            tname = str(t.get("name") or "")
            parent_id = str(t.get("parent_id") or "")
            parent = channel_by_id.get(parent_id, {})
            pname = str(parent.get("name") or "")
            if parent_suffix and not pname.endswith(parent_suffix):
                continue
            if thread_suffix and not tname.endswith(thread_suffix):
                continue
            mention_info = self._channel_mention_info(pname, parent_suffix, tname, thread_suffix)
            items.append(
                {
                    "id": t.get("id"),
                    "name": tname,
                    "parent_id": parent_id,
                    "parent_name": pname,
                    "type": "thread",
                    "archived": bool((t.get("thread_metadata") or {}).get("archived", False)),
                    **mention_info,
                }
            )

        items.sort(key=lambda x: (x.get("type") or "", x.get("parent_name") or "", x.get("name") or ""))
        return {
            "ok": True,
            "count": len(items),
            "parent_suffix": parent_suffix,
            "thread_suffix": thread_suffix,
            "threads": items,
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

    def _thread_rename(self, payload: dict[str, Any]) -> dict[str, Any]:
        thread_id = str(payload.get("thread_id") or payload.get("threadId") or "").strip()
        new_name = str(payload.get("new_name") or payload.get("newName") or payload.get("name") or "").strip()
        if not thread_id:
            raise DiscordMasterError("discord.thread.rename requires thread_id")
        if not new_name:
            raise DiscordMasterError("discord.thread.rename requires new_name")
        if len(new_name) > 100:
            raise DiscordMasterError("discord.thread.rename new_name must be <= 100 chars")

        updated = self._request_json("PATCH", f"/channels/{thread_id}", data={"name": new_name})
        return {
            "ok": True,
            "thread": {
                "id": updated.get("id") or thread_id,
                "name": updated.get("name") or new_name,
                "parent_id": updated.get("parent_id"),
            },
        }

    def _thread_create(self, payload: dict[str, Any]) -> dict[str, Any]:
        parent_id = str(
            payload.get("parent_id")
            or payload.get("parentId")
            or payload.get("channel_id")
            or payload.get("channelId")
            or self.config.default_hive_channel_id
            or ""
        ).strip()
        name = str(payload.get("name") or payload.get("thread_name") or payload.get("threadName") or "").strip()
        content = str(payload.get("content") or payload.get("message") or payload.get("text") or "").strip()

        if not parent_id:
            raise DiscordMasterError(
                "discord.thread.create requires parent_id (or configure discord_master.default_hive_channel_id)"
            )
        if not name:
            raise DiscordMasterError("discord.thread.create requires name")
        if len(name) > 100:
            raise DiscordMasterError("discord.thread.create name must be <= 100 chars")

        mentions = self._build_mentions(payload)
        mention_prefix = " ".join(mentions).strip()
        final_content = " ".join([x for x in [mention_prefix, content] if x]).strip()

        parent = self._request_json("GET", f"/channels/{parent_id}")
        parent_type = int(parent.get("type", 0)) if isinstance(parent, dict) else 0

        # Forum channels (type 15) require create-thread-with-message.
        if parent_type == 15:
            body: dict[str, Any] = {"name": name, "message": {"content": final_content or name}}
            thread_meta = payload.get("thread_metadata")
            if isinstance(thread_meta, dict):
                body.update(thread_meta)
            created = self._request_json("POST", f"/channels/{parent_id}/threads", data=body)
            return {
                "ok": True,
                "thread": {
                    "id": created.get("id"),
                    "name": created.get("name") or name,
                    "parent_id": created.get("parent_id") or parent_id,
                },
                "mentions": mentions,
            }

        # Text channel path: create a starter message, then spin a thread from it.
        starter = self._request_json(
            "POST",
            f"/channels/{parent_id}/messages",
            data={"content": final_content or name},
        )
        starter_id = str(starter.get("id") or "").strip()
        if not starter_id:
            raise DiscordMasterError("discord.thread.create failed to create starter message")

        thread_payload: dict[str, Any] = {"name": name}
        auto_archive_duration = payload.get("auto_archive_duration") or payload.get("autoArchiveDuration")
        if auto_archive_duration is not None:
            thread_payload["auto_archive_duration"] = int(auto_archive_duration)

        created = self._request_json(
            "POST",
            f"/channels/{parent_id}/messages/{starter_id}/threads",
            data=thread_payload,
        )
        return {
            "ok": True,
            "thread": {
                "id": created.get("id"),
                "name": created.get("name") or name,
                "parent_id": created.get("parent_id") or parent_id,
            },
            "starter_message_id": starter_id,
            "mentions": mentions,
        }

    def _build_mentions(self, payload: dict[str, Any]) -> list[str]:
        mentions: list[str] = []

        def add(m: str | None) -> None:
            mm = (m or "").strip()
            if mm and mm not in mentions:
                mentions.append(mm)

        targets = payload.get("mention_targets") or payload.get("mentionTargets") or []
        if isinstance(targets, list):
            for target in targets:
                mention = self._mention_from_target(str(target), "auto")
                add(mention)

        queries = payload.get("mention_queries") or payload.get("mentionQueries") or []
        if isinstance(queries, list):
            for query in queries:
                q = str(query).strip()
                if not q:
                    continue
                resolved = self._mention_resolve({"query": q, "mention_type": "auto"})
                if isinstance(resolved, dict) and resolved.get("ok") is True:
                    add(str(resolved.get("mention") or ""))

        return mentions

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
            alias_info = self._resolve_alias_mention(channel_name)
            if alias_info and alias_info.get("mention"):
                target = str(alias_info.get("mention_target") or "")
                return {
                    "ok": True,
                    "source": str(alias_info.get("mention_source") or "alias"),
                    "mention": alias_info["mention"],
                    "mention_type": "role" if target.startswith("role:") else "user" if target.startswith("user:") else mention_type,
                    "target": target,
                }

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
