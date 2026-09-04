# HiveOS onboarding

Run `hive init` in the workspace that should own HiveOS configuration. The
wizard creates or updates only that workspace's `.env`; it never prints stored
credential values.

## Interactive setup

```text
hive init
```

The wizard reports the workspace, execution provider, credential presence,
secret hardening, memory configuration, optional Telegram channel state, doctor
result, and optional initial memory seeding. It prompts only for a missing
MiniMax key and memory home. Existing values are preserved.

## CI and automation

```text
hive init --non-interactive --json
```

This never prompts. It writes safe defaults that Hive owns (`HIVE_EXEC_PROVIDER`,
`HIVE_SECRET`, and `HIVE_MNEMOSYNE_HOME`) but never invents provider credentials.
The JSON result includes a `missing` list, so CI can fail or provision
`MINIMAX_API_KEY` without placing secrets in logs. Non-interactive onboarding
skips the seed script and runs the doctor without automatic repair.

Do not commit `.env` files or paste their contents into chat, issues, or PRs.
