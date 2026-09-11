"""Discover general*.bat strategies and extract a resolved winws.exe command line."""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path

from zapret_gui.paths import bin_dir, lists_dir, winws_path, with_trailing_sep

# Matches service.bat :game_switch_status when utils\\game_filter.enabled is absent.
DEFAULT_GAME_FILTER_PORT = "12"
GAME_FILTER_WIDE = "1024-65535"

_WINWS_MARKER = re.compile(r"winws\.exe", re.IGNORECASE)
_PERCENT_VAR = re.compile(r"%([^%]+)%")


class StrategyError(ValueError):
    """A strategy file could not be discovered or parsed."""


@dataclass(frozen=True)
class GameFilter:
    tcp: str
    udp: str
    mode: str  # disabled | all | tcp | udp
    source: Path | None


@dataclass(frozen=True)
class ParsedStrategy:
    path: Path
    name: str
    image: str
    args: tuple[str, ...]
    args_line: str
    game_filter: GameFilter


def discover_strategies(root: Path) -> list[Path]:
    """Return this tree's ``general*.bat`` files, excluding ``service*.bat``."""
    root = Path(root)
    found: list[Path] = []
    try:
        entries = list(root.iterdir())
    except OSError as exc:
        raise StrategyError(f"Cannot list project root {root}: {exc}") from exc
    for path in entries:
        if not path.is_file():
            continue
        name = path.name
        lower = name.lower()
        if not lower.endswith(".bat"):
            continue
        if lower.startswith("service"):
            continue
        if lower.startswith("general"):
            found.append(path)
    found.sort(key=_natural_key)
    return found


def load_game_filter(root: Path) -> GameFilter:
    """Resolve GameFilterTCP/UDP the same way service.bat :game_switch_status does.

    Missing ``utils\\game_filter.enabled`` → disabled dummy port ``12``.
    Contents ``all`` / ``tcp`` / ``udp`` (case-insensitive, first line) match the bat.
    """
    flag = Path(root) / "utils" / "game_filter.enabled"
    if not flag.is_file():
        return GameFilter(
            tcp=DEFAULT_GAME_FILTER_PORT,
            udp=DEFAULT_GAME_FILTER_PORT,
            mode="disabled",
            source=None,
        )
    try:
        raw = flag.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return GameFilter(
            tcp=DEFAULT_GAME_FILTER_PORT,
            udp=DEFAULT_GAME_FILTER_PORT,
            mode="disabled",
            source=flag,
        )
    mode = ""
    for line in raw.splitlines():
        stripped = line.strip()
        if stripped:
            mode = stripped.lower()
            break
    if mode == "all":
        return GameFilter(GAME_FILTER_WIDE, GAME_FILTER_WIDE, "all", flag)
    if mode == "tcp":
        return GameFilter(GAME_FILTER_WIDE, DEFAULT_GAME_FILTER_PORT, "tcp", flag)
    # service.bat else-branch (including a bare "udp" or any other non-empty value)
    return GameFilter(DEFAULT_GAME_FILTER_PORT, GAME_FILTER_WIDE, "udp", flag)


GAME_FILTER_MODES = ("disabled", "all", "tcp", "udp")


def save_game_filter(root: Path, mode: str) -> GameFilter:
    """Write ``utils\\game_filter.enabled`` the way service.bat :game_switch does.

    ``disabled`` deletes the flag file (dummy port 12/12). ``all`` / ``tcp`` / ``udp``
    write that token as the first line. Does not restart the zapret service.
    """
    token = (mode or "").strip().lower()
    if token not in GAME_FILTER_MODES:
        raise StrategyError(
            f"invalid game filter mode {mode!r}; expected one of {', '.join(GAME_FILTER_MODES)}"
        )
    flag = Path(root) / "utils" / "game_filter.enabled"
    if token == "disabled":
        if flag.is_file():
            try:
                flag.unlink()
            except OSError as exc:
                raise StrategyError(f"Cannot disable game filter: {exc}") from exc
        return load_game_filter(root)
    try:
        flag.parent.mkdir(parents=True, exist_ok=True)
        flag.write_text(token + "\n", encoding="utf-8", newline="\n")
    except OSError as exc:
        raise StrategyError(f"Cannot write {flag}: {exc}") from exc
    return load_game_filter(root)


def parse_strategy(path: Path, root: Path, game_filter: GameFilter | None = None) -> ParsedStrategy:
    """Extract winws.exe image + args with BIN/LISTS/GameFilter variables resolved."""
    path = Path(path)
    root = Path(root)
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        raise StrategyError(f"Cannot read strategy {path}: {exc}") from exc

    gf = game_filter if game_filter is not None else load_game_filter(root)
    blob = extract_winws_args_blob(text)
    env = _expansion_env(root, gf)
    expanded = expand_percent_vars(blob, env)
    expanded = _unescape_batch(expanded)
    tokens = tokenize_args(expanded)
    if not tokens:
        raise StrategyError(f"{path.name}: winws.exe invocation has no arguments")
    image = str(winws_path(root))
    args_line = join_args(tokens)
    _assert_resolved(args_line, path.name)
    return ParsedStrategy(
        path=path,
        name=path.name,
        image=image,
        args=tuple(tokens),
        args_line=args_line,
        game_filter=gf,
    )


def extract_winws_args_blob(text: str) -> str:
    """Join ``^`` continuations and return the text after ``winws.exe``."""
    joined = join_batch_continuations(text)
    for line in joined.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("::") or stripped.lower().startswith("rem "):
            continue
        match = _WINWS_MARKER.search(stripped)
        if not match:
            continue
        rest = stripped[match.end() :]
        if rest.startswith('"'):
            rest = rest[1:]
        rest = rest.strip()
        if rest:
            return rest
    raise StrategyError("no winws.exe invocation found in strategy file")


def join_batch_continuations(text: str) -> str:
    lines = text.splitlines()
    out: list[str] = []
    buf = ""
    for line in lines:
        stripped = line.rstrip()
        if stripped.endswith("^"):
            buf += stripped[:-1] + " "
            continue
        buf += stripped
        out.append(buf)
        buf = ""
    if buf:
        out.append(buf)
    return "\n".join(out)


def expand_percent_vars(text: str, env: dict[str, str]) -> str:
    lowered = {key.lower(): value for key, value in env.items()}

    def _repl(match: re.Match[str]) -> str:
        name = match.group(1)
        value = lowered.get(name.lower())
        return value if value is not None else match.group(0)

    return _PERCENT_VAR.sub(_repl, text)


def tokenize_args(blob: str) -> list[str]:
    """Split a winws argument blob, consuming quotes so ``--flag="path"`` stays one token."""
    tokens: list[str] = []
    i = 0
    n = len(blob)
    while i < n:
        ch = blob[i]
        if ch.isspace():
            i += 1
            continue
        buf: list[str] = []
        while i < n and not blob[i].isspace():
            if blob[i] == '"':
                i += 1
                while i < n and blob[i] != '"':
                    buf.append(blob[i])
                    i += 1
                if i < n and blob[i] == '"':
                    i += 1
            else:
                buf.append(blob[i])
                i += 1
        token = "".join(buf)
        if token and token not in {"^", "^^"}:
            tokens.append(token)
    return tokens


def join_args(tokens: list[str] | tuple[str, ...]) -> str:
    parts: list[str] = []
    for token in tokens:
        if any(ch in token for ch in (' ', "\t")):
            parts.append(f'"{token}"')
        else:
            parts.append(token)
    return " ".join(parts)


def _expansion_env(root: Path, gf: GameFilter) -> dict[str, str]:
    dp0 = with_trailing_sep(Path(root).resolve())
    return {
        "BIN": with_trailing_sep(bin_dir(root)),
        "LISTS": with_trailing_sep(lists_dir(root)),
        "GameFilterTCP": gf.tcp,
        "GameFilterUDP": gf.udp,
        "GameFilter": gf.tcp,
        "~dp0": dp0,
        "cd": dp0,
    }


def _unescape_batch(text: str) -> str:
    # service.bat restores EXCL_MARK → !; the bats write ^! for a literal !
    return text.replace("^!", "!").replace("^^", "^")


def _assert_resolved(args_line: str, name: str) -> None:
    leftover = []
    for token in ("%BIN%", "%LISTS%", "%GameFilterTCP%", "%GameFilterUDP%"):
        if token.lower() in args_line.lower():
            leftover.append(token)
    if leftover:
        raise StrategyError(f"{name}: unresolved variables remain: {', '.join(leftover)}")


def _natural_key(path: Path) -> list[object]:
    parts = re.split(r"(\d+)", path.name.lower())
    key: list[object] = []
    for part in parts:
        key.append(int(part) if part.isdigit() else part)
    return key
