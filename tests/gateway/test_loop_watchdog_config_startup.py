"""Real YAML-to-startup coverage of the opt-out reported in #76541."""

import asyncio
from unittest.mock import MagicMock

import pytest


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("diagnostics", [False, True])
def test_yaml_watchdog_config_reaches_startup(tmp_path, monkeypatch, enabled, diagnostics):
    from gateway.config import GatewayConfig
    from gateway.run import GatewayRunner, load_gateway_config_for_runner
    from gateway import shutdown_watchdog
    from hermes_cli.config_defaults import DEFAULT_CONFIG

    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    (tmp_path / "config.yaml").write_text(
        "gateway:\n"
        f"  loop_watchdog: {str(enabled).lower()}\n"
        f"  loop_watchdog_diagnostics: {str(diagnostics).lower()}\n"
        "  loop_watchdog_probe_interval_s: 45\n"
        "  loop_watchdog_probe_timeout_s: 15\n"
        "  loop_watchdog_max_strikes: 12\n",
        encoding="utf-8",
    )
    # Use the process runner's real loader, then its real guard. Only the arm
    # boundary is intercepted so this test cannot start an exiting daemon.
    runner = object.__new__(GatewayRunner)
    runner.config = load_gateway_config_for_runner()
    arm = MagicMock()
    floor = MagicMock()
    monkeypatch.setattr(shutdown_watchdog, "start_loop_liveness_watchdog", arm)
    monkeypatch.setattr(shutdown_watchdog, "_arm_loop_floor_timer", floor)
    loop = MagicMock(spec=asyncio.AbstractEventLoop)
    runner._start_loop_liveness_guards(loop)
    assert runner.config.loop_watchdog is enabled
    assert runner.config.loop_watchdog_diagnostics is diagnostics
    serialized = runner.config.to_dict()
    assert GatewayConfig.from_dict(serialized).loop_watchdog_diagnostics is diagnostics
    assert GatewayConfig.from_dict({}).loop_watchdog_diagnostics == DEFAULT_CONFIG["gateway"]["loop_watchdog_diagnostics"]
    if enabled:
        floor.assert_called_once_with(loop)
        arm.assert_called_once_with(loop, probe_interval=45.0, probe_timeout=15.0, max_strikes=12,
                                    diagnostics=diagnostics, executor_owner=runner)
    else:
        floor.assert_not_called()
        arm.assert_not_called()
