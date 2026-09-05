"""A gateway child that cannot get a restart-safe systemd scope is dispatched directly, not refused.

``restart_safe_gateway_child_argv`` failed closed whenever ``systemd-run --user --scope`` was
unavailable to a systemd-supervised gateway. That is the normal state of a root system unit
whose environment carries no ``XDG_RUNTIME_DIR`` or ``DBUS_SESSION_BUS_ADDRESS`` (and of
containers without a user session), so every cron dispatch on such a host failed with
"Restart-safe cron worker dispatch failed" and every scheduled job silently stopped. On
5 Sep 2026 a 1 GB Ubuntu 22.04 box lost its finance watchdogs, self-check and Schengen
watchdog for hours this way.

Now the child is dispatched as a plain subprocess (the behaviour before the scope was
introduced) with a WARNING that names the unit and the remedy; the hard failure stays
available behind ``HERMES_GATEWAY_CHILD_REQUIRE_SCOPE=1`` (cron-scope-degrade). The tests
force the Linux branch so the contract is checked on every platform.
"""

from __future__ import annotations

import logging
from unittest.mock import Mock

import pytest

import tools.process_registry as process_registry

COMMAND = ["python", "-m", "cron.scheduler", "--external-worker-file", "payload.json"]


@pytest.fixture
def managed_gateway(monkeypatch):
    """A systemd-supervised Linux gateway whose user scope probe fails."""
    monkeypatch.setattr(process_registry, "_IS_LINUX", True)
    monkeypatch.setattr(process_registry, "_is_supervised_gateway_process", lambda: True)
    monkeypatch.setenv("INVOCATION_ID", "managed-service")
    monkeypatch.setattr(process_registry, "_systemd_run_user_scope_available", lambda: False)
    monkeypatch.delenv(process_registry._GATEWAY_CHILD_REQUIRE_SCOPE_ENV, raising=False)
    return monkeypatch


def test_degrades_to_the_plain_command_with_a_warning(managed_gateway, caplog):
    with caplog.at_level(logging.WARNING, logger="tools.process_registry"):
        result = process_registry.restart_safe_gateway_child_argv(COMMAND, unit_suffix="cron-job-1-exec-9")

    assert result is COMMAND
    (record,) = [r for r in caplog.records if "restart-safe cgroup isolation" in r.getMessage()]
    assert record.levelno == logging.WARNING
    assert "cron-job-1-exec-9" in record.getMessage()
    assert "systemd-run --user --scope is unavailable" in record.getMessage()
    assert "HERMES_GATEWAY_CHILD_REQUIRE_SCOPE=1" in record.getMessage()


def test_fails_closed_when_the_operator_asks_for_it(managed_gateway):
    managed_gateway.setenv(process_registry._GATEWAY_CHILD_REQUIRE_SCOPE_ENV, " 1 ")

    with pytest.raises(RuntimeError, match="systemd-run --user --scope is unavailable"):
        process_registry.restart_safe_gateway_child_argv(COMMAND, unit_suffix="cron-job-1")


@pytest.mark.parametrize("value", ["", "0", "true", "yes"])
def test_only_the_literal_one_means_fail_closed(managed_gateway, value):
    managed_gateway.setenv(process_registry._GATEWAY_CHILD_REQUIRE_SCOPE_ENV, value)

    assert process_registry.restart_safe_gateway_child_argv(COMMAND, unit_suffix="cron-job-1") is COMMAND


def test_a_scope_that_vanishes_after_the_probe_degrades_too(managed_gateway, caplog):
    managed_gateway.setattr(process_registry, "_systemd_run_user_scope_available", lambda: True)
    managed_gateway.setattr(process_registry, "_build_systemd_scope_argv", lambda command, unit_suffix: command)

    with caplog.at_level(logging.WARNING, logger="tools.process_registry"):
        assert process_registry.restart_safe_gateway_child_argv(COMMAND, unit_suffix="cron-job-2") is COMMAND
    assert "systemd-run disappeared after the availability probe" in caplog.text

    managed_gateway.setenv(process_registry._GATEWAY_CHILD_REQUIRE_SCOPE_ENV, "1")
    with pytest.raises(RuntimeError, match="disappeared after the availability probe"):
        process_registry.restart_safe_gateway_child_argv(COMMAND, unit_suffix="cron-job-2")


def test_an_available_scope_is_still_used(managed_gateway):
    managed_gateway.setattr(process_registry, "_systemd_run_user_scope_available", lambda: True)
    managed_gateway.setattr(
        process_registry, "_build_systemd_scope_argv",
        lambda command, unit_suffix: ["systemd-run", "--user", "--scope", "--unit", unit_suffix, "--", *command])

    scoped = process_registry.restart_safe_gateway_child_argv(COMMAND, unit_suffix="cron-job-3")

    assert scoped[:3] == ["systemd-run", "--user", "--scope"]
    assert scoped[-len(COMMAND):] == COMMAND


def test_the_scheduler_runs_the_job_in_process_when_the_child_argv_is_unchanged(managed_gateway, caplog):
    """The scheduler hands off only when the argv was wrapped; a degraded child stays in-process."""
    import cron.scheduler as scheduler

    popen = Mock()
    managed_gateway.setattr(scheduler.subprocess, "Popen", popen)

    with caplog.at_level(logging.WARNING, logger="tools.process_registry"):
        handed_off = scheduler._launch_external_cron_worker({"id": "job-1", "execution_id": "exec-1"})

    assert handed_off is False
    popen.assert_not_called()
    assert "restart-safe cgroup isolation" in caplog.text
