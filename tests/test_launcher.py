from __future__ import annotations

import sys
from pathlib import Path

from zapret_gui.bootstrap import (
    CompletedProc,
    default_project_root,
    ensure_runtime,
    launch_gui,
    runtime_requirement_names,
)

ROOT = Path(__file__).resolve().parents[1]


def test_runtime_names_come_from_this_trees_requirements() -> None:
    req = ROOT / "requirements.txt"
    names = runtime_requirement_names(req)
    text = req.read_text(encoding="utf-8")
    assert "PySide6" in names
    assert "pytest" not in names
    assert "PySide6" in text
    assert default_project_root() == ROOT


def test_ensure_succeeds_when_pyside6_present() -> None:
    result = ensure_runtime(project_root=ROOT, python=sys.executable)
    assert result.ok is True
    assert result.python == sys.executable
    assert result.missing == ()
    assert result.error is None
    assert result.installed is False
    assert result.requirements_path is not None
    assert Path(result.requirements_path) == ROOT / "requirements.txt"


def test_missing_import_takes_requirements_txt_install_path(project_root: Path) -> None:
    pip_calls: list[tuple[str, Path]] = []
    present = {"PySide6": False}

    def importer(name: str) -> object:
        if name == "PySide6" and not present["PySide6"]:
            raise ImportError("No module named 'PySide6'")
        return object()

    def pip_runner(python: str, requirements_path: Path) -> CompletedProc:
        pip_calls.append((python, requirements_path))
        text = requirements_path.read_text(encoding="utf-8")
        assert "PySide6" in text
        present["PySide6"] = True
        return CompletedProc(returncode=0, stdout="installed", stderr="")

    result = ensure_runtime(
        project_root=project_root,
        python=sys.executable,
        importer=importer,
        pip_runner=pip_runner,
    )
    assert result.ok is True
    assert result.installed is True
    assert len(pip_calls) == 1
    assert pip_calls[0][0] == sys.executable
    assert pip_calls[0][1] == project_root / "requirements.txt"


def test_failed_install_does_not_report_success(project_root: Path) -> None:
    def importer(name: str) -> object:
        raise ImportError(name)

    def pip_runner(python: str, requirements_path: Path) -> CompletedProc:
        assert requirements_path == project_root / "requirements.txt"
        return CompletedProc(returncode=1, stdout="", stderr="pip exploded")

    result = ensure_runtime(
        project_root=project_root,
        python=sys.executable,
        importer=importer,
        pip_runner=pip_runner,
    )
    assert result.ok is False
    assert result.error is not None
    assert "pip exploded" in result.error
    assert "requirements.txt" in result.error


def test_missing_python_does_not_start_gui(project_root: Path) -> None:
    spawned: list[tuple] = []

    def finder() -> str | None:
        return None

    def spawn(python: str, args, cwd: Path) -> int:
        spawned.append((python, tuple(args), cwd))
        return 0

    outcome = launch_gui(
        project_root=project_root,
        gui_args=["--smoke"],
        finder=finder,
        spawn=spawn,
    )
    assert outcome.started_gui is False
    assert outcome.exit_code != 0
    assert spawned == []
    assert outcome.error is not None
    assert "python" in outcome.error.lower()
    assert "-m" not in "".join(str(x) for x in spawned)


def test_failed_check_does_not_invoke_zapret_gui(project_root: Path) -> None:
    spawned: list[tuple] = []

    def importer(name: str) -> object:
        raise ImportError(name)

    def pip_runner(python: str, requirements_path: Path) -> CompletedProc:
        return CompletedProc(returncode=9, stdout="", stderr="no network")

    def spawn(python: str, args, cwd: Path) -> int:
        spawned.append((python, tuple(args), Path(cwd)))
        raise AssertionError(f"GUI must not start: {args}")

    outcome = launch_gui(
        project_root=project_root,
        gui_args=["--smoke"],
        python=sys.executable,
        importer=importer,
        pip_runner=pip_runner,
        spawn=spawn,
    )
    assert outcome.started_gui is False
    assert outcome.exit_code == 1
    assert spawned == []
    assert outcome.error is not None
    assert "no network" in outcome.error


def test_successful_check_spawns_python_m_zapret_gui(project_root: Path) -> None:
    spawned: list[tuple] = []

    def spawn(python: str, args, cwd: Path) -> int:
        spawned.append((python, tuple(args), Path(cwd)))
        return 0

    outcome = launch_gui(
        project_root=project_root,
        gui_args=["--smoke", "--root", str(project_root)],
        python=sys.executable,
        spawn=spawn,
    )
    assert outcome.started_gui is True
    assert outcome.exit_code == 0
    assert len(spawned) == 1
    python, args, cwd = spawned[0]
    assert python == sys.executable
    assert args[0] == "-m"
    assert args[1] == "zapret_gui"
    assert "--smoke" in args
    assert cwd == project_root


def test_bat_relocates_checks_requirements_and_starts_module() -> None:
    bat = ROOT / "ZapretControl.bat"
    assert bat.is_file()
    text = bat.read_text(encoding="utf-8")
    assert "%~dp0" in text
    assert "cd /d" in text
    assert "requirements.txt" in text
    assert "zapret_gui.bootstrap" in text
    assert "--ensure" in text
    assert "python -m zapret_gui" in text
    assert "-m zapret_gui" in text
    for line in text.splitlines():
        stripped = line.strip().lower()
        if stripped.startswith("rem"):
            continue
        assert not stripped.startswith("start ")
