from __future__ import annotations

from pathlib import Path

from zapret_gui import HOSTS_PATH
from zapret_gui.privileges import (
    HostsLaunchPlan,
    build_hosts_launch_plan,
    is_process_elevated,
    launch_hosts_notepad,
)


def test_privilege_probe_returns_bool() -> None:
    result = is_process_elevated()
    assert result is True or result is False


def test_hosts_plan_targets_notepad_and_hosts_with_elevation() -> None:
    plan = build_hosts_launch_plan(elevated=False)
    assert isinstance(plan, HostsLaunchPlan)
    assert "notepad" in plan.file.lower()
    assert plan.params == HOSTS_PATH
    assert plan.hosts_path == r"C:\Windows\System32\drivers\etc\hosts"
    assert plan.verb.lower() == "runas"
    assert plan.uses_elevation is True


def test_hosts_plan_open_when_already_elevated() -> None:
    plan = build_hosts_launch_plan(elevated=True)
    assert plan.verb.lower() == "open"
    assert "notepad" in plan.file.lower()
    assert plan.params == HOSTS_PATH


def test_unelevated_plan_never_uses_open() -> None:
    plan = build_hosts_launch_plan(elevated=False)
    assert plan.verb.lower() != "open"


def test_missing_notepad_is_reported_not_raised(tmp_path: Path) -> None:
    missing = tmp_path / "no-such-notepad.exe"
    result = launch_hosts_notepad(
        notepad_path=str(missing),
        hosts_path=HOSTS_PATH,
        elevated=False,
    )
    assert result.ok is False
    assert result.error is not None
    assert "notepad" in result.error.lower()
    assert result.plan.missing_notepad is True


def test_missing_hosts_is_reported_not_raised(tmp_path: Path) -> None:
    notepad = tmp_path / "notepad.exe"
    notepad.write_bytes(b"fake")
    missing_hosts = tmp_path / "no-hosts"
    result = launch_hosts_notepad(
        notepad_path=str(notepad),
        hosts_path=str(missing_hosts),
        elevated=False,
    )
    assert result.ok is False
    assert result.error is not None
    assert "hosts" in result.error.lower()
    assert result.plan.missing_hosts is True


def test_simulated_launch_failure_is_handled() -> None:
    def boom(_plan: HostsLaunchPlan) -> int:
        raise OSError("simulated launch failure")

    result = launch_hosts_notepad(executor=boom, elevated=False)
    assert result.ok is False
    assert result.error is not None
    assert "simulated launch failure" in result.error
    assert "notepad" in result.plan.file.lower()
    assert result.plan.params == HOSTS_PATH


def test_simulated_shellexecute_error_code_is_handled() -> None:
    def deny(_plan: HostsLaunchPlan) -> int:
        return 5  # SE_ERR_ACCESSDENIED

    result = launch_hosts_notepad(executor=deny, elevated=False)
    assert result.ok is False
    assert result.native_code == 5
    assert result.error is not None


def test_successful_injected_executor() -> None:
    seen: list[HostsLaunchPlan] = []

    def ok(plan: HostsLaunchPlan) -> int:
        seen.append(plan)
        return 42  # ShellExecute success is > 32

    result = launch_hosts_notepad(executor=ok, elevated=False)
    assert result.ok is True
    assert result.error is None
    assert seen and seen[0].verb.lower() == "runas"
    assert seen[0].params == HOSTS_PATH
