"""Self-contained Bash, Zsh, and Fish completion scripts for Hive's CLI."""
from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True)
class CompletionSpec:
    name: str
    category: str
    help: str
    subcommands: tuple[str, ...] = ()


def _validated(specs: list[CompletionSpec]) -> list[CompletionSpec]:
    """Keep registry text out of executable shell syntax."""
    for spec in specs:
        for word in (spec.name, *spec.subcommands):
            if re.fullmatch(r"[a-z][a-z0-9-]*", word) is None:
                raise ValueError("invalid completion command name")
    return specs


def _commands_line(specs: list[CompletionSpec]) -> str:
    return " ".join(("--no-color", *(spec.name for spec in specs)))


def bash_completion(specs: list[CompletionSpec]) -> str:
    """Complete the command after optional global flags, then its subcommand."""
    specs = _validated(specs)
    lines = [
        "# bash completion for hive",
        "_hive_complete() {",
        '    local current="${COMP_WORDS[COMP_CWORD]}"',
        "    local index=1",
        '    while (( index < COMP_CWORD )) && [[ "${COMP_WORDS[index]}" == "--no-color" ]]; do',
        "        ((index+=1))",
        "    done",
        "    if (( index == COMP_CWORD )); then",
        f'        COMPREPLY=( $(compgen -W "{_commands_line(specs)}" -- "$current") )',
        "        return",
        "    fi",
        '    case "${COMP_WORDS[index]}" in',
    ]
    for spec in specs:
        if spec.subcommands:
            subcommands = " ".join(spec.subcommands)
            lines.extend((
                f"        {spec.name})",
                "            if (( COMP_CWORD == index + 1 )); then",
                f'                COMPREPLY=( $(compgen -W "{subcommands}" -- "$current") )',
                "            fi",
                "            ;;",
            ))
    lines.extend(("    esac", "}", "complete -F _hive_complete hive"))
    return "\n".join(lines) + "\n"


def zsh_completion(specs: list[CompletionSpec]) -> str:
    """Use Zsh's current word index to complete the actual command position."""
    specs = _validated(specs)
    lines = [
        "#compdef hive",
        "_hive() {",
        "    local index=2",
        '    while (( index < CURRENT )) && [[ "${words[index]}" == "--no-color" ]]; do',
        "        ((index+=1))",
        "    done",
        "    if (( index == CURRENT )); then",
        "        local -a commands",
        "        commands=( '--no-color:Disable ANSI color'",
    ]
    for spec in specs:
        # Descriptions are display text, never shell code or Zsh's ':' delimiter.
        description = re.sub(r"[^a-zA-Z0-9 ,./_-]", " ", spec.help)[:80]
        lines.append(f"            '{spec.name}:{description}'")
    lines.extend((
        "        )",
        "        _describe 'command' commands",
        "        return",
        "    fi",
        '    case "${words[index]}" in',
    ))
    for spec in specs:
        if spec.subcommands:
            subcommands = " ".join(f"'{sub}'" for sub in spec.subcommands)
            lines.extend((
                f"        {spec.name})",
                "            if (( CURRENT == index + 1 )); then",
                "                local -a subcommands",
                f"                subcommands=( {subcommands} )",
                "                _describe 'subcommand' subcommands",
                "            fi",
                "            ;;",
            ))
    lines.extend(("    esac", "}", '_hive "$@"'))
    return "\n".join(lines) + "\n"


def fish_completion(specs: list[CompletionSpec]) -> str:
    """Use Fish's completed tokens to select command or subcommand candidates."""
    specs = _validated(specs)
    lines = [
        "# fish completion for hive",
        "function __hive_completion_context",
        "    set -l tokens (commandline -xpc)",
        "    set -e tokens[1]",
        '    while test (count $tokens) -gt 0; and test "$tokens[1]" = "--no-color"',
        "        set -e tokens[1]",
        "    end",
        "    if test (count $tokens) -eq 0",
        "        echo __command",
        "    else if test (count $tokens) -eq 1",
        "        echo $tokens[1]",
        "    else",
        "        echo __none",
        "    end",
        "end",
        "complete -c hive -l no-color -n 'test (__hive_completion_context) = __command' -d 'Disable ANSI color'",
        f"complete -c hive -n 'test (__hive_completion_context) = __command' -a '{_commands_line(specs)}' -d 'Hive command' -f",
    ]
    for spec in specs:
        if spec.subcommands:
            subcommands = " ".join(spec.subcommands)
            lines.append(
                f"complete -c hive -n 'test (__hive_completion_context) = {spec.name}' "
                f"-a '{subcommands}' -d 'Hive subcommand' -f"
            )
    return "\n".join(lines) + "\n"
