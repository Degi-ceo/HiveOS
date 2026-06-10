# HiveOS deploy (systemd, 24/7 on a VPS)

Run Hive under a non-root `hive` user with auto-restart and reboot survival.

## Layout
- `/opt/hiveos` — checkout (owned by `hive`), with `.venv` (`pip install -e .`) and `.env`.
- `data/`, `vault/` — the only writable paths (state DB, Obsidian export). The units
  set `ProtectSystem=strict` + `ReadWritePaths` so nothing else is writable.

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
