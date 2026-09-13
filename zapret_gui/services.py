"""Windows Service Control Manager adapter for the zapret / winws.exe service.

Live ``sc create`` / ``sc start`` / ``sc stop`` of winws/WinDivert is gated behind
``allow_live=True``. Tests must inject a runner or omit that flag so the host
SCM is never mutated during verification.

The live install walks ``install_plan()``, which mirrors ``service.bat``
``:service_install`` step for step: tcp timestamps, stop, delete, create,
description, start. ``sc create ... start= auto`` only sets the *boot* start
type, so the explicit ``sc start`` is what actually brings winws up.
"""

from __future__ import annotations

import functools
import re
import subprocess
import sys
import time
from dataclasses import dataclass, replace
from pathlib import Path, PureWindowsPath
from typing import Callable, Literal

from zapret_gui import SERVICE_NAME, STRATEGY_VALUE_NAME
from zapret_gui.identity import read_installed_strategy
from zapret_gui.privileges import is_process_elevated, shell_execute

ServiceState = Literal["running", "stopped", "not_installed", "error"]
ScmAction = Literal["install", "start", "stop", "delete", "status", "describe", "tcp"]
StepKind = Literal[
    "tcp", "stop", "delete", "create", "describe", "start", "verify", "identity", "taskkill"
]

# Windows ``sc`` / OpenService / StartService
ERROR_FILE_NOT_FOUND = 2
ERROR_ACCESS_DENIED = 5
ERROR_SERVICE_REQUEST_TIMEOUT = 1053
ERROR_SERVICE_ALREADY_RUNNING = 1056
ERROR_SERVICE_DISABLED = 1058
ERROR_SERVICE_DOES_NOT_EXIST = 1060
ERROR_SERVICE_NOT_ACTIVE = 1062
ERROR_SERVICE_MARKED_FOR_DELETE = 1072
ERROR_SERVICE_EXISTS = 1073
ERROR_SERVICE_DEPENDENCY_DELETED = 1075
ERROR_SERVICE_NEVER_STARTED = 1077
ERROR_DUPLICATE_SERVICE_NAME = 1078
ERROR_TASKKILL_NOT_FOUND = 128

# stop/delete during replace: not-installed / not-started / already-deleting are not failures
_REPLACE_STOP_OK = frozenset({0, ERROR_SERVICE_DOES_NOT_EXIST, ERROR_SERVICE_NOT_ACTIVE})
_REPLACE_DELETE_OK = frozenset({0, ERROR_SERVICE_DOES_NOT_EXIST, ERROR_SERVICE_MARKED_FOR_DELETE})
# service.bat issues a bare ``sc start``; a service already up is not a failure.
_START_OK = frozenset({0, ERROR_SERVICE_ALREADY_RUNNING})
_TASKKILL_OK = frozenset({0, 1, ERROR_TASKKILL_NOT_FOUND})

# `net stop` blocks until STOPPED; `sc stop` returns at STOP_PENDING. These bound
# the waits that stand in for that blocking.
STOP_SETTLE_S = 20.0
DELETE_SETTLE_S = 10.0

# service.bat:357
SERVICE_DESCRIPTION = "Zapret DPI bypass software"

_CREATE_NO_WINDOW = 0x08000000
_PID_LINE = re.compile(r"^\s*PID\s*:\s*(\d+)", re.IGNORECASE | re.MULTILINE)
_EXIT_CODE_LINE = re.compile(r"WIN32_EXIT_CODE\s*:\s*(\d+)", re.IGNORECASE)

SC_ERRORS: dict[int, str] = {
    ERROR_FILE_NOT_FOUND: "the winws.exe image was not found",
    ERROR_ACCESS_DENIED: "access denied - this process is not elevated",
    ERROR_SERVICE_REQUEST_TIMEOUT: "the service did not respond to the start request in time",
    ERROR_SERVICE_ALREADY_RUNNING: "the service is already running",
    ERROR_SERVICE_DISABLED: "the service is disabled",
    ERROR_SERVICE_DOES_NOT_EXIST: "the service is not installed",
    ERROR_SERVICE_NOT_ACTIVE: "the service has not been started",
    ERROR_SERVICE_MARKED_FOR_DELETE: (
        "the service is marked for deletion - it disappears once it has stopped "
        "and no process holds a handle to it"
    ),
    ERROR_SERVICE_EXISTS: "the service already exists",
    ERROR_SERVICE_DEPENDENCY_DELETED: "a service dependency was deleted",
    ERROR_SERVICE_NEVER_STARTED: "the service has never been started",
    ERROR_DUPLICATE_SERVICE_NAME: "another service already uses this name or display name",
}


def describe_sc_error(code: int) -> str:
    """Human text for an ``sc``/SCM exit code. Mirrors describe_shellexecute_failure."""
    try:
        value = int(code)
    except (TypeError, ValueError):
        return f"exit {code}"
    if value == 0:
        return "success"
    return SC_ERRORS.get(value) or f"exit code {value}"


def _sc_hint(code: int, detail: str) -> str:
    """The trailing '` - <explanation>`', omitted when there is nothing to add."""
    text = SC_ERRORS.get(int(code))
    if not text or text in detail:
        return ""
    return f" - {text}"


@dataclass(frozen=True)
class ScmCommand:
    action: ScmAction
    argv: tuple[str, ...]
    command_line: str
    service_name: str
    image: str = ""
    args: str = ""
    elevation_verb: str = "runas"

    def uses_image_args(self) -> bool:
        if self.action != "install":
            return False
        blob = " ".join(self.argv) + " " + self.command_line
        return bool(self.image) and self.image in blob and bool(self.args) and self.args in blob


@dataclass
class CompletedScm:
    returncode: int
    stdout: str = ""
    stderr: str = ""


@dataclass(frozen=True)
class ServiceStatus:
    state: ServiceState
    service_name: str
    message: str
    raw: str = ""
    returncode: int | None = None
    strategy_name: str | None = None


_SNAPSHOT_STATES = {
    "RUNNING": "running",
    "START_PENDING": "pending",
    "CONTINUE_PENDING": "pending",
    "STOP_PENDING": "pending",
    "PAUSE_PENDING": "pending",
    "STOPPED": "stopped",
    "PAUSED": "stopped",
    "NOT_INSTALLED": "not_installed",
}
_SCM_STATE_TOKENS = {
    1: "STOPPED",
    2: "START_PENDING",
    3: "STOP_PENDING",
    4: "RUNNING",
    5: "CONTINUE_PENDING",
    6: "PAUSE_PENDING",
    7: "PAUSED",
}


@dataclass(frozen=True)
class ServiceSnapshot:
    """What the always-on status line shows, read straight from the SCM."""

    service_name: str
    token: str  # RUNNING / START_PENDING / ... / NOT_INSTALLED / ERROR
    pid: int | None = None
    exit_code: int = 0
    image: str = ""
    strategy: str | None = None
    error: str = ""

    @property
    def state(self) -> str:
        """running / pending / stopped / not_installed / error."""
        return _SNAPSHOT_STATES.get(self.token, "error")


@dataclass(frozen=True)
class StepReport:
    """One executed step of a multi-step SCM action, as the console renders it."""

    kind: StepKind
    command_line: str
    returncode: int
    stdout: str = ""
    stderr: str = ""
    ok: bool = True
    fatal: bool = True
    note: str = ""

    @property
    def detail(self) -> str:
        return (self.stdout or self.stderr or "").strip()


@dataclass(frozen=True)
class PlanStep:
    kind: StepKind
    command: ScmCommand
    ok_codes: frozenset[int]
    fatal: bool = True


@dataclass(frozen=True)
class ServiceWait:
    """Outcome of polling SCM until the service reaches a target state."""

    status: ServiceStatus
    reached: bool
    pid: int | None = None
    polls: int = 0
    # Raw SCM token, e.g. RUNNING / START_PENDING. map_sc_query folds the pending
    # tokens into running/stopped, which is right for a status label and wrong
    # for a completion check.
    token: str = ""


@dataclass(frozen=True)
class ScmOpResult:
    ok: bool
    command: ScmCommand
    status: ServiceStatus | None = None
    error: str | None = None
    executed: bool = False
    completed: CompletedScm | None = None
    needs_elevation: bool = False
    steps: tuple[StepReport, ...] = ()
    # True only when a query actually observed the intended state. A UAC-delegated
    # action cannot be observed from here, so it stays False.
    verified: bool = False
    pid: int | None = None


ScmRunner = Callable[[ScmCommand], CompletedScm]
StepObserver = Callable[[StepReport], None]
Waiter = Callable[..., ServiceWait]


class ScmError(RuntimeError):
    """SCM adapter failure intended for UI display."""


def sc_executable() -> str:
    return "sc.exe"


def build_install_command(
    image: str,
    args: str,
    service_name: str = SERVICE_NAME,
) -> ScmCommand:
    """``sc create`` whose binPath is winws.exe plus the resolved strategy args."""
    if not image:
        raise ScmError("winws.exe image path is empty")
    bin_path_value = f'"{image}" {args}'.strip()
    # service.bat hardcodes DisplayName= "zapret" because it only ever manages
    # that one service. Deriving it from service_name is identical for the
    # default, and avoids 1078 (name already in use) for any other name.
    display_name = service_name
    argv = (
        sc_executable(),
        "create",
        service_name,
        "binPath=",
        bin_path_value,
        "DisplayName=",
        display_name,
        "start=",
        "auto",
    )
    # Faithful to service.bat: sc create NAME binPath= "\"IMAGE\" ARGS" ...
    command_line = (
        f'{sc_executable()} create {service_name} '
        f'binPath= "\\"{image}\\" {args}" '
        f'DisplayName= "{display_name}" start= auto'
    )
    return ScmCommand(
        action="install",
        argv=argv,
        command_line=command_line,
        service_name=service_name,
        image=image,
        args=args,
    )


def build_start_command(service_name: str = SERVICE_NAME, *, image: str = "", args: str = "") -> ScmCommand:
    argv = (sc_executable(), "start", service_name)
    return ScmCommand(
        action="start",
        argv=argv,
        command_line=" ".join(argv),
        service_name=service_name,
        image=image,
        args=args,
    )


def build_stop_command(service_name: str = SERVICE_NAME, *, image: str = "", args: str = "") -> ScmCommand:
    argv = (sc_executable(), "stop", service_name)
    return ScmCommand(
        action="stop",
        argv=argv,
        command_line=" ".join(argv),
        service_name=service_name,
        image=image,
        args=args,
    )


def build_delete_command(service_name: str = SERVICE_NAME) -> ScmCommand:
    argv = (sc_executable(), "delete", service_name)
    return ScmCommand(
        action="delete",
        argv=argv,
        command_line=" ".join(argv),
        service_name=service_name,
    )


def build_describe_command(
    service_name: str = SERVICE_NAME,
    text: str = SERVICE_DESCRIPTION,
) -> ScmCommand:
    """service.bat:357 — ``sc description zapret "Zapret DPI bypass software"``."""
    argv = (sc_executable(), "description", service_name, text)
    return ScmCommand(
        action="describe",
        argv=argv,
        command_line=f'{sc_executable()} description {service_name} "{text}"',
        service_name=service_name,
    )


def build_status_command(service_name: str = SERVICE_NAME) -> ScmCommand:
    argv = (sc_executable(), "query", service_name)
    return ScmCommand(
        action="status",
        argv=argv,
        command_line=" ".join(argv),
        service_name=service_name,
        elevation_verb="open",
    )


def build_queryex_command(service_name: str = SERVICE_NAME) -> ScmCommand:
    """``sc queryex`` — same STATE line as ``sc query`` plus the winws PID."""
    argv = (sc_executable(), "queryex", service_name)
    return ScmCommand(
        action="status",
        argv=argv,
        command_line=" ".join(argv),
        service_name=service_name,
        elevation_verb="open",
    )


def build_tcp_probe_command(service_name: str = SERVICE_NAME) -> ScmCommand:
    """service.bat:136 — read the global TCP timestamps setting."""
    argv = ("netsh.exe", "interface", "tcp", "show", "global")
    return ScmCommand(
        action="tcp",
        argv=argv,
        command_line=" ".join(argv),
        service_name=service_name,
        elevation_verb="open",
    )


def build_tcp_enable_command(service_name: str = SERVICE_NAME) -> ScmCommand:
    """service.bat:136 — ``netsh interface tcp set global timestamps=enabled``."""
    argv = ("netsh.exe", "interface", "tcp", "set", "global", "timestamps=enabled")
    return ScmCommand(
        action="tcp",
        argv=argv,
        command_line=" ".join(argv),
        service_name=service_name,
    )


def tcp_timestamps_enabled(stdout: str) -> bool:
    """True when ``netsh interface tcp show global`` reports timestamps enabled."""
    for line in (stdout or "").splitlines():
        lowered = line.lower()
        if "timestamp" in lowered and "enabled" in lowered:
            return True
    return False


def extract_pid(stdout: str) -> int | None:
    """Pull ``PID : 24188`` out of ``sc queryex``. A stopped service reports 0."""
    match = _PID_LINE.search(stdout or "")
    if not match:
        return None
    return int(match.group(1)) or None


def exit_code_hint(raw: str) -> str:
    """Explain a stopped service's WIN32_EXIT_CODE, e.g. 1077 never-started."""
    match = _EXIT_CODE_LINE.search(raw or "")
    if not match:
        return ""
    code = int(match.group(1))
    if code == 0:
        return ""
    return f"WIN32_EXIT_CODE {code} - {describe_sc_error(code)}"


def image_from_image_path(image_path: str) -> str:
    """The executable inside a service ImagePath: ``"C:\\x\\winws.exe" args`` → ``C:\\x\\winws.exe``."""
    text = (image_path or "").strip()
    if not text:
        return ""
    if text.startswith('"'):
        end = text.find('"', 1)
        return text[1:end] if end > 0 else text[1:]
    match = re.match(r"(.+?\.exe)(?:\s|$)", text, re.IGNORECASE)
    return match.group(1) if match else text.split()[0]


def image_install_root(image: str) -> str:
    """``E:\\.ZAPRET\\bin\\winws.exe`` → ``E:\\.ZAPRET``: the tree a winws image belongs to."""
    parent = PureWindowsPath(image).parent
    return str(parent.parent if parent.name.lower() == "bin" else parent)


def image_is_under(image: str, root: Path) -> bool:
    """Case-insensitive, whole-component containment (``...-project-old`` is not ``...-project``)."""
    image_parts = [part.lower() for part in PureWindowsPath(image).parts]
    root_parts = [part.lower() for part in PureWindowsPath(str(root)).parts]
    return bool(root_parts) and image_parts[: len(root_parts)] == root_parts


def query_service_snapshot(service_name: str = SERVICE_NAME) -> ServiceSnapshot:
    """Read the service state via QueryServiceStatusEx: ~0.1 ms and no process spawned.

    Polling ``sc query`` instead costs ~35 ms and a process launch per read.
    """
    if sys.platform != "win32":
        return ServiceSnapshot(service_name, "ERROR", error="the SCM is only available on Windows")
    try:
        token, pid, exit_code = _query_status_ex(service_name)
    except OSError as exc:
        return ServiceSnapshot(service_name, "ERROR", error=str(exc))
    if token == "NOT_INSTALLED":
        return ServiceSnapshot(service_name, token)
    image, strategy = _read_service_registry(service_name)
    return ServiceSnapshot(
        service_name,
        token,
        pid=pid or None,
        exit_code=exit_code,
        image=image,
        strategy=strategy,
    )


_SC_MANAGER_CONNECT = 0x0001
_SERVICE_QUERY_STATUS = 0x0004
_SC_STATUS_PROCESS_INFO = 0


@functools.lru_cache(maxsize=1)
def _scm_api():
    import ctypes
    from ctypes import wintypes

    class ServiceStatusProcess(ctypes.Structure):
        _fields_ = [
            (name, wintypes.DWORD)
            for name in (
                "dwServiceType",
                "dwCurrentState",
                "dwControlsAccepted",
                "dwWin32ExitCode",
                "dwServiceSpecificExitCode",
                "dwCheckPoint",
                "dwWaitHint",
                "dwProcessId",
                "dwServiceFlags",
            )
        ]

    advapi = ctypes.WinDLL("advapi32", use_last_error=True)
    advapi.OpenSCManagerW.restype = wintypes.HANDLE
    advapi.OpenSCManagerW.argtypes = (wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD)
    advapi.OpenServiceW.restype = wintypes.HANDLE
    advapi.OpenServiceW.argtypes = (wintypes.HANDLE, wintypes.LPCWSTR, wintypes.DWORD)
    advapi.QueryServiceStatusEx.restype = wintypes.BOOL
    advapi.QueryServiceStatusEx.argtypes = (
        wintypes.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wintypes.DWORD,
        ctypes.POINTER(wintypes.DWORD),
    )
    advapi.CloseServiceHandle.restype = wintypes.BOOL
    advapi.CloseServiceHandle.argtypes = (wintypes.HANDLE,)
    return ctypes, wintypes, advapi, ServiceStatusProcess


def _query_status_ex(service_name: str) -> tuple[str, int, int]:
    ctypes, wintypes, advapi, ServiceStatusProcess = _scm_api()
    manager = advapi.OpenSCManagerW(None, None, _SC_MANAGER_CONNECT)
    if not manager:
        code = ctypes.get_last_error()
        raise OSError(code, f"OpenSCManager failed ({code})")
    try:
        handle = advapi.OpenServiceW(manager, service_name, _SERVICE_QUERY_STATUS)
        if not handle:
            code = ctypes.get_last_error()
            if code == ERROR_SERVICE_DOES_NOT_EXIST:
                return "NOT_INSTALLED", 0, 0
            raise OSError(code, f"OpenService failed ({code})")
        try:
            status = ServiceStatusProcess()
            needed = wintypes.DWORD()
            if not advapi.QueryServiceStatusEx(
                handle,
                _SC_STATUS_PROCESS_INFO,
                ctypes.byref(status),
                ctypes.sizeof(status),
                ctypes.byref(needed),
            ):
                code = ctypes.get_last_error()
                raise OSError(code, f"QueryServiceStatusEx failed ({code})")
            token = _SCM_STATE_TOKENS.get(int(status.dwCurrentState), "ERROR")
            return token, int(status.dwProcessId), int(status.dwWin32ExitCode)
        finally:
            advapi.CloseServiceHandle(handle)
    finally:
        advapi.CloseServiceHandle(manager)


def _read_service_registry(service_name: str) -> tuple[str, str | None]:
    try:
        import winreg
    except ImportError:
        return "", None
    try:
        with winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            rf"SYSTEM\CurrentControlSet\Services\{service_name}",
        ) as key:
            try:
                image_path = str(winreg.QueryValueEx(key, "ImagePath")[0])
            except OSError:
                image_path = ""
            try:
                strategy = str(winreg.QueryValueEx(key, STRATEGY_VALUE_NAME)[0]).strip() or None
            except OSError:
                strategy = None
    except OSError:
        return "", None
    return image_from_image_path(image_path), strategy


def map_sc_query(
    returncode: int,
    stdout: str,
    stderr: str = "",
    *,
    service_name: str = SERVICE_NAME,
) -> ServiceStatus:
    """Map ``sc query`` output onto a definite state."""
    blob = f"{stdout}\n{stderr}"
    combined = blob.upper()
    raw = blob.strip()

    if returncode == ERROR_SERVICE_DOES_NOT_EXIST or "1060" in blob:
        return ServiceStatus(
            state="not_installed",
            service_name=service_name,
            message=f'Service "{service_name}" is not installed',
            raw=raw,
            returncode=returncode,
        )
    if "DOES NOT EXIST" in combined:
        return ServiceStatus(
            state="not_installed",
            service_name=service_name,
            message=f'Service "{service_name}" is not installed',
            raw=raw,
            returncode=returncode,
        )

    state_token = _extract_state_token(stdout)
    if state_token in {"RUNNING", "START_PENDING", "CONTINUE_PENDING"}:
        mapped: ServiceState = "running"
    elif state_token in {"STOPPED", "STOP_PENDING", "PAUSED", "PAUSE_PENDING"}:
        mapped = "stopped"
    else:
        mapped = "error"

    if mapped == "error":
        if returncode not in (0, None) and not state_token:
            message = stderr.strip() or stdout.strip() or f"sc query failed with code {returncode}"
            return ServiceStatus(
                state="error",
                service_name=service_name,
                message=message,
                raw=raw,
                returncode=returncode,
            )
        if not state_token:
            return ServiceStatus(
                state="error",
                service_name=service_name,
                message="sc query returned no STATE line",
                raw=raw,
                returncode=returncode,
            )

    pretty = {
        "running": f'Service "{service_name}" is running',
        "stopped": f'Service "{service_name}" is stopped',
        "error": f'Service "{service_name}" is in an unexpected state ({state_token})',
    }[mapped]
    return ServiceStatus(
        state=mapped,
        service_name=service_name,
        message=pretty,
        raw=raw,
        returncode=returncode,
    )


def query_service_status(
    service_name: str = SERVICE_NAME,
    *,
    runner: ScmRunner | None = None,
    strategy_reader: Callable[[], str | None] | None = None,
) -> ServiceStatus:
    """Query SCM. Read-only; does not create or start winws/WinDivert."""
    command = build_status_command(service_name)
    try:
        completed = (runner or _run_subprocess)(command)
    except Exception as exc:
        return ServiceStatus(
            state="error",
            service_name=service_name,
            message=f"Failed to query service: {exc}",
            raw=str(exc),
        )
    status = map_sc_query(
        completed.returncode,
        completed.stdout,
        completed.stderr,
        service_name=service_name,
    )
    try:
        stem = read_installed_strategy(reader=strategy_reader)
    except Exception:
        stem = None
    if stem:
        return ServiceStatus(
            state=status.state,
            service_name=status.service_name,
            message=f'{status.message} — strategy "{stem}"',
            raw=status.raw,
            returncode=status.returncode,
            strategy_name=stem,
        )
    return status


def wait_for_state(
    service_name: str = SERVICE_NAME,
    *,
    target: tuple[str, ...] = ("running",),
    timeout_s: float = 6.0,
    poll_s: float = 0.35,
    runner: ScmRunner | None = None,
    clock: Callable[[], float] | None = None,
    sleeper: Callable[[float], None] | None = None,
) -> ServiceWait:
    """Poll ``sc queryex`` until the service reaches ``target`` or the deadline.

    ``sc start`` returns as soon as SCM accepts the request, typically at
    START_PENDING, so winws can still die a moment later. Only a query proves it.
    ``clock``/``sleeper`` are injected by tests so nothing actually sleeps.
    """
    now = clock or time.monotonic
    sleep = sleeper or time.sleep
    run = runner or _run_subprocess
    wanted = {str(item) for item in target}
    command = build_queryex_command(service_name)
    deadline = now() + float(timeout_s)
    polls = 0
    while True:
        polls += 1
        try:
            completed = run(command)
        except Exception as exc:
            return ServiceWait(
                status=ServiceStatus(
                    state="error",
                    service_name=service_name,
                    message=f"Failed to query service: {exc}",
                    raw=str(exc),
                ),
                reached=False,
                polls=polls,
            )
        status = map_sc_query(
            completed.returncode,
            completed.stdout,
            completed.stderr,
            service_name=service_name,
        )
        pid = extract_pid(completed.stdout)
        token = _extract_state_token(completed.stdout)
        # START_PENDING is exactly where ``sc start`` returns, and map_sc_query
        # folds it into "running". Accepting it here would make the poll a no-op.
        settled = not token.endswith("_PENDING")
        if status.state in wanted and settled:
            return ServiceWait(status=status, reached=True, pid=pid, polls=polls, token=token)
        # A missing service will never transition; stop burning polls on it.
        if status.state == "not_installed" and "not_installed" not in wanted:
            return ServiceWait(status=status, reached=False, pid=pid, polls=polls, token=token)
        if now() >= deadline:
            return ServiceWait(status=status, reached=False, pid=pid, polls=polls, token=token)
        sleep(poll_s)


def live_install_steps(
    image: str,
    args: str,
    service_name: str = SERVICE_NAME,
) -> tuple[ScmCommand, ScmCommand, ScmCommand]:
    """stop + delete + create. Live install must run all three in both elevation paths."""
    create = build_install_command(image, args, service_name)
    stop = build_stop_command(service_name, image=image, args=args)
    delete = build_delete_command(service_name)
    return stop, delete, create


def install_plan(
    image: str,
    args: str,
    service_name: str = SERVICE_NAME,
) -> tuple[PlanStep, ...]:
    """service.bat :service_install, step for step (service.bat:347-358).

    ``tcp`` and ``describe`` are advisory: service.bat discards their errors.
    """
    stop, delete, create = live_install_steps(image, args, service_name)
    return (
        PlanStep("tcp", build_tcp_enable_command(service_name), frozenset({0}), fatal=False),
        PlanStep("stop", stop, _REPLACE_STOP_OK),
        PlanStep("delete", delete, _REPLACE_DELETE_OK),
        PlanStep("create", create, frozenset({0})),
        PlanStep("describe", build_describe_command(service_name), frozenset({0}), fatal=False),
        PlanStep("start", build_start_command(service_name, image=image, args=args), _START_OK),
    )


def install_service(
    image: str,
    args: str,
    *,
    service_name: str = SERVICE_NAME,
    runner: ScmRunner | None = None,
    allow_live: bool = False,
    privileged: bool | None = None,
    on_step: StepObserver | None = None,
    waiter: Waiter | None = None,
) -> ScmOpResult:
    """Install and start the strategy service.

    With an injected ``runner`` (or without ``allow_live``) only the ``sc create``
    command is built/issued, so unit tests never walk the live sequence. The live
    path runs ``install_plan()`` in full and verifies the service reached RUNNING.
    """
    _stop, _delete, create = live_install_steps(image, args, service_name)
    if runner is not None or not allow_live:
        return execute_scm(
            create,
            runner=runner,
            allow_live=allow_live,
            privileged=privileged,
        )
    return run_install_sequence(
        image,
        args,
        service_name=service_name,
        privileged=privileged,
        on_step=on_step,
        waiter=waiter,
    )


def _chained_install_command(
    plan: tuple[PlanStep, ...],
    image: str,
    args: str,
    service_name: str,
) -> ScmCommand:
    create = next(step.command for step in plan if step.kind == "create")
    return ScmCommand(
        action="install",
        argv=create.argv,
        command_line=" & ".join(step.command.command_line for step in plan),
        service_name=service_name,
        image=image,
        args=args,
    )


def _execute_plan_step(step: PlanStep, run: ScmRunner) -> StepReport:
    if step.kind == "tcp":
        probe = build_tcp_probe_command(step.command.service_name)
        try:
            seen = run(probe)
        except Exception:
            seen = CompletedScm(returncode=1)
        if tcp_timestamps_enabled(seen.stdout):
            return StepReport(
                kind="tcp",
                command_line=probe.command_line,
                returncode=0,
                stdout=seen.stdout,
                ok=True,
                fatal=False,
                note="timestamps already enabled",
            )

    try:
        completed = run(step.command)
    except Exception as exc:
        return StepReport(
            kind=step.kind,
            command_line=step.command.command_line,
            returncode=-1,
            stderr=str(exc),
            ok=False,
            fatal=step.fatal,
            note="the command could not be launched",
        )
    code = int(completed.returncode)
    return StepReport(
        kind=step.kind,
        command_line=step.command.command_line,
        returncode=code,
        stdout=completed.stdout,
        stderr=completed.stderr,
        ok=code in step.ok_codes,
        fatal=step.fatal,
        note="" if code == 0 else describe_sc_error(code),
    )


def _settle_stop(
    report: StepReport,
    wait: Waiter,
    service_name: str,
    runner: ScmRunner | None,
) -> StepReport:
    """Wait for STOPPED the way ``net stop`` blocks; ``sc stop`` returns at STOP_PENDING."""
    settled = wait(service_name, target=("stopped", "not_installed"), timeout_s=STOP_SETTLE_S, runner=runner)
    if settled.reached:
        return replace(report, note="stopped")
    # service.bat swallows a failed `net stop` and carries on; the delete wait decides.
    return replace(report, note=f"still {settled.token or settled.status.state} after {STOP_SETTLE_S:g} s")


def _settle_delete(
    report: StepReport,
    wait: Waiter,
    service_name: str,
    runner: ScmRunner | None,
) -> StepReport:
    """A deleted service lingers, marked, until it stops and every handle to it closes.

    ``sc create`` issued inside that window fails with 1072.
    """
    gone = wait(service_name, target=("not_installed",), timeout_s=DELETE_SETTLE_S, runner=runner)
    if gone.reached:
        return replace(report, note="removed")
    return replace(
        report,
        returncode=ERROR_SERVICE_MARKED_FOR_DELETE,
        stdout="",
        stderr=(
            f"{service_name} is still marked for deletion after {DELETE_SETTLE_S:g} s; "
            "close services.msc or Event Viewer and retry"
        ),
        ok=False,
        note=describe_sc_error(ERROR_SERVICE_MARKED_FOR_DELETE),
    )


def run_install_sequence(
    image: str,
    args: str,
    *,
    service_name: str = SERVICE_NAME,
    runner: ScmRunner | None = None,
    privileged: bool | None = None,
    on_step: StepObserver | None = None,
    waiter: Waiter | None = None,
) -> ScmOpResult:
    """Walk install_plan(), then confirm the service actually reached RUNNING.

    Unelevated: one UAC ``cmd /c a & b & c`` for the whole chain. Its ``sc``
    output cannot be captured, so the result is ``verified=False`` — the caller
    must re-query rather than report success.
    Elevated: each step runs in this process with stdout/stderr captured, and the
    stop and delete steps wait for SCM to settle before the next command.
    """
    plan = install_plan(image, args, service_name)
    chained = _chained_install_command(plan, image, args, service_name)
    is_admin = is_process_elevated() if privileged is None else privileged

    if runner is None and not is_admin:
        try:
            native = shell_execute("cmd.exe", f"/c {chained.command_line}", "runas")
        except Exception as exc:
            return ScmOpResult(
                ok=False,
                command=chained,
                error=f"SCM install failed: {exc}",
                executed=False,
                needs_elevation=True,
            )
        if native <= 32:
            from zapret_gui.privileges import describe_shellexecute_failure

            return ScmOpResult(
                ok=False,
                command=chained,
                error=f"Elevation failed: {describe_shellexecute_failure(native)}",
                executed=False,
                needs_elevation=True,
            )
        return ScmOpResult(
            ok=True,
            command=chained,
            executed=True,
            needs_elevation=True,
            verified=False,
        )

    run = runner or _run_subprocess
    wait = waiter or wait_for_state
    reports: list[StepReport] = []
    last: CompletedScm | None = None
    for step in plan:
        report = _execute_plan_step(step, run)
        if step.kind == "stop" and report.returncode == 0:
            report = _settle_stop(report, wait, service_name, runner)
        elif step.kind == "delete" and report.returncode in (0, ERROR_SERVICE_MARKED_FOR_DELETE):
            report = _settle_delete(report, wait, service_name, runner)
        reports.append(report)
        if on_step is not None:
            on_step(report)
        last = CompletedScm(report.returncode, report.stdout, report.stderr)
        if not report.ok and step.fatal:
            return ScmOpResult(
                ok=False,
                command=chained,
                error=_report_error(step.kind, report),
                executed=True,
                completed=last,
                steps=tuple(reports),
                needs_elevation=False,
            )

    settled = wait(service_name, target=("running",), runner=runner)
    verify = _verify_report(service_name, settled, "running")
    reports.append(verify)
    if on_step is not None:
        on_step(verify)

    return ScmOpResult(
        ok=settled.reached,
        command=chained,
        status=settled.status,
        error=None if settled.reached else f"Service did not reach RUNNING: {verify.note}",
        executed=True,
        completed=last,
        steps=tuple(reports),
        verified=True,
        pid=settled.pid,
    )


def _report_error(kind: StepKind, report: StepReport) -> str:
    """``sc start failed (1053): <sc text> - the service did not respond ...``"""
    detail = " ".join(report.detail.split()) or describe_sc_error(report.returncode)
    return f"sc {kind} failed ({report.returncode}): {detail}{_sc_hint(report.returncode, detail)}"


def _verify_report(service_name: str, wait: ServiceWait, target: str) -> StepReport:
    if wait.reached:
        label = target.upper()
        note = f"{label}  pid {wait.pid}" if wait.pid else label
    else:
        note = exit_code_hint(wait.status.raw) or wait.status.message
    return StepReport(
        kind="verify",
        command_line=build_queryex_command(service_name).command_line,
        returncode=0 if wait.reached else 1,
        stdout=wait.status.raw,
        ok=wait.reached,
        note=note,
    )


def start_service(
    service_name: str = SERVICE_NAME,
    *,
    image: str = "",
    args: str = "",
    runner: ScmRunner | None = None,
    allow_live: bool = False,
    privileged: bool | None = None,
    waiter: Waiter | None = None,
) -> ScmOpResult:
    return _execute_verified(
        build_start_command(service_name, image=image, args=args),
        target="running",
        ok_codes=_START_OK,
        runner=runner,
        allow_live=allow_live,
        privileged=privileged,
        waiter=waiter,
    )


def stop_service(
    service_name: str = SERVICE_NAME,
    *,
    image: str = "",
    args: str = "",
    runner: ScmRunner | None = None,
    allow_live: bool = False,
    privileged: bool | None = None,
    waiter: Waiter | None = None,
) -> ScmOpResult:
    return _execute_verified(
        build_stop_command(service_name, image=image, args=args),
        target="stopped",
        ok_codes=_REPLACE_STOP_OK,
        runner=runner,
        allow_live=allow_live,
        privileged=privileged,
        waiter=waiter,
    )


def _execute_verified(
    command: ScmCommand,
    *,
    target: str,
    ok_codes: frozenset[int],
    runner: ScmRunner | None,
    allow_live: bool,
    privileged: bool | None,
    waiter: Waiter | None,
) -> ScmOpResult:
    """execute_scm plus a state poll, on the live in-process path only.

    With an injected runner the poll is skipped so unit tests observe exactly the
    one command they asked for.
    """
    result = execute_scm(
        command,
        runner=runner,
        allow_live=allow_live,
        privileged=privileged,
        ok_codes=ok_codes,
    )
    if runner is not None or not result.ok or not result.executed or result.needs_elevation:
        return result
    wait = (waiter or wait_for_state)(command.service_name, target=(target,))
    verify = _verify_report(command.service_name, wait, target)
    return ScmOpResult(
        ok=wait.reached,
        command=result.command,
        status=wait.status,
        error=None if wait.reached else f"Service did not reach {target.upper()}: {verify.note}",
        executed=True,
        completed=result.completed,
        steps=result.steps + (verify,),
        verified=True,
        pid=wait.pid,
    )


RemoveKind = Literal["stop", "delete", "taskkill"]
WINDIVERT_SERVICES = ("WinDivert", "WinDivert14")
# service.bat:221-222 silences these (`>nul 2>&1`); their failures never matter.
REMOVE_ADVISORY = frozenset({"WinDivert14"})


@dataclass(frozen=True)
class RemoveStep:
    kind: RemoveKind
    target: str
    argv: tuple[str, ...]
    command_line: str
    ok_codes: frozenset[int]


@dataclass(frozen=True)
class RemoveResult:
    ok: bool
    steps: tuple[RemoveStep, ...]
    executed: bool = False
    error: str | None = None
    completed: CompletedScm | None = None
    needs_elevation: bool = False
    reports: tuple[StepReport, ...] = ()
    verified: bool = False

    @property
    def command_line(self) -> str:
        return " & ".join(step.command_line for step in self.steps)


RemoveRunner = Callable[[RemoveStep], CompletedScm]


def _stop_step(service_name: str) -> RemoveStep:
    argv = (sc_executable(), "stop", service_name)
    return RemoveStep(
        kind="stop",
        target=service_name,
        argv=argv,
        command_line=" ".join(argv),
        ok_codes=_REPLACE_STOP_OK,
    )


def _delete_step(service_name: str) -> RemoveStep:
    argv = (sc_executable(), "delete", service_name)
    return RemoveStep(
        kind="delete",
        target=service_name,
        argv=argv,
        command_line=" ".join(argv),
        ok_codes=_REPLACE_DELETE_OK,
    )


def _taskkill_winws_step() -> RemoveStep:
    argv = ("taskkill.exe", "/IM", "winws.exe", "/F")
    return RemoveStep(
        kind="taskkill",
        target="winws.exe",
        argv=argv,
        command_line=" ".join(argv),
        ok_codes=_TASKKILL_OK,
    )


def plan_remove_steps(service_name: str = SERVICE_NAME) -> tuple[RemoveStep, ...]:
    """service.bat :service_remove — stop/delete zapret, kill leftover winws, then WinDivert*."""
    steps: list[RemoveStep] = [
        _stop_step(service_name),
        _delete_step(service_name),
        _taskkill_winws_step(),
    ]
    for divert in WINDIVERT_SERVICES:
        steps.append(_stop_step(divert))
        steps.append(_delete_step(divert))
    return tuple(steps)


def _remove_report(step: RemoveStep, completed: CompletedScm) -> StepReport:
    code = int(completed.returncode)
    if code == 0:
        note = ""
    elif step.kind == "taskkill" and code == ERROR_TASKKILL_NOT_FOUND:
        note = "winws.exe was not running"
    else:
        note = describe_sc_error(code)
    return StepReport(
        kind=step.kind,
        command_line=step.command_line,
        returncode=code,
        stdout=completed.stdout,
        stderr=completed.stderr,
        ok=code in step.ok_codes,
        fatal=step.target not in REMOVE_ADVISORY,
        note=note,
    )


def _walk_remove_steps(
    steps: tuple[RemoveStep, ...],
    run: RemoveRunner,
    wait: Waiter | None,
    on_step: StepObserver | None,
) -> tuple[list[StepReport], CompletedScm | None, list[str]]:
    """Run every step like service.bat does: a failure is reported, never an abort."""
    reports: list[StepReport] = []
    failures: list[str] = []
    last: CompletedScm | None = None
    for step in steps:
        try:
            completed = run(step)
        except Exception as exc:
            completed = CompletedScm(returncode=-1, stderr=str(exc))
        last = completed
        report = _remove_report(step, completed)
        if (
            wait is not None
            and step.kind == "stop"
            and completed.returncode == 0
            and step.target not in REMOVE_ADVISORY
        ):
            # `net stop` in the bat blocks until STOPPED before its `sc delete`.
            settled = wait(step.target, target=("stopped", "not_installed"), timeout_s=STOP_SETTLE_S)
            report = replace(
                report,
                note="stopped"
                if settled.reached
                else f"still {settled.token or settled.status.state} after {STOP_SETTLE_S:g} s",
            )
        reports.append(report)
        if on_step is not None:
            on_step(report)
        if not report.ok and report.fatal:
            detail = " ".join((completed.stderr or completed.stdout or "").split()) or report.note
            failures.append(f"{step.kind} {step.target} failed ({completed.returncode}): {detail or 'no output'}")
    return reports, last, failures


def _remove_outcome(
    *,
    steps: tuple[RemoveStep, ...],
    reports: list[StepReport],
    last: CompletedScm | None,
    failures: list[str],
    wait: Waiter | None,
    on_step: StepObserver | None,
    service_name: str,
    needs_elevation: bool,
) -> RemoveResult:
    if wait is not None:
        gone = wait(service_name, target=("not_installed",), timeout_s=DELETE_SETTLE_S)
        verify = StepReport(
            kind="verify",
            command_line=build_queryex_command(service_name).command_line,
            returncode=0 if gone.reached else 1,
            stdout=gone.status.raw,
            ok=gone.reached,
            note="NOT INSTALLED"
            if gone.reached
            else f"{service_name} is still installed ({gone.token or gone.status.state})",
        )
        reports.append(verify)
        if on_step is not None:
            on_step(verify)
        ok = gone.reached
        if not ok:
            failures.append(verify.note)
    else:
        zapret_delete = next(
            (
                report
                for step, report in zip(steps, reports)
                if step.kind == "delete" and step.target == service_name
            ),
            None,
        )
        ok = zapret_delete is not None and zapret_delete.ok
    return RemoveResult(
        ok=ok,
        steps=steps,
        executed=True,
        error="; ".join(failures) or None,
        completed=last,
        needs_elevation=needs_elevation,
        reports=tuple(reports),
        verified=True,
    )


def remove_service(
    *,
    service_name: str = SERVICE_NAME,
    runner: RemoveRunner | None = None,
    allow_live: bool = False,
    privileged: bool | None = None,
    on_step: StepObserver | None = None,
    waiter: Waiter | None = None,
) -> RemoveResult:
    """Stop and remove zapret, kill leftover winws, drop WinDivert.

    Like service.bat :service_remove every step runs even when an earlier one
    fails; the verdict is whether ``zapret`` is actually gone. Refuses live SCM
    mutation without ``allow_live`` or an injected ``runner``. On the runner path
    the settle waits run only when a ``waiter`` is supplied.
    """
    steps = plan_remove_steps(service_name)
    is_admin = is_process_elevated() if privileged is None else privileged
    needs_elevation = not is_admin

    if runner is not None:
        reports, last, failures = _walk_remove_steps(steps, runner, waiter, on_step)
        return _remove_outcome(
            steps=steps,
            reports=reports,
            last=last,
            failures=failures,
            wait=waiter,
            on_step=on_step,
            service_name=service_name,
            needs_elevation=needs_elevation,
        )

    if not allow_live:
        return RemoveResult(
            ok=False,
            steps=steps,
            executed=False,
            error="Refusing live SCM mutation without allow_live=True (winws/WinDivert is not removed from tests)",
            needs_elevation=needs_elevation,
        )

    if not is_admin:
        chained = " & ".join(step.command_line for step in steps)
        try:
            native = shell_execute("cmd.exe", f"/c {chained}", "runas")
        except Exception as exc:
            return RemoveResult(
                ok=False,
                steps=steps,
                executed=False,
                error=f"Remove failed: {exc}",
                needs_elevation=True,
            )
        if native <= 32:
            from zapret_gui.privileges import describe_shellexecute_failure

            return RemoveResult(
                ok=False,
                steps=steps,
                executed=False,
                error=f"Elevation failed: {describe_shellexecute_failure(native)}",
                needs_elevation=True,
            )
        # cmd.exe launched; whether sc succeeded is not observable from here.
        return RemoveResult(
            ok=True,
            steps=steps,
            executed=True,
            needs_elevation=True,
            verified=False,
        )

    wait = waiter or wait_for_state
    reports, last, failures = _walk_remove_steps(steps, lambda step: _run_argv(step.argv), wait, on_step)
    return _remove_outcome(
        steps=steps,
        reports=reports,
        last=last,
        failures=failures,
        wait=wait,
        on_step=on_step,
        service_name=service_name,
        needs_elevation=False,
    )


def execute_scm(
    command: ScmCommand,
    *,
    runner: ScmRunner | None = None,
    allow_live: bool = False,
    privileged: bool | None = None,
    ok_codes: frozenset[int] | None = None,
) -> ScmOpResult:
    """Run an SCM command, or return the built command without touching the host.

    Mutating actions (install/start/stop/delete) refuse to call the real ``sc.exe``
    unless ``allow_live=True`` or a test ``runner`` is injected.
    """
    is_admin = is_process_elevated() if privileged is None else privileged
    needs_elevation = command.action != "status" and not is_admin
    accepted = ok_codes if ok_codes is not None else frozenset({0})

    if runner is not None:
        try:
            completed = runner(command)
        except Exception as exc:
            return ScmOpResult(
                ok=False,
                command=command,
                error=str(exc),
                executed=False,
                needs_elevation=needs_elevation,
            )
        status = None
        if command.action == "status":
            status = map_sc_query(
                completed.returncode,
                completed.stdout,
                completed.stderr,
                service_name=command.service_name,
            )
        ok = int(completed.returncode) in accepted
        return ScmOpResult(
            ok=ok,
            command=command,
            status=status,
            error=None if ok else _step_error(command, completed),
            executed=True,
            completed=completed,
            needs_elevation=needs_elevation,
            steps=(_single_report(command, completed, accepted),),
        )

    if command.action != "status" and not allow_live:
        return ScmOpResult(
            ok=False,
            command=command,
            error="Refusing live SCM mutation without allow_live=True (winws/WinDivert is not installed from tests)",
            executed=False,
            needs_elevation=needs_elevation,
        )

    try:
        if command.action != "status" and not is_admin:
            native = shell_execute("cmd.exe", f"/c {command.command_line}", "runas")
            if native <= 32:
                from zapret_gui.privileges import describe_shellexecute_failure

                return ScmOpResult(
                    ok=False,
                    command=command,
                    error=f"Elevation failed: {describe_shellexecute_failure(native)}",
                    executed=False,
                    needs_elevation=True,
                )
            # cmd.exe launched; whether sc succeeded is not observable from here.
            return ScmOpResult(
                ok=True,
                command=command,
                executed=True,
                needs_elevation=True,
                verified=False,
            )
        completed = _run_subprocess(command)
    except Exception as exc:
        return ScmOpResult(
            ok=False,
            command=command,
            error=f"SCM operation failed: {exc}",
            executed=False,
            needs_elevation=needs_elevation,
        )

    status = None
    if command.action == "status":
        status = map_sc_query(
            completed.returncode,
            completed.stdout,
            completed.stderr,
            service_name=command.service_name,
        )
    ok = int(completed.returncode) in accepted
    return ScmOpResult(
        ok=ok,
        command=command,
        status=status,
        error=None if ok else _step_error(command, completed),
        executed=True,
        completed=completed,
        needs_elevation=False,
        steps=(_single_report(command, completed, accepted),),
    )


def _single_report(
    command: ScmCommand,
    completed: CompletedScm,
    ok_codes: frozenset[int],
) -> StepReport:
    code = int(completed.returncode)
    kind: StepKind = "create" if command.action == "install" else command.action  # type: ignore[assignment]
    return StepReport(
        kind=kind,
        command_line=command.command_line,
        returncode=code,
        stdout=completed.stdout,
        stderr=completed.stderr,
        ok=code in ok_codes,
        note="" if code == 0 else describe_sc_error(code),
    )


def _step_error(command: ScmCommand, completed: CompletedScm) -> str:
    code = int(completed.returncode)
    raw = (completed.stderr or completed.stdout or "").strip()
    detail = " ".join(raw.split()) or describe_sc_error(code)
    return f"sc {command.action} failed ({code}): {detail}{_sc_hint(code, detail)}"


def _extract_state_token(stdout: str) -> str:
    for line in stdout.splitlines():
        upper = line.upper()
        if "STATE" not in upper:
            continue
        # ``STATE              : 4  RUNNING``
        if ":" in line:
            rhs = line.split(":", 1)[1]
            parts = rhs.split()
            for part in parts:
                token = part.strip().upper()
                if token.isalpha() or "_" in token:
                    return token
        for token in (
            "RUNNING",
            "STOPPED",
            "STOP_PENDING",
            "START_PENDING",
            "PAUSE_PENDING",
            "CONTINUE_PENDING",
            "PAUSED",
        ):
            if token in upper:
                return token
    return ""


def _run_subprocess(command: ScmCommand) -> CompletedScm:
    return _run_argv(command.argv)


def _run_argv(argv: tuple[str, ...] | list[str]) -> CompletedScm:
    kwargs: dict = {
        "capture_output": True,
        "check": False,
    }
    if sys.platform == "win32":
        kwargs["creationflags"] = _CREATE_NO_WINDOW
    proc = subprocess.run(list(argv), **kwargs)
    return CompletedScm(
        returncode=int(proc.returncode),
        stdout=_decode(proc.stdout),
        stderr=_decode(proc.stderr),
    )


def _decode(blob: bytes | str) -> str:
    if isinstance(blob, str):
        return blob
    for encoding in ("utf-8", "oem", "cp437", "cp1251"):
        try:
            return blob.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return blob.decode("utf-8", errors="replace")


def command_contains(command: ScmCommand, *needles: str) -> bool:
    haystack = (" ".join(command.argv) + " " + command.command_line).lower()
    return all(needle.lower() in haystack for needle in needles)
