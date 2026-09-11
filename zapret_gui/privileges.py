"""Elevation probe and elevated-Notepad launcher for the Windows hosts file."""

from __future__ import annotations

import ctypes
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from zapret_gui import HOSTS_PATH

SW_SHOWNORMAL = 1
ERROR_CANCELLED = 1223
SE_ERR_FNF = 2
SE_ERR_PNF = 3
SE_ERR_ACCESSDENIED = 5
SE_ERR_OOM = 8
SE_ERR_DLLNOTFOUND = 32
SE_ERR_SHARE = 26
SE_ERR_ASSOCINCOMPLETE = 27
SE_ERR_DDETIMEOUT = 28
SE_ERR_DDEFAIL = 29
SE_ERR_DDEBUSY = 30
SE_ERR_NOASSOC = 31


class PrivilegeError(RuntimeError):
    """Elevation or ShellExecute failed in a way the GUI should display, not raise past."""


@dataclass(frozen=True)
class HostsLaunchPlan:
    """The constructed action: notepad + hosts path + elevation verb."""

    file: str
    params: str
    verb: str
    notepad_path: str
    hosts_path: str
    missing_notepad: bool
    missing_hosts: bool

    @property
    def uses_elevation(self) -> bool:
        return self.verb.lower() == "runas"

    @property
    def can_launch(self) -> bool:
        return not self.missing_notepad and not self.missing_hosts


@dataclass(frozen=True)
class HostsLaunchResult:
    ok: bool
    plan: HostsLaunchPlan
    error: str | None = None
    native_code: int | None = None


Executor = Callable[[HostsLaunchPlan], int]


def is_process_elevated() -> bool:
    """Return a definite True/False for the current process token."""
    if sys.platform != "win32":
        return False
    try:
        return bool(ctypes.windll.shell32.IsUserAnAdmin())
    except Exception:
        return False


def default_notepad_path() -> str:
    windir = os.environ.get("WINDIR", r"C:\Windows")
    return str(Path(windir) / "System32" / "notepad.exe")


def default_hosts_path() -> str:
    return HOSTS_PATH


def build_hosts_launch_plan(
    *,
    elevated: bool | None = None,
    notepad_path: str | None = None,
    hosts_path: str | None = None,
) -> HostsLaunchPlan:
    """Build the ShellExecute action. Never raises.

    If the GUI process is not elevated, the verb is always ``runas`` so hosts
    cannot be opened unelevated. An already-elevated process uses ``open``.
    """
    notepad = notepad_path or default_notepad_path()
    hosts = hosts_path or default_hosts_path()
    if elevated is None:
        elevated = is_process_elevated()
    verb = "open" if elevated else "runas"
    return HostsLaunchPlan(
        file=notepad,
        params=hosts,
        verb=verb,
        notepad_path=notepad,
        hosts_path=hosts,
        missing_notepad=not Path(notepad).is_file(),
        missing_hosts=not Path(hosts).is_file(),
    )


def launch_hosts_notepad(
    *,
    executor: Executor | None = None,
    elevated: bool | None = None,
    notepad_path: str | None = None,
    hosts_path: str | None = None,
) -> HostsLaunchResult:
    """Launch elevated Notepad on the hosts file. Failures are returned, not raised."""
    plan = build_hosts_launch_plan(
        elevated=elevated,
        notepad_path=notepad_path,
        hosts_path=hosts_path,
    )
    if plan.missing_notepad:
        return HostsLaunchResult(
            ok=False,
            plan=plan,
            error=f"Notepad was not found at {plan.notepad_path}",
        )
    if plan.missing_hosts:
        return HostsLaunchResult(
            ok=False,
            plan=plan,
            error=f"Hosts file was not found at {plan.hosts_path}",
        )
    fn = executor if executor is not None else _default_executor
    try:
        native = fn(plan)
    except Exception as exc:
        return HostsLaunchResult(
            ok=False,
            plan=plan,
            error=f"Failed to launch elevated Notepad: {exc}",
        )
    if native is not None and int(native) <= 32:
        return HostsLaunchResult(
            ok=False,
            plan=plan,
            error=describe_shellexecute_failure(int(native)),
            native_code=int(native),
        )
    return HostsLaunchResult(ok=True, plan=plan, native_code=int(native) if native else None)


def build_relaunch_plan(project_root: Path | None = None) -> HostsLaunchPlan:
    """Relaunch this GUI elevated via ``python -m zapret_gui``."""
    python = sys.executable
    params = "-m zapret_gui"
    return HostsLaunchPlan(
        file=python,
        params=params,
        verb="runas",
        notepad_path=python,
        hosts_path=str(project_root) if project_root else "",
        missing_notepad=not Path(python).is_file(),
        missing_hosts=False,
    )


def relaunch_self_elevated(
    *,
    executor: Executor | None = None,
    project_root: Path | None = None,
) -> HostsLaunchResult:
    plan = build_relaunch_plan(project_root)
    if not plan.can_launch:
        return HostsLaunchResult(ok=False, plan=plan, error="Python interpreter not found")
    fn = executor if executor is not None else _default_executor
    try:
        native = fn(plan)
    except Exception as exc:
        return HostsLaunchResult(ok=False, plan=plan, error=str(exc))
    if native is not None and int(native) <= 32:
        return HostsLaunchResult(
            ok=False,
            plan=plan,
            error=describe_shellexecute_failure(int(native)),
            native_code=int(native),
        )
    return HostsLaunchResult(ok=True, plan=plan, native_code=int(native) if native else None)


def shell_execute(file: str, params: str, verb: str, directory: str | None = None) -> int:
    """ctypes ShellExecuteW. Return value > 32 means success (Windows convention)."""
    if sys.platform != "win32":
        raise PrivilegeError("ShellExecute is only available on Windows")
    shell32 = ctypes.windll.shell32
    kernel32 = ctypes.windll.kernel32
    kernel32.SetLastError(0)
    rc = shell32.ShellExecuteW(None, verb, file, params, directory, SW_SHOWNORMAL)
    return int(rc)


def _default_executor(plan: HostsLaunchPlan) -> int:
    return shell_execute(plan.file, plan.params, plan.verb)


def describe_shellexecute_failure(code: int) -> str:
    mapping = {
        0: "out of memory or ShellExecute could not start",
        SE_ERR_FNF: "file not found",
        SE_ERR_PNF: "path not found",
        SE_ERR_ACCESSDENIED: "access denied or UAC elevation was refused",
        SE_ERR_OOM: "out of memory",
        SE_ERR_DLLNOTFOUND: "dynamic-link library not found",
        SE_ERR_SHARE: "sharing violation",
        SE_ERR_ASSOCINCOMPLETE: "file association is incomplete",
        SE_ERR_DDETIMEOUT: "DDE timed out",
        SE_ERR_DDEFAIL: "DDE failed",
        SE_ERR_DDEBUSY: "DDE is busy",
        SE_ERR_NOASSOC: "no file association",
        ERROR_CANCELLED: "UAC elevation was cancelled",
    }
    detail = mapping.get(code, f"ShellExecute error {code}")
    if code == SE_ERR_ACCESSDENIED:
        last = _get_last_error()
        if last == ERROR_CANCELLED:
            return "UAC elevation was cancelled"
        if last:
            return f"{detail} (GetLastError={last})"
    return detail


def _get_last_error() -> int:
    try:
        return int(ctypes.windll.kernel32.GetLastError())
    except Exception:
        return 0
