from __future__ import annotations

from pathlib import Path

from zapret_gui.app import MainWindow
from zapret_gui.theme import GUI_FONT_FAMILY, STYLESHEET, build_stylesheet


def test_shipped_font_source_is_tahoma() -> None:
    assert GUI_FONT_FAMILY == "Tahoma"
    qss = STYLESHEET
    assert GUI_FONT_FAMILY in qss
    assert f'font-family: "{GUI_FONT_FAMILY}"' in qss
    assert qss == build_stylesheet()
    lowered = qss.lower()
    assert "consolas" not in lowered
    assert "segoe" not in lowered
    app_src = (Path(__file__).resolve().parents[1] / "zapret_gui" / "app.py").read_text(
        encoding="utf-8"
    )
    assert "GUI_FONT_FAMILY" in app_src
    assert 'QFont("Consolas"' not in app_src
    assert "QFont('Consolas'" not in app_src
    assert 'QFont("Segoe UI"' not in app_src
    assert "QFont('Segoe UI'" not in app_src


def test_constructed_widgets_report_tahoma(qapp, project_root: Path) -> None:
    window = MainWindow(project_root=project_root)
    try:
        qapp.setStyleSheet(STYLESHEET)
        expected = GUI_FONT_FAMILY.lower()
        samples = (
            window.install_button,
            window.list_editor,
            window.args_preview,
            window.log_view,
            window.language_button,
            window.statusBar(),
        )
        for widget in samples:
            family = widget.font().family()
            assert expected in family.lower(), f"{widget.objectName() or widget} family={family!r}"
    finally:
        window.close()
