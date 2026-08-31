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

## Restore drill

A restore overwrites the configured `HIVE_STATE_DB`. It is intentionally not scheduled and requires `--confirm`.

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