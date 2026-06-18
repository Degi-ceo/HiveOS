# HiveOS — Production Deployment Guide

Running HiveOS 24/7 on a Linux VPS (tested on Hetzner with Ubuntu 22.04 / Debian 12).
Three systemd units provide always-on service:

| Unit | Command | Role |
|---|---|---|
| `hiveos-gateway.service` | `hive serve` | FastAPI gateway — chat, SSE, WebSocket, approvals, Telegram webhook |
| `hiveos-orchestrator.service` | `hive heartbeat` | Never-idle autonomy loop — cron jobs, commitments, task dispatch |
| `hiveos-keeper.service` + `.timer` | `hive consolidate` | Nightly memory consolidation (03:00, triggered by the timer) |

---

## 1. System preparation

```bash
# Create a non-root service user with a home directory
sudo useradd --system --create-home --home-dir /opt/hiveos hive

# Clone the repository
sudo -u hive git clone https://github.com/hiveosagent/hiveos.git /opt/hiveos
```

---

## 2. Python environment

```bash
cd /opt/hiveos
sudo -u hive python3 -m venv .venv
sudo -u hive .venv/bin/pip install --upgrade pip

# Core + all production extras
sudo -u hive .venv/bin/pip install -e ".[memory,cron,mcp]"
```

**Extras explained:**
- `memory` — Mnemosyne active memory backend (semantic search, sleep consolidation, BEAM banks). Without it, HiveOS falls back to SQLite-only `LocalMemoryProvider`.
- `cron` — croniter-based cron scheduling for the autonomy loop. Without it, the scheduler uses a simple interval loop.
- `mcp` — MCP SDK required for `hive mcp-serve` and loading external MCP servers from `HIVE_MCP_SERVERS`.

---

## 3. Configuration

```bash
sudo -u hive cp /opt/hiveos/.env.example /opt/hiveos/.env
sudo chmod 600 /opt/hiveos/.env   # owner-only
sudo -u hive nano /opt/hiveos/.env
```

Minimum required variables (see [`docs/CONFIGURATION.md`](CONFIGURATION.md) for full reference):

```bash
# Executor model
MINIMAX_API_KEY=your_minimax_api_key_here
HIVE_EXEC_MODEL=MiniMax-M3
HIVE_EXEC_FALLBACK_MODEL=MiniMax-M2.7
HIVE_AUX_MODEL=MiniMax-M2.7

# Gateway authentication
HIVE_SECRET=generate-a-strong-secret-here
HIVE_HOST=0.0.0.0
HIVE_PORT=8088

# GitHub identity (for automatic draft PR opening on self-mod)
HIVE_GITHUB_TOKEN=ghp_...
HIVE_GITHUB_OWNER=hiveosagent
HIVE_GITHUB_REPO=hiveos

# Telegram (optional — enables /telegram/webhook and ExternalMessage tool)
TELEGRAM_BOT_TOKEN=123456:AAF...
TELEGRAM_WEBHOOK_SECRET=some-webhook-secret
```

---

## 4. Build the dashboard (optional)

The gateway serves the Mission Control SPA at `/app` when `dashboard/dist/` exists.
Skip this step if you only need the JSON API.

```bash
# Requires Node ≥ 18
sudo apt-get install -y nodejs npm   # if not installed

cd /opt/hiveos/dashboard
sudo -u hive npm ci
sudo -u hive npm run build
# Produces dashboard/dist/ — served automatically at /app
```

---

## 5. Health check

```bash
sudo -u hive /opt/hiveos/.venv/bin/hive doctor --fix
```

This verifies SOUL.md + approval gate are present, creates `data/` directories, and
initialises the SQLite schema. Fix any reported errors before continuing.

---

## 6. Install systemd units

```bash
sudo cp /opt/hiveos/deploy/hiveos-*.service \
        /opt/hiveos/deploy/hiveos-*.timer \
        /etc/systemd/system/

sudo systemctl daemon-reload

# Start gateway + autonomy loop immediately, enable on reboot
sudo systemctl enable --now hiveos-gateway.service
sudo systemctl enable --now hiveos-orchestrator.service

# Enable keeper timer (nightly consolidation at 03:00)
sudo systemctl enable --now hiveos-keeper.timer
```

The gateway service includes `ExecStartPre=…scripts/seed_memories.py` — it seeds Hive's
identity and system facts into memory on every gateway start (fail-open: startup proceeds
even if seeding fails). To re-seed manually without restarting:

```bash
sudo -u hive /opt/hiveos/.venv/bin/python /opt/hiveos/scripts/seed_memories.py
```

---

## 7. Verify

```bash
# Check unit status
systemctl status hiveos-gateway hiveos-orchestrator hiveos-keeper.timer

# Tail live logs
journalctl -u hiveos-gateway -f

# Health probe
curl http://localhost:8088/health
# Expected: {"status":"ok","service":"hiveos-gateway","protocol_version":"1.0"}

# Test authentication
curl -H "Authorization: Bearer <HIVE_SECRET>" http://localhost:8088/budget

# Reboot survival
sudo reboot
# After reboot:
systemctl is-active hiveos-gateway hiveos-orchestrator
```

---

## 8. Mnemosyne setup (optional but recommended)

Without Mnemosyne, HiveOS uses `LocalMemoryProvider` — a SQLite fallback with no semantic
search or sleep consolidation. Install Mnemosyne to unlock the full memory layer.

Follow `docs/memory/MNEMOSYNE_INTEGRATION_PHASES.md` — it is a step-by-step runbook for
phases 0–9 (installation, configuration, schema init, host-LLM bridge). Phase 10 (optional)
sets up the Mnemosyne MCP SSE server so HiveOS can reach memory over the network.

After Mnemosyne is installed, add to `.env`:

```bash
MNEMOSYNE_HOME=/opt/hiveos/data/mnemosyne
# Optional: MCP SSE server (Phase 10)
MNEMOSYNE_MCP_URL=http://localhost:8765/mcp
```

Then restart the gateway and orchestrator:

```bash
sudo systemctl restart hiveos-gateway hiveos-orchestrator
```

---

## 9. Telegram webhook registration

Once the gateway is running and reachable from the internet (or via a tunnel):

```bash
# Register your bot's webhook with Telegram
curl -X POST "https://api.telegram.org/bot<BOT_TOKEN>/setWebhook" \
  -H "Content-Type: application/json" \
  -d '{
    "url": "https://your-domain.com/telegram/webhook",
    "secret_token": "<TELEGRAM_WEBHOOK_SECRET>"
  }'
```

Messages to the bot now arrive at `/telegram/webhook` and Hive replies via Telegram.

---

## 10. Reverse proxy (nginx)

Recommended for production: TLS termination + rate limiting in front of the gateway.
The production template is `deploy/nginx-hiveos.conf` — replace `YOUR_SERVER_IP` with your
server's actual IP or domain (it appears twice in the file).

```nginx
server {
    listen 443 ssl;
    server_name hive.your-domain.com;

    ssl_certificate     /etc/letsencrypt/live/hive.your-domain.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/hive.your-domain.com/privkey.pem;

    location / {
        proxy_pass         http://127.0.0.1:8088;
        proxy_set_header   Host $host;
        proxy_set_header   X-Real-IP $remote_addr;
        # Required for SSE streaming
        proxy_buffering    off;
        proxy_cache        off;
        proxy_read_timeout 300s;
    }
}
```

---

## Systemd unit reference

The units in `deploy/` use hardened settings:

```ini
ProtectSystem=strict          # read-only filesystem except explicitly allowed paths
PrivateTmp=true               # isolated /tmp
NoNewPrivileges=true          # can't gain new privileges
ReadWritePaths=/opt/hiveos/data /opt/hiveos/vault /opt/hiveos/.worktrees
User=hive
Group=hive
Restart=always
RestartSec=5
```

The `hiveos-keeper.timer` triggers `hiveos-keeper.service` at 03:00 daily:

```ini
[Timer]
OnCalendar=*-*-* 03:00:00
Unit=hiveos-keeper.service
```

---

## Monitoring

```bash
# Ongoing log tail
journalctl -u hiveos-gateway -u hiveos-orchestrator -f

# Last 100 lines of gateway log
journalctl -u hiveos-gateway -n 100 --no-pager

# Check budget via API
curl -s -H "Authorization: Bearer <SECRET>" localhost:8088/budget | python3 -m json.tool

# Check task queue
curl -s -H "Authorization: Bearer <SECRET>" localhost:8088/tasks | python3 -m json.tool

# View recent tool calls
curl -s -H "Authorization: Bearer <SECRET>" "localhost:8088/audit?limit=10" | python3 -m json.tool
```

---

## Updating HiveOS

Hive never merges to `main` directly — all updates come via pull requests that you merge.
After merging a PR:

```bash
sudo -u hive git -C /opt/hiveos pull origin main
sudo -u hive /opt/hiveos/.venv/bin/pip install -e ".[memory,cron,mcp]" --quiet
sudo systemctl restart hiveos-gateway hiveos-orchestrator
```

If a DB migration is needed, `hive doctor --fix` handles it:

```bash
sudo -u hive /opt/hiveos/.venv/bin/hive doctor --fix
```

---

## Voice surface (optional, non-VPS)

The voice surface (`surfaces/voice.py`) requires a microphone and audio output.
It cannot run on a headless VPS. Run it on a local machine instead:

```bash
pip install -e ".[voice]"   # faster-whisper + piper
python -m hive.surfaces.voice   # wake word: "hej hive"
```

The voice surface connects to your running gateway over HTTP — set `HIVE_HOST` and
`HIVE_PORT` to point at the VPS gateway.

---

## Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| Gateway starts but `hive ask` times out | `MINIMAX_API_KEY` missing or wrong | Check `.env` and `hive doctor` |
| `hive mcp-serve` fails with ImportError | `mcp` extra not installed | `pip install -e ".[mcp]"` |
| Cron jobs never fire | `croniter` not installed | `pip install -e ".[cron]"` |
| `hive heartbeat` exits immediately | No tasks + planner disabled | Normal if `HIVE_PLANNER_ENABLED=false` and nothing is scheduled |
| `/app` returns 404 | Dashboard not built | `cd dashboard && npm ci && npm run build` |
| Telegram messages not arriving | Webhook URL unreachable | Check `TELEGRAM_WEBHOOK_SECRET` and nginx proxy |
| `systemctl status` shows `failed` | Python exception at startup | `journalctl -u hiveos-gateway -n 50` for the traceback |
