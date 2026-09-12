"""GitHub Primer Dark High Contrast (dark_high_contrast) palette → Qt stylesheet."""

from __future__ import annotations

from typing import Mapping

# Primer `dark_high_contrast` functional colors (VS Code github-vscode-theme /
# primer/primitives). Hexes are the tokens, not approximations.
PALETTE: dict[str, str] = {
    "canvas": "#0a0c10",
    "canvas_inset": "#010409",
    "canvas_overlay": "#272b33",
    "fg": "#f0f3f6",
    "fg_muted": "#9ea7b3",
    "border": "#7a828e",
    # 7zDark.ini [dark] borderColor / [dark.colors] edge. "#FFFFFF" is a
    # 7-Zip sentinel for system default; the reachable near-white edge is this.
    "window_edge": "#b7bdc8",
    "accent": "#71b7ff",
    "accent_emphasis": "#409eff",
    "danger": "#ff6a69",
    "success": "#26cd4d",
    # Primer DHC attention-fg. The console warn colour, matching service.bat's
    # :PrintYellow for advisory steps that failed without aborting the install.
    "attention": "#f0b72f",
}

# Single GUI typeface. Every widget inherits this family.
GUI_FONT_FAMILY = "Tahoma"
# The console pane is the one place that needs column alignment, so it opts out.
CONSOLE_FONT_FAMILY = "Consolas"


def build_stylesheet(palette: Mapping[str, str] | None = None) -> str:
    """Interpolate the shipped QSS from Primer DHC tokens. Tests import this."""
    p = dict(PALETTE if palette is None else palette)
    family = GUI_FONT_FAMILY
    mono = CONSOLE_FONT_FAMILY
    return f"""
* {{
    font-family: "{family}";
}}
QMainWindow {{
    background: {p["canvas"]};
    color: {p["fg"]};
    border: 1px solid {p["window_edge"]};
    font-family: "{family}";
}}
QWidget#central {{
    background: {p["canvas"]};
    color: {p["fg"]};
    border: 1px solid {p["window_edge"]};
}}
QLabel#titleLabel {{
    color: {p["fg"]};
    font-size: 18px;
    font-weight: 700;
    letter-spacing: 1px;
}}
QLabel#subtitleLabel {{
    color: {p["fg_muted"]};
    font-size: 11px;
}}
QLabel#sectionLabel {{
    color: {p["accent"]};
    font-size: 11px;
    font-weight: 700;
    letter-spacing: 1.4px;
}}
QLabel#privilegeBanner {{
    padding: 6px 10px;
    border-radius: 2px;
    font-weight: 600;
}}
QLabel#privilegeBanner[elevated="true"] {{
    background: {p["canvas_inset"]};
    color: {p["success"]};
    border: 1px solid {p["success"]};
}}
QLabel#privilegeBanner[elevated="false"] {{
    background: {p["canvas_inset"]};
    color: {p["danger"]};
    border: 1px solid {p["danger"]};
}}
QLabel#serviceStatusLabel {{
    color: {p["fg"]};
    font-weight: 600;
    padding: 4px 0;
}}
QPushButton {{
    background: {p["canvas_overlay"]};
    color: {p["fg"]};
    border: 1px solid {p["border"]};
    padding: 7px 14px;
    min-height: 28px;
}}
QPushButton:hover {{
    background: {p["canvas_overlay"]};
    border-color: {p["accent"]};
    color: {p["fg"]};
}}
QPushButton:pressed {{
    background: {p["canvas_inset"]};
    border-color: {p["accent_emphasis"]};
}}
QPushButton:disabled {{
    color: {p["fg_muted"]};
    border-color: {p["border"]};
    background: {p["canvas"]};
}}
QPushButton#installButton, QPushButton#hostsButton {{
    background: {p["canvas_overlay"]};
    border-color: {p["accent"]};
    color: {p["accent"]};
}}
QPushButton#installButton:hover, QPushButton#hostsButton:hover {{
    border-color: {p["accent_emphasis"]};
    color: {p["fg"]};
}}
QPushButton#stopButton, QPushButton#removeButton {{
    border-color: {p["danger"]};
    color: {p["fg"]};
}}
QPushButton#stopButton:hover, QPushButton#removeButton:hover {{
    border-color: {p["danger"]};
    color: {p["danger"]};
}}
QComboBox, QListWidget, QPlainTextEdit, QTextEdit {{
    background: {p["canvas_inset"]};
    color: {p["fg"]};
    border: 1px solid {p["border"]};
    selection-background-color: {p["accent_emphasis"]};
    selection-color: {p["fg"]};
}}
QComboBox:hover, QListWidget:hover, QPlainTextEdit:hover, QTextEdit:hover {{
    border-color: {p["accent"]};
}}
QComboBox::drop-down {{
    border: 0;
    width: 22px;
}}
QComboBox QAbstractItemView {{
    background: {p["canvas_inset"]};
    color: {p["fg"]};
    border: 1px solid {p["border"]};
    selection-background-color: {p["accent_emphasis"]};
    selection-color: {p["fg"]};
}}
QListWidget::item:selected {{
    background: {p["accent_emphasis"]};
    color: {p["fg"]};
}}
QStatusBar {{
    background: {p["canvas_inset"]};
    color: {p["fg"]};
    border-top: 1px solid {p["border"]};
}}
QSplitter::handle {{
    background: {p["canvas_overlay"]};
}}
/* The `*` rule above sets Tahoma everywhere; an id selector outranks it. */
QTextEdit#consoleView {{
    background: {p["canvas_inset"]};
    color: {p["fg"]};
    border: 1px solid {p["border"]};
    font-family: "{mono}", "Courier New", monospace;
    font-size: 12px;
}}
QPushButton#clearConsoleButton {{
    padding: 3px 10px;
    min-height: 20px;
    font-size: 11px;
}}
QScrollBar:vertical, QScrollBar:horizontal {{
    background: {p["canvas"]};
    border: 1px solid {p["border"]};
}}
QScrollBar::handle:vertical, QScrollBar::handle:horizontal {{
    background: {p["border"]};
}}
"""


STYLESHEET = build_stylesheet(PALETTE)
