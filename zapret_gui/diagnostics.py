"""Native port of ``service.bat`` ``:service_diagnostics`` (menu option 11).

Checks run in the bat's order and print its text verbatim, so the console reads
like the bat's own window. Every touch of the host goes through ``DiagProbes``;
tests replace it and never reach the real machine.
"""

from __future__ import annotations

import os
import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Literal, Mapping, Sequence

from zapret_gui.services import (
    CompletedScm,
    _run_argv,
    build_tcp_enable_command,
    build_tcp_probe_command,
    tcp_timestamps_enabled,
)

DiagLevel = Literal["ok", "warn", "fail", "info", "blank"]

PROMPT_CONFLICTS = "diagConflictsPrompt"
PROMPT_DISCORD = "diagDiscordPrompt"

INTERNET_SETTINGS = r"Software\Microsoft\Windows\CurrentVersion\Internet Settings"
DOH_PARAMETERS = r"SYSTEM\CurrentControlSet\Services\Dnscache\InterfaceSpecificParameters"

# service.bat:652 — services that fight zapret for WinDivert.
CONFLICTING_BYPASSES = ("GoodbyeDPI", "discordfix_zapret", "winws1", "winws2")
# service.bat:606 — what the WinDivert auto-fix removes when the driver will not go.
WINDIVERT_BLOCKERS = ("GoodbyeDPI",)
DISCORD_CACHE_DIRS = ("Cache", "Code Cache", "GPUCache")
DISCORD_INSTALLS = (
    ("discord", "Discord.exe", "Discord"),
    ("discordptb", "DiscordPTB.exe", "Discord PTB"),
    ("discordcanary", "DiscordCanary.exe", "Discord Canary"),
    ("discorddevelopment", "DiscordDevelopment.exe", "Discord Development"),
)
MOVE_HINT = "If bypass doesn't work, try to move Zapret to another directory, for example in C:\\zapret"

_CYRILLIC = re.compile("[\u0430-\u044f\u0410-\u042f\u0451\u0401]")


@dataclass(frozen=True)
class DiagLine:
    """One console line: PrintGreen / PrintYellow / PrintRed / echo / echo:."""

    level: DiagLevel
    text: str = ""


Emit = Callable[[DiagLine], None]
Ask = Callable[[str, bool], bool]


def _remove_tree(path: Path) -> None:
    shutil.rmtree(path, ignore_errors=True)


@dataclass
class DiagProbes:
    """Every way the diagnostics touch the machine."""

    run: Callable[[Sequence[str]], CompletedScm]
    read_reg: Callable[[str, str], object | None]
    doh_count: Callable[[], int]
    env: Mapping[str, str]
    hosts_path: Path
    remove_tree: Callable[[Path], None] = field(default=_remove_tree)


def default_probes() -> DiagProbes:
    system_root = os.environ.get("SystemRoot") or os.environ.get("WINDIR") or r"C:\Windows"
    return DiagProbes(
        run=_run_argv,
        read_reg=_read_user_reg,
        doh_count=_count_doh_flags,
        env=dict(os.environ),
        hosts_path=Path(system_root) / "System32" / "drivers" / "etc" / "hosts",
    )


def run_diagnostics(root: Path, *, probes: DiagProbes, emit: Emit, ask: Ask) -> None:
    """service.bat:412-726, in order, with the bat's text."""
    out = _Out(emit)
    root = Path(root)
    dp0 = _with_sep(str(root))

    out.ok(f"Zapret is installed in: '{dp0}'")
    out.blank()

    _check_bfe(probes, out)
    _check_proxy(probes, out)
    _check_tcp_timestamps(probes, out)
    _check_adguard(probes, out)
    # The bat runs `sc query | findstr` once per check; the listing is the same each time.
    listing = (probes.run(("sc.exe", "query")).stdout or "").splitlines()
    _check_service_listing(listing, out)
    _check_cyrillic(dp0, out)
    _check_onedrive(probes, dp0, out)
    _check_windivert_driver(root, out)
    _check_vpn(listing, out)
    _check_secure_dns(probes, out)
    _check_hosts(probes, out)
    _fix_windivert_conflict(probes, out)
    _remove_conflicting_bypasses(probes, out, ask)
    _clear_discord(probes, out, ask)


class _Out:
    def __init__(self, emit: Emit) -> None:
        self._emit = emit

    def ok(self, text: str) -> None:
        self._emit(DiagLine("ok", text))

    def warn(self, text: str) -> None:
        self._emit(DiagLine("warn", text))

    def fail(self, text: str) -> None:
        self._emit(DiagLine("fail", text))

    def info(self, text: str) -> None:
        self._emit(DiagLine("info", text))

    def blank(self) -> None:
        self._emit(DiagLine("blank"))


def _check_bfe(probes: DiagProbes, out: _Out) -> None:
    state = probes.run(("sc.exe", "query", "BFE")).stdout or ""
    if "RUNNING" in state.upper():
        out.ok("Base Filtering Engine check passed")
    else:
        out.fail("[X] Base Filtering Engine is not running. This service is required for zapret to work")
    out.blank()


def _check_proxy(probes: DiagProbes, out: _Out) -> None:
    if _as_int(probes.read_reg(INTERNET_SETTINGS, "ProxyEnable")) == 1:
        server = probes.read_reg(INTERNET_SETTINGS, "ProxyServer") or ""
        out.warn(f"[?] System proxy is enabled: {server}")
        out.warn("Make sure it's valid or disable it if you don't use a proxy")
    else:
        out.ok("Proxy check passed")
    out.blank()


def _check_tcp_timestamps(probes: DiagProbes, out: _Out) -> None:
    shown = probes.run(build_tcp_probe_command().argv)
    if tcp_timestamps_enabled(shown.stdout):
        out.ok("TCP timestamps check passed")
    else:
        out.warn("[?] TCP timestamps are disabled. Enabling timestamps...")
        if probes.run(build_tcp_enable_command().argv).returncode == 0:
            out.ok("TCP timestamps successfully enabled")
        else:
            out.fail("[X] Failed to enable TCP timestamps")
    out.blank()


def _check_adguard(probes: DiagProbes, out: _Out) -> None:
    if _running(probes, "AdguardSvc.exe"):
        out.fail("[X] Adguard process found. Adguard may cause problems with Discord")
        out.fail("https://github.com/Flowseal/zapret-discord-youtube/issues/417")
    else:
        out.ok("Adguard check passed")
    out.blank()


def _check_service_listing(listing: list[str], out: _Out) -> None:
    lowered = [line.lower() for line in listing]

    def found(*needles: str) -> bool:
        # Chained findstr pipes keep a line only if it holds every needle.
        return any(all(needle in line for needle in needles) for line in lowered)

    if found("killer"):
        out.fail("[X] Killer services found. Killer conflicts with zapret")
        out.fail("https://github.com/Flowseal/zapret-discord-youtube/issues/2512#issuecomment-2821119513")
    else:
        out.ok("Killer check passed")
    out.blank()

    if found("intel", "connectivity", "network"):
        out.fail("[X] Intel Connectivity Network Service found. It conflicts with zapret")
        out.fail("https://github.com/ValdikSS/GoodbyeDPI/issues/541#issuecomment-2661670982")
    else:
        out.ok("Intel Connectivity check passed")
    out.blank()

    if found("tracsrvwrapper") or found("epwd"):
        out.fail("[X] Check Point services found. Check Point conflicts with zapret")
        out.fail("Try to uninstall Check Point")
    else:
        out.ok("Check Point check passed")
    out.blank()

    if found("smartbyte"):
        out.fail("[X] SmartByte services found. SmartByte conflicts with zapret")
        out.fail("Try to uninstall or disable SmartByte through services.msc")
    else:
        out.ok("SmartByte check passed")
    out.blank()


def _check_cyrillic(dp0: str, out: _Out) -> None:
    if _CYRILLIC.search(dp0):
        out.warn("[?] The path where Zapret is installed contains Cyrillic characters")
        out.warn(MOVE_HINT)
    else:
        out.ok("Cyrillic path check passed")
    out.blank()


def _check_onedrive(probes: DiagProbes, dp0: str, out: _Out) -> None:
    onedrive = _env(probes, "OneDrive")
    # The bat builds `findstr /C:"%OneDrive%\\"`. With the variable unset that
    # collapses to a lone backslash and matches every path, so only a real
    # OneDrive folder counts here.
    if onedrive and _with_sep(onedrive).lower() in dp0.lower():
        out.fail("[X] Zapret is installed in a OneDrive folder")
        out.fail(MOVE_HINT)
    else:
        out.ok("OneDrive check passed")
    out.blank()


def _check_windivert_driver(root: Path, out: _Out) -> None:
    if not any((root / "bin").glob("*.sys")):
        out.fail("WinDivert64.sys file NOT found.")
        out.blank()


def _check_vpn(listing: list[str], out: _Out) -> None:
    matches = [line for line in listing if "vpn" in line.lower()]
    if matches:
        # `for /f "tokens=2 delims=:"` — the text between the first and second colon.
        names = ",".join(line.split(":")[1] if ":" in line else "" for line in matches)
        out.warn(f"[?] VPN services found:{names}. Some VPNs can conflict with zapret")
        out.warn("Make sure that all VPNs are disabled")
    else:
        out.ok("VPN check passed")
    out.blank()


def _check_secure_dns(probes: DiagProbes, out: _Out) -> None:
    if probes.doh_count() > 0:
        out.ok("Secure DNS check passed")
    else:
        out.warn(
            "[?] Make sure you have configured secure DNS in a browser with some non-default DNS service provider,"
        )
        out.warn("If you use Windows 11 you can configure encrypted DNS in the Settings to hide this warning")
    out.blank()


def _check_hosts(probes: DiagProbes, out: _Out) -> None:
    try:
        text = probes.hosts_path.read_text(encoding="utf-8", errors="replace").lower()
    except OSError:
        return
    if "youtube.com" in text or "youtu.be" in text:
        out.warn(
            "[?] Your hosts file contains entries for youtube.com or youtu.be. "
            "This may cause problems with YouTube access"
        )


def _fix_windivert_conflict(probes: DiagProbes, out: _Out) -> None:
    winws_running = _running(probes, "winws.exe")
    driver = (probes.run(("sc.exe", "query", "WinDivert")).stdout or "").upper()
    driver_active = "RUNNING" in driver or "STOP_PENDING" in driver
    if winws_running or not driver_active:
        return

    out.warn("[?] winws.exe is not running but WinDivert service is active. Attempting to delete WinDivert...")
    _stop_and_delete(probes, "WinDivert")
    if not _exists(probes, "WinDivert"):
        out.ok("WinDivert successfully removed")
        out.blank()
        return

    out.fail("[X] Failed to delete WinDivert. Checking for conflicting services...")
    removed_any = False
    for name in WINDIVERT_BLOCKERS:
        if not _exists(probes, name):
            continue
        out.warn(f"[?] Found conflicting service: {name}. Stopping and removing...")
        if _stop_and_delete(probes, name) == 0:
            out.ok(f"Successfully removed service: {name}")
        else:
            out.fail(f"[X] Failed to remove service: {name}")
        removed_any = True

    if not removed_any:
        out.fail("[X] No conflicting services found. Check manually if any other bypass is using WinDivert.")
    else:
        out.warn("[?] Attempting to delete WinDivert again...")
        _stop_and_delete(probes, "WinDivert")
        if not _exists(probes, "WinDivert"):
            out.ok("WinDivert successfully deleted after removing conflicting services")
        else:
            out.fail("[X] WinDivert still cannot be deleted. Check manually if any other bypass is using WinDivert.")
    out.blank()


def _remove_conflicting_bypasses(probes: DiagProbes, out: _Out, ask: Ask) -> None:
    found = [name for name in CONFLICTING_BYPASSES if _exists(probes, name)]
    if not found:
        return
    out.fail(f"[X] Conflicting bypass services found: {' '.join(found)}")
    if ask(PROMPT_CONFLICTS, False):
        for name in found:
            out.warn(f"Stopping and removing service: {name}")
            if _stop_and_delete(probes, name) == 0:
                out.ok(f"Successfully removed service: {name}")
            else:
                out.fail(f"[X] Failed to remove service: {name}")
        for driver in ("WinDivert", "WinDivert14"):
            _stop_and_delete(probes, driver)
    out.blank()


def _clear_discord(probes: DiagProbes, out: _Out, ask: Ask) -> None:
    if ask(PROMPT_DISCORD, True):
        appdata = _env(probes, "APPDATA")
        found = False
        for folder, image, label in DISCORD_INSTALLS:
            base = Path(appdata) / folder if appdata else None
            if base is None or not base.is_dir():
                continue
            found = True
            _clear_discord_cache(probes, out, image, label, base)
        if not found:
            out.fail("Discord installations were not found")
    out.blank()


def _clear_discord_cache(probes: DiagProbes, out: _Out, image: str, label: str, base: Path) -> None:
    """service.bat :clear_discord_cache."""
    if _running(probes, image):
        out.info(f"{label} is running, closing...")
        if probes.run(("taskkill.exe", "/IM", image, "/F")).returncode == 0:
            out.ok(f"{label} was successfully closed")
        else:
            out.fail(f"Unable to close {label}")

    for name in DISCORD_CACHE_DIRS:
        path = base / name
        if not path.is_dir():
            out.fail(f"{path} does not exist")
            continue
        probes.remove_tree(path)
        if path.is_dir():
            out.fail(f"Failed to delete {path}")
        else:
            out.ok(f"Successfully deleted {path}")


def _exists(probes: DiagProbes, name: str) -> bool:
    return probes.run(("sc.exe", "query", name)).returncode == 0


def _running(probes: DiagProbes, image: str) -> bool:
    listing = probes.run(("tasklist.exe", "/FI", f"IMAGENAME eq {image}"))
    return image.lower() in (listing.stdout or "").lower()


def _stop_and_delete(probes: DiagProbes, name: str) -> int:
    # `net stop` blocks until the service stops, exactly as the bat relies on.
    probes.run(("net.exe", "stop", name))
    return probes.run(("sc.exe", "delete", name)).returncode


def _env(probes: DiagProbes, name: str) -> str:
    wanted = name.lower()
    for key, value in probes.env.items():
        if key.lower() == wanted:
            return value or ""
    return ""


def _with_sep(path: str) -> str:
    return path if path.endswith(("\\", "/")) else path + "\\"


def _as_int(value: object) -> int | None:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _read_user_reg(subkey: str, name: str) -> object | None:
    try:
        import winreg
    except ImportError:
        return None
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, subkey) as key:
            return winreg.QueryValueEx(key, name)[0]
    except OSError:
        return None


def _count_doh_flags() -> int:
    """Subkeys of Dnscache\\InterfaceSpecificParameters with DohFlags > 0.

    Mirrors `Get-ChildItem -Recurse ... | Where-Object { $_.DohFlags -gt 0 }`:
    descendants only, never the root key itself.
    """
    try:
        import winreg
    except ImportError:
        return 0

    def walk(key) -> int:
        count = 0
        index = 0
        while True:
            try:
                name = winreg.EnumKey(key, index)
            except OSError:
                return count
            index += 1
            try:
                with winreg.OpenKey(key, name) as child:
                    try:
                        value = winreg.QueryValueEx(child, "DohFlags")[0]
                    except OSError:
                        value = 0
                    if isinstance(value, int) and value > 0:
                        count += 1
                    count += walk(child)
            except OSError:
                continue

    try:
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, DOH_PARAMETERS) as root:
            return walk(root)
    except OSError:
        return 0
