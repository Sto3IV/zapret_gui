"""In-app lists/ reader, writer, and timestamped backup. No external process."""

from __future__ import annotations

import re
import shutil
import zipfile
from datetime import datetime
from pathlib import Path

BACKUP_NAME = re.compile(r"\.\d{8}-\d{6}(?:-\d+)?\.bak$", re.IGNORECASE)


class ListsError(OSError):
    """lists/ I/O failure intended for UI display."""


def list_files(lists_dir: Path) -> list[Path]:
    """Editable files under lists/. Backups and the editor's own snapshots are hidden."""
    lists_dir = Path(lists_dir)
    if not lists_dir.is_dir():
        raise ListsError(f"lists directory does not exist: {lists_dir}")
    files: list[Path] = []
    for path in lists_dir.iterdir():
        if not path.is_file():
            continue
        if _is_snapshot(path):
            continue
        files.append(path)
    files.sort(key=lambda p: p.name.lower())
    return files


def read_list(path: Path) -> str:
    path = Path(path)
    try:
        data = path.read_bytes()
    except OSError as exc:
        raise ListsError(f"Cannot read {path}: {exc}") from exc
    for encoding in ("utf-8-sig", "utf-8", "cp1251", "cp437"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def write_list(path: Path, content: str) -> None:
    """Persist editor contents. Does not launch an external process."""
    path = Path(path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8", newline="\n")
    except OSError as exc:
        raise ListsError(f"Cannot write {path}: {exc}") from exc


def backup_list(path: Path, *, when: datetime | None = None) -> Path:
    """Copy ``path`` to a timestamped sibling ``{name}.{YYYYMMDD-HHMMSS}.bak``."""
    path = Path(path)
    if not path.is_file():
        raise ListsError(f"Cannot backup missing file: {path}")
    stamp = (when or datetime.now()).strftime("%Y%m%d-%H%M%S")
    dest = path.with_name(f"{path.name}.{stamp}.bak")
    counter = 1
    while dest.exists():
        dest = path.with_name(f"{path.name}.{stamp}-{counter}.bak")
        counter += 1
    try:
        shutil.copy2(path, dest)
    except OSError as exc:
        raise ListsError(f"Cannot backup {path}: {exc}") from exc
    return dest


def backup_all_lists(lists_dir: Path, *, when: datetime | None = None) -> Path:
    """Zip every editable list file into ``lists/backups/lists-YYYYMMDD-HHMMSS.zip``."""
    lists_dir = Path(lists_dir)
    files = list_files(lists_dir)
    if not files:
        raise ListsError(f"No list files to archive in {lists_dir}")
    stamp = (when or datetime.now()).strftime("%Y%m%d-%H%M%S")
    archive_dir = lists_dir / "backups"
    try:
        archive_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ListsError(f"Cannot create backup directory: {exc}") from exc
    dest = archive_dir / f"lists-{stamp}.zip"
    counter = 1
    while dest.exists():
        dest = archive_dir / f"lists-{stamp}-{counter}.zip"
        counter += 1
    try:
        with zipfile.ZipFile(dest, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for path in files:
                zf.write(path, arcname=path.name)
    except OSError as exc:
        raise ListsError(f"Cannot write archive {dest}: {exc}") from exc
    return dest


def backup_matches_source(source: Path, backup: Path) -> bool:
    return Path(source).read_bytes() == Path(backup).read_bytes()


def _is_snapshot(path: Path) -> bool:
    name = path.name.lower()
    if name.endswith(".backup") or name.endswith(".bak"):
        return True
    if BACKUP_NAME.search(path.name):
        return True
    return False
