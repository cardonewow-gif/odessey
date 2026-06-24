# Backup and restore

Odysseus supports two different backup flows:

1. **Export / Import Data (UI / JSON)** for user-level app state such as memories,
   presets, skills, settings, feature flags, and preferences.
2. **`scripts/odysseus-backup`** for a full `data/` snapshot used in disaster
   recovery or server migration.

The **Settings → System → Data Backup** card is **not** a full `data/` snapshot.
It does **not** replace backing up the SQLite DB, uploads, Chroma data, personal
docs, attachments, or other files under `data/`.

## Full `data/` snapshots with `scripts/odysseus-backup`

For full recovery or machine migration, use the bundled backup helper:

```bash
python scripts/odysseus-backup snapshot
python scripts/odysseus-backup list
python scripts/odysseus-backup verify backups/odysseus-backup-YYYYMMDD-HHMMSS.tar.gz
python scripts/odysseus-backup restore backups/odysseus-backup-YYYYMMDD-HHMMSS.tar.gz --yes
```

## What the backup helper does

- Snapshots the whole `data/` directory into a `.tar.gz`
- Uses SQLite's backup API for `.db` files so a running app can still be backed up safely
- Verifies tarball integrity without extracting via `verify`
- Refuses restore tarballs with absolute paths or parent-traversal entries

## Default snapshot behavior

By default, `snapshot`:

- Includes the main app state under `data/`
- Skips `data/deep_research/` unless you pass `--include-research`
- Skips `data/mail-attachments/` unless you pass `--include-attachments`

## Common commands

```bash
# Standard snapshot into ./backups/
python scripts/odysseus-backup snapshot

# Write to a custom location
python scripts/odysseus-backup snapshot --out /mnt/nas/odysseus-backup.tgz

# Include larger optional directories
python scripts/odysseus-backup snapshot --include-research --include-attachments

# List available archives
python scripts/odysseus-backup list

# Verify archive integrity before trusting a backup
python scripts/odysseus-backup verify backups/odysseus-backup-YYYYMMDD-HHMMSS.tar.gz

# Restore a backup (destructive; requires confirmation flag)
python scripts/odysseus-backup restore backups/odysseus-backup-YYYYMMDD-HHMMSS.tar.gz --yes
```

## Which backup flow should I use?

- Use **Export Data** for app-level config/state portability.
- Use **`odysseus-backup snapshot`** for disaster recovery, machine migration, or full rollback.

## Restore safety notes

- `restore` is destructive and requires `--yes`.
- The current `data/` directory is stashed aside before extraction.
- Run `verify` on a backup archive before trusting it as your only copy.
