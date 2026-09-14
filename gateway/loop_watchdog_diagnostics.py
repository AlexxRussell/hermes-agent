"""Bounded first-miss evidence, independent of logging and executor availability."""

from __future__ import annotations

import contextlib
import json
import os
import sys
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from itertools import islice
from pathlib import Path

PROBE_HISTORY_LIMIT = 16
THREAD_LIMIT = 64
FRAME_LIMIT = 32
DUMP_BYTE_LIMIT = 512 * 1024
DUMP_NAME = "gateway-loop-watchdog.json"


class TimedProbe(threading.Event):
    def __init__(self):
        super().__init__()
        self.scheduled_at = time.monotonic()
        self.acknowledged_at = None
        self.deadline = None
        self.observed_at = None
        self.missed = None

    def set(self):
        # A bound method keeps a late acknowledgement attached to its own probe.
        self.acknowledged_at = time.monotonic()
        super().set()

    def to_dict(self):
        return {key: getattr(self, key) for key in (
            "scheduled_at", "acknowledged_at", "deadline", "observed_at", "missed",
        )}


def _executor_snapshot(executor):
    if executor is None:
        return {"available": False}
    if type(executor) is not ThreadPoolExecutor:
        return {"available": False, "type": type(executor).__name__[:128]}
    # Observational CPython fields only. Never submit work, acquire a pool lock,
    # or instantiate the loop's lazy default executor to collect diagnostics.
    return {
        "available": True,
        "id": hex(id(executor)),
        "max_workers": executor._max_workers,
        "queue_size_approx": executor._work_queue.qsize(),
        "thread_name_prefix": executor._thread_name_prefix[:128],
        "thread_ids": [t.ident for t in islice(executor._threads.copy(), THREAD_LIMIT)],
        "thread_count": len(executor._threads),
    }


def _thread_snapshot(loop_thread_id):
    captured_at = time.monotonic()
    frames = sys._current_frames()
    # Avoid enumerate()'s thread-registration lock in a potentially wedged process.
    active = getattr(threading, "_active", {}).copy()
    identifiers = [loop_thread_id] + [ident for ident in frames if ident != loop_thread_id]
    threads = []
    for ident in identifiers[:THREAD_LIMIT]:
        frame = frames.get(ident)
        thread = active.get(ident)
        stack = []
        for _ in range(FRAME_LIMIT):
            if frame is None:
                break
            # Do not read source lines, locals, or repr user objects: those can
            # perform I/O, run arbitrary code, or expose credentials.
            stack.append({"file": frame.f_code.co_filename[:256],
                          "line": frame.f_lineno, "function": frame.f_code.co_name[:128]})
            frame = frame.f_back
        threads.append({
            "id": ident, "id_hex": hex(ident),
            "name": thread.name[:128] if thread else None,
            "native_id": thread.native_id if thread else None,
            "stack": stack, "stack_truncated": frame is not None,
        })
    return {"captured_at": captured_at, "loop_thread_id": loop_thread_id,
            "thread_count": len(frames), "threads_truncated": len(frames) > THREAD_LIMIT,
            "threads": threads}


def _write_snapshot(path: Path, snapshot: dict) -> None:
    """One fixed-size artifact plus one fixed staging file, replaced rather than appended."""
    payload = json.dumps(snapshot, ensure_ascii=True).encode("ascii")
    while len(payload) > DUMP_BYTE_LIMIT and snapshot["threads"]:
        snapshot["threads"].pop()
        snapshot["threads_truncated"] = True
        payload = json.dumps(snapshot, ensure_ascii=True).encode("ascii")
    if len(payload) > DUMP_BYTE_LIMIT:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    staging = path.with_suffix(".tmp")
    try:
        with staging.open("wb") as stream:
            stream.write(payload)
        os.replace(staging, path)
    finally:
        staging.unlink(missing_ok=True)


class LoopWatchdogDiagnostics:
    def __init__(self, loop, executor_owner, home: Path):
        self.loop = loop
        self.executor_owner = executor_owner
        self.path = home / "logs" / DUMP_NAME
        self.loop_thread_id = threading.get_ident()
        self.probes = deque(maxlen=PROBE_HISTORY_LIMIT)
        self.snapshot = None
        self.pending = None
        self.writer_lock = threading.Lock()
        self.writer = None

    def observe(self, probe: TimedProbe, strikes: int):
        """Capture synchronously before any output, then hand off immutable scalar data."""
        probe.observed_at = time.monotonic()
        probe.missed = strikes > 0
        self.probes.append(probe)
        if strikes == 1:
            self.snapshot = {
                "event": "loop_watchdog_first_miss", "pid": os.getpid(),
                **_thread_snapshot(self.loop_thread_id),
                "executors": {
                    "loop_default": _executor_snapshot(getattr(self.loop, "_default_executor", None)),
                    "gateway": _executor_snapshot(getattr(self.executor_owner, "_executor", None)),
                },
            }
        if self.snapshot is None:
            return
        snapshot = {**self.snapshot, "strikes": strikes, "observed_at": probe.observed_at,
                    "recovered": strikes == 0, "probes": [p.to_dict() for p in self.probes]}
        # The writer may trim its list to the byte budget; keep the original snapshot intact.
        snapshot["threads"] = list(snapshot["threads"])
        with self.writer_lock:
            self.pending = snapshot
            if self.writer is None:
                self.writer = threading.Thread(target=self._write_pending, daemon=True,
                                               name="gateway-loop-diagnostics")
                self.writer.start()
        if strikes == 0:
            self.snapshot = None

    def _write_pending(self):
        while True:
            # This lock protects only the one-slot handoff, never formatting or
            # I/O. Retire when drained, with no timer wakeups on a healthy loop.
            with self.writer_lock:
                snapshot, self.pending = self.pending, None
                if snapshot is None:
                    self.writer = None
                    return
            # Logging here could block behind the very handler under investigation.
            with contextlib.suppress(Exception):
                _write_snapshot(self.path, snapshot)
