from __future__ import annotations

from pathlib import Path

from PySide6.QtWidgets import QPushButton

from zapret_gui.app import MainWindow
from zapret_gui.i18n import (
    BUTTON_OBJECT_NAMES,
    apply_locale,
    default_lang_file,
    load_catalog,
)
from zapret_gui.services import ServiceSnapshot

REQUIRED_BUTTONS = (
    "installButton",
    "removeButton",
    "testsButton",
    "diagnosticsButton",
    "backupButton",
    "saveButton",
    "backupAllButton",
    "hostsButton",
    "relaunchAdminButton",
    "languageButton",
    "clearConsoleButton",
)

REQUIRED_LABELS = (
    "serviceSection",
    "hostsSection",
    "listsSection",
    "consoleSection",
    "testsHint",
)

STATUS_KEYS = (
    "statusRunning",
    "statusStarting",
    "statusStopping",
    "statusStopped",
    "statusNotInstalled",
    "statusError",
)
PROMPT_KEYS = ("diagConflictsPrompt", "diagDiscordPrompt")
RETIRED_KEYS = ("startButton", "stopButton", "statusButton")
TESTS_HINT_EN = "Don't know what strategy to use? Run tests and it will choose the best strategy for you!"


def test_lang_file_covers_section_labels() -> None:
    catalog = load_catalog()
    for locale in ("en", "ru"):
        for key in REQUIRED_LABELS:
            assert key in catalog[locale], f"{locale} missing {key}"
            assert catalog[locale][key].strip()


def test_lang_file_covers_all_gui_buttons() -> None:
    path = default_lang_file()
    assert path.is_file()
    catalog = load_catalog(path)
    assert "en" in catalog and "ru" in catalog
    for locale in ("en", "ru"):
        for key in REQUIRED_BUTTONS:
            assert key in catalog[locale], f"{locale} missing {key}"
            assert catalog[locale][key].strip()
    for key in REQUIRED_BUTTONS:
        assert catalog["ru"][key] != catalog["en"][key]
    assert set(REQUIRED_BUTTONS) <= set(BUTTON_OBJECT_NAMES)
    assert not set(RETIRED_KEYS) & set(BUTTON_OBJECT_NAMES)


def test_lang_file_covers_status_prompts_and_tests_hint() -> None:
    catalog = load_catalog()
    assert catalog["en"]["testsHint"] == TESTS_HINT_EN
    for key in STATUS_KEYS + PROMPT_KEYS + ("testsHint",):
        for locale in ("en", "ru"):
            assert catalog[locale].get(key, "").strip(), f"{locale} missing {key}"
        assert catalog["ru"][key] != catalog["en"][key], f"{key} is not translated"
    for key in RETIRED_KEYS:
        for locale in ("en", "ru"):
            assert key not in catalog[locale], f"{locale} still carries retired {key}"


def test_apply_russian_then_english_uses_lang_file(qapp, project_root: Path, monkeypatch) -> None:
    monkeypatch.setattr(
        "zapret_gui.app.query_service_snapshot",
        lambda *_a, **_k: ServiceSnapshot("zapret", "RUNNING", strategy="general (ALT2)"),
    )
    catalog = load_catalog()
    window = MainWindow(project_root=project_root)
    try:
        strategies_before = [
            window.strategy_combo.itemText(i) for i in range(window.strategy_combo.count())
        ]
        lists_before = [
            window.list_files_widget.item(i).text()
            for i in range(window.list_files_widget.count())
        ]
        assert "general.bat" in strategies_before
        assert any(name.endswith(".txt") for name in lists_before)

        ru = apply_locale(window, "ru", catalog)
        for key in REQUIRED_BUTTONS:
            widget = getattr(window, _attr(key), None)
            if widget is None:
                widget = window.findChild(QPushButton, key)
            assert widget is not None, f"missing button {key}"
            assert widget.text() == ru[key]
            assert widget.text() == catalog["ru"][key]
        assert window.tests_hint.text() == ru["testsHint"]
        assert window.tests_button.toolTip() == ru["testsHint"]
        assert ru["statusRunning"] in window.service_status_label.text()

        strategies_ru = [
            window.strategy_combo.itemText(i) for i in range(window.strategy_combo.count())
        ]
        lists_ru = [
            window.list_files_widget.item(i).text()
            for i in range(window.list_files_widget.count())
        ]
        assert strategies_ru == strategies_before
        assert lists_ru == lists_before

        en = apply_locale(window, "en", catalog)
        for key in REQUIRED_BUTTONS:
            widget = window.findChild(QPushButton, key)
            assert widget is not None
            assert widget.text() == en[key]
            assert widget.text() == catalog["en"][key]
        assert window.tests_hint.text() == TESTS_HINT_EN
        assert window.tests_button.toolTip() == TESTS_HINT_EN
        assert en["statusRunning"] in window.service_status_label.text()
        assert [
            window.strategy_combo.itemText(i) for i in range(window.strategy_combo.count())
        ] == strategies_before
        assert [
            window.list_files_widget.item(i).text()
            for i in range(window.list_files_widget.count())
        ] == lists_before
    finally:
        window.close()


def test_language_button_toggles_via_shipped_handler(qapp, project_root: Path) -> None:
    catalog = load_catalog()
    window = MainWindow(project_root=project_root)
    try:
        assert window.language_button.objectName() == "languageButton"
        assert window.language_button.text() == catalog["en"]["languageButton"]
        window.language_button.click()
        assert window._locale == "ru"
        assert window.install_button.text() == catalog["ru"]["installButton"]
        assert window.hosts_button.text() == catalog["ru"]["hostsButton"]
        assert window.tests_button.text() == catalog["ru"]["testsButton"]
        assert window.language_button.text() == catalog["ru"]["languageButton"]
        window.language_button.click()
        assert window._locale == "en"
        assert window.install_button.text() == catalog["en"]["installButton"]
    finally:
        window.close()


def _attr(object_name: str) -> str:
    mapping = {
        "installButton": "install_button",
        "removeButton": "remove_button",
        "testsButton": "tests_button",
        "diagnosticsButton": "diagnostics_button",
        "backupButton": "backup_button",
        "saveButton": "save_button",
        "backupAllButton": "backup_all_button",
        "hostsButton": "hosts_button",
        "relaunchAdminButton": "relaunch_button",
        "languageButton": "language_button",
        "clearConsoleButton": "clear_console_button",
    }
    return mapping.get(object_name, object_name)
