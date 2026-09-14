"""Diagnostic controls for the September 2026 gateway watchdog incidents.

These controls distinguish waiting network tasks from a stalled event loop.
They do not reproduce the production stalls.
"""

import asyncio
from concurrent.futures import ThreadPoolExecutor
import socket
import threading

import httpx
import pytest

pytest.importorskip("telegram.request")

from telegram.error import TimedOut
from telegram.request import HTTPXRequest

from plugins.platforms.telegram.telegram_network import TelegramFallbackTransport


async def _probe_loop_from_thread():
    """Use the same executor-independent round trip as the liveness watchdog."""
    loop = asyncio.get_running_loop()
    finished = asyncio.Event()
    acknowledgements = []

    def probe():
        try:
            for _ in range(3):
                acknowledged = threading.Event()
                loop.call_soon_threadsafe(acknowledged.set)
                acknowledgements.append(acknowledged.wait(timeout=5))
        finally:
            loop.call_soon_threadsafe(finished.set)

    thread = threading.Thread(target=probe, daemon=True)
    thread.start()
    try:
        await asyncio.wait_for(finished.wait(), timeout=20)
    finally:
        thread.join(timeout=2)
    assert acknowledgements == [True, True, True]


def test_saturated_dns_executor_does_not_starve_loop_probes(monkeypatch):
    async def scenario():
        loop = asyncio.get_running_loop()
        # CPython 3.11's default capacity when os.cpu_count() returns one.
        capacity = 5
        pool = ThreadPoolExecutor(max_workers=capacity)
        loop.set_default_executor(pool)
        saturated = asyncio.Event()
        release = threading.Event()
        started_lock = threading.Lock()
        started = 0

        def held_resolver(*args, **kwargs):
            nonlocal started
            with started_lock:
                started += 1
                if started == capacity:
                    loop.call_soon_threadsafe(saturated.set)
            if not release.wait(timeout=30):
                raise TimeoutError("resolver control was not released")
            return []

        monkeypatch.setattr(socket, "getaddrinfo", held_resolver)
        requests = [
            asyncio.create_task(loop.getaddrinfo("held.invalid", 443))
            for _ in range(capacity)
        ]
        queued = None
        queued_ran = threading.Event()
        try:
            await asyncio.wait_for(saturated.wait(), timeout=10)
            queued = loop.run_in_executor(None, queued_ran.set)
            requests[0].cancel()
            await asyncio.gather(requests[0], return_exceptions=True)
            await _probe_loop_from_thread()
            assert not queued_ran.is_set()
            assert not queued.done()
            assert all(not request.done() for request in requests[1:])
        finally:
            release.set()
            await asyncio.gather(*requests, return_exceptions=True)
            if queued is not None:
                await asyncio.wait_for(queued, timeout=10)
        assert queued_ran.is_set()

    asyncio.run(scenario())


def test_silent_tls_peer_suspends_ptb_request_and_connect_timeout_fires():
    async def scenario():
        client_hello = asyncio.Event()
        release_peer = asyncio.Event()
        peer_closed = asyncio.Event()

        async def silent_peer(reader, writer):
            try:
                if await reader.read(65536):
                    client_hello.set()
                    await release_peer.wait()
            finally:
                writer.close()
                await writer.wait_closed()
                peer_closed.set()

        server = await asyncio.start_server(silent_peer, "127.0.0.1", 0)
        port = server.sockets[0].getsockname()[1]
        transport = TelegramFallbackTransport([], proxy=None, trust_env=False)
        request = HTTPXRequest(
            connect_timeout=5,
            httpx_kwargs={"transport": transport, "trust_env": False},
        )
        pending = asyncio.create_task(
            request.do_request(f"https://127.0.0.1:{port}/", "GET")
        )
        try:
            await asyncio.wait_for(client_hello.wait(), timeout=10)
            await _probe_loop_from_thread()
            assert not pending.done()
            with pytest.raises(TimedOut) as error:
                await asyncio.wait_for(pending, timeout=15)
            assert isinstance(error.value.__cause__, httpx.ConnectTimeout)
        finally:
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
            release_peer.set()
            await request.shutdown()
            server.close()
            await server.wait_closed()
            if client_hello.is_set():
                await asyncio.wait_for(peer_closed.wait(), timeout=10)

    asyncio.run(scenario())
