"""``service.bat`` menu option 12 (Run Tests): the PowerShell launch and its results.

The tests script is interactive and runs in its own console window, exactly as
``start "" powershell ...`` opens it. It reports ``Best config`` and writes a
results file ending in ``Best strategy: <file>.bat``; it installs nothing.
"""

from __future__ import annotations

import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

from zapret_gui.privileges import describe_shellexecute_failure, shell_execute

TESTS_SCRIPT = Path("utils") / "test zapret.ps1"
RESULTS_DIR = Path("utils") / "test results"
RESULTS_GLOB = "test_results_*.txt"

# service.bat:1120
POWERSHELL_PROBE = (
    "if ($PSVersionTable -and $PSVersionTable.PSVersion -and "
    "$PSVersionTable.PSVersion.Major -ge 3) { exit 0 } else { exit 1 }"
)
# service.bat:1122-1123
POWERSHELL_REQUIRED = (
    "PowerShell 3.0 or newer is required.",
    "Please upgrade PowerShell and rerun this script.",
)
# service.bat:1129
TESTS_STARTING = "Starting configuration tests in PowerShell window..."

_CREATE_NO_WINDOW = 0x08000000
_BEST_LINE = re.compile(r"^[ \t]*Best strategy:[ \t]*(.*?)[ \t]*\r?$", re.MULTILINE)


@dataclass(frozen=True)
class ToolLaunch:
    file: str
    params: str
    verb: str
    directory: str
    script: Path


@dataclass(frozen=True)
class ToolLaunchResult:
    ok: bool
    plan: ToolLaunch
    error: str | None = None
    native_code: int | None = None


def powershell_supported(run: Callable[[Sequence[str]], int] | None = None) -> bool:
    """service.bat's PowerShell 3.0+ gate."""
    argv = ("powershell.exe", "-NoProfile", "-Command", POWERSHELL_PROBE)
    try:
        return int((run or _run_quiet)(argv)) == 0
    except OSError:
        return False


def build_tests_launch(root: Path, *, elevated: bool) -> ToolLaunch:
    root = Path(root)
    script = root / TESTS_SCRIPT
    return ToolLaunch(
        file="powershell.exe",
        params=f'-NoProfile -ExecutionPolicy Bypass -File "{script}"',
        # An elevated GUI hands its token down like the admin-launched bat;
        # otherwise the script needs its own UAC prompt to drive winws.
        verb="open" if elevated else "runas",
        directory=str(root),
        script=script,
    )


def launch_tests(
    root: Path,
    *,
    elevated: bool,
    executor: Callable[[ToolLaunch], int] | None = None,
) -> ToolLaunchResult:
    """Open the tests in a new PowerShell window. Failures are returned, not raised."""
    plan = build_tests_launch(root, elevated=elevated)
    if not plan.script.is_file():
        return ToolLaunchResult(ok=False, plan=plan, error=f"Tests script not found: {plan.script}")
    run = executor or _shell_launch
    try:
        native = int(run(plan))
    except Exception as exc:
        return ToolLaunchResult(ok=False, plan=plan, error=f"Failed to start PowerShell: {exc}")
    if native <= 32:
        return ToolLaunchResult(
            ok=False,
            plan=plan,
            error=f"Failed to start PowerShell: {describe_shellexecute_failure(native)}",
            native_code=native,
        )
    return ToolLaunchResult(ok=True, plan=plan, native_code=native)


def find_best_strategy_line(text: str) -> str | None:
    """Winner on the last ``Best strategy:`` line: ``""`` when blank, ``None`` when absent."""
    matches = _BEST_LINE.findall((text or "").lstrip("﻿"))
    return matches[-1] if matches else None


def parse_best_strategy(text: str) -> str | None:
    return find_best_strategy_line(text) or None


def newest_result_since(results_dir: Path, since: float) -> Path | None:
    """The most recent results file written at or after ``since`` (epoch seconds)."""
    try:
        candidates = [
            (path.stat().st_mtime, path)
            for path in Path(results_dir).glob(RESULTS_GLOB)
            if path.is_file()
        ]
    except OSError:
        return None
    fresh = [item for item in candidates if item[0] >= since]
    if not fresh:
        return None
    return max(fresh, key=lambda item: item[0])[1]


def _shell_launch(plan: ToolLaunch) -> int:
    return shell_execute(plan.file, plan.params, plan.verb, plan.directory)


def _run_quiet(argv: Sequence[str]) -> int:
    kwargs: dict = {"capture_output": True, "check": False}
    if sys.platform == "win32":
        kwargs["creationflags"] = _CREATE_NO_WINDOW
    return int(subprocess.run(list(argv), **kwargs).returncode)
