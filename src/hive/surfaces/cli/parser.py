"""Argparse factory for the `hive` CLI."""
from __future__ import annotations

import argparse
from argparse import HelpFormatter
from typing import Sequence

from . import registry


class RichHelpFormatter(HelpFormatter):
    """HelpFormatter that renders section headings + usage prefix in the
    current theme's tokens (bold cyan for headings, dim for the rest)."""

    def start_section(self, heading: str | None) -> None:
        from . import style
        if heading is not None and style.is_color_enabled():
            heading = style.paint("bold cyan", heading)
        super().start_section(heading)

    def _format_usage(self, usage, actions, groups, prefix: str | None) -> str:
        from . import style
        if prefix is None:
            prefix = style.paint("bold cyan", "usage: ") if style.is_color_enabled() else "usage: "
        return super()._format_usage(usage, actions, groups, prefix)


_FLAG_DEFAULTS = {
    "--tail": 20,
    "--limit": 10,
}


def make_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="hive",
        formatter_class=RichHelpFormatter,
        description="HiveOS terminal surface — REPL, gateway, ops commands.",
    )
    sub = p.add_subparsers(dest="command", metavar="<command>")
    for spec in registry.REGISTRY.values():
        sp = sub.add_parser(
            spec.name, help=spec.help, formatter_class=RichHelpFormatter
        )
        for entry in spec.args:
            _add_arg(sp, entry)
        if spec.subcommands:
            sub2 = sp.add_subparsers(dest="subcommand", metavar="<subcommand>")
            for subname, subspec in spec.subcommands.items():
                sp2 = sub2.add_parser(
                    subname, help=subspec.help, formatter_class=RichHelpFormatter
                )
                for entry in subspec.args:
                    _add_arg(sp2, entry)
    return p


def _add_arg(parser, entry) -> None:
    """Add an argparse argument from a registry entry (flag, type, help)."""
    flag, type_, helptext = entry
    kwargs: dict = {"help": helptext}
    if type_ is not None:
        kwargs["type"] = type_
    if flag in _FLAG_DEFAULTS:
        kwargs["default"] = _FLAG_DEFAULTS[flag]
    parser.add_argument(flag, **kwargs)


def parse(argv: Sequence[str]) -> tuple[registry.CommandSpec, argparse.Namespace]:
    p = make_parser()
    raw = list(argv) if argv else ["chat"]
    args = p.parse_args(raw)
    cmd_name = args.command or "chat"
    spec = registry.REGISTRY[cmd_name]
    sub = getattr(args, "subcommand", None)
    if sub and spec.subcommands:
        spec = spec.subcommands[sub]
    return spec, args
