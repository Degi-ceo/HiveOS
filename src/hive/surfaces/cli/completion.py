"""Shell completion script generators — bash / zsh / fish (SPRINT_6 P-J J3).

Pure functions: take a list of CompletionSpec, return a self-contained
installable shell script. No I/O, no env reads, no Python runtime
required at completion time — the script uses only the shell's native
completion machinery (compgen / compdef / complete).
"""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CompletionSpec:
    name: str
    category: str
    help: str
    subcommands: tuple[str, ...] = ()


def _commands_line(specs: list[CompletionSpec]) -> str:
    return " ".join(s.name for s in specs)


def bash_completion(specs: list[CompletionSpec]) -> str:
    """Return a bash completion script. Install: `hive completion bash > /etc/bash_completion.d/hive`."""
    lines = [
        "# bash completion for hive",
        "_hive_commands() {",
        f"    local cmds=\"{_commands_line(specs)}\"",
        "    COMPREPLY=( $(compgen -W \"${cmds}\" -- \"${COMP_WORDS[COMP_CWORD]}\") )",
        "}",
        "complete -F _hive_commands hive",
    ]
    # Per-subcommand completion for commands that have subcommands
    for s in specs:
        if s.subcommands:
            fn = f"_hive_{s.name}"
            subs = " ".join(s.subcommands)
            lines.append(f"{fn}() {{ local subs=\"{subs}\"; COMPREPLY=( $(compgen -W \"${{subs}}\" -- \"${{COMP_WORDS[COMP_CWORD]}}\") ); }}")
            lines.append(f"complete -F {fn} \"hive {s.name}\"")
    return "\n".join(lines) + "\n"


def zsh_completion(specs: list[CompletionSpec]) -> str:
    """Return a zsh completion script. Install: `hive completion zsh > \"${fpath[1]}/_hive\"`."""
    lines = ["#compdef hive", "", "_hive() {"]
    lines.append("    local -a commands")
    for s in specs:
        lines.append(f"    commands+=(\"{s.name}:{s.help}\")")
        # Include parent:sub entries so that subcommands show up in completion
        # for the parent command as well (e.g. `learning:status`).
        for sub in s.subcommands:
            lines.append(f"    commands+=(\"{s.name}:{sub}:{s.help}\")")
    lines.append("    _describe 'command' commands")
    lines.append("}")
    for s in specs:
        if s.subcommands:
            lines.append("")
            lines.append(f"_hive_{s.name}() {{")
            lines.append("    local -a subs")
            for sub in s.subcommands:
                lines.append(f"    subs+=(\"{sub}\")")
            lines.append("    _describe 'subcommand' subs")
            lines.append("}")
    lines.append("")
    lines.append("_hive \"$@\"")
    return "\n".join(lines) + "\n"


def fish_completion(specs: list[CompletionSpec]) -> str:
    """Return a fish completion script. Install: `hive completion fish > ~/.config/fish/completions/hive.fish`."""
    lines = [
        "# fish completion for hive",
        "function __hive_no_subcommand",
        "    set -l cmd (commandline -opc)",
        "    if [ (count $cmd) -eq 2 ]",
        "        return 0",
        "    end",
        "    return 1",
        "end",
    ]
    for s in specs:
        if s.subcommands:
            for sub in s.subcommands:
                lines.append(f"complete -c hive -n '__hive_no_subcommand' -a '{s.name} {sub}' -d '{s.help}: {sub}'")
        else:
            lines.append(f"complete -c hive -n '__hive_no_subcommand' -a '{s.name}' -d '{s.help}'")
    return "\n".join(lines) + "\n"
