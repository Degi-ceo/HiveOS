# HiveOS deploy (systemd, 24/7 on a VPS)

Run Hive under a non-root `hive` user with auto-restart and reboot survival.

## Layout
- `/opt/hiveos` — checkout (owned by `hive`), with `.venv` (`pip install -e .`) and `.env`.
- `data/`, `vault/` — the only writable paths (state DB, Obsidian export). The units
  set `ProtectSystem=strict` + `ReadWritePaths` so nothing else is writable.
- The checked-in unit files currently refer to `/home/hive/HiveOS`, while this
  example layout uses `/opt/hiveos`. Set every unit path to the actual checkout
  before installing; do not assume a fixed path when verifying a deployment.

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

The gateway unit sets `PYTHONDONTWRITEBYTECODE=1`. On a **fresh** clean source
checkout, `/health` can then report `source_revision` as the process-start Git
HEAD. Existing `__pycache__` directories or any other ignored/untracked source
artifacts cause `source_revision: null` until a clean release checkout is used.
Do not treat this field alone as a verified deployment or remove cache files
from a live installation just to make the field non-null.

When `HIVE_DEPLOY_VERIFY_ENABLED=true`, the `deploy` tool accepts only a local
`gateway/systemctl` restart with a durable verification receipt. Other targets
or modes are refused before restart because Hive cannot verify their outcome.
With verification disabled, supported restarts remain explicitly unverified.

## Selected-release launcher preview (not installed by the current unit)

`hive.core.release_launcher` is an opt-in foundation for a future immutable
release deployment. It reads the M48 installed-release selection from a
separately managed `releases/<full-sha>` catalog, rechecks every file against
the Git tree, and executes `hive.surfaces.cli serve` from that selected tree
using a fixed interpreter and no shell. The launcher code and interpreter
must be installed outside the release catalog under host control. The
catalog, Git object store and selection writes must be inaccessible for
modification to the gateway user. Set `HIVE_DATA_DIR`, `HIVE_STATE_DB`,
`OBSIDIAN_VAULT_PATH`, and `HIVE_ENV_FILE` to absolute paths outside the
catalog; the launcher refuses missing paths or a state database different
from its own. `MNEMOSYNE_HOME` must also resolve outside the catalog; the
launcher pins its external default in the child before dotenv loads. The
current shared state database does not provide that isolation by itself. Keep
the state database and other mutable data outside the release tree. Do not point
the current service unit at this
preview until a host-owned selection store, stable-identity guard wiring, and
managed-service verification are implemented and tested on the
target Linux host. No automatic code rollback is enabled.

`HIVE_INSTALLED_RELEASE_ROOT` can now be set to the absolute catalog root as
an opt-in runtime startup guard. It verifies that the running source and
configured root are the selected `releases/<sha>` tree and uses the catalog
root for a stable deployment host key. It requires
`HIVE_DEPLOY_VERIFY_ENABLED=true`, external data paths, and autonomous
self-modification disabled. Mutable roots must be disjoint from the catalog,
and existing links in those trees or unresolved legacy deployment/recovery
work refuse startup. An atomic database fence prevents later old-key
deployment/recovery writes from an already-running process. The launcher and
runtime must use the same catalog, state database, host key and systemd scope.
This guard does not make the shared selection database host-owned; the current
unit still needs a separate controlled deployment design before production use.
This shared database fence coordinates cooperating processes; a writer with
database DDL access can alter it.

## Notes
- Secrets live in `/opt/hiveos/.env` (mode 600). Never commit real secrets.
- Branch-protection on `main` means Hive opens PRs but a human merges (SOUL.md).
- Logs go to the journal; rotate via the host's `journald` retention settings.
