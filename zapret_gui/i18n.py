"""Load the shipped lang-file and apply locale to GUI chrome.

Strategy ``general*.bat`` names, ``lists/`` filenames, the hosts path, and
winws argument text are not translated.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from PySide6.QtWidgets import QComboBox, QLabel, QPlainTextEdit, QPushButton, QWidget

from zapret_gui import HOSTS_PATH

LANG_FILE = Path(__file__).with_name("lang.json")

BUTTON_OBJECT_NAMES: tuple[str, ...] = (
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

# Window methods that re-render text built at runtime rather than from one key.
_RERENDER_HOOKS = ("_refresh_privilege_banner", "_render_service_status")


def default_lang_file() -> Path:
    return LANG_FILE


def load_catalog(path: Path | None = None) -> dict[str, dict[str, str]]:
    """Return the shipped lang-file as ``{locale: {key: string}}``."""
    target = Path(path) if path is not None else default_lang_file()
    data = json.loads(target.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or "en" not in data or "ru" not in data:
        raise ValueError(f"lang-file {target} must contain 'en' and 'ru' objects")
    catalog: dict[str, dict[str, str]] = {}
    for locale, table in data.items():
        if not isinstance(table, dict):
            raise ValueError(f"lang-file locale {locale!r} is not an object")
        catalog[str(locale)] = {str(k): str(v) for k, v in table.items()}
    return catalog


def apply_locale(
    window: QWidget,
    locale: str,
    catalog: dict[str, dict[str, str]] | None = None,
) -> dict[str, str]:
    """Set chrome widget text from the lang-file. Returns the applied table."""
    data = catalog if catalog is not None else load_catalog()
    if locale not in data:
        raise ValueError(f"unknown locale {locale!r}")
    table = data[locale]
    window.setProperty("locale", locale)
    setattr(window, "_locale", locale)
    setattr(window, "_i18n_table", table)
    setattr(window, "_i18n_catalog", data)

    for name in BUTTON_OBJECT_NAMES:
        widget = window.findChild(QPushButton, name)
        if widget is None:
            continue
        if name not in table:
            continue
        widget.setText(table[name])

    for button in window.findChildren(QPushButton):
        key = button.property("i18nTooltip")
        if key and str(key) in table:
            button.setToolTip(table[str(key)])

    for label in window.findChildren(QLabel):
        key = label.property("i18n")
        if not key:
            continue
        key_s = str(key)
        if key_s not in table:
            continue
        text = table[key_s]
        if "{path}" in text:
            text = text.format(path=HOSTS_PATH)
        label.setText(text)

    combo = window.findChild(QComboBox, "gameFilterCombo")
    if combo is not None:
        combo.blockSignals(True)
        for index in range(combo.count()):
            mode = str(combo.itemData(index) or "")
            key = f"gameFilter_{mode}"
            if key in table:
                combo.setItemText(index, table[key])
        combo.blockSignals(False)

    editor = window.findChild(QPlainTextEdit, "listEditor")
    if editor is not None and "listEditorPlaceholder" in table:
        editor.setPlaceholderText(table["listEditorPlaceholder"])
    args = window.findChild(QPlainTextEdit, "argsPreview")
    if args is not None and "argsPlaceholder" in table:
        args.setPlaceholderText(table["argsPlaceholder"])

    for hook in _RERENDER_HOOKS:
        refresh = getattr(window, hook, None)
        if callable(refresh):
            refresh()
    return table


def toggle_locale(window: QWidget, catalog: dict[str, dict[str, str]] | None = None) -> str:
    current = str(getattr(window, "_locale", "en") or "en")
    nxt = "en" if current == "ru" else "ru"
    apply_locale(window, nxt, catalog)
    return nxt


def lookup(window: Any, key: str, default: str = "") -> str:
    table = getattr(window, "_i18n_table", None) or {}
    return str(table.get(key, default or key))
