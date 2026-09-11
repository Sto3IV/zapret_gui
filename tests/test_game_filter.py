from __future__ import annotations

import shutil
from pathlib import Path

from zapret_gui.strategies import (
    DEFAULT_GAME_FILTER_PORT,
    GAME_FILTER_WIDE,
    load_game_filter,
    parse_strategy,
    save_game_filter,
)


def _temp_strategy_tree(tmp_path: Path, project_root: Path) -> Path:
    root = tmp_path / "zapret-tree"
    root.mkdir()
    shutil.copy2(project_root / "general.bat", root / "general.bat")
    (root / "bin").mkdir()
    (root / "bin" / "winws.exe").write_bytes(b"")
    (root / "lists").mkdir()
    (root / "utils").mkdir()
    return root


def test_disabled_has_no_flag_and_parses_dummy_port(project_root: Path, tmp_path: Path) -> None:
    root = _temp_strategy_tree(tmp_path, project_root)
    flag = root / "utils" / "game_filter.enabled"
    flag.write_text("all\n", encoding="utf-8")
    gf = save_game_filter(root, "disabled")
    assert gf.mode == "disabled"
    assert not flag.exists()
    parsed = parse_strategy(root / "general.bat", root)
    assert parsed.game_filter.tcp == DEFAULT_GAME_FILTER_PORT
    assert parsed.game_filter.udp == DEFAULT_GAME_FILTER_PORT
    assert DEFAULT_GAME_FILTER_PORT in parsed.args_line
    assert GAME_FILTER_WIDE not in parsed.args_line


def test_all_tcp_udp_write_flag_and_expand_ports(project_root: Path, tmp_path: Path) -> None:
    root = _temp_strategy_tree(tmp_path, project_root)
    flag = root / "utils" / "game_filter.enabled"
    cases = (
        ("all", GAME_FILTER_WIDE, GAME_FILTER_WIDE),
        ("tcp", GAME_FILTER_WIDE, DEFAULT_GAME_FILTER_PORT),
        ("udp", DEFAULT_GAME_FILTER_PORT, GAME_FILTER_WIDE),
    )
    for mode, tcp, udp in cases:
        gf = save_game_filter(root, mode)
        assert gf.mode == mode
        assert flag.is_file()
        first = flag.read_text(encoding="utf-8").splitlines()[0].strip().lower()
        assert first == mode
        parsed = parse_strategy(root / "general.bat", root)
        assert parsed.game_filter.tcp == tcp
        assert parsed.game_filter.udp == udp
        assert tcp in parsed.args_line
        assert udp in parsed.args_line
        assert "%GameFilterTCP%" not in parsed.args_line
        assert "%GameFilterUDP%" not in parsed.args_line


def test_mode_change_then_reparse_updates_args(project_root: Path, tmp_path: Path) -> None:
    root = _temp_strategy_tree(tmp_path, project_root)
    save_game_filter(root, "disabled")
    before = parse_strategy(root / "general.bat", root)
    save_game_filter(root, "all")
    after = parse_strategy(root / "general.bat", root)
    assert before.args_line != after.args_line
    assert GAME_FILTER_WIDE in after.args_line
    assert DEFAULT_GAME_FILTER_PORT in before.args_line
    loaded = load_game_filter(root)
    assert loaded.mode == "all"
