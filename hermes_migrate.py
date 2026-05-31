#!/usr/bin/env python3
"""
Hermes Migrate — Export/Import Hermes Agent configurations between hosts.

Exports all Hermes configuration (config.yaml, .env, auth tokens, skills,
cron jobs, memory, channel directory, profiles) into a portable tar.gz archive.
Imports that archive onto a new Hermes instance with conflict detection.

Usage:
    hermes-migrate export [--output PATH] [--redact-secrets] [--no-profiles]
    hermes-migrate import ARCHIVE [--dry-run] [--force] [--target-home PATH]
    hermes-migrate list ARCHIVE
    hermes-migrate diff ARCHIVE
"""

import argparse
import io
import json
import os
import re
import shutil
import sys
import tarfile
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


# ── Constants ────────────────────────────────────────────────────────────────

# Files/dirs that constitute Hermes configuration (relative to HERMES_HOME)
CONFIG_FILES = [
    "config.yaml",
    ".env",
    "auth.json",
    "channel_directory.json",
    "SOUL.md",
]

CONFIG_DIRS = [
    "skills",
    "memories",
    "cron",
    "plugins",
]

# Files/dirs we explicitly exclude from skills/
SKILLS_EXCLUDE = {
    ".bundled_manifest",   # regenerated on install
    ".usage.json",         # local usage tracking
    ".usage.json.lock",
    ".curator_state",      # curator runtime state
    ".curator_backups",    # curator backup archives
}

# Files/dirs we explicitly exclude from cron/
CRON_EXCLUDE = {
    "output",              # cron run output logs
    ".tick.lock",          # runtime lock
}

# Files at the top level we exclude (runtime state, caches, logs, sessions)
TOP_LEVEL_EXCLUDE = {
    "sessions",
    "logs",
    "state.db",
    "state.db-shm",
    "state.db-wal",
    "hermes-agent",
    "lsp",
    "bin",
    "audio_cache",
    "image_cache",
    "images",
    "cache",
    "checkpoints",
    "sandboxes",
    "hooks",
    "pairing",
    ".hermes_history",
    "gateway.pid",
    "gateway.lock",
    "gateway_state.json",
    "processes.json",
    "models_dev_cache.json",
    "ollama_cloud_models_cache.json",
    "interrupt_debug.log",
    ".install_method",
    "kanban.db",
    "kanban.db-shm",
    "kanban.db-wal",
    "shell-hooks-allowlist.json",
    "auth.lock",
    "state-snapshots",
    "config.yaml.bak." + "*",   # backup configs
    ".env.bak." + "*",
}


def get_hermes_home() -> Path:
    """Resolve HERMES_HOME from environment or default."""
    return Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes")).expanduser().resolve()


def get_export_name() -> str:
    """Generate a descriptive export filename."""
    hostname = os.uname().nodename
    date_str = datetime.now().strftime("%Y%m%d-%H%M%S")
    return f"hermes-export-{hostname}-{date_str}.tar.gz"


# ── Export ───────────────────────────────────────────────────────────────────

def export_config(
    output_path: Optional[str] = None,
    redact_secrets: bool = False,
    include_profiles: bool = True,
    hermes_home: Optional[Path] = None,
) -> Path:
    """
    Package Hermes configuration into a tar.gz archive.

    Returns the path to the created archive.
    """
    hermes_home = hermes_home or get_hermes_home()

    if not hermes_home.exists():
        print(f"ERROR: Hermes home not found at {hermes_home}")
        sys.exit(1)

    if not output_path:
        output_path = get_export_name()

    output_path = Path(output_path).expanduser().resolve()

    print(f"Hermes home: {hermes_home}")
    print(f"Export to:   {output_path}")

    # Build manifest of files to include
    manifest = build_export_manifest(hermes_home, include_profiles)

    if not manifest:
        print("ERROR: No Hermes configuration files found to export.")
        sys.exit(1)

    print(f"\nExporting {len(manifest)} items:")

    # Build archive-internal paths (strip hermes_home prefix)
    archive_members = []
    for src_path in sorted(manifest):
        arcname = src_path.relative_to(hermes_home.parent)
        size = src_path.stat().st_size if src_path.is_file() else sum(
            f.stat().st_size for f in src_path.rglob("*") if f.is_file()
        )
        print(f"  {arcname}  ({_human_size(size)})")
        archive_members.append((src_path, str(arcname)))

    # Write manifest metadata into the archive
    manifest_data = {
        "version": 1,
        "exported_at": datetime.now(timezone.utc).isoformat(),
        "hostname": os.uname().nodename,
        "hermes_home": str(hermes_home),
        "item_count": len(manifest),
        "redacted": redact_secrets,
    }

    print("\nCompressing...")
    with tarfile.open(output_path, "w:gz") as tar:
        # Write the manifest as the first entry
        manifest_json = json.dumps(manifest_data, indent=2).encode("utf-8")
        info = tarfile.TarInfo(name=".hermes-migrate-manifest.json")
        info.size = len(manifest_json)
        tar.addfile(info, io.BytesIO(manifest_json))

        for src_path, arcname in archive_members:
            if src_path.is_file():
                content = src_path.read_bytes()

                # Redact secrets in .env files
                if redact_secrets and src_path.name == ".env":
                    content = redact_env_content(content)

                # Write with original permissions
                info = tar.gettarinfo(
                    name=str(src_path),
                    arcname=arcname,
                )
                info.uid = 0
                info.gid = 0
                info.uname = ""
                info.gname = ""
                # For files, set size from processed content
                if src_path.is_file():
                    info.size = len(content)
                    tar.addfile(info, io.BytesIO(content))
                else:
                    tar.addfile(info)
            else:
                tar.add(src_path, arcname=arcname, recursive=True)

    archive_size = output_path.stat().st_size
    print(f"\nDone: {output_path} ({_human_size(archive_size)})")

    if not redact_secrets:
        print("\n*** SECURITY NOTICE ***")
        print("This export CONTAINS your API keys and secrets (.env, auth.json).")
        print("Transfer it securely. Use --redact-secrets to strip them out.")

    return output_path


def build_export_manifest(hermes_home: Path, include_profiles: bool) -> list[Path]:
    """Build the list of files/dirs to include in the export."""
    manifest = []

    # Top-level config files
    for fname in CONFIG_FILES:
        fpath = hermes_home / fname
        if fpath.exists():
            manifest.append(fpath)

    # Config directories (filtered)
    for dname in CONFIG_DIRS:
        dpath = hermes_home / dname
        if not dpath.exists():
            continue
        _add_dir_contents(manifest, dpath, hermes_home, dname)

    # Profiles
    if include_profiles:
        profiles_dir = hermes_home / "profiles"
        if profiles_dir.exists():
            for profile_dir in sorted(profiles_dir.iterdir()):
                if not profile_dir.is_dir():
                    continue
                _add_profile_contents(manifest, profile_dir, hermes_home)

    # Check for top-level files we might have missed
    for entry in sorted(hermes_home.iterdir()):
        if entry.name.startswith("."):
            continue
        if _should_exclude_top_level(entry.name):
            continue
        if entry.name in CONFIG_FILES or entry.name in CONFIG_DIRS:
            continue
        if entry.is_dir() and entry.name == "profiles":
            continue
        # Any other file at top level that isn't excluded — include it
        if entry.is_file():
            manifest.append(entry)

    return manifest


def _add_dir_contents(
    manifest: list,
    dirpath: Path,
    hermes_home: Path,
    dirname: str,
) -> None:
    """Add contents of a config directory, filtering out excluded items."""
    excludes = {"skills": SKILLS_EXCLUDE, "cron": CRON_EXCLUDE}.get(dirname, set())

    for entry in sorted(dirpath.rglob("*")):
        # Build relative path from dirpath
        rel = entry.relative_to(dirpath)
        parts = rel.parts

        # Check if any part is excluded
        skip = False
        for part in parts:
            if part in excludes:
                skip = True
                break
            # Glob-style wildcard match for backup files
            for exc in excludes:
                if "*" in exc:
                    import fnmatch
                    if fnmatch.fnmatch(part, exc):
                        skip = True
                        break
            if skip:
                break
        if skip:
            continue

        # Skip lock files
        if entry.name.endswith(".lock"):
            continue

        if entry.is_file():
            manifest.append(entry)


def _add_profile_contents(
    manifest: list,
    profile_dir: Path,
    hermes_home: Path,
) -> None:
    """Add a profile's config files, filtered."""
    # Profile config files
    for fname in CONFIG_FILES:
        fpath = profile_dir / fname
        if fpath.exists():
            manifest.append(fpath)

    # Profile skills, memories, cron, plugins
    for dname in CONFIG_DIRS + ["plugins"]:
        dpath = profile_dir / dname
        if not dpath.exists():
            continue
        _add_dir_contents(manifest, dpath, hermes_home, dname)

    # Any other profile files
    for entry in sorted(profile_dir.iterdir()):
        if entry.name.startswith("."):
            continue
        if entry.name in CONFIG_FILES or entry.name in CONFIG_DIRS or entry.name == "plugins":
            continue
        if entry.is_file():
            manifest.append(entry)


def _should_exclude_top_level(name: str) -> bool:
    """Check if a top-level file/dir should be excluded."""
    import fnmatch
    for pattern in TOP_LEVEL_EXCLUDE:
        if fnmatch.fnmatch(name, pattern):
            return True
    return False


def redact_env_content(content: bytes) -> bytes:
    """Replace API key values with [REDACTED] placeholders."""
    text = content.decode("utf-8", errors="replace")
    lines = []
    for line in text.splitlines():
        # Match KEY=VALUE or KEY="VALUE" or KEY='VALUE'
        m = re.match(r'^(\s*[A-Z_][A-Z0-9_]*\s*=\s*)(.+)$', line)
        if m:
            prefix = m.group(1)
            value = m.group(2).strip()
            # Check if value looks like a secret
            if _looks_like_secret(value):
                lines.append(f"{prefix}[REDACTED]")
                continue
        lines.append(line)
    return ("\n".join(lines) + "\n").encode("utf-8")


def _looks_like_secret(value: str) -> bool:
    """Heuristic: does this value look like an API key/token/secret?"""
    value = value.strip().strip('"').strip("'")
    if not value:
        return False
    if len(value) < 8:
        return False
    # Common patterns: sk-..., eyJ..., ghp_..., hf_..., etc.
    secret_prefixes = (
        "sk-", "sk_", "eyJ", "ghp_", "gho_", "ghu_", "ghs_",
        "hf_", "xai-", "gsk_", "dsk_", "ak-",
    )
    if any(value.startswith(p) for p in secret_prefixes):
        return True
    # Long random-looking strings (>30 chars, mostly alphanumeric)
    if len(value) > 30:
        alpha_ratio = sum(c.isalnum() or c in "-_." for c in value) / max(len(value), 1)
        if alpha_ratio > 0.8:
            return True
    # Contains "token", "key", "secret" in variable name context (we don't have
    # the var name here, but typical env file patterns)
    return False


# ── Import ───────────────────────────────────────────────────────────────────

def import_config(
    archive_path: str,
    dry_run: bool = False,
    force: bool = False,
    target_home: Optional[str] = None,
) -> None:
    """
    Import Hermes configuration from a tar.gz archive.

    Extracts files to the target Hermes home, checking for conflicts.
    """
    archive_path = Path(archive_path).expanduser().resolve()
    hermes_home = Path(target_home).expanduser().resolve() if target_home else get_hermes_home()

    if not archive_path.exists():
        print(f"ERROR: Archive not found: {archive_path}")
        sys.exit(1)

    print(f"Archive:     {archive_path}")
    print(f"Target home: {hermes_home}")
    print(f"Mode:        {'dry-run' if dry_run else 'live'}{' (force)' if force else ''}")

    with tarfile.open(archive_path, "r:gz") as tar:
        members = tar.getmembers()

        # Read manifest
        manifest = None
        for m in members:
            if m.name == ".hermes-migrate-manifest.json":
                manifest = json.loads(tar.extractfile(m).read())
                break

        if manifest:
            print(f"\nExport metadata:")
            print(f"  Created:    {manifest.get('exported_at', 'unknown')}")
            print(f"  From host:  {manifest.get('hostname', 'unknown')}")
            print(f"  Redacted:   {manifest.get('redacted', False)}")
            if manifest.get("redacted"):
                print("  *** WARNING: Secrets were redacted in this export. ***")
                print("  *** You'll need to fill in API keys manually.       ***")

        conflicts = []
        to_extract = []

        for m in members:
            if m.name == ".hermes-migrate-manifest.json":
                continue

            # Resolve target path
            # Archive paths are relative to the hermes home PARENT
            # e.g., .hermes/config.yaml → {target_home}/../.hermes/config.yaml
            # Actually, we want them relative to the target hermes_home
            # Archive arcname format: .hermes/config.yaml
            # Target: {target_home_parent}/.hermes/config.yaml
            # If target_home is /home/user/.hermes, parent is /home/user
            # So target = parent / arcname

            # Extract the last component for the hermes home relative part
            # Archive stores as: .hermes/config.yaml or .hermes/skills/...
            # We need to map this to the target's hermes home
            parts = Path(m.name).parts
            if parts[0] == ".hermes":
                # Map to target hermes home
                rel_path = Path(*parts[1:]) if len(parts) > 1 else Path(".")
                target_path = hermes_home / rel_path
            elif parts[0].startswith("."):
                # Other dot-prefixed things
                target_path = hermes_home.parent / m.name
            else:
                target_path = hermes_home.parent / m.name

            if m.isdir():
                if not dry_run:
                    target_path.mkdir(parents=True, exist_ok=True)
                continue

            if target_path.exists() and not force:
                conflicts.append((m.name, str(target_path)))
                continue

            to_extract.append((m, target_path))

        # Report conflicts
        if conflicts:
            print(f"\n{len(conflicts)} file(s) already exist on target (use --force to overwrite):")
            for arcname, target in conflicts:
                print(f"  {arcname}  →  {target}")
            if not dry_run:
                print("\nAborting. Use --force to overwrite, or --dry-run to preview.")
                sys.exit(1)

        if dry_run:
            print(f"\nWould extract {len(to_extract)} file(s):")
            for m, target in sorted(to_extract, key=lambda x: str(x[1])):
                print(f"  {target}")
            return

        # Extract
        print(f"\nExtracting {len(to_extract)} file(s)...")
        for m, target_path in to_extract:
            target_path.parent.mkdir(parents=True, exist_ok=True)
            content = tar.extractfile(m).read()

            # Restore permissions
            target_path.write_bytes(content)
            if m.mode:
                target_path.chmod(m.mode)

            print(f"  {target_path}")

    print(f"\nImport complete. {len(to_extract)} files restored to {hermes_home}")

    if manifest and manifest.get("redacted"):
        print("\n*** REMINDER: This export had secrets redacted. ***")
        print("*** Run 'hermes auth add <provider>' to re-authenticate each provider. ***")
        print("*** Or manually edit .env to fill in your API keys. ***")

    print("\nNext steps:")
    print("  1. Verify:  hermes config check")
    print("  2. Re-auth: hermes auth list    (re-authenticate OAuth providers)")
    print("  3. Test:    hermes doctor")
    print("  4. Restart gateway if running: hermes gateway restart")


# ── List ─────────────────────────────────────────────────────────────────────

def list_archive(archive_path: str) -> None:
    """List the contents of an export archive."""
    archive_path = Path(archive_path).expanduser().resolve()

    if not archive_path.exists():
        print(f"ERROR: Archive not found: {archive_path}")
        sys.exit(1)

    with tarfile.open(archive_path, "r:gz") as tar:
        members = tar.getmembers()

        # Show manifest first
        for m in members:
            if m.name == ".hermes-migrate-manifest.json":
                manifest = json.loads(tar.extractfile(m).read())
                print("=== Export Manifest ===")
                for k, v in manifest.items():
                    print(f"  {k}: {v}")
                print()

        print(f"=== Contents ({len([m for m in members if m.name != '.hermes-migrate-manifest.json'])} items) ===")
        for m in members:
            if m.name == ".hermes-migrate-manifest.json":
                continue
            type_char = "D" if m.isdir() else "F"
            size = _human_size(m.size) if m.isfile() else ""
            print(f"  [{type_char}] {m.name:60s} {size}")


# ── Diff ─────────────────────────────────────────────────────────────────────

def diff_archive(archive_path: str, target_home: Optional[str] = None) -> None:
    """Compare an export archive against the current (or target) Hermes home."""
    archive_path = Path(archive_path).expanduser().resolve()
    hermes_home = Path(target_home).expanduser().resolve() if target_home else get_hermes_home()

    if not archive_path.exists():
        print(f"ERROR: Archive not found: {archive_path}")
        sys.exit(1)

    print(f"Comparing archive {archive_path.name}")
    print(f"  against {hermes_home}\n")

    with tarfile.open(archive_path, "r:gz") as tar:
        members = [m for m in tar.getmembers()
                   if m.isfile() and m.name != ".hermes-migrate-manifest.json"]

        new_files = []
        different = []
        identical = []
        missing_target = []

        for m in members:
            parts = Path(m.name).parts
            if parts[0] == ".hermes":
                rel_path = Path(*parts[1:]) if len(parts) > 1 else Path(".")
                target_path = hermes_home / rel_path
            else:
                target_path = hermes_home.parent / m.name

            if not target_path.exists():
                new_files.append(str(target_path))
                continue

            # Compare content for text files
            if m.size < 1_000_000:  # skip diff for files > 1MB
                archive_content = tar.extractfile(m).read()
                target_content = target_path.read_bytes()
                if archive_content != target_content:
                    different.append(str(target_path))
                else:
                    identical.append(str(target_path))
            else:
                # Just compare size for large files
                if m.size != target_path.stat().st_size:
                    different.append(f"{target_path} (size differs)")
                else:
                    identical.append(str(target_path))

        # Check for files that exist on target but not in archive
        # (only for tracked config paths)
        archive_files = set()
        for m in members:
            parts = Path(m.name).parts
            if parts[0] == ".hermes":
                rel = str(Path(*parts[1:])) if len(parts) > 1 else "."
                archive_files.add(rel)

    print(f"  New (would be created):     {len(new_files)}")
    for f in sorted(new_files)[:20]:
        print(f"    + {f}")
    if len(new_files) > 20:
        print(f"    ... and {len(new_files) - 20} more")

    print(f"\n  Different (would overwrite): {len(different)}")
    for f in sorted(different)[:20]:
        print(f"    ~ {f}")
    if len(different) > 20:
        print(f"    ... and {len(different) - 20} more")

    print(f"\n  Identical (no change):       {len(identical)}")


# ── Helpers ──────────────────────────────────────────────────────────────────

def _human_size(size: int) -> str:
    """Format byte size for human readability."""
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024:
            return f"{size:.1f} {unit}" if unit != "B" else f"{size} B"
        size /= 1024
    return f"{size:.1f} TB"


# ── CLI ──────────────────────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description="Hermes Migrate — Export/Import Hermes Agent configurations.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  hermes-migrate export                           # Export full config
  hermes-migrate export --redact-secrets          # Export with API keys redacted
  hermes-migrate export -o backup.tar.gz          # Custom output path
  hermes-migrate import backup.tar.gz             # Import to current host
  hermes-migrate import backup.tar.gz --dry-run   # Preview what would happen
  hermes-migrate import backup.tar.gz --force     # Overwrite conflicts
  hermes-migrate list backup.tar.gz               # List archive contents
  hermes-migrate diff backup.tar.gz               # Compare archive vs current
        """,
    )

    sub = parser.add_subparsers(dest="command", help="Command")

    # export
    p_export = sub.add_parser("export", help="Export Hermes configuration to archive")
    p_export.add_argument(
        "-o", "--output",
        help="Output archive path (default: hermes-export-<host>-<date>.tar.gz)",
    )
    p_export.add_argument(
        "--redact-secrets",
        action="store_true",
        help="Replace API keys/secrets with [REDACTED] in the export",
    )
    p_export.add_argument(
        "--no-profiles",
        action="store_true",
        help="Exclude named profiles from the export",
    )
    p_export.add_argument(
        "--hermes-home",
        help=f"Path to Hermes home directory (default: {get_hermes_home()})",
    )

    # import
    p_import = sub.add_parser("import", help="Import Hermes configuration from archive")
    p_import.add_argument("archive", help="Path to the export archive (.tar.gz)")
    p_import.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be imported without making changes",
    )
    p_import.add_argument(
        "--force",
        action="store_true",
        help="Overwrite existing files on target",
    )
    p_import.add_argument(
        "--target-home",
        help=f"Target Hermes home directory (default: {get_hermes_home()})",
    )

    # list
    p_list = sub.add_parser("list", help="List contents of an export archive")
    p_list.add_argument("archive", help="Path to the export archive (.tar.gz)")

    # diff
    p_diff = sub.add_parser("diff", help="Compare archive against current/target config")
    p_diff.add_argument("archive", help="Path to the export archive (.tar.gz)")
    p_diff.add_argument(
        "--target-home",
        help=f"Target Hermes home to compare against (default: {get_hermes_home()})",
    )

    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(1)

    if args.command == "export":
        export_config(
            output_path=args.output,
            redact_secrets=args.redact_secrets,
            include_profiles=not args.no_profiles,
            hermes_home=Path(args.hermes_home) if args.hermes_home else None,
        )
    elif args.command == "import":
        import_config(
            archive_path=args.archive,
            dry_run=args.dry_run,
            force=args.force,
            target_home=args.target_home,
        )
    elif args.command == "list":
        list_archive(args.archive)
    elif args.command == "diff":
        diff_archive(args.archive, target_home=args.target_home)


if __name__ == "__main__":
    main()
