# HiveOS CLI — Operator Guide

The `hive` binary is the canonical terminal surface for HiveOS. It exposes
every operator action — interactive REPL, gateway control, ops utilities,
memory maintenance, and learning-loop introspection — through a single
argparse-driven command tree.

## First-run configuration

`hive init` stores `MINIMAX_API_KEY` and a generated `HIVE_SECRET` in the same
`.env` that the runtime reads, independent of the current working directory.
API-key entry is hidden and requires a terminal; Ctrl-C/EOF before completion
leaves the file unchanged. It does not run diagnostics or seed memory. Run
`hive doctor` explicitly afterward. For automation, use
`hive init --non-interactive --json` with `MINIMAX_API_KEY` supplied in the
process environment; it returns one JSON result with `ok`, `changed`, and
`env_file` (or a safe `error` code), without disclosing credentials. Set an
absolute `HIVE_ENV_FILE` whose basename is `.env` only when an alternative
configuration path is intended.

On POSIX, a newly written dotenv is mode `0600`. On Windows, the file inherits
the directory's ACL; operators should secure that directory separately.

## Built-in `--help`

```bash
hive --help
```

Output is grouped by category (`core`, `runtime`, `ops`, `library`,
`gateway`) with bold-cyan category headers and right-aligned command
summaries. Per-command `--help` is also available, e.g.:

```bash
hive learning --help
hive doctor --help
```

## Shell completion

`hive` can emit a self-contained completion script for bash, zsh, or
fish. The script does not shell out to Python at tab-completion time;
it uses only the shell's native completion machinery.

### bash

```bash
hive completion bash | sudo tee /etc/bash_completion.d/hive >/dev/null
# or for current user only:
hive completion bash > ~/.local/share/bash-completion/completions/hive
exec bash   # reload
```

### zsh

```bash
hive completion zsh > "${fpath[1]}/_hive"
# ensure fpath contains the directory; e.g.:
#   fpath=(~/.zsh/completions $fpath)
# then run: autoload -U compinit && compinit
```

### fish

```bash
hive completion fish > ~/.config/fish/completions/hive.fish
```

The emitted scripts include every command in `registry.REGISTRY` plus a
per-command completer for any command that has subcommands (e.g.
`hive learning status` / `hive learning replay`).

## Themes

The CLI honors `HIVE_THEME=neon|minimal|mono` and `NO_COLOR=1` /
`HIVE_NO_COLOR=1`. The neon theme (default for TTY sessions) uses the
shared CSS palette from `dashboard/src/styles/theme.css`. See
`src/hive/surfaces/cli/themes.py` for the full palette.

## Command taxonomy

| Category | Commands |
|---|---|
| `core`     | `chat`, `ask`, `version`, `completion` |
| `runtime`  | `serve`, `init`, `doctor`, `mcp-serve`, `heartbeat`, `consolidate`, `learning` |
| `ops`      | `status`, `logs` (+ learning subcommands `status`, `replay`) |
| `gateway`  | `budget`, `approvals` |

Categories live on `CommandSpec.category` in `registry.py` and drive the
categorized `--help` overview.
