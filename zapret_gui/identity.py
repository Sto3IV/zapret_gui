"""Installed-strategy identity: the registry value service.bat writes after sc create."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable

from zapret_gui import SERVICE_NAME, STRATEGY_REG_KEY, STRATEGY_VALUE_NAME

_WINREG_SUBKEY = rf"SYSTEM\CurrentControlSet\Services\{SERVICE_NAME}"


@dataclass(frozen=True)
class RegistryWrite:
    key: str
    value_name: str
    data: str


@dataclass(frozen=True)
class IdentityResult:
    ok: bool
    plan: RegistryWrite
    executed: bool = False
    error: str | None = None


RegistryWriter = Callable[[RegistryWrite], None]
RegistryReader = Callable[[], str | None]


def strategy_stem(filename: str) -> str:
    """``%%~nF`` equivalent: ``general.bat`` → ``general``, ``general (ALT).bat`` → ``general (ALT)``."""
    name = filename.replace("\\", "/").rsplit("/", 1)[-1]
    if name.lower().endswith(".bat"):
        return name[: -len(".bat")]
    return name


def build_strategy_identity(stem: str) -> RegistryWrite:
    if not stem or not str(stem).strip():
        raise ValueError("strategy stem is empty")
    return RegistryWrite(
        key=STRATEGY_REG_KEY,
        value_name=STRATEGY_VALUE_NAME,
        data=str(stem).strip(),
    )


def persist_installed_strategy(
    stem: str,
    *,
    writer: RegistryWriter | None = None,
    allow_live: bool = False,
) -> IdentityResult:
    """Store the strategy file stem as ``zapret-discord-youtube``. Tests inject ``writer``."""
    plan = build_strategy_identity(stem)
    if writer is not None:
        writer(plan)
        return IdentityResult(ok=True, plan=plan, executed=True)
    if not allow_live:
        return IdentityResult(
            ok=False,
            plan=plan,
            executed=False,
            error="Refusing live registry write without allow_live=True",
        )
    try:
        _winreg_write(plan)
    except Exception as exc:
        return IdentityResult(ok=False, plan=plan, executed=False, error=str(exc))
    return IdentityResult(ok=True, plan=plan, executed=True)


def read_installed_strategy(*, reader: RegistryReader | None = None) -> str | None:
    """Return the installed strategy stem, or None if absent/unreadable."""
    if reader is not None:
        value = reader()
        return str(value).strip() if value else None
    return _winreg_read()


def _winreg_write(plan: RegistryWrite) -> None:
    import winreg

    key = winreg.CreateKeyEx(
        winreg.HKEY_LOCAL_MACHINE,
        _WINREG_SUBKEY,
        0,
        winreg.KEY_SET_VALUE,
    )
    try:
        winreg.SetValueEx(key, plan.value_name, 0, winreg.REG_SZ, plan.data)
    finally:
        winreg.CloseKey(key)


def _winreg_read() -> str | None:
    try:
        import winreg
    except ImportError:
        return None
    try:
        key = winreg.OpenKey(
            winreg.HKEY_LOCAL_MACHINE,
            _WINREG_SUBKEY,
            0,
            winreg.KEY_READ,
        )
    except OSError:
        return None
    try:
        value, _typ = winreg.QueryValueEx(key, STRATEGY_VALUE_NAME)
    except OSError:
        return None
    finally:
        winreg.CloseKey(key)
    text = str(value).strip() if value is not None else ""
    return text or None
