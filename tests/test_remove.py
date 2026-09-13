from __future__ import annotations

from zapret_gui.services import (
    CompletedScm,
    RemoveStep,
    ServiceStatus,
    ServiceWait,
    plan_remove_steps,
    remove_service,
)


def _waiter(events: list | None = None, *, zapret_gone: bool = True):
    """Fake wait_for_state: stops settle, the final 'gone' check is configurable."""

    def waiter(service_name: str, *, target=("stopped",), **_kwargs) -> ServiceWait:
        target = tuple(target)
        if events is not None:
            events.append(("wait", service_name, target))
        if target == ("not_installed",):
            reached = zapret_gone
            state = "not_installed" if reached else "stopped"
        else:
            reached, state = True, "stopped"
        return ServiceWait(
            status=ServiceStatus(state=state, service_name=service_name, message=state),  # type: ignore[arg-type]
            reached=reached,
            token="" if reached else "STOPPED",
        )

    return waiter


def _runner(events: list, codes: dict | None = None):
    table = codes or {}

    def run(step: RemoveStep) -> CompletedScm:
        events.append((step.kind, step.target))
        code = int(table.get((step.kind, step.target), 0))
        return CompletedScm(returncode=code, stdout="ok" if code == 0 else "", stderr="" if code == 0 else "failed")

    return run


def test_remove_plan_covers_zapret_winws_and_windivert() -> None:
    steps = plan_remove_steps()
    pairs = [(step.kind, step.target) for step in steps]
    assert pairs[0] == ("stop", "zapret")
    assert pairs[1] == ("delete", "zapret")
    assert pairs[2] == ("taskkill", "winws.exe")
    assert ("stop", "WinDivert") in pairs
    assert ("delete", "WinDivert") in pairs
    assert ("stop", "WinDivert14") in pairs
    assert ("delete", "WinDivert14") in pairs
    kill = steps[2]
    assert "taskkill" in kill.argv[0].lower()
    assert "winws.exe" in kill.argv
    blob = " ".join(kill.argv).lower()
    assert "/im" in blob and "/f" in blob
    zapret_stop = steps[0]
    assert "sc" in zapret_stop.argv[0].lower()
    assert "stop" in zapret_stop.argv
    assert "zapret" in zapret_stop.argv


def test_remove_refuses_live_sc_and_taskkill(monkeypatch) -> None:
    def boom(*args, **kwargs):
        raise AssertionError(f"refusing live mutation in test: {args!r}")

    monkeypatch.setattr("zapret_gui.services.subprocess.run", boom)
    monkeypatch.setattr("zapret_gui.services.shell_execute", boom)
    result = remove_service(allow_live=False)
    assert result.ok is False
    assert result.executed is False
    assert "allow_live" in (result.error or "").lower() or "refusing" in (result.error or "").lower()
    kinds = [step.kind for step in result.steps]
    assert "stop" in kinds and "delete" in kinds and "taskkill" in kinds


def test_remove_injected_runner_order_nonfatal_missing(monkeypatch) -> None:
    seen: list[tuple[str, str]] = []

    def runner(step: RemoveStep) -> CompletedScm:
        seen.append((step.kind, step.target))
        if step.kind == "stop":
            return CompletedScm(returncode=1062, stdout="", stderr="The service has not been started.")
        if step.kind == "delete":
            return CompletedScm(
                returncode=1060,
                stdout="",
                stderr="The specified service does not exist as an installed service.",
            )
        if step.kind == "taskkill":
            return CompletedScm(returncode=128, stdout="", stderr="The process winws.exe not found.")
        return CompletedScm(returncode=0, stdout="ok", stderr="")

    monkeypatch.setattr(
        "zapret_gui.services.subprocess.run",
        lambda *a, **k: (_ for _ in ()).throw(AssertionError("live sc.exe")),
    )
    result = remove_service(runner=runner, allow_live=False)
    assert result.ok is True
    assert result.executed is True
    assert seen[0] == ("stop", "zapret")
    assert seen[1] == ("delete", "zapret")
    assert seen[2] == ("taskkill", "winws.exe")
    assert ("stop", "WinDivert") in seen
    assert ("delete", "WinDivert") in seen
    assert ("stop", "WinDivert14") in seen
    assert ("delete", "WinDivert14") in seen
    assert seen.index(("stop", "WinDivert")) > seen.index(("taskkill", "winws.exe"))
    kill = next(report for report in result.reports if report.kind == "taskkill")
    assert kill.note == "winws.exe was not running"


def test_remove_continues_after_a_failed_step() -> None:
    """service.bat :service_remove never aborts; neither does Remove."""
    events: list = []
    run = _runner(events, codes={("stop", "WinDivert"): 1052})
    result = remove_service(runner=run, allow_live=False)

    after_failure = events[events.index(("stop", "WinDivert")) + 1 :]
    assert after_failure == [("delete", "WinDivert"), ("stop", "WinDivert14"), ("delete", "WinDivert14")]
    failed = next(report for report in result.reports if report.command_line == "sc.exe stop WinDivert")
    assert failed.ok is False and failed.fatal is True
    # zapret itself went away, which is what Remove promises.
    assert result.ok is True
    assert "stop WinDivert failed (1052)" in (result.error or "")


def test_remove_waits_after_stops_and_checks_zapret_is_gone() -> None:
    events: list = []
    run = _runner(events)
    result = remove_service(runner=run, allow_live=False, waiter=_waiter(events))

    assert events == [
        ("stop", "zapret"),
        ("wait", "zapret", ("stopped", "not_installed")),
        ("delete", "zapret"),
        ("taskkill", "winws.exe"),
        ("stop", "WinDivert"),
        ("wait", "WinDivert", ("stopped", "not_installed")),
        ("delete", "WinDivert"),
        # WinDivert14 is advisory in the bat (>nul 2>&1): no wait.
        ("stop", "WinDivert14"),
        ("delete", "WinDivert14"),
        ("wait", "zapret", ("not_installed",)),
    ]
    assert result.ok is True
    assert result.verified is True
    assert [report.kind for report in result.reports][-1] == "verify"


def test_remove_fails_when_zapret_survives() -> None:
    events: list = []
    result = remove_service(runner=_runner(events), allow_live=False, waiter=_waiter(events, zapret_gone=False))
    assert result.ok is False
    assert "zapret is still installed" in (result.error or "")


def test_remove_advisory_windivert14_failure_is_not_an_error() -> None:
    events: list = []
    run = _runner(events, codes={("stop", "WinDivert14"): 1052, ("delete", "WinDivert14"): 5})
    result = remove_service(runner=run, allow_live=False)
    assert result.ok is True
    assert result.error is None
    advisory = [report for report in result.reports if "WinDivert14" in report.command_line]
    assert advisory and all(report.fatal is False for report in advisory)
