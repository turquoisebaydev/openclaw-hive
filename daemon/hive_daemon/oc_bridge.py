"""OpenClaw agent turn injection bridge.

Injects hive messages into local OpenClaw instances by running
``openclaw [--profile X] agent --agent default --message "..."`` as a subprocess.

Previous versions used ``system event`` which only queues text passively;
the ``agent`` command triggers a real LLM turn so the message is processed
immediately.
"""

from __future__ import annotations

import asyncio
import logging
import os

from hive_daemon.config import OcInstance
from hive_daemon.envelope import Envelope

log = logging.getLogger(__name__)

# Agent turns involve LLM processing — allow generous timeout.
DEFAULT_TIMEOUT = 300

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
    subprocess for each configured OC instance.  This triggers a real
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

        Prepends ``[hive:{from}->{to} ch:{ch}]`` and optional prefix to
        the envelope text.  Appends ``ENVELOPE_JSON:`` with the raw
        envelope so the receiving OC agent can use ``hive-cli reply``.
        """
        import json

        meta = f"[hive:{envelope.from_}->{envelope.to} ch:{envelope.ch}]"
        parts = [meta]
        if prefix:
            parts.append(prefix)
        parts.append(envelope.text)
        readable = " ".join(parts)
        envelope_json = json.dumps(envelope.to_json(), separators=(",", ":"))
        return f"{readable}\nENVELOPE_JSON:{envelope_json}"

    def _build_command(self, instance: OcInstance, text: str) -> list[str]:
        """Build the openclaw CLI command for a given instance."""
        cmd = ["openclaw"]
        if instance.profile:
            cmd.extend(["--profile", instance.profile])
        cmd.extend(["agent", "--agent", "default", "--message", text])
        return cmd

    async def inject_event(
        self,
        text: str,
        instance_name: str | None = None,
    ) -> None:
        """Inject an agent message into OC instance(s).

        Fires off the agent turn as a background task (does not block
        waiting for the LLM to finish).  The turn runs asynchronously
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
        log.info(
            "injecting agent message to OC instance %r: %s",
            instance.name,
            " ".join(cmd[:4]) + " ...",  # log command prefix only (text may be large)
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
                    "OC agent injection timed out for instance %r after %ds",
                    instance.name,
                    self._timeout,
                )
                return

            if proc.returncode == 0:
                log.info("agent message processed by OC instance %r", instance.name)
            else:
                stderr_text = stderr.decode(errors="replace").strip()
                log.error(
                    "OC agent injection failed for instance %r (exit %d): %s",
                    instance.name,
                    proc.returncode,
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
