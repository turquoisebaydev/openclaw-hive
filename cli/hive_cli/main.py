"""Hive CLI entry point — click group with config loading."""

from __future__ import annotations

from pathlib import Path
from importlib import metadata as importlib_metadata
import inspect

import click

from hive_daemon.config import HiveConfig, load_config

from hive_cli.commands import send, reply, status, sessions, session_history, roster, discord, hive_thread_list, hive_thread_send


def _parse_semver_triplet(version: str) -> tuple[int, int, int]:
    raw = version.split('+', 1)[0].split('-', 1)[0]
    parts = raw.split('.')
    nums = []
    for p in parts[:3]:
        digits = ''.join(ch for ch in p if ch.isdigit())
        nums.append(int(digits) if digits else 0)
    while len(nums) < 3:
        nums.append(0)
    return tuple(nums)  # type: ignore[return-value]


def _enforce_runtime_compat() -> None:
    required = (0, 1, 1)
    try:
        cli_v = importlib_metadata.version("hive-cli")
        daemon_v = importlib_metadata.version("hive-daemon")
    except importlib_metadata.PackageNotFoundError as exc:
        raise click.ClickException(f"Missing runtime package: {exc}") from exc

    if _parse_semver_triplet(cli_v) < required or _parse_semver_triplet(daemon_v) < required:
        raise click.ClickException(
            "Incompatible hive runtime: requires hive-cli>=0.1.1 and hive-daemon>=0.1.1. "
            f"Detected hive-cli={cli_v}, hive-daemon={daemon_v}."
        )

    from hive_daemon.envelope import create_envelope
    if "target_session" not in inspect.signature(create_envelope).parameters:
        raise click.ClickException(
            "Incompatible hive-daemon API: create_envelope() missing target_session. "
            "Upgrade/reinstall hive-daemon to a matching build."
        )


@click.group()
@click.option(
    "--config", "config_path",
    type=click.Path(exists=True, dir_okay=False, path_type=Path),
    default=Path.home() / ".config" / "hive" / "hive.toml",
    help="Path to hive.toml config file (defaults to ~/.config/hive/hive.toml).",
)
@click.pass_context
def cli(ctx: click.Context, config_path: Path) -> None:
    """Hive CLI — stateless tool for OpenClaw hive coordination."""
    ctx.ensure_object(dict)
    ctx.obj["config"] = load_config(config_path)


cli.add_command(send)
cli.add_command(reply)
cli.add_command(status)
cli.add_command(sessions)
cli.add_command(session_history)
cli.add_command(roster)
cli.add_command(discord)
cli.add_command(hive_thread_list)
cli.add_command(hive_thread_send)


def main() -> None:
    _enforce_runtime_compat()
    cli()


if __name__ == "__main__":
    main()
