from __future__ import annotations

from pathlib import Path

from zapret_gui import STRATEGY_REG_KEY, STRATEGY_VALUE_NAME
from zapret_gui.identity import (
    RegistryWrite,
    persist_installed_strategy,
    read_installed_strategy,
    strategy_stem,
)
from zapret_gui.services import CompletedScm, ScmCommand, query_service_status
from zapret_gui.strategies import parse_strategy


def test_persist_uses_general_bat_stem_and_service_bat_value(project_root: Path) -> None:
    parsed = parse_strategy(project_root / "general.bat", project_root)
    stem = strategy_stem(parsed.name)
    assert stem == Path(project_root / "general.bat").stem
    seen: list[RegistryWrite] = []
    result = persist_installed_strategy(stem, writer=seen.append)
    assert result.ok is True
    assert result.executed is True
    assert len(seen) == 1
    plan = seen[0]
    assert plan.value_name == "zapret-discord-youtube"
    assert plan.value_name == STRATEGY_VALUE_NAME
    assert plan.key.replace("/", "\\").upper().endswith(r"\SERVICES\ZAPRET")
    assert "HKLM" in plan.key.upper()
    assert plan.key.upper() == STRATEGY_REG_KEY.upper()
    assert plan.data == stem
    assert plan.data == "general"


def test_persist_refuses_live_registry_write() -> None:
    result = persist_installed_strategy("general", allow_live=False)
    assert result.ok is False
    assert result.executed is False
    assert result.plan.value_name == "zapret-discord-youtube"
    assert "general" == result.plan.data


def test_read_returns_stem_and_status_message_includes_it() -> None:
    stored = {"stem": "general"}

    def reader() -> str | None:
        return stored["stem"]

    assert read_installed_strategy(reader=reader) == "general"

    def runner(cmd: ScmCommand) -> CompletedScm:
        assert cmd.action == "status"
        return CompletedScm(
            returncode=0,
            stdout="SERVICE_NAME: zapret\n        STATE              : 4  RUNNING\n",
            stderr="",
        )

    status = query_service_status("zapret", runner=runner, strategy_reader=reader)
    assert status.state == "running"
    assert status.strategy_name == "general"
    assert "general" in status.message


def test_status_without_registry_value_still_maps_state() -> None:
    def reader() -> str | None:
        return None

    def runner(cmd: ScmCommand) -> CompletedScm:
        return CompletedScm(
            returncode=0,
            stdout="SERVICE_NAME: zapret\n        STATE              : 1  STOPPED\n",
            stderr="",
        )

    status = query_service_status("zapret", runner=runner, strategy_reader=reader)
    assert status.state == "stopped"
    assert status.strategy_name is None
    assert "general" not in status.message.lower()

    def missing_runner(cmd: ScmCommand) -> CompletedScm:
        return CompletedScm(returncode=1060, stdout="", stderr="does not exist")

    missing = query_service_status("zapret", runner=missing_runner, strategy_reader=reader)
    assert missing.state == "not_installed"
    assert missing.strategy_name is None
