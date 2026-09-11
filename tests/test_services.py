from __future__ import annotations

from pathlib import Path

from zapret_gui import SERVICE_NAME
from zapret_gui.services import (
    CompletedScm,
    ScmCommand,
    build_install_command,
    build_start_command,
    build_status_command,
    build_stop_command,
    execute_scm,
    install_service,
    live_install_steps,
    map_sc_query,
    query_service_status,
    start_service,
    stop_service,
)
from zapret_gui.strategies import parse_strategy


def _parsed(project_root: Path):
    return parse_strategy(project_root / "general.bat", project_root)


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


def test_elevated_live_install_runs_stop_delete_create(project_root: Path, monkeypatch) -> None:
    """GUI path when already admin must not skip stop/delete (sc create → 1073)."""
    parsed = _parsed(project_root)
    expected_stop, expected_delete, expected_create = live_install_steps(
        parsed.image, parsed.args_line
    )
    seen: list[ScmCommand] = []

    def forbid_live(*args, **kwargs):
        raise AssertionError(f"refusing live sc.exe in test: {args!r}")

    def fake_run(cmd: ScmCommand) -> CompletedScm:
        seen.append(cmd)
        return CompletedScm(returncode=0, stdout="ok", stderr="")

    def forbid_shell(*args, **kwargs):
        raise AssertionError("elevated install must not ShellExecute; it runs sc.exe in-process")

    monkeypatch.setattr("zapret_gui.services.subprocess.run", forbid_live)
    monkeypatch.setattr("zapret_gui.services.shell_execute", forbid_shell)
    monkeypatch.setattr("zapret_gui.services._run_subprocess", fake_run)

    result = install_service(
        parsed.image,
        parsed.args_line,
        allow_live=True,
        privileged=True,
    )
    assert result.ok is True
    assert result.executed is True
    assert [c.action for c in seen] == ["stop", "delete", "install"]
    assert list(seen[0].argv) == list(expected_stop.argv)
    assert list(seen[1].argv) == list(expected_delete.argv)
    assert list(seen[2].argv) == list(expected_create.argv)
    assert "stop" in seen[0].argv
    assert "delete" in seen[1].argv
    assert "create" in seen[2].argv
    blob = " ".join(seen[2].argv)
    assert parsed.image in blob
    assert parsed.args_line in blob
    assert expected_create.image == parsed.image


def test_elevated_install_tolerates_stop_delete_missing(project_root: Path, monkeypatch) -> None:
    parsed = _parsed(project_root)
    seen: list[int] = []

    def fake_run(cmd: ScmCommand) -> CompletedScm:
        if cmd.action == "stop":
            seen.append(1062)
            return CompletedScm(returncode=1062, stdout="", stderr="The service has not been started.")
        if cmd.action == "delete":
            seen.append(1060)
            return CompletedScm(
                returncode=1060,
                stdout="",
                stderr="The specified service does not exist as an installed service.",
            )
        seen.append(0)
        return CompletedScm(returncode=0, stdout="[SC] CreateService SUCCESS", stderr="")

    monkeypatch.setattr(
        "zapret_gui.services.subprocess.run",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("live sc.exe")),
    )
    monkeypatch.setattr("zapret_gui.services._run_subprocess", fake_run)
    result = install_service(
        parsed.image,
        parsed.args_line,
        allow_live=True,
        privileged=True,
    )
    assert result.ok is True
    assert seen == [1062, 1060, 0]


def test_execute_scm_runner_exception_is_handled(project_root: Path) -> None:
    parsed = _parsed(project_root)
    cmd = build_install_command(parsed.image, parsed.args_line)

    def boom(_cmd: ScmCommand) -> CompletedScm:
        raise RuntimeError("simulated scm failure")

    result = execute_scm(cmd, runner=boom)
    assert result.ok is False
    assert "simulated scm failure" in (result.error or "")
