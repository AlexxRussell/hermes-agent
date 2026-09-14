"""First-miss evidence and recovery independence from diagnostic output."""

import asyncio
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from gateway import loop_watchdog_diagnostics as diagnostics
from gateway import shutdown_watchdog as watchdog


def test_first_miss_preserves_stacks_and_late_probe_acknowledgements(tmp_path, monkeypatch):
    loop = asyncio.new_event_loop()
    loop_started = threading.Event()
    blocked = threading.Event()
    release = threading.Event()
    healthy = threading.Event()
    captured = threading.Event()
    recovered = threading.Event()
    snapshots = []
    handles = []
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    original_set = diagnostics.TimedProbe.set
    original_write = diagnostics._write_snapshot

    def acknowledge(probe):
        original_set(probe)
        healthy.set()

    def write_snapshot(path, snapshot):
        original_write(path, snapshot)
        snapshots.append(json.loads(path.read_text()))
        (recovered if snapshot["recovered"] else captured).set()

    monkeypatch.setattr(diagnostics.TimedProbe, "set", acknowledge)
    monkeypatch.setattr(diagnostics, "_write_snapshot", write_snapshot)

    def run_loop():
        asyncio.set_event_loop(loop)
        loop.call_soon(loop_started.set)
        loop.run_forever()

    def hold_loop():
        blocked.set()
        release.wait(10)

    thread = threading.Thread(target=run_loop, name="evidence-loop", daemon=True)
    thread.start()
    assert loop_started.wait(5)
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="resolver-pool") as default_pool, \
            ThreadPoolExecutor(max_workers=1, thread_name_prefix="gateway-pool") as gateway_pool:
        owner = SimpleNamespace(_executor=gateway_pool)
        workers_ready = [threading.Event(), threading.Event()]

        def occupy_pool(ready):
            ready.set()
            release.wait(10)

        for pool, ready in zip((default_pool, gateway_pool), workers_ready):
            pool.submit(occupy_pool, ready)
            assert ready.wait(5)
            pool.submit(lambda: None)
        loop.set_default_executor(default_pool)
        loop.call_soon_threadsafe(lambda: handles.append(watchdog.start_loop_liveness_watchdog(
            loop, probe_interval=0.05, probe_timeout=0.05, max_strikes=1000,
            diagnostics=True, executor_owner=owner,
        )))
        try:
            assert healthy.wait(5)
            loop.call_soon_threadsafe(hold_loop)
            assert blocked.wait(5)
            assert captured.wait(5), "no diagnostics on the first missed probe"
            first = snapshots[0]
            assert first["strikes"] == 1
            assert not first["recovered"]
            assert first["probes"][-1]["missed"]
            assert first["probes"][-1]["acknowledged_at"] is None
            assert first["probes"][-1]["scheduled_at"] < first["probes"][-1]["deadline"]
            assert first["probes"][-1]["deadline"] <= first["captured_at"]
            for pool in first["executors"].values():
                assert pool["queue_size_approx"] >= 1
                assert pool["thread_count"] == pool["max_workers"] == 1
                assert pool["thread_ids"]
            loop_stack = next(t for t in first["threads"] if t["id"] == thread.ident)
            assert loop_stack["name"] == thread.name
            assert loop_stack["native_id"] == thread.native_id
            assert any(frame["function"] == "hold_loop" for frame in loop_stack["stack"])
            release.set()
            assert recovered.wait(5)
            last = snapshots[-1]
            assert last["threads"] == first["threads"], "recovery overwrote the stall stack"
            missed = next(p for p in last["probes"] if p["missed"])
            assert missed["acknowledged_at"] >= missed["deadline"]
            successful = [p for p in last["probes"] if not p["missed"]]
            assert successful
            assert all(p["scheduled_at"] <= p["acknowledged_at"] <= p["observed_at"] for p in successful)
        finally:
            release.set()
            for handle in handles:
                handle.stop()
                handle.join(5)
            loop.call_soon_threadsafe(loop.stop)
            thread.join(5)
            loop.close()
            for writer in threading.enumerate():
                if writer.name == "gateway-loop-diagnostics":
                    writer.join(5)


@pytest.mark.parametrize("failure", ["blocked_write", "failed_write", "failed_capture", "failed_init", "disabled"])
def test_diagnostic_failure_and_repeated_stalls_keep_probing_with_bounded_storage(
    tmp_path, monkeypatch, failure,
):
    release = threading.Event()
    writing = threading.Event()
    finished = threading.Event()
    probes = []
    captured = []
    monkeypatch.setenv("HERMES_HOME", str(tmp_path))
    real_capture = diagnostics._thread_snapshot
    real_write = diagnostics._write_snapshot

    def capture(ident):
        captured.append(ident)
        if failure == "failed_capture":
            raise RuntimeError("stack inspection unavailable")
        return real_capture(ident)

    def write(path, snapshot):
        writing.set()
        if failure == "blocked_write":
            release.wait(10)
        if failure == "failed_write":
            raise OSError("disk unavailable")
        real_write(path, snapshot)

    monkeypatch.setattr(diagnostics, "_thread_snapshot", capture)
    monkeypatch.setattr(diagnostics, "_write_snapshot", write)
    if failure == "failed_init":
        def fail_init(*args):
            raise RuntimeError("diagnostic initialization unavailable")
        monkeypatch.setattr(diagnostics, "LoopWatchdogDiagnostics", fail_init)
    loop = MagicMock(spec=asyncio.AbstractEventLoop)

    def schedule(callback):
        probes.append(callback)
        # Many short stalls alternate with recovery. A blocked writer must not
        # consume workers or stop either acknowledgements or subsequent probes.
        if len(probes) % 2 == 0:
            probes[-2]()
            callback()
        if len(probes) >= diagnostics.PROBE_HISTORY_LIMIT * 3:
            finished.set()

    loop.call_soon_threadsafe.side_effect = schedule
    handle = watchdog.start_loop_liveness_watchdog(
        loop, probe_interval=0.001, probe_timeout=0.001,
        diagnostics=failure != "disabled",
    )
    try:
        assert finished.wait(5), "diagnostics blocked subsequent probes"
        if failure == "disabled":
            assert not captured and not writing.is_set()
        elif failure not in {"failed_capture", "failed_init"}:
            assert writing.wait(5)
            assert len([t for t in threading.enumerate() if t.name == "gateway-loop-diagnostics"]) <= 1
    finally:
        handle.stop()
        release.set()
        handle.join(5)
        for thread in threading.enumerate():
            if thread.name == "gateway-loop-diagnostics":
                thread.join(5)
    artifacts = list((tmp_path / "logs").glob("*"))
    if failure == "blocked_write":
        assert [p.name for p in artifacts] == [diagnostics.DUMP_NAME]
        payload = json.loads(artifacts[0].read_text())
        assert len(payload["probes"]) <= diagnostics.PROBE_HISTORY_LIMIT
        assert artifacts[0].stat().st_size <= diagnostics.DUMP_BYTE_LIMIT
        # Exercise the byte bound with valid but large stack metadata.
        payload["threads"] = [{"name": "x" * diagnostics.DUMP_BYTE_LIMIT}] * 3
        real_write(artifacts[0], payload)
        assert artifacts[0].stat().st_size <= diagnostics.DUMP_BYTE_LIMIT
        assert json.loads(artifacts[0].read_text())["threads_truncated"]
    else:
        assert not artifacts


@pytest.mark.parametrize("blocked_step", ["log", "dump", "ledger"])
def test_final_reporting_cannot_prevent_exit_at_the_configured_strike_count(monkeypatch, blocked_step):
    entered = threading.Event()
    release = threading.Event()
    exited = threading.Event()
    exit_codes = []
    loop = MagicMock(spec=asyncio.AbstractEventLoop)

    def block(*args, **kwargs):
        entered.set()
        release.wait(10)

    def exit_process(code):
        exit_codes.append(code)
        exited.set()

    monkeypatch.setattr(watchdog.logger, "critical", block if blocked_step == "log" else lambda *a: None)
    monkeypatch.setattr(watchdog.faulthandler, "dump_traceback", block if blocked_step == "dump" else lambda **k: None)
    monkeypatch.setattr(watchdog, "_mark_exited_quietly", block if blocked_step == "ledger" else lambda *a: None)
    monkeypatch.setattr(watchdog.os, "_exit", exit_process)
    handle = watchdog.start_loop_liveness_watchdog(loop, probe_interval=0.01, probe_timeout=0.01, max_strikes=3)
    try:
        assert entered.wait(5)
        assert exited.wait(3), "fatal reporting prevented supervisor recovery"
        assert loop.call_soon_threadsafe.call_count == 3
        assert exit_codes == [75]
    finally:
        handle.stop()
        release.set()
        handle.join(5)
        for thread in threading.enumerate():
            if thread.name == "gateway-loop-final-report":
                thread.join(5)
