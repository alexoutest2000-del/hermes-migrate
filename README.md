# Hermes Migrate

Export/import a complete Hermes Agent configuration between hosts.

One command packages everything — config, API keys, skills, cron jobs, memory,
profiles — into a portable `.tar.gz` archive. One command restores it elsewhere.

## Quick Start

```bash
# Export everything from this host
python3 hermes_migrate.py export

# Creates: hermes-export-<hostname>-<date>.tar.gz

# Import onto another host
python3 hermes_migrate.py import hermes-export-bot-20260531.tar.gz
```

## Commands

### export

```bash
python3 hermes_migrate.py export [options]
```

Packages all Hermes configuration into a portable archive.

| Option | Default | Description |
|--------|---------|-------------|
| `-o, --output PATH` | auto-named | Output archive path |
| `--redact-secrets` | off | Replace API keys with `[REDACTED]` |
| `--no-profiles` | off | Exclude named profiles |
| `--hermes-home PATH` | `~/.hermes` | Source Hermes home |

**What gets exported:**
- `config.yaml` — model providers, tools, gateway config
- `.env` — API keys, tokens, environment variables
- `auth.json` — OAuth tokens
- `channel_directory.json` — messaging channel mappings
- `skills/` — all bundled + custom skills (excluding runtime state)
- `cron/jobs.json` — scheduled jobs
- `memories/` — persistent memory (MEMORY.md, USER.md)
- `profiles/` — all named profiles with their config, skills, cron, memories

**What's intentionally excluded:**
- Sessions, state.db, logs, caches, sandboxes
- Background process state, gateway PID/lock files
- Cron output logs, curator backups, usage tracking

### import

```bash
python3 hermes_migrate.py import ARCHIVE [options]
```

Restores configuration from an export archive.

| Option | Default | Description |
|--------|---------|-------------|
| `--dry-run` | off | Preview what would be imported |
| `--force` | off | Overwrite existing files (default: abort on conflict) |
| `--target-home PATH` | `~/.hermes` | Destination Hermes home |

If files already exist on the target, the import aborts with a conflict list.
Use `--force` to overwrite, or `--dry-run` to preview first.

After import, follow the printed next steps to verify the restored config.

### list

```bash
python3 hermes_migrate.py list ARCHIVE
```

Shows the manifest (export metadata) and all file entries with sizes.

### diff

```bash
python3 hermes_migrate.py diff ARCHIVE [--target-home PATH]
```

Compares an export archive against a target Hermes home. Reports:
- New files (would be created)
- Different files (would be overwritten)
- Identical files (no change)

## Security

### Secrets in exports

By default, exports **include your API keys and tokens** from `.env` and
`auth.json`. Transfer the archive securely (SSH, encrypted USB, etc.).

Use `--redact-secrets` to strip secrets before sharing or storing in less
secure locations:

```bash
python3 hermes_migrate.py export --redact-secrets
```

The redaction heuristic replaces values that look like API keys, JWTs, and
long random strings with `[REDACTED]`. Short numeric config values (timeouts,
port numbers) are preserved. Docker image names and similar technical strings
may also be caught — you'll need to restore those manually on import.

### After importing redacted exports

You'll need to re-authenticate providers:
```bash
hermes auth add <provider>     # OAuth providers
# Or manually edit .env to fill in API keys
```

## Example Workflow

### Migrating to a new server

```bash
# On old host
python3 hermes_migrate.py export -o migrate.tar.gz
scp migrate.tar.gz user@new-host:/tmp/

# On new host (after installing Hermes)
python3 hermes_migrate.py import /tmp/migrate.tar.gz --dry-run
python3 hermes_migrate.py import /tmp/migrate.tar.gz --force
hermes gateway restart
```

### Backing up before major changes

```bash
python3 hermes_migrate.py export -o backup-$(date +%Y%m%d).tar.gz
# Now safe to experiment — restore with import if needed
```

### Sharing config without secrets

```bash
python3 hermes_migrate.py export --redact-secrets -o shared-config.tar.gz
# Safe to share — no API keys included
```

## Requirements

- Python 3.10+ (stdlib only — no pip install needed)
- tar (available on all Linux/macOS)

## Limitations

- Does **not** migrate: session history, log files, gateway state, sandbox
  environments, audio/image caches, background process state
- Import does **not** restart the gateway — you must do that manually
- OAuth tokens in `auth.json` may need re-authentication on a new host
