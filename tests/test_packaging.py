from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _third_party_imports(package_dir: Path) -> set[str]:
    stdlib_hint = {
        "argparse",
        "ctypes",
        "dataclasses",
        "datetime",
        "os",
        "pathlib",
        "re",
        "shutil",
        "subprocess",
        "sys",
        "traceback",
        "typing",
        "zipfile",
        "__future__",
        "collections",
        "enum",
        "importlib",
        "winreg",
        "json",
        "logging",
        "textwrap",
        "functools",
        "itertools",
        "abc",
        "io",
        "time",
        "struct",
        "errno",
    }
    found: set[str] = set()
    for path in package_dir.rglob("*.py"):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    top = alias.name.split(".")[0]
                    if top not in stdlib_hint and top != "zapret_gui":
                        found.add(top)
            elif isinstance(node, ast.ImportFrom) and node.module:
                top = node.module.split(".")[0]
                if top not in stdlib_hint and top != "zapret_gui" and node.level == 0:
                    found.add(top)
    return found


def test_requirements_lists_runtime_imports() -> None:
    req = (ROOT / "requirements.txt").read_text(encoding="utf-8")
    assert "PySide6" in req
    imported = _third_party_imports(ROOT / "zapret_gui")
    req_names = {
        line.split("==")[0].split(">=")[0].split("[")[0].strip()
        for line in req.splitlines()
        if line.strip() and not line.strip().startswith("#")
    }
    missing = imported - req_names
    assert missing == set(), f"runtime imports missing from requirements.txt: {missing}"


def test_readme_documents_install_and_launch() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "pip install -r requirements.txt" in readme
    assert "python -m zapret_gui" in readme
    assert "ZapretControl.bat" in readme
    assert "Remove" in readme
    assert "zapret-discord-youtube" in readme
    assert "Game Filter" in readme
    lowered = readme.lower()
    assert "disabled" in lowered and "tcp" in lowered and "udp" in lowered
    assert "12" in readme
