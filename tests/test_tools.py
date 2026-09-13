from __future__ import annotations

import os
import time
from pathlib import Path

import pytest

from zapret_gui.tools import (
    POWERSHELL_PROBE,
    POWERSHELL_REQUIRED,
    TESTS_STARTING,
    build_tests_launch,
    find_best_strategy_line,
    launch_tests,
    newest_result_since,
    parse_best_strategy,
    powershell_supported,
)

# The tail of utils\test results\test_results_2026-07-25_09-46-04.txt.
JULY_TAIL = (
    "=== ANALYTICS ===\r\n"
    "general (ALT10).bat              : OK:  96, FAIL:  10, UNSUP:   0, BLOCKED:   0\r\n"
    "general (ALT2).bat               : OK:   0, FAIL: 106, UNSUP:   0, BLOCKED:   0\r\n"
    "general (ALT12).bat              : OK:  96, FAIL:  10, UNSUP:   0, BLOCKED:   0\r\n"
    "Best strategy: general (ALT11).bat\r\n"
)


def test_tests_launch_matches_service_bat(project_root: Path) -> None:
    plan = build_tests_launch(project_root, elevated=True)
    script = project_root / "utils" / "test zapret.ps1"
    assert plan.file == "powershell.exe"
    assert plan.params == f'-NoProfile -ExecutionPolicy Bypass -File "{script}"'
    assert plan.verb == "open"
    assert plan.directory == str(project_root)
    assert plan.script == script
    assert build_tests_launch(project_root, elevated=False).verb == "runas"


def test_service_bat_strings_are_verbatim() -> None:
    assert TESTS_STARTING == "Starting configuration tests in PowerShell window..."
    assert POWERSHELL_REQUIRED == (
        "PowerShell 3.0 or newer is required.",
        "Please upgrade PowerShell and rerun this script.",
    )
    assert "PSVersion.Major -ge 3" in POWERSHELL_PROBE


def test_powershell_gate_uses_the_bat_probe() -> None:
    seen: list[tuple[str, ...]] = []
    assert powershell_supported(lambda argv: seen.append(tuple(argv)) or 0) is True
    assert seen == [("powershell.exe", "-NoProfile", "-Command", POWERSHELL_PROBE)]
    assert powershell_supported(lambda argv: 1) is False

    def missing(_argv):
        raise FileNotFoundError("powershell.exe")

    assert powershell_supported(missing) is False


def test_launch_tests_reports_every_outcome(project_root: Path, tmp_path: Path) -> None:
    plans = []
    ok = launch_tests(project_root, elevated=True, executor=lambda plan: plans.append(plan) or 42)
    assert ok.ok is True and ok.native_code == 42
    assert plans[0].verb == "open"

    refused = launch_tests(project_root, elevated=False, executor=lambda plan: 5)
    assert refused.ok is False
    assert "Failed to start PowerShell" in (refused.error or "")

    def boom(_plan):
        raise OSError("no shell")

    assert "no shell" in (launch_tests(project_root, elevated=True, executor=boom).error or "")

    missing = launch_tests(tmp_path, elevated=True, executor=lambda plan: 42)
    assert missing.ok is False
    assert "not found" in (missing.error or "")


def test_best_strategy_line_parsing() -> None:
    assert find_best_strategy_line("﻿" + JULY_TAIL) == "general (ALT11).bat"
    assert parse_best_strategy(JULY_TAIL) == "general (ALT11).bat"
    # A run where nothing passed still writes the line, with no name.
    assert find_best_strategy_line("Best strategy: \r\n") == ""
    assert parse_best_strategy("Best strategy: \r\n") is None
    # No line yet means the file is still being written.
    assert find_best_strategy_line("=== ANALYTICS ===\r\n") is None
    assert parse_best_strategy("") is None


def test_real_july_results_file_names_alt11(project_root: Path) -> None:
    path = project_root / "utils" / "test results" / "test_results_2026-07-25_09-46-04.txt"
    if not path.is_file():
        pytest.skip("the July results file is not present")
    assert parse_best_strategy(path.read_text(encoding="utf-8-sig", errors="replace")) == "general (ALT11).bat"


def test_newest_result_since_ignores_older_runs(tmp_path: Path) -> None:
    now = time.time()
    old = tmp_path / "test_results_2026-07-25_09-46-04.txt"
    old.write_text("Best strategy: a.bat", encoding="utf-8")
    os.utime(old, (now - 3600, now - 3600))
    fresh = tmp_path / "test_results_2026-09-13_10-42-07.txt"
    fresh.write_text("Best strategy: b.bat", encoding="utf-8")
    os.utime(fresh, (now, now))
    newer = tmp_path / "test_results_2026-09-13_10-50-00.txt"
    newer.write_text("Best strategy: c.bat", encoding="utf-8")
    os.utime(newer, (now + 5, now + 5))
    (tmp_path / "notes.txt").write_text("Best strategy: z.bat", encoding="utf-8")

    assert newest_result_since(tmp_path, now - 60) == newer
    assert newest_result_since(tmp_path, now + 60) is None
    assert newest_result_since(tmp_path / "missing", 0) is None
