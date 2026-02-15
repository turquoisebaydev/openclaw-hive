"""Handler script discovery and dispatch.

Discovers executable scripts in the handler directory (hive-daemon.d/) and
dispatches action messages to them. Each handler receives the full envelope
JSON on stdin and returns a JSON result on stdout.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Any

from hive_daemon.envelope import Envelope

log = logging.getLogger(__name__)


def discover_handlers(handler_dir: str | Path) -> dict[str, Path]:
    """Scan the handler directory and return a map of action name to script path.

    Only includes files that are executable. Skips dotfiles and directories.
    """
    handler_dir = Path(handler_dir)
    handlers: dict[str, Path] = {}

    if not handler_dir.is_dir():
        log.warning("handler directory does not exist: %s", handler_dir)
        return handlers

    for entry in sorted(handler_dir.iterdir()):
        if entry.name.startswith("."):
            continue
        if not entry.is_file():
            continue
        if not os.access(entry, os.X_OK):
            log.debug("skipping non-executable: %s", entry)
            continue
        handlers[entry.name] = entry
        log.info("discovered handler: %s -> %s", entry.name, entry)

    return handlers


class DispatchResult:
    """Result of dispatching an envelope to a handler script."""

    __slots__ = ("action", "success", "stdout", "stderr", "exit_code")

    def __init__(
        self,
        action: str,
        success: bool,
        stdout: str,
        stderr: str,
        exit_code: int | None,
    ) -> None:
        self.action = action
        self.success = success
        self.stdout = stdout
        self.stderr = stderr
        self.exit_code = exit_code

    def result_json(self) -> Any:
        """Parse stdout as JSON, or return raw string on parse failure."""
        try:
            return json.loads(self.stdout)
        except (json.JSONDecodeError, ValueError):
            return self.stdout


class Dispatcher:
    """Dispatches action messages to handler scripts.

    Handlers are discovered from the configured handler directory on init.
    Call ``dispatch(envelope)`` to run the matching handler.
    """

    def __init__(self, handler_dir: str | Path, timeout: int = 30) -> None:
        self._handler_dir = Path(handler_dir)
        self._timeout = timeout
        self._handlers: dict[str, Path] = {}

    def discover(self) -> dict[str, Path]:
        """Discover (or re-discover) available handlers. Returns the handler map."""
        self._handlers = discover_handlers(self._handler_dir)
        return self._handlers

    @property
    def available_handlers(self) -> list[str]:
        """List of discovered handler action names."""
        return sorted(self._handlers.keys())

    def has_handler(self, action: str) -> bool:
        """Check if a handler exists for the given action."""
        return action in self._handlers

    async def dispatch(self, envelope: Envelope) -> DispatchResult:
        """Dispatch an envelope to its action handler.

        Raises KeyError if no handler is found for the action.
        The caller is responsible for checking ``has_handler()`` first or
        handling the KeyError (e.g., to escalate to OC).
        """
        action = envelope.action
        if action is None:
            raise ValueError("envelope has no action field")

        handler_path = self._handlers.get(action)
        if handler_path is None:
            raise KeyError(f"no handler for action: {action!r}")

        envelope_json = json.dumps(envelope.to_json())
        log.info("dispatching action %r to %s", action, handler_path)

        try:
            proc = await asyncio.create_subprocess_exec(
                str(handler_path),
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )

            try:
                stdout_bytes, stderr_bytes = await asyncio.wait_for(
                    proc.communicate(input=envelope_json.encode()),
                    timeout=self._timeout,
                )
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
                log.error("handler %r timed out after %ds", action, self._timeout)
                return DispatchResult(
                    action=action,
                    success=False,
                    stdout="",
                    stderr=f"handler timed out after {self._timeout}s",
                    exit_code=None,
                )

            stdout_str = stdout_bytes.decode(errors="replace")
            stderr_str = stderr_bytes.decode(errors="replace")
            exit_code = proc.returncode

            success = exit_code == 0
            if success:
                log.info("handler %r succeeded (exit 0)", action)
            else:
                log.error("handler %r failed (exit %d): %s", action, exit_code, stderr_str.strip())

            return DispatchResult(
                action=action,
                success=success,
                stdout=stdout_str,
                stderr=stderr_str,
                exit_code=exit_code,
            )

        except OSError as exc:
            log.error("failed to execute handler %r: %s", action, exc)
            return DispatchResult(
                action=action,
                success=False,
                stdout="",
                stderr=str(exc),
                exit_code=None,
            )
