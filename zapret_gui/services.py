"""Windows Service Control Manager adapter for the zapret / winws.exe service.

Live ``sc create`` / ``sc start`` / ``sc stop`` of winws/WinDivert is gated behind
``allow_live=True``. Tests must inject a runner or omit that flag so the host
SCM is never mutated during verification.
"""

from __future__ import annotations

import subprocess
import sys
from dataclasses import dataclass
from typing import Callable, Literal

from zapret_gui import SERVICE_NAME
from zapret_gui.identity import read_installed_strategy
from zapret_gui.privileges import is_process_elevated, shell_execute

ServiceState = Literal["running", "stopped", "not_installed", "error"]
ScmAction = Literal["install", "start", "stop", "delete", "status"]

# Windows ``sc`` / OpenService
ERROR_SERVICE_DOES_NOT_EXIST = 1060
ERROR_SERVICE_NOT_ACTIVE = 1062
ERROR_SERVICE_MARKED_FOR_DELETE = 1072
ERROR_TASKKILL_NOT_FOUND = 128
# stop/delete during replace: not-installed / not-started / already-deleting are not failures
_REPLACE_STOP_OK = frozenset({0, ERROR_SERVICE_DOES_NOT_EXIST, ERROR_SERVICE_NOT_ACTIVE})
_REPLACE_DELETE_OK = frozenset({0, ERROR_SERVICE_DOES_NOT_EXIST, ERROR_SERVICE_MARKED_FOR_DELETE})
_TASKKILL_OK = frozenset({0, 1, ERROR_TASKKILL_NOT_FOUND})

_CREATE_NO_WINDOW = 0x08000000


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


@dataclass(frozen=True)
class ScmOpResult:
    ok: bool
    command: ScmCommand
    status: ServiceStatus | None = None
    error: str | None = None
    executed: bool = False
    completed: CompletedScm | None = None
    needs_elevation: bool = False


ScmRunner = Callable[[ScmCommand], CompletedScm]


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
    argv = (
        sc_executable(),
        "create",
        service_name,
        "binPath=",
        bin_path_value,
        "DisplayName=",
        "zapret",
        "start=",
        "auto",
    )
    # Faithful to service.bat: sc create NAME binPath= "\"IMAGE\" ARGS" ...
    command_line = (
        f'{sc_executable()} create {service_name} '
        f'binPath= "\\"{image}\\" {args}" '
        f'DisplayName= "zapret" start= auto'
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


def build_status_command(service_name: str = SERVICE_NAME) -> ScmCommand:
    argv = (sc_executable(), "query", service_name)
    return ScmCommand(
        action="status",
        argv=argv,
        command_line=" ".join(argv),
        service_name=service_name,
        elevation_verb="open",
    )


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


def install_service(
    image: str,
    args: str,
    *,
    service_name: str = SERVICE_NAME,
    runner: ScmRunner | None = None,
    allow_live: bool = False,
    privileged: bool | None = None,
) -> ScmOpResult:
    stop, delete, create = live_install_steps(image, args, service_name)
    if runner is not None or not allow_live:
        return execute_scm(
            create,
            runner=runner,
            allow_live=allow_live,
            privileged=privileged,
        )
    return _execute_live_replace(
        stop=stop,
        delete=delete,
        create=create,
        privileged=privileged,
    )


def _chained_install_command(stop: ScmCommand, delete: ScmCommand, create: ScmCommand) -> ScmCommand:
    return ScmCommand(
        action="install",
        argv=create.argv,
        command_line=f"{stop.command_line} & {delete.command_line} & {create.command_line}",
        service_name=create.service_name,
        image=create.image,
        args=create.args,
    )


def _execute_live_replace(
    *,
    stop: ScmCommand,
    delete: ScmCommand,
    create: ScmCommand,
    privileged: bool | None = None,
) -> ScmOpResult:
    """Replace an existing zapret service.

    Unelevated: one UAC ``cmd /c stop & delete & create``.
    Elevated: three ``sc.exe`` subprocess calls in this process. ``sc create``
    alone against a running service is 1073; stop/delete must run first.
    1060/1062 on stop and 1060/1072 on delete are non-fatal.
    """
    is_admin = is_process_elevated() if privileged is None else privileged
    chained = _chained_install_command(stop, delete, create)
    if not is_admin:
        return execute_scm(chained, allow_live=True, privileged=False)

    steps: tuple[tuple[ScmCommand, frozenset[int], str], ...] = (
        (stop, _REPLACE_STOP_OK, "stop"),
        (delete, _REPLACE_DELETE_OK, "delete"),
        (create, frozenset({0}), "create"),
    )
    last: CompletedScm | None = None
    try:
        for cmd, ok_codes, label in steps:
            completed = _run_subprocess(cmd)
            last = completed
            if int(completed.returncode) not in ok_codes:
                detail = (completed.stderr or completed.stdout or "").strip()
                return ScmOpResult(
                    ok=False,
                    command=chained,
                    error=f"sc {label} failed ({completed.returncode}): {detail or 'no output'}",
                    executed=True,
                    completed=completed,
                    needs_elevation=False,
                )
    except Exception as exc:
        return ScmOpResult(
            ok=False,
            command=chained,
            error=f"SCM replace failed: {exc}",
            executed=False,
            needs_elevation=False,
        )
    return ScmOpResult(
        ok=True,
        command=chained,
        executed=True,
        completed=last,
        needs_elevation=False,
    )


def start_service(
    service_name: str = SERVICE_NAME,
    *,
    image: str = "",
    args: str = "",
    runner: ScmRunner | None = None,
    allow_live: bool = False,
    privileged: bool | None = None,
) -> ScmOpResult:
    command = build_start_command(service_name, image=image, args=args)
    return execute_scm(
        command,
        runner=runner,
        allow_live=allow_live,
        privileged=privileged,
    )


def stop_service(
    service_name: str = SERVICE_NAME,
    *,
    image: str = "",
    args: str = "",
    runner: ScmRunner | None = None,
    allow_live: bool = False,
    privileged: bool | None = None,
) -> ScmOpResult:
    command = build_stop_command(service_name, image=image, args=args)
    return execute_scm(
        command,
        runner=runner,
        allow_live=allow_live,
        privileged=privileged,
    )


RemoveKind = Literal["stop", "delete", "taskkill"]
WINDIVERT_SERVICES = ("WinDivert", "WinDivert14")


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


def remove_service(
    *,
    service_name: str = SERVICE_NAME,
    runner: RemoveRunner | None = None,
    allow_live: bool = False,
    privileged: bool | None = None,
) -> RemoveResult:
    """Remove zapret + leftover winws + WinDivert. Refuse-by-default without allow_live/runner."""
    steps = plan_remove_steps(service_name)
    is_admin = is_process_elevated() if privileged is None else privileged
    needs_elevation = not is_admin

    if runner is not None:
        last: CompletedScm | None = None
        try:
            for step in steps:
                completed = runner(step)
                last = completed
                if int(completed.returncode) not in step.ok_codes:
                    detail = (completed.stderr or completed.stdout or "").strip()
                    return RemoveResult(
                        ok=False,
                        steps=steps,
                        executed=True,
                        error=f"{step.kind} {step.target} failed ({completed.returncode}): {detail or 'no output'}",
                        completed=completed,
                        needs_elevation=needs_elevation,
                    )
        except Exception as exc:
            return RemoveResult(
                ok=False,
                steps=steps,
                executed=False,
                error=str(exc),
                needs_elevation=needs_elevation,
            )
        return RemoveResult(
            ok=True,
            steps=steps,
            executed=True,
            completed=last,
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

    chained = " & ".join(step.command_line for step in steps)
    try:
        if not is_admin:
            native = shell_execute("cmd.exe", f"/c {chained}", "runas")
            if native <= 32:
                from zapret_gui.privileges import describe_shellexecute_failure

                return RemoveResult(
                    ok=False,
                    steps=steps,
                    executed=False,
                    error=f"Elevation failed: {describe_shellexecute_failure(native)}",
                    needs_elevation=True,
                )
            return RemoveResult(
                ok=True,
                steps=steps,
                executed=True,
                needs_elevation=True,
            )
        last = None
        for step in steps:
            completed = _run_argv(step.argv)
            last = completed
            if int(completed.returncode) not in step.ok_codes:
                detail = (completed.stderr or completed.stdout or "").strip()
                return RemoveResult(
                    ok=False,
                    steps=steps,
                    executed=True,
                    error=f"{step.kind} {step.target} failed ({completed.returncode}): {detail or 'no output'}",
                    completed=completed,
                    needs_elevation=False,
                )
        return RemoveResult(
            ok=True,
            steps=steps,
            executed=True,
            completed=last,
            needs_elevation=False,
        )
    except Exception as exc:
        return RemoveResult(
            ok=False,
            steps=steps,
            executed=False,
            error=f"Remove failed: {exc}",
            needs_elevation=needs_elevation,
        )


def execute_scm(
    command: ScmCommand,
    *,
    runner: ScmRunner | None = None,
    allow_live: bool = False,
    privileged: bool | None = None,
) -> ScmOpResult:
    """Run an SCM command, or return the built command without touching the host.

    Mutating actions (install/start/stop/delete) refuse to call the real ``sc.exe``
    unless ``allow_live=True`` or a test ``runner`` is injected.
    """
    is_admin = is_process_elevated() if privileged is None else privileged
    needs_elevation = command.action != "status" and not is_admin

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
        ok = completed.returncode == 0
        return ScmOpResult(
            ok=ok,
            command=command,
            status=status,
            error=None if ok else (completed.stderr or completed.stdout or f"exit {completed.returncode}"),
            executed=True,
            completed=completed,
            needs_elevation=needs_elevation,
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
            return ScmOpResult(
                ok=True,
                command=command,
                executed=True,
                needs_elevation=True,
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
    ok = completed.returncode == 0
    return ScmOpResult(
        ok=ok,
        command=command,
        status=status,
        error=None if ok else (completed.stderr or completed.stdout or f"exit {completed.returncode}"),
        executed=True,
        completed=completed,
        needs_elevation=False,
    )


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
