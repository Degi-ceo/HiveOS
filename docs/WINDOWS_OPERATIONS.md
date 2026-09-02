# HiveOS on Windows — safe state operations

This guide is for the local HiveOS installation on `H:`. It does **not** authorize unattended autonomy: keep `HIVE_AUTONOMY_ENABLED=false` and `HIVE_AUTONOMOUS_SELFMOD_ENABLED=false` until the release gates in [AUTONOMY_READINESS.md](AUTONOMY_READINESS.md) are satisfied.

## Backup and integrity

From `H:\HiveOS`:

```powershell
.\.venv\Scripts\hive.exe state verify
.\.venv\Scripts\hive.exe state backup
```

The default backup is written below `HIVE_DATA_DIR\backups` with a UTC timestamp. It is a SQLite online-backup snapshot, not a filesystem copy of the `.sqlite`, `-wal`, or `-shm` files. SQLite documents that the online backup API produces a consistent snapshot even while the source is in use; Hive verifies that snapshot with `PRAGMA integrity_check`. [SQLite Backup API](https://www.sqlite.org/backup.html)

To choose a separate destination on `H:`:

```powershell
.\.venv\Scripts\hive.exe state backup --output 'H:\HiveOS\data\backups\manual-20260831.sqlite'
.\.venv\Scripts\hive.exe state verify --path 'H:\HiveOS\data\backups\manual-20260831.sqlite'
```

## Non-destructive restore drill

A restore drill validates a backup without touching `HIVE_STATE_DB`. Its output must be a new path; Hive refuses an existing destination. It is the required evidence step before considering an actual restore:

```powershell
.\.venv\Scripts\hive.exe state drill 'H:\HiveOS\data\backups\manual-20260831.sqlite' `
  --output 'H:\HiveOS\data\drills\manual-20260831.sqlite'
.\.venv\Scripts\hive.exe state verify --path 'H:\HiveOS\data\drills\manual-20260831.sqlite'
```

An actual restore overwrites the configured `HIVE_STATE_DB`. It is intentionally not scheduled and requires `--confirm`.

1. Stop every Hive process that may use the state DB (gateway, heartbeat, tests, or a terminal session).
2. Verify the candidate snapshot.
3. Run the explicit restore command.
4. Run `hive doctor` and `hive state verify` before starting any read-only diagnostic process.

```powershell
.\.venv\Scripts\hive.exe state verify --path 'H:\HiveOS\data\backups\manual-20260831.sqlite'
.\.venv\Scripts\hive.exe state restore 'H:\HiveOS\data\backups\manual-20260831.sqlite' --confirm
.\.venv\Scripts\hive.exe doctor
.\.venv\Scripts\hive.exe state verify
```

Do not delete WAL or SHM files manually during recovery. Preserve the original database and its companion files for audit until the restored copy is verified.

## Supervision boundary

A Windows Task Scheduler job may run the **backup** command at a chosen cadence once an operator creates and validates it. Do not schedule `hive heartbeat`, `state restore`, Telegram delivery, or self-modification. Record the task name, command, output path, last exit code, and a successful restore drill in the operator log before considering a shadow soak.
## Read-only shadow soak

Use the checked-in helper to collect repeatable, non-effectful evidence. It verifies the selected source DB before and after `hive shadow`; the only permitted write is the separate evidence SQLite database. It does not start a runtime, heartbeat, tool executor, Telegram delivery, memory projection, or self-modification.

```powershell
Set-Location H:\HiveOS
.\scripts\windows\shadow-soak.ps1
```

To target explicit paths, keep the evidence database distinct from the source:

```powershell
.\scripts\windows\shadow-soak.ps1 `
  -Source 'H:\HiveOS\data\hive.sqlite' `
  -Evidence 'H:\HiveOS\data\shadow\shadow-evidence.sqlite'
```

For the 24–72 hour readiness soak, run this command at a documented cadence (manually or through a reviewed *backup-only/shadow-only* operator procedure), preserve every JSON result and exit code, and record restart/fault-injection observations. `HIVE_DATA_DIR\operations\operation-evidence.sqlite` is an append-only, aggregate-only companion log for explicit backup, drill, and shadow invocations; it is not a tamper-proof substitute for retaining the backup and shadow evidence databases. A non-zero exit, `requires_review`, expired lease, or unleased-running aggregate is evidence to investigate—not authorization to repair, replay, or enable autonomy.

Before the Telegram gateway-only pilot, run `hive pilot-doctor`. `ready` only means local prerequisite checks and zero known review counts; it does not send a Telegram message, verify a remote webhook, or authorize a heartbeat.
