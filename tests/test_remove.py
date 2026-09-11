from __future__ import annotations

from zapret_gui.services import (
    CompletedScm,
    RemoveStep,
    plan_remove_steps,
    remove_service,
)


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
