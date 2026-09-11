from __future__ import annotations

import re
from datetime import datetime
from pathlib import Path

from zapret_gui.lists_io import (
    backup_all_lists,
    backup_list,
    list_files,
    read_list,
    write_list,
)


def test_read_real_lists_file(project_root: Path) -> None:
    path = project_root / "lists" / "list-exclude-user.txt"
    assert path.is_file()
    shipped = read_list(path)
    independent = path.read_text(encoding="utf-8")
    assert shipped.replace("\r\n", "\n") == independent.replace("\r\n", "\n")
    assert shipped  # this tree's exclude-user list is not empty


def test_list_files_hides_snapshots(project_root: Path) -> None:
    names = {p.name for p in list_files(project_root / "lists")}
    assert "list-general.txt" in names
    assert "ipset-all.txt" in names
    assert not any(n.endswith(".bak") for n in names)
    assert "ipset-all.txt.backup" not in names


def test_backup_real_file_timestamped_and_matches(project_root: Path) -> None:
    src = project_root / "lists" / "list-exclude-user.txt"
    when = datetime(2026, 9, 11, 15, 30, 45)
    dest = backup_list(src, when=when)
    try:
        assert dest.exists()
        assert dest.parent == src.parent
        assert dest.read_bytes() == src.read_bytes()
        assert re.search(r"\d{8}-\d{6}", dest.name)
        assert dest.name.startswith(src.name)
        assert dest.name.endswith(".bak")
        assert dest != src
    finally:
        dest.unlink(missing_ok=True)


def test_save_changes_original_on_copied_real_file(project_root: Path, tmp_path: Path) -> None:
    src = project_root / "lists" / "list-exclude-user.txt"
    work = tmp_path / "list-exclude-user.txt"
    work.write_bytes(src.read_bytes())
    original = read_list(work)
    marker = "# zapret-gui-save-probe"
    assert marker not in original
    write_list(work, original + "\n" + marker + "\n")
    after = read_list(work)
    assert marker in after
    assert after != original
    # The real project file must be untouched.
    assert marker not in read_list(src)


def test_backup_then_save_preserves_snapshot(tmp_path: Path) -> None:
    work = tmp_path / "list-general-user.txt"
    work.write_text("alpha.example\n", encoding="utf-8", newline="\n")
    before = work.read_bytes()
    snap = backup_list(work)
    write_list(work, "beta.example\n")
    assert snap.read_bytes() == before
    assert read_list(work).replace("\r\n", "\n") == "beta.example\n"


def test_backup_all_zip(project_root: Path, tmp_path: Path) -> None:
    # Copy two real lists into an isolated lists dir so we don't drop zip files in the tree.
    isolated = tmp_path / "lists"
    isolated.mkdir()
    for name in ("list-exclude-user.txt", "list-general-user.txt"):
        data = (project_root / "lists" / name).read_bytes()
        (isolated / name).write_bytes(data)
    archive = backup_all_lists(isolated, when=datetime(2026, 9, 11, 16, 0, 0))
    assert archive.is_file()
    assert archive.suffix == ".zip"
    assert "20260911-160000" in archive.name
    import zipfile

    with zipfile.ZipFile(archive) as zf:
        names = set(zf.namelist())
        assert "list-exclude-user.txt" in names
        assert zf.read("list-exclude-user.txt") == (isolated / "list-exclude-user.txt").read_bytes()


def test_lists_module_does_not_invoke_notepad() -> None:
    source = Path(__file__).resolve().parents[1] / "zapret_gui" / "lists_io.py"
    text = source.read_text(encoding="utf-8").lower()
    assert "notepad.exe" not in text
    assert "subprocess" not in text
    assert "os.startfile" not in text
    assert "shell_execute" not in text
