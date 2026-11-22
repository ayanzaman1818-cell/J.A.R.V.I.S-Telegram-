"""Consistent online SQLite backups with simple retention."""

from __future__ import annotations

import logging
import sqlite3
from datetime import datetime
from pathlib import Path

import config

log = logging.getLogger("jarvis.backup")

_PREFIX = "jarvis-"


def list_backups(backup_dir: Path | None = None) -> list[Path]:
    """Return existing backups, newest first."""
    folder = backup_dir or config.BACKUP_DIR
    if not folder.exists():
        return []
    return sorted(
        folder.glob(f"{_PREFIX}*.db"),
        key=lambda p: (p.stat().st_mtime_ns, p.name),
        reverse=True,
    )


def backup_database(
    db_path: Path | None = None,
    backup_dir: Path | None = None,
    keep: int | None = None,
) -> Path:
    """Copy the live database using SQLite's backup API (safe while JARVIS is writing).

    Returns the new backup path and prunes everything beyond the newest `keep` files.
    """
    source = Path(db_path or config.DB_PATH)
    folder = Path(backup_dir or config.BACKUP_DIR)
    keep = keep if keep is not None else config.BACKUP_KEEP
    if not source.exists():
        raise FileNotFoundError(f"Database not found: {source}")

    folder.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(config.get_zoneinfo()).strftime("%Y%m%d-%H%M%S")
    counter = 0
    target = folder / f"{_PREFIX}{stamp}-{counter:02d}.db"
    while target.exists():
        counter += 1
        target = folder / f"{_PREFIX}{stamp}-{counter:02d}.db"

    src = sqlite3.connect(str(source))
    try:
        dst = sqlite3.connect(str(target))
        try:
            src.backup(dst)
            ok = dst.execute("PRAGMA integrity_check").fetchone()[0]
        finally:
            dst.close()
    finally:
        src.close()

    if ok != "ok":
        target.unlink(missing_ok=True)
        raise RuntimeError(f"Backup failed integrity check: {ok}")

    for stale in list_backups(folder)[keep:]:
        try:
            stale.unlink()
        except OSError:
            log.warning("Could not prune old backup %s", stale.name)

    log.info("Database backup written: %s (%d bytes)", target.name, target.stat().st_size)
    return target


def backup_summary() -> str:
    backups = list_backups()
    if not backups:
        return "no backups yet"
    newest = backups[0]
    size_kb = newest.stat().st_size / 1024
    return f"{len(backups)} kept, latest {newest.name} ({size_kb:.0f} KB)"
