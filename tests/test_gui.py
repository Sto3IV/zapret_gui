from __future__ import annotations

from pathlib import Path

from zapret_gui.app import (
    REQUIRED_OBJECT_NAMES,
    MainWindow,
    missing_required_widgets,
    run_smoke,
)
from zapret_gui.lists_io import backup_list as shipped_backup_list
from zapret_gui.lists_io import read_list
from zapret_gui.lists_io import write_list as shipped_write_list
from zapret_gui.privileges import HostsLaunchPlan, HostsLaunchResult
from zapret_gui.identity import IdentityResult, RegistryWrite
from zapret_gui.services import RemoveResult, ScmCommand, ScmOpResult, ServiceStatus


def test_gui_source_binds_shipped_functions() -> None:
    source = (Path(__file__).resolve().parents[1] / "zapret_gui" / "app.py").read_text(
        encoding="utf-8"
    )
    for name in REQUIRED_OBJECT_NAMES:
        assert name in source, f"missing object name {name}"
    for fn in (
        "launch_hosts_notepad",
        "install_service",
        "start_service",
        "stop_service",
        "query_service_status",
        "remove_service",
        "save_game_filter",
        "persist_installed_strategy",
        "backup_list",
        "write_list",
        "read_list",
        "discover_strategies",
        "parse_strategy",
    ):
        assert fn in source, f"GUI does not reference shipped function {fn}"
    # List editor path must not spawn notepad; hosts path is allowed to.
    editor_region = source.split("def _on_save_list")[1].split("def _report_hosts")[0]
    assert "notepad" not in editor_region.lower()
    assert "write_list" in source.split("def _on_save_list")[1].split("def _report_hosts")[0]


def test_main_window_constructs_and_wires_controls(qapp, project_root: Path, monkeypatch) -> None:
    window = MainWindow(project_root=project_root)
    try:
        missing = missing_required_widgets(window)
        assert missing == [], f"missing controls: {missing}"
        assert window.hosts_button.objectName() == "hostsButton"
        assert window.strategy_combo.objectName() == "strategyCombo"
        assert window.install_button.objectName() == "installButton"
        assert window.start_button.objectName() == "startButton"
        assert window.stop_button.objectName() == "stopButton"
        assert window.status_button.objectName() == "statusButton"
        assert window.remove_button.objectName() == "removeButton"
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

        def _ok_result(action: str, image: str = "", args: str = "") -> ScmOpResult:
            argv = ("sc.exe", action if action != "install" else "create", "zapret")
            cmd = ScmCommand(
                action=action,  # type: ignore[arg-type]
                argv=argv,
                command_line=" ".join(argv),
                service_name="zapret",
                image=image,
                args=args,
            )
            scm_calls.append(action)
            return ScmOpResult(ok=True, command=cmd, executed=True)

        monkeypatch.setattr(
            "zapret_gui.app.install_service",
            lambda image, args, **k: _ok_result("install", image, args),
        )
        monkeypatch.setattr(
            "zapret_gui.app.start_service",
            lambda *a, **k: _ok_result("start"),
        )
        monkeypatch.setattr(
            "zapret_gui.app.stop_service",
            lambda *a, **k: _ok_result("stop"),
        )
        monkeypatch.setattr(
            "zapret_gui.app.query_service_status",
            lambda *a, **k: ServiceStatus(
                state="not_installed",
                service_name="zapret",
                message="not installed",
            ),
        )
        persisted: list[str] = []

        def fake_persist(stem, **_k):
            persisted.append(stem)
            return IdentityResult(
                ok=True,
                plan=RegistryWrite(
                    key=r"HKLM\SYSTEM\CurrentControlSet\Services\zapret",
                    value_name="zapret-discord-youtube",
                    data=stem,
                ),
                executed=True,
            )

        monkeypatch.setattr("zapret_gui.app.persist_installed_strategy", fake_persist)
        removed: list[str] = []

        def fake_remove(**_k) -> RemoveResult:
            removed.append("remove")
            return RemoveResult(ok=True, steps=(), executed=True)

        monkeypatch.setattr("zapret_gui.app.remove_service", fake_remove)
        window.install_button.click()
        window.start_button.click()
        window.stop_button.click()
        window.status_button.click()
        window.remove_button.click()
        assert "install" in scm_calls
        assert "start" in scm_calls
        assert "stop" in scm_calls
        assert removed == ["remove"]
        assert persisted
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
