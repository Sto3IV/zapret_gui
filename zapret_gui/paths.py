"""Project-tree discovery. The GUI lives beside bin\\, lists\\, and general*.bat."""

from __future__ import annotations

from pathlib import Path


class ProjectRootError(FileNotFoundError):
    """Raised when bin\\winws.exe and lists\\ cannot be located."""


def detect_project_root(start: Path | None = None) -> Path:
    """Return the Zapret tree that contains bin\\winws.exe and lists\\.

    Search order: explicit ``start``, the package parent, then cwd and its parents.
    """
    candidates: list[Path] = []
    if start is not None:
        candidates.append(Path(start).resolve())
    candidates.append(Path(__file__).resolve().parent.parent)
    cwd = Path.cwd().resolve()
    candidates.append(cwd)
    candidates.extend(cwd.parents)

    seen: set[Path] = set()
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        if _is_project_root(candidate):
            return candidate
    raise ProjectRootError(
        "Cannot locate the Zapret project root (need bin\\winws.exe and lists\\)."
    )


def _is_project_root(path: Path) -> bool:
    return (path / "bin" / "winws.exe").is_file() and (path / "lists").is_dir()


def bin_dir(root: Path) -> Path:
    return (root / "bin").resolve()


def lists_dir(root: Path) -> Path:
    return (root / "lists").resolve()


def winws_path(root: Path) -> Path:
    return (root / "bin" / "winws.exe").resolve()


def with_trailing_sep(path: Path) -> str:
    text = str(path)
    return text if text.endswith(("\\", "/")) else text + "\\"
