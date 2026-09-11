"""Operator bootstrap: verify/install GUI runtime deps, then start zapret_gui.

Imported by ZapretControl.bat. Does not import PySide6 or app.py so a missing
runtime package cannot prevent the probe itself from running.
"""

from __future__ import annotations

import importlib
import re
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Sequence

TEST_ONLY_REQUIREMENTS = frozenset(
    {
        "pytest",
        "pytest-cov",
        "pytest-qt",
        "coverage",
        "ruff",
        "mypy",
        "black",
        "flake8",
    }
)

_REQ_NAME = re.compile(r"^([A-Za-z0-9_.-]+)")


@dataclass(frozen=True)
class CompletedProc:
    returncode: int
    stdout: str = ""
    stderr: str = ""


@dataclass(frozen=True)
class ProbeResult:
    ok: bool
    python: str | None
    missing: tuple[str, ...] = ()
    error: str | None = None
    installed: bool = False
    requirements_path: str | None = None


@dataclass(frozen=True)
class LaunchOutcome:
    exit_code: int
    started_gui: bool
    probe: ProbeResult
    error: str | None = None
    spawn_argv: tuple[str, ...] = field(default_factory=tuple)


Importer = Callable[[str], object]
PipRunner = Callable[[str, Path], CompletedProc]
PythonFinder = Callable[[], str | None]
Spawner = Callable[[str, Sequence[str], Path], int]


def default_project_root() -> Path:
    return Path(__file__).resolve().parent.parent


def find_python() -> str | None:
    executable = getattr(sys, "executable", None)
    if executable and Path(executable).is_file():
        return executable
    for name in ("python", "python3"):
        found = shutil.which(name)
        if found:
            return found
    py = shutil.which("py")
    if py:
        return py
    return None


def runtime_requirement_names(requirements_path: Path) -> tuple[str, ...]:
    """Third-party packages from requirements.txt that the GUI needs to start.

    Test-only tools (pytest, …) are skipped: they may be installed as a side
    effect of ``pip install -r`` but are not required to import zapret_gui.
    """
    if not requirements_path.is_file():
        return ()
    names: list[str] = []
    for raw in requirements_path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        match = _REQ_NAME.match(line)
        if not match:
            continue
        name = match.group(1)
        if name.lower() in TEST_ONLY_REQUIREMENTS or name.lower().startswith("pytest"):
            continue
        names.append(name)
    return tuple(names)


def missing_modules(
    modules: Sequence[str],
    *,
    importer: Importer | None = None,
) -> tuple[str, ...]:
    load = importer if importer is not None else importlib.import_module
    missing: list[str] = []
    for name in modules:
        try:
            load(name)
        except ImportError:
            missing.append(name)
    return tuple(missing)


def default_pip_install(python: str, requirements_path: Path) -> CompletedProc:
    proc = subprocess.run(
        [python, "-m", "pip", "install", "-r", str(requirements_path)],
        capture_output=True,
        text=True,
        cwd=str(requirements_path.parent),
        check=False,
    )
    return CompletedProc(int(proc.returncode), proc.stdout or "", proc.stderr or "")


def default_spawn(python: str, args: Sequence[str], cwd: Path) -> int:
    proc = subprocess.run([python, *list(args)], cwd=str(cwd), check=False)
    return int(proc.returncode)


def ensure_runtime(
    *,
    project_root: Path | None = None,
    python: str | None = None,
    finder: PythonFinder | None = None,
    importer: Importer | None = None,
    pip_runner: PipRunner | None = None,
) -> ProbeResult:
    """Shipped requirement check used by ZapretControl.bat.

    Tests inject finder / importer / pip_runner. Never uninstalls packages.
    """
    root = Path(project_root) if project_root is not None else default_project_root()
    requirements_path = root / "requirements.txt"
    resolved_python = python
    if not resolved_python:
        resolved_python = (finder or find_python)()
    if not resolved_python:
        return ProbeResult(
            ok=False,
            python=None,
            error="Python 3.10+ was not found. Install Python and enable 'Add python.exe to PATH'.",
            requirements_path=str(requirements_path),
        )
    if not requirements_path.is_file():
        return ProbeResult(
            ok=False,
            python=resolved_python,
            error=f"requirements.txt not found at {requirements_path}",
            requirements_path=str(requirements_path),
        )
    modules = runtime_requirement_names(requirements_path)
    if not modules:
        return ProbeResult(
            ok=False,
            python=resolved_python,
            error=f"No GUI runtime packages listed in {requirements_path}",
            requirements_path=str(requirements_path),
        )
    missing = missing_modules(modules, importer=importer)
    if not missing:
        return ProbeResult(
            ok=True,
            python=resolved_python,
            missing=(),
            requirements_path=str(requirements_path),
        )
    runner = pip_runner if pip_runner is not None else default_pip_install
    try:
        completed = runner(resolved_python, requirements_path)
    except Exception as exc:
        return ProbeResult(
            ok=False,
            python=resolved_python,
            missing=missing,
            error=f"Failed to install from {requirements_path}: {exc}",
            requirements_path=str(requirements_path),
        )
    if int(completed.returncode) != 0:
        detail = (completed.stderr or completed.stdout or f"exit {completed.returncode}").strip()
        return ProbeResult(
            ok=False,
            python=resolved_python,
            missing=missing,
            error=f"pip install -r {requirements_path} failed: {detail}",
            requirements_path=str(requirements_path),
        )
    still = missing_modules(modules, importer=importer)
    if still:
        return ProbeResult(
            ok=False,
            python=resolved_python,
            missing=still,
            installed=True,
            error=f"Still missing after pip install: {', '.join(still)}",
            requirements_path=str(requirements_path),
        )
    return ProbeResult(
        ok=True,
        python=resolved_python,
        missing=(),
        installed=True,
        requirements_path=str(requirements_path),
    )


def launch_gui(
    *,
    project_root: Path | None = None,
    gui_args: Sequence[str] | None = None,
    python: str | None = None,
    finder: PythonFinder | None = None,
    importer: Importer | None = None,
    pip_runner: PipRunner | None = None,
    spawn: Spawner | None = None,
) -> LaunchOutcome:
    """Ensure runtime, then start ``python -m zapret_gui``. Failures do not spawn the GUI."""
    root = Path(project_root) if project_root is not None else default_project_root()
    probe = ensure_runtime(
        project_root=root,
        python=python,
        finder=finder,
        importer=importer,
        pip_runner=pip_runner,
    )
    if not probe.ok:
        return LaunchOutcome(
            exit_code=1,
            started_gui=False,
            probe=probe,
            error=probe.error,
        )
    argv = ["-m", "zapret_gui", *list(gui_args or ())]
    spawner = spawn if spawn is not None else default_spawn
    try:
        code = int(spawner(probe.python or "python", argv, root))
    except Exception as exc:
        return LaunchOutcome(
            exit_code=1,
            started_gui=False,
            probe=probe,
            error=f"Failed to start python -m zapret_gui: {exc}",
            spawn_argv=tuple(argv),
        )
    return LaunchOutcome(
        exit_code=code,
        started_gui=True,
        probe=probe,
        spawn_argv=tuple(argv),
    )


def _print_probe(result: ProbeResult) -> None:
    stream = sys.stdout if result.ok else sys.stderr
    if result.ok and result.installed:
        print(f"Installed GUI requirements from {result.requirements_path}", file=stream)
    elif result.ok:
        print("GUI requirements OK", file=stream)
    elif result.error:
        print(result.error, file=stream)


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    root = default_project_root()
    if args[:1] in (["--ensure"], ["--ensure-only"]):
        result = ensure_runtime(project_root=root)
        _print_probe(result)
        return 0 if result.ok else 1
    outcome = launch_gui(project_root=root, gui_args=args)
    if outcome.error and not outcome.started_gui:
        print(outcome.error, file=sys.stderr)
    return int(outcome.exit_code)


if __name__ == "__main__":
    raise SystemExit(main())
