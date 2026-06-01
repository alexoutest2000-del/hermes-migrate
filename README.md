# Hermes Migrate v1.2.0

Export/import a complete Hermes Agent configuration between hosts.

One command packages everything — config, API keys, skills, cron jobs, memory,
profiles, plugins — into a portable `.tar.gz` archive. One command restores it elsewhere.

## Quick Start

```bash
# Export everything from this host
python3 hermes_migrate.py export

# Creates: hermes-export-<hostname>-<date>.tar.gz

# Import onto another host
python3 hermes_migrate.py import hermes-export-bot-20260531.tar.gz
```

All commands support `--help` for full option listings:

```bash
python3 hermes_migrate.py --help           # Top-level help + examples
python3 hermes_migrate.py export --help    # Export options
python3 hermes_migrate.py import --help    # Import options
python3 hermes_migrate.py --version        # Print version
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
- `SOUL.md` — agent persona / personality definition
- `skills/` — all bundled + custom skills (excluding runtime state)
- `cron/` — scheduled job definitions (excluding run output)
- `memories/` — persistent memory (MEMORY.md, USER.md)
- `plugins/` — third-party and custom plugins
- `profiles/` — all named profiles with their own config, skills, cron, memories, plugins

**What's intentionally excluded:**
- Session history, state databases (state.db, kanban.db), WAL journals
- Logs, shell history, caches (audio, image, model lists)
- Gateway runtime state (PID, lock files, gateway_state.json)
- Background process tracking, auth locks, shell hook approvals
- Sandboxes, checkpoints, LSP binaries, the Hermes installation itself
- Device pairing data, Git hooks, cron output logs, curator state

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

Shows the export manifest (hostname, date, redaction status) and all file
entries with type indicators and sizes. Use this to inspect an archive before
importing.

### diff

```bash
python3 hermes_migrate.py diff ARCHIVE [--target-home PATH]
```

Compares an export archive against a target Hermes home. Reports:
- New files (would be created)
- Different files (would be overwritten)
- Identical files (no change)

Files >1MB are compared by size only to keep the operation fast.

### migrate

```bash
python3 hermes_migrate.py migrate -s SOURCE -d DEST [options]
```

One-command host-to-host migration over SSH. Exports from the source, transfers
the archive via scp, and imports on the destination. Can be run from the source,
the destination, or a third host.

| Option | Default | Description |
|--------|---------|-------------|
| `-s, --source HOST` | — | Source host (user@host). Omit if running on source. |
| `-d, --dest HOST` | — | Destination host (user@host). Omit if running on dest. |
| `-i, --install` | off | Auto-install Hermes on dest if missing (skip prompt) |
| `--redact-secrets` | off | Redact API keys during transfer |
| `--no-profiles` | off | Exclude named profiles |
| `--target-home PATH` | `~/.hermes` | Target Hermes home on destination |

If Hermes is not installed on the destination, the tool prompts to install it
(use `-i` to auto-install without prompting). SSH key authentication is
required — password prompts are forwarded to the terminal.

```bash
# Run from source host (only --dest needed)
python3 hermes_migrate.py migrate -d user@new-server

# Run from destination host (only --source needed)
python3 hermes_migrate.py migrate -s user@old-server

# Run from a third host (both required)
python3 hermes_migrate.py migrate -s user@host1 -d user@host2

# Auto-install Hermes on destination if missing
python3 hermes_migrate.py migrate -s user@host1 -d user@host2 -i

# With redacted secrets (safer over untrusted networks)
python3 hermes_migrate.py migrate -s user@host1 -d user@host2 --redact-secrets
```

## Security

### Secrets in exports

By default, exports **include your API keys and tokens** from `.env` and
`auth.json`. Transfer the archive securely (SSH, encrypted USB, etc.).

Use `--redact-secrets` to strip secrets before sharing or storing in less
secure locations:

```bash
python3 hermes_migrate.py export --redact-secrets
```

The redaction heuristic replaces values that look like API keys (sk-*, hf_*,
ghp_*, etc.), JWTs, and long random-looking strings with `[REDACTED]`. Short
numeric config values (timeouts, port numbers) are preserved.

### After importing redacted exports

You'll need to fill in secrets manually:

```bash
# OAuth providers
hermes auth add <provider>

# Or edit .env directly to paste in API keys
```

## Example Workflow

### Migrating to a new server

```bash
# On old host
python3 hermes_migrate.py export -o migrate.tar.gz
scp migrate.tar.gz user@new-host:/tmp/

# On new host (after installing Hermes)
python3 hermes_migrate.py diff /tmp/migrate.tar.gz --dry-run    # see what's there
python3 hermes_migrate.py import /tmp/migrate.tar.gz --dry-run  # preview
python3 hermes_migrate.py import /tmp/migrate.tar.gz --force    # apply
hermes gateway restart

# Re-authenticate OAuth providers if needed:
hermes auth list
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

## What Migration Does and Doesn't Transfer

### Transferred (configuration — identical behavior on new host)

- Agent personality, models, providers, tools, gateway config
- All skills, cron jobs, memories, plugins
- Named profiles with full config
- Messaging channel mappings
- API keys (if export was not redacted)

### NOT transferred (runtime state — must be set up manually)

- **Session history** — past conversations stay on the old host
- **OAuth tokens** — `auth.json` copies over but tokens are often
  host-bound. Providers like Google and GitHub typically require
  re-authentication on the new machine.
- **Gateway** — you must restart it manually after import.
- **Git repos** — `/home/bot/projects/` and similar directories are
  completely outside the tool's scope. Clone or scp them separately.
- **Shell hook approvals** — machine-specific, not portable.
- **The Hermes installation** — binaries, venv, LSP servers. Install
  Hermes on the target first, then import config on top.
- **Logs, caches, sandboxes, process state** — all explicitly excluded.
