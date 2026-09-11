from __future__ import annotations

import os
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="session")
def project_root() -> Path:
    assert (PROJECT_ROOT / "bin" / "winws.exe").is_file()
    assert (PROJECT_ROOT / "lists").is_dir()
    return PROJECT_ROOT


@pytest.fixture(scope="session")
def qapp():
    """Session-wide QApplication on the cheapest platform plugin that works."""
    os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication(["zapret-gui-tests"])
    yield app
