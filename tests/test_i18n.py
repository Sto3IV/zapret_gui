from __future__ import annotations

from pathlib import Path

from zapret_gui.i18n import (
    BUTTON_OBJECT_NAMES,
    apply_locale,
    default_lang_file,
    load_catalog,
)
from zapret_gui.app import MainWindow


REQUIRED_BUTTONS = (
    "installButton",
    "startButton",
    "stopButton",
    "removeButton",
    "statusButton",
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
)


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


def test_apply_russian_then_english_uses_lang_file(qapp, project_root: Path) -> None:
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
                from PySide6.QtWidgets import QPushButton

                widget = window.findChild(QPushButton, key)
            assert widget is not None, f"missing button {key}"
            assert widget.text() == ru[key]
            assert widget.text() == catalog["ru"][key]

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
            from PySide6.QtWidgets import QPushButton

            widget = window.findChild(QPushButton, key)
            assert widget is not None
            assert widget.text() == en[key]
            assert widget.text() == catalog["en"][key]
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
        assert window.language_button.text() == catalog["ru"]["languageButton"]
        window.language_button.click()
        assert window._locale == "en"
        assert window.install_button.text() == catalog["en"]["installButton"]
    finally:
        window.close()


def _attr(object_name: str) -> str:
    mapping = {
        "installButton": "install_button",
        "startButton": "start_button",
        "stopButton": "stop_button",
        "removeButton": "remove_button",
        "statusButton": "status_button",
        "backupButton": "backup_button",
        "saveButton": "save_button",
        "backupAllButton": "backup_all_button",
        "hostsButton": "hosts_button",
        "relaunchAdminButton": "relaunch_button",
        "languageButton": "language_button",
        "clearConsoleButton": "clear_console_button",
    }
    return mapping.get(object_name, object_name)
