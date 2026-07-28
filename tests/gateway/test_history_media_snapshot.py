import sys
import threading
import types
from types import SimpleNamespace

import pytest

import gateway.run as gateway_run
from gateway.config import Platform, PlatformConfig, StreamingConfig
from gateway.platforms.base import (
    BasePlatformAdapter,
    MessageEvent,
    MessageType,
    SendResult,
)
from gateway.session import SessionSource


class _Adapter(BasePlatformAdapter):
    def __init__(self):
        super().__init__(PlatformConfig(enabled=True, token="test"), Platform.TELEGRAM)

    async def connect(self, *, is_reconnect: bool = False) -> bool:
        return True

    async def disconnect(self) -> None:
        return None

    async def send(self, chat_id, content=None, **kwargs):
        return SendResult(success=True, message_id="sent")

    async def get_chat_info(self, chat_id):
        return {"id": chat_id}


class _CachedAgent:
    def __init__(self, **kwargs):
        self.session_id = kwargs["session_id"]
        self.model = kwargs["model"]
        self.provider = kwargs.get("provider")
        self.tools = []
        self._session_messages = []
        self._last_compaction_in_place = False
        self.context_compressor = SimpleNamespace(
            last_prompt_tokens=0,
            context_length=100_000,
        )
        self.session_prompt_tokens = 0
        self.session_completion_tokens = 0

    def run_conversation(
        self,
        user_message,
        conversation_history=None,
        task_id=None,
        **kwargs,
    ):
        messages = list(conversation_history or [])
        messages.extend(
            [
                {"role": "user", "content": user_message},
                {"role": "assistant", "content": "done"},
            ]
        )
        self._session_messages = messages
        return {
            "final_response": "done",
            "messages": messages,
            "api_calls": 1,
        }

    def interrupt(self, *_args, **_kwargs):
        return None


class _CompactingQueuedAgent(_CachedAgent):
    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self.turn_count = 0

    def run_conversation(
        self,
        user_message,
        conversation_history=None,
        task_id=None,
        **kwargs,
    ):
        self.turn_count += 1
        self._last_compaction_in_place = True
        messages = [
            {"role": "user", "content": user_message},
            {"role": "assistant", "content": f"done {self.turn_count}"},
        ]
        self._session_messages = messages
        result = {
            "final_response": f"done {self.turn_count}",
            "messages": messages,
            "api_calls": 1,
        }
        if self.turn_count == 1:
            result["pending_steer"] = "queued followup"
        return result


def _runner(adapter):
    runner = object.__new__(gateway_run.GatewayRunner)
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner.config = SimpleNamespace(
        multiplex_profiles=False,
        streaming=StreamingConfig(enabled=False),
        group_sessions_per_user=False,
        thread_sessions_per_user=False,
        stt_enabled=False,
    )
    runner.hooks = SimpleNamespace(loaded_hooks=False)
    runner.session_store = SimpleNamespace(_entries={}, _save=lambda: None)
    runner._session_db = None
    runner._agent_cache = {}
    runner._agent_cache_lock = threading.Lock()
    runner._running_agents = {}
    runner._running_agents_ts = {}
    runner._session_run_generation = {}
    runner._prefill_messages = []
    runner._ephemeral_system_prompt = ""
    runner._provider_routing = {}
    runner._fallback_model = None
    runner._draining = False
    runner._get_proxy_url = lambda: None
    runner._resolve_session_agent_runtime = lambda **_kwargs: (
        "test-model",
        {
            "provider": "test-provider",
            "base_url": "https://example.invalid",
            "api_key": "test",
        },
    )
    runner._resolve_session_reasoning_config = lambda **_kwargs: None
    runner._resolve_session_service_tier = lambda **_kwargs: None
    runner._resolve_turn_agent_config = lambda message, model, runtime: {
        "model": model,
        "runtime": runtime,
        "request_overrides": {},
    }
    runner._agent_config_signature = lambda *_args, **_kwargs: ("stable",)
    runner._extract_cache_busting_config = lambda _config: ()
    runner._get_system_prompt_for_channel = lambda *_args, **_kwargs: None
    runner._refresh_fallback_model = lambda: None
    runner._thread_metadata_for_source = lambda *_args, **_kwargs: {}
    runner._release_running_agent_state = lambda *_args, **_kwargs: None
    return runner


@pytest.mark.asyncio
async def test_run_agent_returns_media_snapshot_from_selected_cached_history(
    monkeypatch,
):
    fake_run_agent = types.ModuleType("run_agent")
    fake_run_agent.AIAgent = _CachedAgent
    monkeypatch.setitem(sys.modules, "run_agent", fake_run_agent)
    monkeypatch.setattr(gateway_run, "_load_gateway_config", lambda: {})
    monkeypatch.setattr(gateway_run, "_resolve_gateway_model", lambda: "test-model")
    monkeypatch.setattr(
        "hermes_cli.tools_config._get_platform_tools",
        lambda *_args, **_kwargs: {"core"},
    )

    source = SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="12345",
        chat_type="dm",
    )
    session_key = "agent:main:telegram:dm:12345"
    session_id = "session-media-snapshot"
    runner = _runner(_Adapter())

    await runner._run_agent(
        message="first turn",
        context_prompt="",
        history=[],
        source=source,
        session_id=session_id,
        session_key=session_key,
    )

    cached_agent = runner._agent_cache[session_key][0]
    prior_path = "/tmp/cached-only.png"
    cached_agent._session_messages = [
        {"role": "user", "content": "make an image"},
        {"role": "assistant", "content": f"MEDIA:{prior_path}"},
    ]

    result = await runner._run_agent(
        message="second turn",
        context_prompt="",
        history=[{"role": "user", "content": "stale persisted row"}],
        source=source,
        session_id=session_id,
        session_key=session_key,
    )

    assert result["history_media_paths"] == {prior_path}


@pytest.mark.asyncio
async def test_queued_followup_keeps_snapshot_removed_by_compaction(
    monkeypatch,
):
    fake_run_agent = types.ModuleType("run_agent")
    fake_run_agent.AIAgent = _CompactingQueuedAgent
    monkeypatch.setitem(sys.modules, "run_agent", fake_run_agent)
    monkeypatch.setattr(gateway_run, "_load_gateway_config", lambda: {})
    monkeypatch.setattr(gateway_run, "_resolve_gateway_model", lambda: "test-model")
    monkeypatch.setattr(
        "hermes_cli.tools_config._get_platform_tools",
        lambda *_args, **_kwargs: {"core"},
    )

    source = SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="12345",
        chat_type="dm",
    )
    runner = _runner(_Adapter())
    prior_path = "/tmp/pre-compaction.png"

    result = await runner._run_agent(
        message="first turn",
        context_prompt="",
        history=[
            {"role": "assistant", "content": f"MEDIA:{prior_path}"},
        ],
        source=source,
        session_id="session-queued-snapshot",
        session_key="agent:main:telegram:dm:12345",
    )

    assert result["history_media_paths"] == {prior_path}


@pytest.mark.asyncio
async def test_queued_depth_cap_returns_finalized_media_snapshot(
    monkeypatch,
):
    fake_run_agent = types.ModuleType("run_agent")
    fake_run_agent.AIAgent = _CompactingQueuedAgent
    monkeypatch.setitem(sys.modules, "run_agent", fake_run_agent)
    monkeypatch.setattr(gateway_run, "_load_gateway_config", lambda: {})
    monkeypatch.setattr(gateway_run, "_resolve_gateway_model", lambda: "test-model")
    monkeypatch.setattr(
        "hermes_cli.tools_config._get_platform_tools",
        lambda *_args, **_kwargs: {"core"},
    )

    source = SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="12345",
        chat_type="dm",
    )
    runner = _runner(_Adapter())
    prior_path = "/tmp/depth-capped.png"

    result = await runner._run_agent(
        message="first turn",
        context_prompt="",
        history=[
            {"role": "assistant", "content": f"MEDIA:{prior_path}"},
        ],
        source=source,
        session_id="session-depth-capped",
        session_key="agent:main:telegram:dm:12345",
        _interrupt_depth=runner._MAX_INTERRUPT_DEPTH,
    )

    assert result["history_media_paths"] == {prior_path}


@pytest.mark.asyncio
async def test_stale_goal_discard_returns_finalized_media_snapshot(
    monkeypatch,
):
    fake_run_agent = types.ModuleType("run_agent")
    fake_run_agent.AIAgent = _CachedAgent
    monkeypatch.setitem(sys.modules, "run_agent", fake_run_agent)
    monkeypatch.setattr(gateway_run, "_load_gateway_config", lambda: {})
    monkeypatch.setattr(gateway_run, "_resolve_gateway_model", lambda: "test-model")
    monkeypatch.setattr(
        "hermes_cli.tools_config._get_platform_tools",
        lambda *_args, **_kwargs: {"core"},
    )

    source = SessionSource(
        platform=Platform.TELEGRAM,
        chat_id="12345",
        chat_type="dm",
    )
    session_key = "agent:main:telegram:dm:12345"
    adapter = _Adapter()
    adapter._pending_messages[session_key] = MessageEvent(
        text="[Continuing toward your standing goal]\nGoal: finish",
        message_type=MessageType.TEXT,
        source=source,
    )
    runner = _runner(adapter)
    runner._goal_still_active_for_session = lambda _session_id: False
    prior_path = "/tmp/stale-goal.png"

    result = await runner._run_agent(
        message="first turn",
        context_prompt="",
        history=[
            {"role": "assistant", "content": f"MEDIA:{prior_path}"},
        ],
        source=source,
        session_id="session-stale-goal",
        session_key=session_key,
    )

    assert result["history_media_paths"] == {prior_path}
