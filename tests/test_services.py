from __future__ import annotations

import sys
from pathlib import Path

import pytest

from zapret_gui import SERVICE_NAME
from zapret_gui.services import (
    ERROR_SERVICE_ALREADY_RUNNING,
    ERROR_SERVICE_NEVER_STARTED,
    ERROR_SERVICE_REQUEST_TIMEOUT,
    CompletedScm,
    ScmCommand,
    ServiceSnapshot,
    ServiceStatus,
    ServiceWait,
    build_install_command,
    build_queryex_command,
    build_start_command,
    build_status_command,
    build_stop_command,
    describe_sc_error,
    execute_scm,
    exit_code_hint,
    extract_pid,
    image_from_image_path,
    image_install_root,
    image_is_under,
    install_plan,
    install_service,
    live_install_steps,
    map_sc_query,
    query_service_snapshot,
    query_service_status,
    run_install_sequence,
    start_service,
    stop_service,
    tcp_timestamps_enabled,
    wait_for_state,
)
from zapret_gui.strategies import parse_strategy

TCP_PROBE_ARGV = ("netsh.exe", "interface", "tcp", "show", "global")
SEQUENCE_KINDS = ["tcp", "stop", "delete", "create", "describe", "start", "verify"]


def _parsed(project_root: Path):
    return parse_strategy(project_root / "general.bat", project_root)


def _waiter(
    *,
    state: str = "running",
    pid: int | None = None,
    reached: bool = True,
    raw: str = "",
    settle: bool = True,
):
    """Stand in for wait_for_state so no test ever polls a real service.

    The install/remove settle waits (targets without "running") succeed unless
    ``settle=False``; the configured outcome applies to everything else.
    """

    def waiter(service_name: str = SERVICE_NAME, *, target=("running",), **_kwargs) -> ServiceWait:
        target = tuple(target)
        if settle and "running" not in target:
            gone = "not_installed" if target == ("not_installed",) else "stopped"
            return ServiceWait(
                status=ServiceStatus(state=gone, service_name=service_name, message=gone),  # type: ignore[arg-type]
                reached=True,
            )
        return ServiceWait(
            status=ServiceStatus(
                state=state,  # type: ignore[arg-type]
                service_name=service_name,
                message=f'Service "{service_name}" is {state}',
                raw=raw,
            ),
            reached=reached,
            pid=pid,
        )

    return waiter


def _recording_waiter(events: list[str], *, gone: bool = True, stopped: bool = True, pid: int | None = None):
    """A waiter that logs each wait into the same timeline as the commands."""

    def waiter(service_name: str = SERVICE_NAME, *, target=("running",), **_kwargs) -> ServiceWait:
        target = tuple(target)
        events.append("wait:" + "|".join(target))
        if "running" in target:
            state, reached = "running", True
        elif target == ("not_installed",):
            state, reached = ("not_installed" if gone else "stopped"), gone
        else:
            state, reached = "stopped", stopped
        return ServiceWait(
            status=ServiceStatus(state=state, service_name=service_name, message=state),  # type: ignore[arg-type]
            reached=reached,
            pid=pid,
        )

    return waiter


def _sequence_runner(seen: list[ScmCommand], *, timestamps: str = "disabled", codes: dict | None = None):
    """Fake in-process sc.exe/netsh for the live install sequence."""
    table = codes or {}

    def run(cmd: ScmCommand) -> CompletedScm:
        seen.append(cmd)
        if cmd.argv[:5] == TCP_PROBE_ARGV:
            return CompletedScm(returncode=0, stdout=f"RFC 1323 Timestamps : {timestamps}\n")
        key = "create" if cmd.action == "install" else cmd.action
        code = int(table.get(key, 0))
        return CompletedScm(returncode=code, stdout="ok" if code == 0 else "", stderr="" if code == 0 else "failed")

    return run


def test_install_command_uses_winws_image_and_resolved_args(project_root: Path) -> None:
    parsed = _parsed(project_root)
    cmd = build_install_command(parsed.image, parsed.args_line, SERVICE_NAME)
    assert cmd.action == "install"
    assert cmd.service_name == "zapret"
    assert cmd.image == parsed.image
    assert cmd.args == parsed.args_line
    blob = " ".join(cmd.argv) + " " + cmd.command_line
    assert parsed.image in blob
    assert "winws.exe" in blob.lower()
    assert "sc" in cmd.argv[0].lower()
    assert "create" in cmd.argv
    assert parsed.args_line in blob
    assert str((project_root / "lists").resolve()).lower() in blob.lower()
    assert str((project_root / "bin").resolve()).lower() in blob.lower()
    assert "%BIN%" not in blob
    assert "%LISTS%" not in blob
    # service.bat's literal for the default service name...
    assert 'DisplayName= "zapret"' in cmd.command_line


def test_install_display_name_follows_the_service_name(project_root: Path) -> None:
    """A hardcoded DisplayName collides with the real service (1078, name in use)."""
    parsed = _parsed(project_root)
    cmd = build_install_command(parsed.image, parsed.args_line, "zapret-scratch")
    assert 'DisplayName= "zapret-scratch"' in cmd.command_line
    assert "zapret-scratch" in cmd.argv
    assert cmd.argv[cmd.argv.index("DisplayName=") + 1] == "zapret-scratch"


def test_start_stop_status_builders() -> None:
    start = build_start_command(SERVICE_NAME, image="X", args="Y")
    stop = build_stop_command(SERVICE_NAME, image="X", args="Y")
    status = build_status_command(SERVICE_NAME)
    assert start.action == "start" and "start" in start.argv and SERVICE_NAME in start.argv
    assert stop.action == "stop" and "stop" in stop.argv and SERVICE_NAME in stop.argv
    assert status.action == "status" and "query" in status.argv and SERVICE_NAME in status.argv
    assert start.image == "X" and start.args == "Y"
    assert stop.image == "X"


def test_map_sc_query_states() -> None:
    running = map_sc_query(
        0,
        "SERVICE_NAME: zapret\n        STATE              : 4  RUNNING\n",
        "",
    )
    assert running.state == "running"

    stopped = map_sc_query(
        0,
        "SERVICE_NAME: zapret\n        STATE              : 1  STOPPED\n",
        "",
    )
    assert stopped.state == "stopped"

    pending = map_sc_query(
        0,
        "STATE              : 3  STOP_PENDING\n",
        "",
    )
    assert pending.state == "stopped"

    missing = map_sc_query(
        1060,
        "",
        "[SC] EnumQueryServicesStatus:OpenService FAILED 1060:\nThe specified service does not exist as an installed service.\n",
    )
    assert missing.state == "not_installed"

    missing_text = map_sc_query(2, "The specified service does not exist", "")
    assert missing_text.state == "not_installed"

    err = map_sc_query(1, "", "boom from scm")
    assert err.state == "error"
    assert "boom" in err.message.lower() or "1" in err.message


def test_install_start_stop_use_injected_runner_not_live_sc(project_root: Path) -> None:
    parsed = _parsed(project_root)
    seen: list[ScmCommand] = []

    def runner(cmd: ScmCommand) -> CompletedScm:
        seen.append(cmd)
        # Refuse to even look like a live mutation.
        assert cmd.action != "status"
        assert "create" in cmd.argv or "start" in cmd.argv or "stop" in cmd.argv
        return CompletedScm(returncode=0, stdout="ok", stderr="")

    installed = install_service(parsed.image, parsed.args_line, runner=runner, allow_live=False)
    started = start_service(SERVICE_NAME, image=parsed.image, args=parsed.args_line, runner=runner)
    stopped = stop_service(SERVICE_NAME, image=parsed.image, args=parsed.args_line, runner=runner)

    assert installed.ok and started.ok and stopped.ok
    assert [c.action for c in seen] == ["install", "start", "stop"]
    assert parsed.image in seen[0].image
    assert parsed.args_line in seen[0].args
    assert seen[0].uses_image_args()


def test_live_mutation_refused_without_allow_live(project_root: Path) -> None:
    parsed = _parsed(project_root)
    result = install_service(parsed.image, parsed.args_line, allow_live=False)
    assert result.ok is False
    assert result.executed is False
    assert result.command.action == "install"
    assert parsed.image in result.command.image
    assert "allow_live" in (result.error or "").lower() or "refusing" in (result.error or "").lower()


def test_query_status_uses_mapper_via_runner() -> None:
    def runner(cmd: ScmCommand) -> CompletedScm:
        assert cmd.action == "status"
        assert "query" in cmd.argv
        return CompletedScm(
            returncode=1060,
            stdout="",
            stderr="The specified service does not exist as an installed service.",
        )

    status = query_service_status(SERVICE_NAME, runner=runner)
    assert status.state == "not_installed"


def test_live_install_chains_stop_delete_create(project_root: Path, monkeypatch) -> None:
    parsed = _parsed(project_root)
    seen: list[tuple[str, str, str]] = []

    monkeypatch.setattr("zapret_gui.services.is_process_elevated", lambda: False)

    def fake_shell(file: str, params: str, verb: str, directory: str | None = None) -> int:
        seen.append((file, params, verb))
        return 42

    monkeypatch.setattr("zapret_gui.services.shell_execute", fake_shell)
    result = install_service(parsed.image, parsed.args_line, allow_live=True)
    assert result.ok is True
    assert result.executed is True
    assert seen, "live install must request an elevated process"
    file, params, verb = seen[0]
    assert file.lower().endswith("cmd.exe") or file.lower() == "cmd.exe"
    assert verb.lower() == "runas"
    lower = params.lower()
    assert "stop" in lower and "zapret" in lower
    assert "delete" in lower
    assert "create" in lower
    assert parsed.image.lower() in params.lower()
    assert parsed.args_line in params or "winws.exe" in params.lower()
    # Must not have invoked a real sc.exe from this test process.
    assert result.command.image == parsed.image


def test_elevated_live_install_runs_service_bat_sequence(project_root: Path, monkeypatch) -> None:
    """The elevated path must run every service.bat :service_install step.

    ``sc create`` alone against a running service is 1073, so stop/delete come
    first; and ``sc create ... start= auto`` only sets the BOOT start type, so
    without the explicit ``sc start`` the service sits STOPPED with
    WIN32_EXIT_CODE 1077 — the bug this sequence fixes.
    """
    parsed = _parsed(project_root)
    expected_stop, expected_delete, expected_create = live_install_steps(
        parsed.image, parsed.args_line
    )
    seen: list[ScmCommand] = []

    def forbid_live(*args, **kwargs):
        raise AssertionError(f"refusing live sc.exe in test: {args!r}")

    def forbid_shell(*args, **kwargs):
        raise AssertionError("elevated install must not ShellExecute; it runs sc.exe in-process")

    monkeypatch.setattr("zapret_gui.services.subprocess.run", forbid_live)
    monkeypatch.setattr("zapret_gui.services.shell_execute", forbid_shell)
    monkeypatch.setattr("zapret_gui.services._run_subprocess", _sequence_runner(seen))

    result = install_service(
        parsed.image,
        parsed.args_line,
        allow_live=True,
        privileged=True,
        waiter=_waiter(pid=24188),
    )
    assert result.ok is True
    assert result.executed is True
    assert result.verified is True
    assert result.pid == 24188
    assert [report.kind for report in result.steps] == SEQUENCE_KINDS

    # netsh probe, netsh set, then the four sc commands in service.bat order.
    actions = [c.action for c in seen]
    assert actions == ["tcp", "tcp", "stop", "delete", "install", "describe", "start"]
    assert list(seen[2].argv) == list(expected_stop.argv)
    assert list(seen[3].argv) == list(expected_delete.argv)
    assert list(seen[4].argv) == list(expected_create.argv)
    assert list(seen[6].argv) == ["sc.exe", "start", SERVICE_NAME]
    blob = " ".join(seen[4].argv)
    assert parsed.image in blob
    assert parsed.args_line in blob
    assert expected_create.image == parsed.image


def test_install_issues_sc_start_after_sc_create(project_root: Path, monkeypatch) -> None:
    """Regression guard for the reported defect: create without start leaves 1077."""
    parsed = _parsed(project_root)
    seen: list[ScmCommand] = []
    monkeypatch.setattr("zapret_gui.services._run_subprocess", _sequence_runner(seen))

    result = install_service(
        parsed.image,
        parsed.args_line,
        allow_live=True,
        privileged=True,
        waiter=_waiter(),
    )
    kinds = [report.kind for report in result.steps]
    assert "start" in kinds, "install must issue sc start"
    assert kinds.index("start") > kinds.index("create")
    assert result.ok is True


def test_install_waits_for_scm_to_settle_like_net_stop(project_root: Path, monkeypatch) -> None:
    """Replacing a running service: STOPPED before delete, gone before create.

    `sc stop` returns at STOP_PENDING and `sc delete` on a stopping service only
    marks it, so an immediate `sc create` fails with 1072. Proven live on a
    scratch service before this wait existed.
    """
    parsed = _parsed(project_root)
    events: list[str] = []

    def run(cmd: ScmCommand) -> CompletedScm:
        events.append(cmd.action)
        if cmd.argv[:5] == TCP_PROBE_ARGV:
            return CompletedScm(returncode=0, stdout="RFC 1323 Timestamps : enabled\n")
        return CompletedScm(returncode=0, stdout="ok")

    monkeypatch.setattr("zapret_gui.services._run_subprocess", run)
    result = install_service(
        parsed.image,
        parsed.args_line,
        allow_live=True,
        privileged=True,
        waiter=_recording_waiter(events, pid=7),
    )
    assert result.ok is True
    assert events == [
        "tcp",
        "stop",
        "wait:stopped|not_installed",
        "delete",
        "wait:not_installed",
        "install",
        "describe",
        "start",
        "wait:running",
    ]
    notes = {report.kind: report.note for report in result.steps}
    assert notes["stop"] == "stopped"
    assert notes["delete"] == "removed"


def test_install_skips_settle_waits_when_nothing_was_running(project_root: Path, monkeypatch) -> None:
    parsed = _parsed(project_root)
    events: list[str] = []
    seen: list[ScmCommand] = []
    runner = _sequence_runner(seen, timestamps="enabled", codes={"stop": 1062, "delete": 1060})

    def run(cmd: ScmCommand) -> CompletedScm:
        events.append(cmd.action)
        return runner(cmd)

    monkeypatch.setattr("zapret_gui.services._run_subprocess", run)
    result = install_service(
        parsed.image,
        parsed.args_line,
        allow_live=True,
        privileged=True,
        waiter=_recording_waiter(events),
    )
    assert result.ok is True
    assert events == ["tcp", "stop", "delete", "install", "describe", "start", "wait:running"]


def test_install_fails_clearly_when_the_deleted_service_never_goes(project_root: Path, monkeypatch) -> None:
    parsed = _parsed(project_root)
    events: list[str] = []

    def run(cmd: ScmCommand) -> CompletedScm:
        events.append(cmd.action)
        if cmd.argv[:5] == TCP_PROBE_ARGV:
            return CompletedScm(returncode=0, stdout="RFC 1323 Timestamps : enabled\n")
        return CompletedScm(returncode=0, stdout="ok")

    monkeypatch.setattr("zapret_gui.services._run_subprocess", run)
    result = install_service(
        parsed.image,
        parsed.args_line,
        allow_live=True,
        privileged=True,
        waiter=_recording_waiter(events, gone=False),
    )
    assert result.ok is False
    assert "1072" in (result.error or "")
    assert "marked for deletion" in (result.error or "")
    assert "services.msc" in (result.error or "")
    assert "install" not in events, "sc create must not be issued while the entry still exists"
    assert [report.kind for report in result.steps][-1] == "delete"


def test_install_carries_on_when_stop_is_slow_and_delete_settles(project_root: Path, monkeypatch) -> None:
    """A slow `net stop` in the bat is swallowed; the delete wait is what decides."""
    parsed = _parsed(project_root)
    events: list[str] = []

    def run(cmd: ScmCommand) -> CompletedScm:
        events.append(cmd.action)
        if cmd.argv[:5] == TCP_PROBE_ARGV:
            return CompletedScm(returncode=0, stdout="RFC 1323 Timestamps : enabled\n")
        return CompletedScm(returncode=0, stdout="ok")

    monkeypatch.setattr("zapret_gui.services._run_subprocess", run)
    result = install_service(
        parsed.image,
        parsed.args_line,
        allow_live=True,
        privileged=True,
        waiter=_recording_waiter(events, stopped=False),
    )
    assert result.ok is True
    stop = next(report for report in result.steps if report.kind == "stop")
    assert stop.ok is True
    assert "after 20 s" in stop.note


def test_elevated_install_tolerates_stop_delete_missing(project_root: Path, monkeypatch) -> None:
    parsed = _parsed(project_root)
    seen: list[ScmCommand] = []
    runner = _sequence_runner(seen, codes={"stop": 1062, "delete": 1060})

    monkeypatch.setattr(
        "zapret_gui.services.subprocess.run",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("live sc.exe")),
    )
    monkeypatch.setattr("zapret_gui.services._run_subprocess", runner)
    result = install_service(
        parsed.image,
        parsed.args_line,
        allow_live=True,
        privileged=True,
        waiter=_waiter(),
    )
    assert result.ok is True
    codes = {report.kind: report.returncode for report in result.steps}
    assert codes["stop"] == 1062
    assert codes["delete"] == 1060
    assert codes["create"] == 0
    assert codes["start"] == 0


def test_install_plan_matches_service_bat_order(project_root: Path) -> None:
    parsed = _parsed(project_root)
    plan = install_plan(parsed.image, parsed.args_line)
    assert [step.kind for step in plan] == [
        "tcp",
        "stop",
        "delete",
        "create",
        "describe",
        "start",
    ]
    # service.bat discards netsh/description errors; the sc steps are load-bearing.
    fatal = {step.kind: step.fatal for step in plan}
    assert fatal["tcp"] is False and fatal["describe"] is False
    assert fatal["stop"] and fatal["delete"] and fatal["create"] and fatal["start"]
    start = next(step for step in plan if step.kind == "start")
    assert ERROR_SERVICE_ALREADY_RUNNING in start.ok_codes


def test_install_reports_failed_start_instead_of_success(project_root: Path, monkeypatch) -> None:
    parsed = _parsed(project_root)
    seen: list[ScmCommand] = []
    runner = _sequence_runner(seen, codes={"start": ERROR_SERVICE_REQUEST_TIMEOUT})
    monkeypatch.setattr("zapret_gui.services._run_subprocess", runner)

    result = install_service(
        parsed.image,
        parsed.args_line,
        allow_live=True,
        privileged=True,
        waiter=_waiter(),
    )
    assert result.ok is False
    assert "1053" in (result.error or "")
    assert "did not respond" in (result.error or "")
    # The failure aborts before the verify poll.
    assert [report.kind for report in result.steps][-1] == "start"


def test_install_tolerates_already_running_service(project_root: Path, monkeypatch) -> None:
    parsed = _parsed(project_root)
    seen: list[ScmCommand] = []
    runner = _sequence_runner(seen, codes={"start": ERROR_SERVICE_ALREADY_RUNNING})
    monkeypatch.setattr("zapret_gui.services._run_subprocess", runner)

    result = install_service(
        parsed.image,
        parsed.args_line,
        allow_live=True,
        privileged=True,
        waiter=_waiter(pid=4242),
    )
    assert result.ok is True
    assert result.pid == 4242


def test_install_fails_when_service_never_reaches_running(project_root: Path, monkeypatch) -> None:
    """sc start returns 0 at START_PENDING; only a query proves winws survived."""
    parsed = _parsed(project_root)
    seen: list[ScmCommand] = []
    monkeypatch.setattr("zapret_gui.services._run_subprocess", _sequence_runner(seen))

    raw = "SERVICE_NAME: zapret\n  STATE : 1  STOPPED\n  WIN32_EXIT_CODE : 1077  (0x435)\n"
    result = install_service(
        parsed.image,
        parsed.args_line,
        allow_live=True,
        privileged=True,
        waiter=_waiter(state="stopped", reached=False, raw=raw),
    )
    assert result.ok is False
    assert result.verified is True
    assert "1077" in (result.error or "")
    assert [report.kind for report in result.steps] == SEQUENCE_KINDS


def test_install_skips_netsh_when_timestamps_already_enabled(project_root: Path, monkeypatch) -> None:
    parsed = _parsed(project_root)
    seen: list[ScmCommand] = []
    monkeypatch.setattr(
        "zapret_gui.services._run_subprocess",
        _sequence_runner(seen, timestamps="enabled"),
    )
    result = install_service(
        parsed.image,
        parsed.args_line,
        allow_live=True,
        privileged=True,
        waiter=_waiter(),
    )
    assert result.ok is True
    # Probe only; the `set global` write is not issued.
    assert [c.action for c in seen].count("tcp") == 1
    tcp = next(report for report in result.steps if report.kind == "tcp")
    assert "already enabled" in tcp.note


def test_unelevated_install_is_not_reported_as_verified(project_root: Path, monkeypatch) -> None:
    """ShellExecute > 32 only means cmd.exe launched, never that sc succeeded."""
    parsed = _parsed(project_root)
    seen: list[tuple[str, str, str]] = []

    def fake_shell(file: str, params: str, verb: str, directory: str | None = None) -> int:
        seen.append((file, params, verb))
        return 42

    monkeypatch.setattr("zapret_gui.services.shell_execute", fake_shell)
    result = run_install_sequence(
        parsed.image,
        parsed.args_line,
        privileged=False,
    )
    assert result.ok is True
    assert result.verified is False
    assert result.needs_elevation is True
    # The chained UAC line must carry the start leg too.
    params = seen[0][1].lower()
    assert "sc.exe start zapret" in params
    assert "sc.exe create zapret" in params
    assert "description" in params


def test_wait_for_state_polls_until_running() -> None:
    states = [
        "SERVICE_NAME: zapret\n  STATE : 2  START_PENDING\n  PID : 0\n",
        "SERVICE_NAME: zapret\n  STATE : 2  START_PENDING\n  PID : 0\n",
        "SERVICE_NAME: zapret\n  STATE : 4  RUNNING\n  PID : 24188\n",
    ]
    calls: list[ScmCommand] = []
    ticks = iter([0.0, 1.0, 2.0, 3.0, 4.0, 5.0])
    slept: list[float] = []

    def runner(cmd: ScmCommand) -> CompletedScm:
        calls.append(cmd)
        return CompletedScm(returncode=0, stdout=states[min(len(calls) - 1, len(states) - 1)])

    wait = wait_for_state(
        SERVICE_NAME,
        runner=runner,
        clock=lambda: next(ticks),
        sleeper=slept.append,
    )
    assert wait.reached is True
    assert wait.status.state == "running"
    assert wait.pid == 24188
    assert wait.polls == 3
    assert len(slept) == 2
    assert list(calls[0].argv) == list(build_queryex_command(SERVICE_NAME).argv)


def test_wait_for_state_treats_stop_pending_as_unsettled() -> None:
    states = [
        "STATE : 3  STOP_PENDING\n",
        "STATE : 1  STOPPED\n",
    ]
    calls: list[ScmCommand] = []
    ticks = iter([0.0, 1.0, 2.0, 3.0])

    def runner(cmd: ScmCommand) -> CompletedScm:
        calls.append(cmd)
        return CompletedScm(returncode=0, stdout=states[min(len(calls) - 1, len(states) - 1)])

    wait = wait_for_state(
        SERVICE_NAME,
        target=("stopped", "not_installed"),
        runner=runner,
        clock=lambda: next(ticks),
        sleeper=lambda _s: None,
    )
    assert wait.reached is True
    assert wait.token == "STOPPED"
    assert wait.polls == 2


def test_wait_for_state_gives_up_at_the_deadline() -> None:
    stopped = "SERVICE_NAME: zapret\n  STATE : 1  STOPPED\n  WIN32_EXIT_CODE : 1077  (0x435)\n  PID : 0\n"
    ticks = iter([0.0, 99.0, 99.0])

    wait = wait_for_state(
        SERVICE_NAME,
        runner=lambda _cmd: CompletedScm(returncode=0, stdout=stopped),
        clock=lambda: next(ticks),
        sleeper=lambda _s: None,
    )
    assert wait.reached is False
    assert wait.pid is None
    assert "1077" in exit_code_hint(wait.status.raw)
    assert "never been started" in exit_code_hint(wait.status.raw)


def test_wait_for_state_does_not_poll_a_missing_service() -> None:
    calls: list[ScmCommand] = []

    def runner(cmd: ScmCommand) -> CompletedScm:
        calls.append(cmd)
        return CompletedScm(returncode=1060, stderr="The specified service does not exist")

    wait = wait_for_state(SERVICE_NAME, runner=runner, sleeper=lambda _s: None)
    assert wait.reached is False
    assert wait.status.state == "not_installed"
    assert len(calls) == 1


def test_describe_sc_error_covers_the_codes_the_gui_shows() -> None:
    assert describe_sc_error(0) == "success"
    for code in (2, 5, 1053, 1056, 1058, 1060, 1062, 1072, 1073, 1077, 1078):
        text = describe_sc_error(code)
        assert text and not text.startswith("exit code")
    assert "never been started" in describe_sc_error(ERROR_SERVICE_NEVER_STARTED)
    # 1072 is a wait-it-out condition, not a reboot.
    assert "reboot" not in describe_sc_error(1072)
    assert describe_sc_error(31337) == "exit code 31337"


def test_pid_and_timestamp_parsers() -> None:
    assert extract_pid("  PID                : 24188\n") == 24188
    assert extract_pid("  PID                : 0\n") is None
    assert extract_pid("") is None
    assert tcp_timestamps_enabled("RFC 1323 Timestamps                 : enabled") is True
    assert tcp_timestamps_enabled("RFC 1323 Timestamps                 : disabled") is False
    assert tcp_timestamps_enabled("") is False


def test_start_and_stop_verify_the_resulting_state(monkeypatch) -> None:
    monkeypatch.setattr(
        "zapret_gui.services._run_subprocess",
        lambda _cmd: CompletedScm(returncode=0, stdout="ok"),
    )
    started = start_service(
        SERVICE_NAME,
        allow_live=True,
        privileged=True,
        waiter=_waiter(pid=777),
    )
    assert started.ok is True and started.verified is True and started.pid == 777
    assert [report.kind for report in started.steps] == ["start", "verify"]

    raw = "STATE : 4  RUNNING\n"
    stuck = stop_service(
        SERVICE_NAME,
        allow_live=True,
        privileged=True,
        waiter=_waiter(state="running", reached=False, raw=raw, settle=False),
    )
    assert stuck.ok is False
    assert "STOPPED" in (stuck.error or "")


def test_execute_scm_runner_exception_is_handled(project_root: Path) -> None:
    parsed = _parsed(project_root)
    cmd = build_install_command(parsed.image, parsed.args_line)

    def boom(_cmd: ScmCommand) -> CompletedScm:
        raise RuntimeError("simulated scm failure")

    result = execute_scm(cmd, runner=boom)
    assert result.ok is False
    assert "simulated scm failure" in (result.error or "")


def test_image_path_helpers() -> None:
    assert image_from_image_path('"E:\\.ZAPRET\\bin\\winws.exe"   --wf-tcp 80') == r"E:\.ZAPRET\bin\winws.exe"
    assert image_from_image_path(r"C:\Tools\winws.exe --wf-tcp=80") == r"C:\Tools\winws.exe"
    assert image_from_image_path("") == ""
    assert image_install_root(r"E:\.ZAPRET\bin\winws.exe") == r"E:\.ZAPRET"
    assert image_install_root(r"C:\Tools\winws.exe") == r"C:\Tools"
    root = Path(r"S:\NEYRONKI\Zapret-GUI-project")
    assert image_is_under(r"S:\NEYRONKI\Zapret-GUI-project\bin\winws.exe", root)
    assert image_is_under(r"s:\neyronki\zapret-gui-project\BIN\winws.exe", root)
    assert not image_is_under(r"E:\.ZAPRET\bin\winws.exe", root)
    # Whole path components only: a sibling tree with a longer name is not inside.
    assert not image_is_under(r"S:\NEYRONKI\Zapret-GUI-project-old\bin\winws.exe", root)


def test_snapshot_state_folds_scm_tokens() -> None:
    for token, state in (
        ("RUNNING", "running"),
        ("START_PENDING", "pending"),
        ("CONTINUE_PENDING", "pending"),
        ("STOP_PENDING", "pending"),
        ("PAUSE_PENDING", "pending"),
        ("STOPPED", "stopped"),
        ("PAUSED", "stopped"),
        ("NOT_INSTALLED", "not_installed"),
        ("ERROR", "error"),
    ):
        assert ServiceSnapshot(SERVICE_NAME, token).state == state


@pytest.mark.skipif(sys.platform != "win32", reason="the SCM exists only on Windows")
def test_query_service_snapshot_reports_a_missing_service() -> None:
    """Read-only: opens the SCM with connect rights and asks about a name nobody uses."""
    snapshot = query_service_snapshot("ZapretGuiNoSuchService")
    assert snapshot.token == "NOT_INSTALLED"
    assert snapshot.pid is None
    assert snapshot.image == ""
    assert snapshot.error == ""
