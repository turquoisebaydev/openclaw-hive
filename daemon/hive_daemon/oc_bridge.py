"""OpenClaw agent turn injection bridge.

Injects hive messages into local OpenClaw instances by running
``openclaw [--profile X] agent --agent default --message "..."`` as a subprocess.

Previous versions used ``system event`` which only queues text passively;
the ``agent`` command triggers a real LLM turn so the message is processed
immediately.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from datetime import datetime, timezone

from hive_daemon.config import OcInstance
from hive_daemon.envelope import Envelope

log = logging.getLogger(__name__)

# Agent turns involve LLM processing — allow generous timeout.
DEFAULT_TIMEOUT = 300

# Hard hint included in injected text so OC reliably loads the hive-member skill.
# Keep it short: it is paid every injection.
_HIVE_SKILL_HINT = "Use the hive-member skill for protocol details (esp. hive-cli reply/send)."

# Env vars injected into openclaw subprocess for self-signed cert compat.
_SUBPROCESS_ENV: dict[str, str] | None = None


def _get_subprocess_env() -> dict[str, str]:
    """Build subprocess environment with NODE_TLS_REJECT_UNAUTHORIZED=0."""
    global _SUBPROCESS_ENV
    if _SUBPROCESS_ENV is None:
        _SUBPROCESS_ENV = {**os.environ, "NODE_TLS_REJECT_UNAUTHORIZED": "0"}
    return _SUBPROCESS_ENV


class OcBridge:
    """Bridges hive messages into local OpenClaw instances via CLI.

    Runs ``openclaw agent --agent default --message ...`` as an async
    subprocess for each configured OC instance. This triggers a real
    agent turn so the gateway processes the message immediately.

    Args:
        oc_instances: List of local OC instances from config.
        timeout: Subprocess timeout in seconds (default 300).
    """

    def __init__(
        self,
        oc_instances: list[OcInstance],
        timeout: int = DEFAULT_TIMEOUT,
    ) -> None:
        self._instances = oc_instances
        self._timeout = timeout

    @staticmethod
    def format_event_text(envelope: Envelope, prefix: str = "") -> str:
        """Format envelope into message text with hive metadata.

        Prepends ``[hive:{from}->{to} ch:{ch}]`` + a skill hint + optional
        prefix to the envelope text.

        Appends ``ENVELOPE_JSON:`` with the raw envelope so the receiving OC
        agent can use ``hive-cli reply``.
        """
        meta = f"[hive:{envelope.from_}->{envelope.to} ch:{envelope.ch}]"

        parts: list[str] = [meta]
        if prefix:
            parts.append(prefix)
        parts.append(_HIVE_SKILL_HINT)
        parts.append(envelope.text)

        readable = " ".join(parts)
        envelope_json = json.dumps(envelope.to_json(), separators=(",", ":"))
        return f"{readable}\nENVELOPE_JSON:{envelope_json}"

    @staticmethod
    def _session_id_for_instance(instance: OcInstance) -> str:
        """Choose a stable session id for hive injections.

        OpenClaw snapshots eligible skills at session creation time.
        If hive-member was added after the session existed, injections can
        land in a stale session where hive-member is missing from
        <available_skills>.

        To avoid that, we pin to a *daily* session id per instance.
        """
        day = datetime.now(timezone.utc).strftime("%Y%m%d")
        return f"hive-{instance.name}-{day}"

    def _build_command(self, instance: OcInstance, text: str) -> list[str]:
        """Build the openclaw CLI command for a given instance."""
        cmd = ["openclaw"]
        if instance.profile:
            cmd.extend(["--profile", instance.profile])

        session_id = self._session_id_for_instance(instance)

        # Use --json so we can parse & log session/skills diagnostics.
        # Thinking minimal keeps tool-driving deterministic for protocol glue.
        cmd.extend(
            [
                "agent",
                "--agent",
                "default",
                "--session-id",
                session_id,
                "--thinking",
                "minimal",
                "--json",
                "--message",
                text,
            ]
        )
        return cmd

    async def inject_event(
        self,
        text: str,
        instance_name: str | None = None,
    ) -> None:
        """Inject an agent message into OC instance(s).

        Fires off the agent turn as a background task (does not block
        waiting for the LLM to finish). The turn runs asynchronously
        in the gateway.

        Args:
            text: The message text to send.
            instance_name: Target a specific instance by name.
                If None, injects to all configured instances.
        """
        if not self._instances:
            log.warning("no OC instances configured, skipping event injection")
            return

        targets = self._instances
        if instance_name is not None:
            targets = [i for i in self._instances if i.name == instance_name]
            if not targets:
                log.error("OC instance %r not found in config", instance_name)
                return

        for instance in targets:
            # Fire-and-forget: launch as background task so we don't
            # block the daemon while the LLM processes.
            asyncio.create_task(
                self._inject_to_instance(instance, text),
                name=f"oc-inject-{instance.name}",
            )

    async def _inject_to_instance(self, instance: OcInstance, text: str) -> None:
        """Run the openclaw CLI command for a single instance."""
        cmd = self._build_command(instance, text)
        session_id = self._session_id_for_instance(instance)

        log.info(
            "injecting agent message to OC instance %r (session=%s): %s",
            instance.name,
            session_id,
            " ".join(cmd[:6]) + " ...",  # log command prefix only (text may be large)
        )

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=_get_subprocess_env(),
            )

            try:
                stdout, stderr = await asyncio.wait_for(
                    proc.communicate(),
                    timeout=self._timeout,
                )
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                log.error(
                    "OC agent injection timed out for instance %r after %ds (session=%s)",
                    instance.name,
                    self._timeout,
                    session_id,
                )
                return

            stdout_text = stdout.decode(errors="replace").strip()
            stderr_text = stderr.decode(errors="replace").strip()

            if stderr_text:
                log.warning(
                    "OC inject stderr for instance %r (session=%s): %s",
                    instance.name,
                    session_id,
                    stderr_text[:800],
                )

            if proc.returncode == 0:
                # Parse JSON so we can confirm hive-member is present in the prompt.
                try:
                    data = json.loads(stdout_text)
                    result = data.get("result") or {}
                    meta = result.get("meta") or {}
                    agent_meta = meta.get("agentMeta") or {}
                    sid = agent_meta.get("sessionId")

                    skills_obj = (meta.get("systemPromptReport") or {}).get("skills") or {}
                    skills_entries = skills_obj.get("entries") or []
                    skill_names = [e.get("name") for e in skills_entries if isinstance(e, dict)]

                    payloads = result.get("payloads") or []
                    reply_preview = ""
                    if payloads and isinstance(payloads[0], dict):
                        reply_preview = (payloads[0].get("text") or "")[:200]

                    log.info(
                        "OC inject ok instance=%r session=%s reportedSession=%s skills=%s reply=%r",
                        instance.name,
                        session_id,
                        sid,
                        ",".join([s for s in skill_names if s]) or "(none)",
                        reply_preview,
                    )
                except Exception as exc:
                    log.info(
                        "OC inject ok instance=%r session=%s (stdout not parsed as JSON: %s). stdout=%r",
                        instance.name,
                        session_id,
                        exc,
                        stdout_text[:500],
                    )
            else:
                log.error(
                    "OC agent injection failed for instance %r (session=%s) (exit %d). stdout=%r stderr=%r",
                    instance.name,
                    session_id,
                    proc.returncode,
                    stdout_text[:500],
                    stderr_text[:500],
                )

        except FileNotFoundError:
            log.error(
                "openclaw CLI not found — OC may not be installed. "
                "Skipping injection for instance %r",
                instance.name,
            )
        except OSError as exc:
            log.error(
                "failed to run openclaw for instance %r: %s",
                instance.name,
                exc,
            )

    async def inject_envelope(
        self,
        envelope: Envelope,
        prefix: str = "",
        instance_name: str | None = None,
    ) -> None:
        """Format an envelope and inject it as an agent message.

        Convenience method that formats the envelope text with hive
        metadata before injecting.
        """
        text = self.format_event_text(envelope, prefix=prefix)
        await self.inject_event(text, instance_name=instance_name)
