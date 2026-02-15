"""OpenClaw system event injection bridge.

Injects hive messages into local OpenClaw instances by running
``openclaw [--profile X] system event --text "..."`` as a subprocess.
"""

from __future__ import annotations

import asyncio
import logging
import shlex

from hive_daemon.config import OcInstance
from hive_daemon.envelope import Envelope

log = logging.getLogger(__name__)


class OcBridge:
    """Bridges hive messages into local OpenClaw instances via CLI.

    Runs ``openclaw system event --text ...`` as an async subprocess for
    each configured OC instance.

    Args:
        oc_instances: List of local OC instances from config.
        timeout: Subprocess timeout in seconds (default 30).
    """

    def __init__(
        self,
        oc_instances: list[OcInstance],
        timeout: int = 30,
    ) -> None:
        self._instances = oc_instances
        self._timeout = timeout

    @staticmethod
    def format_event_text(envelope: Envelope, prefix: str = "") -> str:
        """Format envelope into system event text with hive metadata.

        Prepends ``[hive:{from}->{to} ch:{ch}]`` and optional prefix to
        the envelope text.
        """
        meta = f"[hive:{envelope.from_}->{envelope.to} ch:{envelope.ch}]"
        parts = [meta]
        if prefix:
            parts.append(prefix)
        parts.append(envelope.text)
        return " ".join(parts)

    def _build_command(self, instance: OcInstance, text: str) -> list[str]:
        """Build the openclaw CLI command for a given instance."""
        cmd = ["openclaw"]
        if instance.profile:
            cmd.extend(["--profile", instance.profile])
        cmd.extend(["system", "event", "--text", text])
        return cmd

    async def inject_event(
        self,
        text: str,
        instance_name: str | None = None,
    ) -> None:
        """Inject a system event into OC instance(s).

        Args:
            text: The event text to inject.
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
            await self._inject_to_instance(instance, text)

    async def _inject_to_instance(self, instance: OcInstance, text: str) -> None:
        """Run the openclaw CLI command for a single instance."""
        cmd = self._build_command(instance, text)
        log.info("injecting event to OC instance %r: %s", instance.name, cmd)

        try:
            proc = await asyncio.create_subprocess_exec(
                *cmd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
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
                    "OC event injection timed out for instance %r after %ds",
                    instance.name,
                    self._timeout,
                )
                return

            if proc.returncode == 0:
                log.info("event injected to OC instance %r", instance.name)
            else:
                stderr_text = stderr.decode(errors="replace").strip()
                log.error(
                    "OC event injection failed for instance %r (exit %d): %s",
                    instance.name,
                    proc.returncode,
                    stderr_text,
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
        """Format an envelope and inject it as a system event.

        Convenience method that formats the envelope text with hive
        metadata before injecting.
        """
        text = self.format_event_text(envelope, prefix=prefix)
        await self.inject_event(text, instance_name=instance_name)
