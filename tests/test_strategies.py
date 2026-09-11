from __future__ import annotations

from pathlib import Path

from zapret_gui.strategies import (
    DEFAULT_GAME_FILTER_PORT,
    discover_strategies,
    load_game_filter,
    parse_strategy,
)


UNRESOLVED = ("%BIN%", "%LISTS%", "%GameFilterTCP%", "%GameFilterUDP%")


def test_discovery_returns_real_general_bats_excludes_service(project_root: Path) -> None:
    found = discover_strategies(project_root)
    names = [p.name for p in found]
    assert names, "expected general*.bat strategies in this tree"
    globbed = sorted(p.name for p in project_root.glob("general*.bat"))
    assert sorted(names) == globbed
    assert any(n == "general.bat" for n in names)
    assert any(n.startswith("general (ALT") for n in names)
    assert all(n.lower().startswith("general") for n in names)
    assert all(n.lower().endswith(".bat") for n in names)
    assert not any(n.lower().startswith("service") for n in names)
    assert (project_root / "service.bat").is_file()
    discovered_paths = {p.resolve() for p in found}
    assert (project_root / "service.bat").resolve() not in discovered_paths


def test_parse_general_and_alt_resolve_winws_lists_bin(project_root: Path) -> None:
    general = project_root / "general.bat"
    alt = project_root / "general (ALT).bat"
    assert general.is_file()
    assert alt.is_file()

    for path in (general, alt):
        parsed = parse_strategy(path, project_root)
        image = Path(parsed.image)
        assert image.name.lower() == "winws.exe"
        assert image.parent.name.lower() == "bin"
        assert image.is_file()
        assert image == (project_root / "bin" / "winws.exe").resolve()

        args = parsed.args_line
        lowered = args.lower()
        for token in UNRESOLVED:
            assert token.lower() not in lowered, f"{path.name} still contains {token}"

        lists_abs = str((project_root / "lists").resolve())
        bin_abs = str((project_root / "bin").resolve())
        assert lists_abs.lower() in args.lower()
        assert bin_abs.lower() in args.lower()
        assert "list-general.txt" in args
        assert args.startswith("--")
        assert parsed.game_filter.tcp
        assert parsed.game_filter.udp


def test_all_general_strategies_parse_without_percent_tokens(project_root: Path) -> None:
    failures: list[str] = []
    parsed_count = 0
    for path in discover_strategies(project_root):
        parsed = parse_strategy(path, project_root)
        parsed_count += 1
        blob = parsed.args_line.lower()
        for token in UNRESOLVED:
            if token.lower() in blob:
                failures.append(f"{path.name}: leftover {token}")
        if "winws.exe" not in parsed.image.lower():
            failures.append(f"{path.name}: image is not winws.exe ({parsed.image})")
        if str((project_root / "lists").resolve()).lower() not in blob:
            # ALT5 is hostlist-less on some profiles but still references ipset-all under lists\
            if "ipset" not in blob and "list-" not in blob:
                failures.append(f"{path.name}: no lists\\ path in args")
    assert parsed_count >= 20
    assert failures == []


def test_game_filter_default_matches_service_bat_disabled(project_root: Path) -> None:
    gf = load_game_filter(project_root)
    flag = project_root / "utils" / "game_filter.enabled"
    if not flag.is_file():
        assert gf.mode == "disabled"
        assert gf.tcp == DEFAULT_GAME_FILTER_PORT
        assert gf.udp == DEFAULT_GAME_FILTER_PORT
    parsed = parse_strategy(project_root / "general.bat", project_root, game_filter=gf)
    assert f",{gf.tcp}" in parsed.args_line or parsed.args_line.endswith(gf.tcp)
    assert f",{gf.udp}" in parsed.args_line or gf.udp in parsed.args_line


def test_game_filter_flag_file(project_root: Path, tmp_path: Path) -> None:
    utils = tmp_path / "utils"
    utils.mkdir()
    (utils / "game_filter.enabled").write_text("all\n", encoding="utf-8")
    gf = load_game_filter(tmp_path)
    assert gf.mode == "all"
    assert gf.tcp == "1024-65535"
    assert gf.udp == "1024-65535"

    (utils / "game_filter.enabled").write_text("tcp\n", encoding="utf-8")
    gf = load_game_filter(tmp_path)
    assert gf.mode == "tcp"
    assert gf.tcp == "1024-65535"
    assert gf.udp == DEFAULT_GAME_FILTER_PORT
