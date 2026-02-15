"""Hive CLI entry point — click group with config loading."""

from __future__ import annotations

from pathlib import Path

import click

from hive_daemon.config import HiveConfig, load_config

from hive_cli.commands import send, reply, status, roster


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
cli.add_command(roster)


def main() -> None:
    cli()


if __name__ == "__main__":
    main()
