from __future__ import annotations

from zapret_gui.theme import GUI_FONT_FAMILY, PALETTE, STYLESHEET, build_stylesheet


DHC_REQUIRED = {
    "canvas": "#0a0c10",
    "fg": "#f0f3f6",
    "border": "#7a828e",
    "danger": "#ff6a69",
}

OLD_BRASS = "#f3d48b"
OLD_CANVAS = "#0c0f12"
OLD_BUTTON = "#1b2229"
OLD_HAIRLINE = "#2c3540"


def test_palette_is_primer_dark_high_contrast() -> None:
    for key, hex_color in DHC_REQUIRED.items():
        assert PALETTE[key].lower() == hex_color
    assert PALETTE["canvas_inset"].lower() == "#010409"
    assert PALETTE["accent"].lower() in {"#71b7ff", "#409eff"}
    assert PALETTE["accent_emphasis"].lower() in {"#71b7ff", "#409eff"}
    assert PALETTE["success"].lower() == "#26cd4d"
    assert PALETTE["fg_muted"].lower() == "#9ea7b3"
    assert PALETTE["window_edge"].lower() == "#b7bdc8"


def test_stylesheet_is_built_from_shipped_palette() -> None:
    qss = STYLESHEET.lower()
    built = build_stylesheet(PALETTE).lower()
    assert qss == built
    for hex_color in DHC_REQUIRED.values():
        assert hex_color in qss
    assert "#71b7ff" in qss or "#409eff" in qss
    assert PALETTE["accent"].lower() in qss
    assert PALETTE["danger"].lower() in qss
    assert PALETTE["success"].lower() in qss
    assert PALETTE["border"].lower() in qss
    # Interactive widgets keep the DHC control border, not the window-edge token.
    assert "border: 1px solid #7a828e" in qss
    assert "1px solid #b7bdc8" in qss
    assert GUI_FONT_FAMILY.lower() == "tahoma"
    assert f'font-family: "{GUI_FONT_FAMILY.lower()}"' in qss


def test_old_brass_and_canvas_are_gone() -> None:
    blob = (STYLESHEET + " " + " ".join(PALETTE.values())).lower()
    assert OLD_BRASS not in blob
    assert OLD_CANVAS not in blob
    assert OLD_BUTTON not in STYLESHEET.lower()
    assert OLD_HAIRLINE not in STYLESHEET.lower()
    assert "gold" not in STYLESHEET.lower()
    assert "#f3d48b" not in STYLESHEET.lower()


def test_window_frame_is_1px_7zdark_edge() -> None:
    """7zDark.ini borderColor/edge is #B7BDC8; QSS paints it on the window surface."""
    assert PALETTE["window_edge"].lower() == "#b7bdc8"
    qss = STYLESHEET
    assert "#b7bdc8" in qss.lower()
    lowered = qss.lower()
    main_idx = lowered.find("qmainwindow")
    assert main_idx != -1
    window_block = lowered[main_idx : lowered.find("}", main_idx) + 1]
    assert "1px" in window_block
    assert "#b7bdc8" in window_block
    assert "border:" in window_block
    central_idx = lowered.find("qwidget#central")
    assert central_idx != -1
    central_block = lowered[central_idx : lowered.find("}", central_idx) + 1]
    assert "1px" in central_block
    assert "#b7bdc8" in central_block
    # Inner controls still use #7a828e, not the window-edge token.
    assert "qpushbutton" in lowered
    btn_idx = lowered.find("qpushbutton {")
    btn_block = lowered[btn_idx : lowered.find("}", btn_idx) + 1]
    assert "#7a828e" in btn_block
    assert "#ffffff" not in PALETTE["window_edge"].lower()


def test_app_uses_theme_stylesheet() -> None:
    from zapret_gui import app as app_mod
    from zapret_gui import theme as theme_mod

    assert app_mod.STYLESHEET is theme_mod.STYLESHEET
    assert "#0a0c10" in app_mod.STYLESHEET.lower()
    assert "#f3d48b" not in app_mod.STYLESHEET.lower()
