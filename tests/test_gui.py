from __future__ import annotations

import time
from pathlib import Path

import pytest
from PySide6.QtWidgets import QMessageBox, QPushButton

from zapret_gui.app import (
    REQUIRED_OBJECT_NAMES,
    MainWindow,
    missing_required_widgets,
    run_smoke,
)
from zapret_gui.diagnostics import DiagLine
from zapret_gui.identity import IdentityResult, RegistryWrite
from zapret_gui.lists_io import backup_list as shipped_backup_list
from zapret_gui.lists_io import read_list
from zapret_gui.lists_io import write_list as shipped_write_list
from zapret_gui.privileges import HostsLaunchPlan, HostsLaunchResult
from zapret_gui.services import RemoveResult, ScmCommand, ScmOpResult, ServiceSnapshot, StepReport
from zapret_gui.tools import POWERSHELL_REQUIRED, TESTS_STARTING, ToolLaunch, ToolLaunchResult

TESTS_HINT_EN = "Don't know what strategy to use? Run tests and it will choose the best strategy for you!"


def _snapshot(token: str = "NOT_INSTALLED", *, strategy: str | None = None, image: str = "") -> ServiceSnapshot:
    return ServiceSnapshot(service_name="zapret", token=token, image=image, strategy=strategy)


@pytest.fixture(autouse=True)
def _quiet_scm(monkeypatch):
    """GUI tests never read the live service; each test states the SCM it expects."""
    monkeypatch.setattr("zapret_gui.app.query_service_snapshot", lambda *_a, **_k: _snapshot())


def _identity(stem: str) -> IdentityResult:
    return IdentityResult(
        ok=True,
        plan=RegistryWrite(
            key=r"HKLM\SYSTEM\CurrentControlSet\Services\zapret",
            value_name="zapret-discord-youtube",
            data=stem,
        ),
        executed=True,
    )


def _fake_launch(calls: list):
    def launch(root, *, elevated, **_kwargs) -> ToolLaunchResult:
        calls.append((Path(root), elevated))
        plan = ToolLaunch(
            file="powershell.exe",
            params="-NoProfile -ExecutionPolicy Bypass -File x",
            verb="open" if elevated else "runas",
            directory=str(root),
            script=Path(root) / "utils" / "test zapret.ps1",
        )
        return ToolLaunchResult(ok=True, plan=plan, native_code=42)

    return launch


def _click_and_settle(qapp, window, button, timeout_s: float = 10.0) -> None:
    """Click a service button and pump the loop until its worker thread reports back."""
    button.click()
    deadline = time.monotonic() + timeout_s
    while window._worker is not None and time.monotonic() < deadline:
        qapp.processEvents()
    qapp.processEvents()
    assert window._worker is None, f"{button.objectName()} worker did not finish"


def test_gui_source_binds_shipped_functions() -> None:
    source = (Path(__file__).resolve().parents[1] / "zapret_gui" / "app.py").read_text(
        encoding="utf-8"
    )
    for name in REQUIRED_OBJECT_NAMES:
        assert name in source, f"missing object name {name}"
    for fn in (
        "launch_hosts_notepad",
        "install_service",
        "remove_service",
        "query_service_snapshot",
        "run_diagnostics",
        "default_probes",
        "launch_tests",
        "powershell_supported",
        "newest_result_since",
        "find_best_strategy_line",
        "save_game_filter",
        "persist_installed_strategy",
        "backup_list",
        "write_list",
        "read_list",
        "discover_strategies",
        "parse_strategy",
    ):
        assert fn in source, f"GUI does not reference shipped function {fn}"
    for gone in ("startButton", "stopButton", "statusButton", "start_service", "stop_service"):
        assert gone not in source, f"{gone} should be gone from the GUI"
    # List editor path must not spawn notepad; hosts path is allowed to.
    editor_region = source.split("def _on_save_list")[1].split("def _report_hosts")[0]
    assert "notepad" not in editor_region.lower()
    assert "write_list" in source.split("def _on_save_list")[1].split("def _report_hosts")[0]


def test_main_window_constructs_and_wires_controls(qapp, project_root: Path, tmp_path: Path, monkeypatch) -> None:
    window = MainWindow(project_root=project_root)
    window.results_dir = tmp_path
    try:
        missing = missing_required_widgets(window)
        assert missing == [], f"missing controls: {missing}"
        assert window.hosts_button.objectName() == "hostsButton"
        assert window.strategy_combo.objectName() == "strategyCombo"
        assert window.install_button.objectName() == "installButton"
        assert window.remove_button.objectName() == "removeButton"
        assert window.tests_button.objectName() == "testsButton"
        assert window.diagnostics_button.objectName() == "diagnosticsButton"
        for gone in ("startButton", "stopButton", "statusButton"):
            assert window.findChild(QPushButton, gone) is None
        assert window.game_filter_combo.objectName() == "gameFilterCombo"
        assert window.game_filter_combo.count() == 4
        assert window.list_editor.objectName() == "listEditor"
        assert window.backup_button.objectName() == "backupButton"
        assert window.language_button.objectName() == "languageButton"
        assert window.strategy_combo.count() >= 20
        names = [window.strategy_combo.itemText(i) for i in range(window.strategy_combo.count())]
        assert "general.bat" in names
        assert any(n.startswith("general (ALT") for n in names)
        assert not any(n.lower().startswith("service") for n in names)

        hosts_calls: list[str] = []

        def fake_hosts(**_kwargs) -> HostsLaunchResult:
            plan = HostsLaunchPlan(
                file=r"C:\Windows\System32\notepad.exe",
                params=r"C:\Windows\System32\drivers\etc\hosts",
                verb="runas",
                notepad_path=r"C:\Windows\System32\notepad.exe",
                hosts_path=r"C:\Windows\System32\drivers\etc\hosts",
                missing_notepad=False,
                missing_hosts=False,
            )
            hosts_calls.append("launch")
            return HostsLaunchResult(ok=True, plan=plan)

        monkeypatch.setattr("zapret_gui.app.launch_hosts_notepad", fake_hosts)
        window.hosts_button.click()
        assert hosts_calls == ["launch"]

        scm_calls: list[str] = []

        def fake_install(image, args, **_kwargs) -> ScmOpResult:
            argv = ("sc.exe", "create", "zapret")
            scm_calls.append("install")
            return ScmOpResult(ok=True, command=ScmCommand("install", argv, " ".join(argv), "zapret", image, args), executed=True)

        monkeypatch.setattr("zapret_gui.app.install_service", fake_install)
        persisted: list[str] = []
        monkeypatch.setattr(
            "zapret_gui.app.persist_installed_strategy",
            lambda stem, **_k: persisted.append(stem) or _identity(stem),
        )
        removed: list[str] = []

        def fake_remove(**_kwargs) -> RemoveResult:
            removed.append("remove")
            return RemoveResult(ok=True, steps=(), executed=True)

        monkeypatch.setattr("zapret_gui.app.remove_service", fake_remove)
        launches: list = []
        monkeypatch.setattr("zapret_gui.app.powershell_supported", lambda *a, **k: True)
        monkeypatch.setattr("zapret_gui.app.launch_tests", _fake_launch(launches))
        diagnosed: list[Path] = []

        def fake_diagnostics(root, *, probes, emit, ask):
            diagnosed.append(Path(root))
            emit(DiagLine("ok", "Base Filtering Engine check passed"))

        monkeypatch.setattr("zapret_gui.app.run_diagnostics", fake_diagnostics)
        monkeypatch.setattr("zapret_gui.app.default_probes", lambda: object())

        _click_and_settle(qapp, window, window.install_button)
        _click_and_settle(qapp, window, window.remove_button)
        _click_and_settle(qapp, window, window.tests_button)
        _click_and_settle(qapp, window, window.diagnostics_button)

        assert scm_calls == ["install"]
        assert persisted
        assert removed == ["remove"]
        assert launches == [(project_root, launches[0][1])]
        assert diagnosed == [project_root]
        console = window.console.toPlainText()
        for header in ("=== Install Service", "=== Remove ===", "=== Tests ===", "=== Diagnostics ==="):
            assert header in console
        assert "Base Filtering Engine check passed" in console
        assert "Diagnostics finished" in console
    finally:
        window.close()


def test_service_buttons_sit_two_per_row(qapp, project_root: Path) -> None:
    window = MainWindow(project_root=project_root)
    try:
        grid = window.service_buttons_grid
        assert grid.rowCount() == 2 and grid.columnCount() == 2
        cells = [
            [grid.itemAtPosition(row, column).widget().objectName() for column in range(2)]
            for row in range(2)
        ]
        assert cells == [["installButton", "removeButton"], ["testsButton", "diagnosticsButton"]]
        assert window.tests_hint.text() == TESTS_HINT_EN
        assert window.tests_button.toolTip() == TESTS_HINT_EN
    finally:
        window.close()


def test_status_line_sits_beside_the_title_and_tracks_scm(qapp, project_root: Path, monkeypatch) -> None:
    current = {"snapshot": _snapshot("RUNNING", strategy="general (ALT2)", image=str(project_root / "bin" / "winws.exe"))}
    monkeypatch.setattr("zapret_gui.app.query_service_snapshot", lambda *_a, **_k: current["snapshot"])
    window = MainWindow(project_root=project_root)
    try:
        label = window.service_status_label
        title = next(child for child in window.findChildren(type(label)) if child.objectName() == "titleLabel")
        assert label.parentWidget() is title.parentWidget(), "status must share the title's row"
        assert label.text() == "● RUNNING  ·  general (ALT2)"
        assert label.property("state") == "running"

        current["snapshot"] = _snapshot("STOPPED", strategy="general (ALT2)", image=r"E:\.ZAPRET\bin\winws.exe")
        window._poll_service_status()
        assert label.text() == "● STOPPED  ·  general (ALT2)  ·  E:\\.ZAPRET"
        assert label.property("state") == "stopped"

        window._poll_service_status()
        console = window.console.toPlainText()
        assert console.count("running → stopped") == 1, "log the change once, not every poll"

        current["snapshot"] = _snapshot("NOT_INSTALLED")
        window._poll_service_status()
        assert label.text() == "● NOT INSTALLED"
        assert label.property("state") == "not_installed"
    finally:
        window.close()


def test_status_poll_pauses_while_a_worker_runs(qapp, project_root: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        "zapret_gui.app.remove_service",
        lambda **_k: RemoveResult(ok=True, steps=(), executed=True),
    )
    window = MainWindow(project_root=project_root)
    try:
        assert window._status_poll.isActive()
        window.remove_button.click()
        assert not window._status_poll.isActive(), "an SCM handle across sc delete reintroduces 1072"
        deadline = time.monotonic() + 10
        while window._worker is not None and time.monotonic() < deadline:
            qapp.processEvents()
        qapp.processEvents()
        assert window._worker is None
        assert window._status_poll.isActive()
    finally:
        window.close()


def test_install_streams_every_step_into_the_console(qapp, project_root: Path, monkeypatch) -> None:
    """The console is the 'service.bat window' — each sc step must show up in it."""
    steps = (
        StepReport("tcp", "netsh.exe interface tcp show global", 0, note="timestamps already enabled", fatal=False),
        StepReport("stop", "sc.exe stop zapret", 0, note="stopped"),
        StepReport("delete", "sc.exe delete zapret", 0, stdout="[SC] DeleteService SUCCESS", note="removed"),
        StepReport("create", "sc.exe create zapret binPath= ...", 0, stdout="[SC] CreateService SUCCESS"),
        StepReport("describe", 'sc.exe description zapret "Zapret DPI bypass software"', 0, fatal=False),
        StepReport("start", "sc.exe start zapret", 0, stdout="STATE : 2  START_PENDING"),
        StepReport("verify", "sc.exe queryex zapret", 0, note="RUNNING  pid 24188"),
    )

    def fake_install(image, args, **kwargs):
        emit = kwargs.get("on_step")
        for report in steps:
            if emit is not None:
                emit(report)
        argv = ("sc.exe", "create", "zapret")
        return ScmOpResult(
            ok=True,
            command=ScmCommand("install", argv, " ".join(argv), "zapret", image, args),
            executed=True,
            steps=steps,
            verified=True,
            pid=24188,
        )

    monkeypatch.setattr("zapret_gui.app.install_service", fake_install)
    monkeypatch.setattr("zapret_gui.app.persist_installed_strategy", lambda stem, **_k: _identity(stem))

    window = MainWindow(project_root=project_root)
    try:
        _click_and_settle(qapp, window, window.install_button)
        console = window.console.toPlainText()
        for line in (
            "netsh.exe interface tcp show global",
            "sc.exe stop zapret",
            "sc.exe delete zapret",
            "sc.exe create zapret",
            "sc.exe description zapret",
            "sc.exe start zapret",
            "sc.exe queryex zapret",
        ):
            assert line in console, f"console is missing {line!r}"
        assert "[SC] CreateService SUCCESS" in console
        assert "RUNNING  pid 24188" in console
        assert "zapret-discord-youtube" in console
        assert "Install OK" in console
        assert "winws pid 24188" in console

        window.clear_console_button.click()
        qapp.processEvents()
        assert window.console.toPlainText().strip() == ""
    finally:
        window.close()


def test_unelevated_install_does_not_claim_success_or_stamp_the_registry(
    qapp, project_root: Path, monkeypatch
) -> None:
    """ShellExecute > 32 only means cmd.exe launched; nothing was observed."""
    argv = ("sc.exe", "create", "zapret")
    monkeypatch.setattr(
        "zapret_gui.app.install_service",
        lambda image, args, **_k: ScmOpResult(
            ok=True,
            command=ScmCommand("install", argv, " ".join(argv), "zapret", image, args),
            executed=True,
            needs_elevation=True,
            verified=False,
        ),
    )
    monkeypatch.setattr("zapret_gui.app.is_process_elevated", lambda: False)
    stamped: list[str] = []
    monkeypatch.setattr(
        "zapret_gui.app.persist_installed_strategy",
        lambda stem, **_k: stamped.append(stem) or _identity(stem),
    )

    window = MainWindow(project_root=project_root)
    try:
        _click_and_settle(qapp, window, window.install_button)
        console = window.console.toPlainText()
        assert "not visible here" in console
        assert "Install OK" not in console
        assert stamped == [], "an unobserved install must not write the strategy name"
    finally:
        window.close()


def test_tests_button_launches_like_service_bat_and_preselects_the_winner(
    qapp, project_root: Path, tmp_path: Path, monkeypatch
) -> None:
    monkeypatch.setattr(
        "zapret_gui.app.query_service_snapshot",
        lambda *_a, **_k: _snapshot("RUNNING", strategy="general (ALT2)", image=r"E:\.ZAPRET\bin\winws.exe"),
    )
    launches: list = []
    monkeypatch.setattr("zapret_gui.app.powershell_supported", lambda *a, **k: True)
    monkeypatch.setattr("zapret_gui.app.launch_tests", _fake_launch(launches))

    window = MainWindow(project_root=project_root)
    window.results_dir = tmp_path
    try:
        window.strategy_combo.setCurrentIndex(window.strategy_combo.findText("general.bat"))
        _click_and_settle(qapp, window, window.tests_button)
        console = window.console.toPlainText()
        assert TESTS_STARTING in console
        assert "the tests refuse to run until you press Remove" in console
        assert len(launches) == 1 and launches[0][0] == project_root
        assert window._results_since is not None, "the results folder must be watched"

        results = tmp_path / "test_results_2026-09-13_10-42-07.txt"
        results.write_bytes(
            (
                "\ufeff=== ANALYTICS ===\r\n"
                "general (ALT11).bat : OK:  96, FAIL:  10, UNSUP:   0, BLOCKED:   0\r\n"
                "Best strategy: general (ALT11).bat\r\n"
            ).encode("utf-8")
        )
        deadline = time.monotonic() + 8
        while window.strategy_combo.currentText() != "general (ALT11).bat" and time.monotonic() < deadline:
            qapp.processEvents()
            time.sleep(0.02)
        assert window.strategy_combo.currentText() == "general (ALT11).bat"
        assert "Tests picked general (ALT11).bat" in window.console.toPlainText()
        assert window._results_since is None, "one result disarms the watch"
    finally:
        window.close()


def test_tests_button_refuses_without_powershell_3(qapp, project_root: Path, monkeypatch) -> None:
    launches: list = []
    monkeypatch.setattr("zapret_gui.app.powershell_supported", lambda *a, **k: False)
    monkeypatch.setattr("zapret_gui.app.launch_tests", _fake_launch(launches))
    window = MainWindow(project_root=project_root)
    try:
        _click_and_settle(qapp, window, window.tests_button)
        console = window.console.toPlainText()
        for line in POWERSHELL_REQUIRED:
            assert line in console
        assert TESTS_STARTING not in console
        assert launches == []
    finally:
        window.close()


def test_diagnostics_prompt_reaches_a_dialog_and_back(qapp, project_root: Path, monkeypatch) -> None:
    answers: list[bool] = []

    def fake_diagnostics(root, *, probes, emit, ask):
        emit(DiagLine("ok", "Proxy check passed"))
        emit(DiagLine("blank"))
        emit(DiagLine("fail", "[X] Adguard process found. Adguard may cause problems with Discord"))
        answers.append(ask("diagDiscordPrompt", True))

    asked: list = []

    def fake_question(*args, **_kwargs):
        asked.append(args)
        return QMessageBox.StandardButton.No

    monkeypatch.setattr("zapret_gui.app.run_diagnostics", fake_diagnostics)
    monkeypatch.setattr("zapret_gui.app.default_probes", lambda: object())
    monkeypatch.setattr("zapret_gui.app.QMessageBox.question", fake_question)

    window = MainWindow(project_root=project_root)
    try:
        _click_and_settle(qapp, window, window.diagnostics_button)
        assert answers == [False]
        assert len(asked) == 1
        _parent, _title, text, _buttons, preferred = asked[0]
        assert text == "Do you want to clear the Discord cache (Stable, PTB, Canary, Development)?"
        # service.bat's default for this prompt is Y.
        assert preferred == QMessageBox.StandardButton.Yes
        console = window.console.toPlainText()
        assert "Proxy check passed" in console
        assert "[X] Adguard process found" in console
        assert "(Y/N) (default: Y) N" in console
        assert "Diagnostics finished" in console
    finally:
        window.close()


def test_editor_open_save_backup_do_not_spawn_notepad(
    qapp, project_root: Path, tmp_path: Path, monkeypatch
) -> None:
    isolated = tmp_path / "lists"
    isolated.mkdir()
    src = project_root / "lists" / "list-exclude-user.txt"
    work = isolated / "list-exclude-user.txt"
    work.write_bytes(src.read_bytes())

    calls: list[str] = []

    def wrapped_backup(path, **kwargs):
        calls.append("backup")
        return shipped_backup_list(path, **kwargs)

    def wrapped_write(path, content):
        calls.append("save")
        return shipped_write_list(path, content)

    monkeypatch.setattr("zapret_gui.app.backup_list", wrapped_backup)
    monkeypatch.setattr("zapret_gui.app.write_list", wrapped_write)
    monkeypatch.setattr(
        "zapret_gui.app.QMessageBox.warning",
        lambda *a, **k: 0,
    )

    window = MainWindow(project_root=project_root, lists_dir=isolated)
    try:
        assert window.list_files_widget.count() >= 1
        window.list_files_widget.setCurrentRow(0)
        qapp.processEvents()
        assert window._current_list is not None
        opened = window.list_editor.toPlainText()
        assert opened.replace("\r\n", "\n") == read_list(work).replace("\r\n", "\n")

        window.backup_button.click()
        qapp.processEvents()
        baks = list(isolated.glob("*.bak"))
        assert len(baks) == 1
        assert baks[0].read_bytes() == work.read_bytes()
        assert "backup" in calls

        window.list_editor.setPlainText(opened + "\n# gui-save-probe\n")
        window.save_button.click()
        qapp.processEvents()
        assert "# gui-save-probe" in read_list(work)
        assert "save" in calls
        app_src = (Path(__file__).resolve().parents[1] / "zapret_gui" / "app.py").read_text(
            encoding="utf-8"
        )
        save_fn = app_src.split("def _on_save_list")[1].split("def _report_hosts")[0]
        backup_fn = app_src.split("def _on_backup")[1].split("def _on_backup_all")[0]
        assert "notepad" not in save_fn.lower()
        assert "notepad" not in backup_fn.lower()
    finally:
        window.close()


def test_run_smoke_exits_zero(project_root: Path, qapp) -> None:
    code = run_smoke(project_root)
    assert code == 0


def test_run_smoke_writes_screenshot(project_root: Path, qapp, tmp_path: Path) -> None:
    dest = tmp_path / "gui.png"
    code = run_smoke(project_root, screenshot=dest)
    assert code == 0
    assert dest.is_file()
    assert dest.stat().st_size > 1000
