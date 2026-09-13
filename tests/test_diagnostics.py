from __future__ import annotations

from pathlib import Path

import pytest

from zapret_gui.diagnostics import (
    DISCORD_CACHE_DIRS,
    PROMPT_CONFLICTS,
    PROMPT_DISCORD,
    DiagLine,
    DiagProbes,
    run_diagnostics,
)
from zapret_gui.services import CompletedScm

BLANK = ("blank", "")
MOVE_HINT = "If bypass doesn't work, try to move Zapret to another directory, for example in C:\\zapret"


class FakeHost:
    """A scriptable machine: services, processes, registry values and a command log."""

    def __init__(self, tmp_path: Path, **overrides) -> None:
        self.calls: list[tuple[str, ...]] = []
        self.services: dict[str, str] = {"BFE": "RUNNING", "WinDivert": "RUNNING"}
        self.processes: set[str] = {"winws.exe"}
        self.listing = "SERVICE_NAME: BFE\nDISPLAY_NAME: Base Filtering Engine\n"
        self.timestamps = "enabled"
        self.enable_rc = 0
        self.proxy: tuple[int, str] = (0, "")
        self.doh = 2
        self.undeletable: dict[str, int] = {}
        self.taskkill_rc = 0
        self.env: dict[str, str] = {"OneDrive": r"E:\OneDrive", "APPDATA": str(tmp_path / "appdata")}
        self.hosts = tmp_path / "hosts"
        self.hosts.write_text("127.0.0.1 localhost\n", encoding="utf-8")
        for key, value in overrides.items():
            setattr(self, key, value)

    def run(self, argv) -> CompletedScm:
        argv = tuple(argv)
        self.calls.append(argv)
        exe = argv[0].lower()
        if argv == ("sc.exe", "query"):
            return CompletedScm(0, self.listing)
        if exe == "sc.exe" and argv[1] == "query":
            state = self.services.get(argv[2])
            if state is None:
                return CompletedScm(1060, "", "[SC] EnumQueryServicesStatus:OpenService FAILED 1060:")
            return CompletedScm(0, f"SERVICE_NAME: {argv[2]}\n        STATE              : 4  {state}\n")
        if exe == "sc.exe" and argv[1] == "delete":
            name = argv[2]
            if name not in self.services:
                return CompletedScm(1060, "", "[SC] OpenService FAILED 1060:")
            if self.undeletable.get(name, 0) > 0:
                self.undeletable[name] -= 1
                return CompletedScm(1072, "", "[SC] DeleteService FAILED 1072:")
            del self.services[name]
            return CompletedScm(0, "[SC] DeleteService SUCCESS")
        if exe == "net.exe" and argv[1] == "stop":
            if argv[2] in self.services:
                self.services[argv[2]] = "STOPPED"
            return CompletedScm(0, "The service was stopped successfully.")
        if exe == "netsh.exe" and "show" in argv:
            return CompletedScm(0, f"RFC 1323 Timestamps                 : {self.timestamps}\n")
        if exe == "netsh.exe" and "set" in argv:
            return CompletedScm(self.enable_rc, "Ok.")
        if exe == "tasklist.exe":
            image = argv[2].split("eq ", 1)[1]
            if image in self.processes:
                return CompletedScm(0, f"Image Name   PID\n{image}   4242 Console\n")
            return CompletedScm(0, "INFO: No tasks are running which match the specified criteria.\n")
        if exe == "taskkill.exe":
            self.processes.discard(argv[2])
            return CompletedScm(self.taskkill_rc, "SUCCESS")
        raise AssertionError(f"unexpected command {argv!r}")

    def read_reg(self, _subkey: str, name: str) -> object | None:
        enabled, server = self.proxy
        return {"ProxyEnable": enabled, "ProxyServer": server}.get(name)

    def probes(self) -> DiagProbes:
        return DiagProbes(
            run=self.run,
            read_reg=self.read_reg,
            doh_count=lambda: self.doh,
            env=self.env,
            hosts_path=self.hosts,
        )

    def mutations(self) -> list[tuple[str, ...]]:
        return [
            call
            for call in self.calls
            if call[0] in ("net.exe", "taskkill.exe")
            or (call[0] == "sc.exe" and call[1] == "delete")
            or (call[0] == "netsh.exe" and "set" in call)
        ]


def _run(root: Path, host: FakeHost, answers: dict[str, bool] | None = None):
    lines: list[DiagLine] = []
    asked: list[tuple[str, bool]] = []
    replies = answers or {}

    def ask(key: str, default: bool) -> bool:
        asked.append((key, default))
        return replies.get(key, False)

    run_diagnostics(root, probes=host.probes(), emit=lines.append, ask=ask)
    return [(line.level, line.text) for line in lines], asked


@pytest.fixture
def root(tmp_path: Path) -> Path:
    tree = tmp_path / "zapret"
    (tree / "bin").mkdir(parents=True)
    (tree / "bin" / "WinDivert64.sys").write_bytes(b"\0")
    return tree


def test_all_clear_run_matches_service_bat_line_for_line(root: Path, tmp_path: Path) -> None:
    host = FakeHost(tmp_path)
    lines, asked = _run(root, host)
    assert lines == [
        ("ok", f"Zapret is installed in: '{root}\\'"),
        BLANK,
        ("ok", "Base Filtering Engine check passed"),
        BLANK,
        ("ok", "Proxy check passed"),
        BLANK,
        ("ok", "TCP timestamps check passed"),
        BLANK,
        ("ok", "Adguard check passed"),
        BLANK,
        ("ok", "Killer check passed"),
        BLANK,
        ("ok", "Intel Connectivity check passed"),
        BLANK,
        ("ok", "Check Point check passed"),
        BLANK,
        ("ok", "SmartByte check passed"),
        BLANK,
        ("ok", "Cyrillic path check passed"),
        BLANK,
        ("ok", "OneDrive check passed"),
        BLANK,
        ("ok", "VPN check passed"),
        BLANK,
        ("ok", "Secure DNS check passed"),
        BLANK,
        # The Discord block always ends with `echo:`, answered or not.
        BLANK,
    ]
    assert asked == [(PROMPT_DISCORD, True)]
    assert host.mutations() == []


@pytest.mark.parametrize(
    "overrides, expected",
    [
        (
            {"services": {"BFE": "STOPPED", "WinDivert": "RUNNING"}},
            [("fail", "[X] Base Filtering Engine is not running. This service is required for zapret to work")],
        ),
        (
            {"proxy": (1, "127.0.0.1:8080")},
            [
                ("warn", "[?] System proxy is enabled: 127.0.0.1:8080"),
                ("warn", "Make sure it's valid or disable it if you don't use a proxy"),
            ],
        ),
        (
            {"timestamps": "disabled"},
            [
                ("warn", "[?] TCP timestamps are disabled. Enabling timestamps..."),
                ("ok", "TCP timestamps successfully enabled"),
            ],
        ),
        (
            {"timestamps": "disabled", "enable_rc": 1},
            [
                ("warn", "[?] TCP timestamps are disabled. Enabling timestamps..."),
                ("fail", "[X] Failed to enable TCP timestamps"),
            ],
        ),
        (
            {"processes": {"winws.exe", "AdguardSvc.exe"}},
            [
                ("fail", "[X] Adguard process found. Adguard may cause problems with Discord"),
                ("fail", "https://github.com/Flowseal/zapret-discord-youtube/issues/417"),
            ],
        ),
        (
            {"listing": "SERVICE_NAME: KillerNetworkService\n"},
            [
                ("fail", "[X] Killer services found. Killer conflicts with zapret"),
                ("fail", "https://github.com/Flowseal/zapret-discord-youtube/issues/2512#issuecomment-2821119513"),
            ],
        ),
        (
            {"listing": "DISPLAY_NAME: Intel(R) Connectivity Network Service\n"},
            [
                ("fail", "[X] Intel Connectivity Network Service found. It conflicts with zapret"),
                ("fail", "https://github.com/ValdikSS/GoodbyeDPI/issues/541#issuecomment-2661670982"),
            ],
        ),
        (
            {"listing": "SERVICE_NAME: EPWD\n"},
            [
                ("fail", "[X] Check Point services found. Check Point conflicts with zapret"),
                ("fail", "Try to uninstall Check Point"),
            ],
        ),
        (
            {"listing": "SERVICE_NAME: SmartByteTelemetry\n"},
            [
                ("fail", "[X] SmartByte services found. SmartByte conflicts with zapret"),
                ("fail", "Try to uninstall or disable SmartByte through services.msc"),
            ],
        ),
        (
            {"listing": "SERVICE_NAME: NordVPN\nDISPLAY_NAME: NordVPN Service\n"},
            [
                ("warn", "[?] VPN services found: NordVPN, NordVPN Service. Some VPNs can conflict with zapret"),
                ("warn", "Make sure that all VPNs are disabled"),
            ],
        ),
        (
            {"doh": 0},
            [
                (
                    "warn",
                    "[?] Make sure you have configured secure DNS in a browser with some non-default DNS service provider,",
                ),
                ("warn", "If you use Windows 11 you can configure encrypted DNS in the Settings to hide this warning"),
            ],
        ),
    ],
)
def test_each_failing_check_prints_service_bat_text(root: Path, tmp_path: Path, overrides, expected) -> None:
    host = FakeHost(tmp_path, **overrides)
    lines, _ = _run(root, host)
    start = lines.index(expected[0])
    assert lines[start : start + len(expected)] == expected
    assert lines[start + len(expected)] == BLANK


def test_hosts_youtube_entry_warns_without_a_passed_line(root: Path, tmp_path: Path) -> None:
    host = FakeHost(tmp_path)
    host.hosts.write_text("127.0.0.1 youtube.com\n", encoding="utf-8")
    lines, _ = _run(root, host)
    secure = lines.index(("ok", "Secure DNS check passed"))
    assert lines[secure + 2] == (
        "warn",
        "[?] Your hosts file contains entries for youtube.com or youtu.be. This may cause problems with YouTube access",
    )


def test_cyrillic_install_path_warns(tmp_path: Path) -> None:
    tree = tmp_path / "\u0437\u0430\u043f\u0440\u0435\u0442"
    (tree / "bin").mkdir(parents=True)
    (tree / "bin" / "WinDivert64.sys").write_bytes(b"\0")
    lines, _ = _run(tree, FakeHost(tmp_path))
    at = lines.index(("warn", "[?] The path where Zapret is installed contains Cyrillic characters"))
    assert lines[at + 1] == ("warn", MOVE_HINT)


def test_onedrive_install_path_fails_case_insensitively(root: Path, tmp_path: Path) -> None:
    host = FakeHost(tmp_path, env={"ONEDRIVE": str(tmp_path).upper(), "APPDATA": str(tmp_path / "appdata")})
    lines, _ = _run(root, host)
    at = lines.index(("fail", "[X] Zapret is installed in a OneDrive folder"))
    assert lines[at + 1] == ("fail", MOVE_HINT)


def test_unset_onedrive_does_not_flag_every_path(root: Path, tmp_path: Path) -> None:
    """The bat's findstr collapses to a lone backslash here; the port does not copy that."""
    host = FakeHost(tmp_path, env={"APPDATA": str(tmp_path / "appdata")})
    lines, _ = _run(root, host)
    assert ("ok", "OneDrive check passed") in lines


def test_missing_driver_file_is_reported_in_place(tmp_path: Path) -> None:
    tree = tmp_path / "zapret"
    (tree / "bin").mkdir(parents=True)
    lines, _ = _run(tree, FakeHost(tmp_path))
    at = lines.index(("fail", "WinDivert64.sys file NOT found."))
    assert lines[at + 1] == BLANK
    assert lines[at - 2] == ("ok", "OneDrive check passed")


def _windivert_block(lines: list) -> list:
    start = lines.index(
        ("warn", "[?] winws.exe is not running but WinDivert service is active. Attempting to delete WinDivert...")
    )
    end = lines.index(BLANK, start)
    return lines[start + 1 : end + 1]


def test_windivert_is_removed_when_winws_is_not_running(root: Path, tmp_path: Path) -> None:
    host = FakeHost(tmp_path, processes=set())
    lines, _ = _run(root, host)
    assert _windivert_block(lines) == [("ok", "WinDivert successfully removed"), BLANK]
    assert ("net.exe", "stop", "WinDivert") in host.calls
    assert "WinDivert" not in host.services


def test_windivert_retry_after_removing_goodbyedpi(root: Path, tmp_path: Path) -> None:
    host = FakeHost(tmp_path, processes=set(), undeletable={"WinDivert": 1})
    host.services["GoodbyeDPI"] = "RUNNING"
    lines, _ = _run(root, host)
    assert _windivert_block(lines) == [
        ("fail", "[X] Failed to delete WinDivert. Checking for conflicting services..."),
        ("warn", "[?] Found conflicting service: GoodbyeDPI. Stopping and removing..."),
        ("ok", "Successfully removed service: GoodbyeDPI"),
        ("warn", "[?] Attempting to delete WinDivert again..."),
        ("ok", "WinDivert successfully deleted after removing conflicting services"),
        BLANK,
    ]


def test_windivert_retry_that_still_fails(root: Path, tmp_path: Path) -> None:
    host = FakeHost(tmp_path, processes=set(), undeletable={"WinDivert": 2})
    host.services["GoodbyeDPI"] = "RUNNING"
    lines, _ = _run(root, host)
    assert _windivert_block(lines)[-2:] == [
        ("fail", "[X] WinDivert still cannot be deleted. Check manually if any other bypass is using WinDivert."),
        BLANK,
    ]


def test_windivert_stuck_with_no_known_blocker(root: Path, tmp_path: Path) -> None:
    host = FakeHost(tmp_path, processes=set(), undeletable={"WinDivert": 1})
    lines, _ = _run(root, host)
    assert _windivert_block(lines) == [
        ("fail", "[X] Failed to delete WinDivert. Checking for conflicting services..."),
        ("fail", "[X] No conflicting services found. Check manually if any other bypass is using WinDivert."),
        BLANK,
    ]


def test_declined_prompts_change_nothing(root: Path, tmp_path: Path) -> None:
    host = FakeHost(tmp_path)
    host.services["GoodbyeDPI"] = "RUNNING"
    host.processes.add("DiscordPTB.exe")
    ptb = Path(host.env["APPDATA"]) / "discordptb"
    for name in DISCORD_CACHE_DIRS:
        (ptb / name).mkdir(parents=True)

    lines, asked = _run(root, host, answers={})
    # service.bat's defaults: conflicts N, Discord cache Y.
    assert asked == [(PROMPT_CONFLICTS, False), (PROMPT_DISCORD, True)]
    assert ("fail", "[X] Conflicting bypass services found: GoodbyeDPI") in lines
    assert host.mutations() == []
    assert "GoodbyeDPI" in host.services
    assert "DiscordPTB.exe" in host.processes
    assert all((ptb / name).is_dir() for name in DISCORD_CACHE_DIRS)


def test_accepted_conflict_prompt_removes_services_then_windivert(root: Path, tmp_path: Path) -> None:
    host = FakeHost(tmp_path)
    host.services.update({"GoodbyeDPI": "RUNNING", "winws1": "RUNNING"})
    lines, _ = _run(root, host, answers={PROMPT_CONFLICTS: True})
    start = lines.index(("fail", "[X] Conflicting bypass services found: GoodbyeDPI winws1"))
    assert lines[start : start + 6] == [
        ("fail", "[X] Conflicting bypass services found: GoodbyeDPI winws1"),
        ("warn", "Stopping and removing service: GoodbyeDPI"),
        ("ok", "Successfully removed service: GoodbyeDPI"),
        ("warn", "Stopping and removing service: winws1"),
        ("ok", "Successfully removed service: winws1"),
        BLANK,
    ]
    assert ("sc.exe", "delete", "WinDivert") in host.calls
    assert ("sc.exe", "delete", "WinDivert14") in host.calls
    assert "GoodbyeDPI" not in host.services and "winws1" not in host.services


def test_accepted_discord_prompt_closes_the_client_and_clears_caches(root: Path, tmp_path: Path) -> None:
    host = FakeHost(tmp_path)
    host.processes.add("DiscordPTB.exe")
    ptb = Path(host.env["APPDATA"]) / "discordptb"
    (ptb / "Cache").mkdir(parents=True)
    (ptb / "Cache" / "data_0").write_bytes(b"x")
    (ptb / "GPUCache").mkdir()

    lines, _ = _run(root, host, answers={PROMPT_DISCORD: True})
    assert lines[-6:] == [
        ("info", "Discord PTB is running, closing..."),
        ("ok", "Discord PTB was successfully closed"),
        ("ok", f"Successfully deleted {ptb / 'Cache'}"),
        ("fail", f"{ptb / 'Code Cache'} does not exist"),
        ("ok", f"Successfully deleted {ptb / 'GPUCache'}"),
        BLANK,
    ]
    assert ("taskkill.exe", "/IM", "DiscordPTB.exe", "/F") in host.calls
    assert not (ptb / "Cache").exists() and not (ptb / "GPUCache").exists()


def test_accepted_discord_prompt_with_nothing_installed(root: Path, tmp_path: Path) -> None:
    lines, _ = _run(root, FakeHost(tmp_path), answers={PROMPT_DISCORD: True})
    assert lines[-2:] == [("fail", "Discord installations were not found"), BLANK]


def test_failed_cache_delete_is_reported(root: Path, tmp_path: Path) -> None:
    host = FakeHost(tmp_path)
    stable = Path(host.env["APPDATA"]) / "discord"
    for name in DISCORD_CACHE_DIRS:
        (stable / name).mkdir(parents=True)
    probes = host.probes()
    probes.remove_tree = lambda _path: None  # a locked cache survives `rd /s /q`

    lines: list[DiagLine] = []
    run_diagnostics(root, probes=probes, emit=lines.append, ask=lambda key, default: key == PROMPT_DISCORD)
    texts = [(line.level, line.text) for line in lines]
    assert ("fail", f"Failed to delete {stable / 'Cache'}") in texts
