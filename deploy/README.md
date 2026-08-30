# HiveOS deploy (systemd, 24/7 on a VPS)

> **Do not enable the orchestrator service yet.** The current durable-execution readiness gate is incomplete. Gateway and read-only diagnostics may be operated manually; `hiveos-orchestrator.service` remains disabled until [`../docs/AUTONOMY_READINESS.md`](../docs/AUTONOMY_READINESS.md) is satisfied and a shadow soak has passed.

Run Hive under a non-root `hive` user with auto-restart and reboot survival.

## Layout
- `/opt/hiveos` — checkout (owned by `hive`), with `.venv` (`pip install -e .`) and `.env`.
- `data/`, `vault/` — the only writable paths (state DB, Obsidian export). The units
  set `ProtectSystem=strict` + `ReadWritePaths` so nothing else is writable.

## Python deps
The base `pip install -e .` covers the gateway, orchestrator, and keeper. For a
full VPS deploy also install the optional extras the autonomy/MCP paths use:
```bash
pip install -e ".[memory,cron,mcp]"   # mnemosyne backend + cron schedules + MCP serve/load
```
Without `cron` the scheduler is interval-only; without `mcp` the `mcp-serve`
command and external-MCP loading raise a clear "pip install mcp" error.

## Dashboard (optional)
The gateway serves Mission Control at `/app` from `dashboard/dist` when that
directory exists. Build it once at deploy time (needs Node ≥ 18):
```bash
cd dashboard && npm ci && npm run build   # produces dashboard/dist/ (gitignored)
```
If you skip this, the JSON API still works; only the `/app` SPA is unavailable.

## Units
| Unit | Command | Role |
|------|---------|------|
| `hiveos-gateway.service` | `hive serve` | FastAPI gateway (chat, SSE, ws, approvals, budget, Telegram webhook) |
| `hiveos-orchestrator.service` | `hive heartbeat` | never-idle autonomy loop (cron + commitments + tasks) |
| `hiveos-keeper.service` + `.timer` | `hive consolidate` | nightly sleep-time memory consolidation (03:00) |

## Install
```bash
sudo useradd --system --home /opt/hiveos hive    # if not present
sudo cp deploy/hiveos-*.service deploy/hiveos-*.timer /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now hiveos-gateway.service hiveos-orchestrator.service
sudo systemctl enable --now hiveos-keeper.timer
```

## Verify
```bash
systemctl status hiveos-gateway hiveos-orchestrator
journalctl -u hiveos-gateway -f
curl localhost:8088/health
sudo reboot   # then re-check status: units should come back up
```

## Notes
- Secrets live in `/opt/hiveos/.env` (mode 600). Never commit real secrets.
- Branch-protection on `main` means Hive opens PRs but a human merges (SOUL.md).
- Logs go to the journal; rotate via the host's `journald` retention settings.
