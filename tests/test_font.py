from __future__ import annotations

from pathlib import Path

from zapret_gui.app import MainWindow
from zapret_gui.theme import CONSOLE_FONT_FAMILY, GUI_FONT_FAMILY, STYLESHEET, build_stylesheet


def test_shipped_font_source_is_tahoma() -> None:
    assert GUI_FONT_FAMILY == "Tahoma"
    qss = STYLESHEET
    assert GUI_FONT_FAMILY in qss
    assert f'font-family: "{GUI_FONT_FAMILY}"' in qss
    assert qss == build_stylesheet()
    lowered = qss.lower()
    assert "segoe" not in lowered
    app_src = (Path(__file__).resolve().parents[1] / "zapret_gui" / "app.py").read_text(
        encoding="utf-8"
    )
    assert "GUI_FONT_FAMILY" in app_src
    assert 'QFont("Segoe UI"' not in app_src
    assert "QFont('Segoe UI'" not in app_src
    # The console family is a shipped token, never a literal in the GUI source.
    assert 'QFont("Consolas"' not in app_src
    assert "QFont('Consolas'" not in app_src
    assert "CONSOLE_FONT_FAMILY" in app_src


def test_console_is_the_only_monospace_exception() -> None:
    """Chrome stays Tahoma; the console needs column alignment, so it opts out."""
    assert CONSOLE_FONT_FAMILY == "Consolas"
    lowered = STYLESHEET.lower()
    start = lowered.find("qtextedit#consoleview")
    assert start != -1, "the console must carry its own QSS block"
    block = lowered[start : lowered.find("}", start) + 1]
    assert "consolas" in block
    assert "monospace" in block
    # Consolas may appear nowhere else in the sheet.
    assert lowered.replace(block, "").count("consolas") == 0


def test_constructed_widgets_report_tahoma(qapp, project_root: Path) -> None:
    window = MainWindow(project_root=project_root)
    try:
        qapp.setStyleSheet(STYLESHEET)
        expected = GUI_FONT_FAMILY.lower()
        samples = (
            window.install_button,
            window.list_editor,
            window.args_preview,
            window.language_button,
            window.clear_console_button,
            window.statusBar(),
        )
        for widget in samples:
            family = widget.font().family()
            assert expected in family.lower(), f"{widget.objectName() or widget} family={family!r}"
        assert CONSOLE_FONT_FAMILY.lower() in window.console.font().family().lower()
    finally:
        window.close()
