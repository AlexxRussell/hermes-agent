from types import SimpleNamespace
from unittest.mock import AsyncMock

from cron import scheduler
from gateway.config import GatewayConfig, Platform, PlatformConfig


def test_cron_delivery_is_mirrored_after_standalone_send(monkeypatch):
    job = {
        "id": "job123",
        "name": "Daily check",
        "deliver": "origin",
        "origin": {
            "platform": "telegram",
            "chat_id": "12345",
            "user_id": "user-1",
        },
    }
    target = {"platform": "telegram", "chat_id": "12345", "thread_id": None}
    mirrored = []

    async def fake_send_to_platform(platform, pconfig, chat_id, message, thread_id=None, media_files=None):
        return {"success": True, "message_id": "777"}

    monkeypatch.setattr(scheduler, "_resolve_delivery_targets", lambda _job: [target])
    monkeypatch.setattr(scheduler, "load_config", lambda: {"cron": {"wrap_response": True}})
    monkeypatch.setattr(
        "gateway.config.load_gateway_config",
        lambda: GatewayConfig(platforms={Platform.TELEGRAM: PlatformConfig(enabled=True, token="tok")}),
    )
    monkeypatch.setattr("tools.send_message_tool._send_to_platform", fake_send_to_platform)
    monkeypatch.setattr(
        "gateway.mirror.mirror_to_session",
        lambda *args, **kwargs: mirrored.append((args, kwargs)) or True,
    )

    assert scheduler._deliver_result(job, "Body") is None

    assert len(mirrored) == 1
    args, kwargs = mirrored[0]
    assert args[:3] == (
        "telegram",
        "12345",
        'Cronjob Response: Daily check\n(job_id: job123)\n-------------\n\nBody\n\nTo stop or manage this job, send me a new message (e.g. "stop reminder Daily check").',
    )
    assert kwargs["source_label"] == "cron:job123"
    assert kwargs["user_id"] == "user-1"
    assert kwargs["platform_message_id"] == "777"
    assert kwargs["observed"] is True


def test_cron_delivery_mirror_can_be_disabled(monkeypatch):
    job = {"id": "job123", "deliver": "telegram"}
    target = {"platform": "telegram", "chat_id": "12345", "thread_id": None}
    calls = []

    monkeypatch.setattr(scheduler, "load_config", lambda: {"cron": {"mirror_deliveries": False}})
    monkeypatch.setattr(
        "gateway.mirror.mirror_to_session",
        lambda *args, **kwargs: calls.append((args, kwargs)) or True,
    )

    scheduler._mirror_cron_delivery_to_session(job, target, "message", platform_message_id="777")

    assert calls == []


def test_cron_delivery_is_mirrored_after_live_adapter_send(monkeypatch):
    job = {"id": "job123", "name": "Daily check", "deliver": "telegram"}
    target = {"platform": "telegram", "chat_id": "12345", "thread_id": None}
    mirrored = []

    class FakeFuture:
        def result(self, timeout=None):
            return SimpleNamespace(success=True, message_id="888", raw_response={})

    class FakeLoop:
        def is_running(self):
            return True

    class FakeAdapter:
        def send(self, chat_id, text, metadata=None):
            return object()

    monkeypatch.setattr(scheduler, "_resolve_delivery_targets", lambda _job: [target])
    monkeypatch.setattr(scheduler, "load_config", lambda: {"cron": {"wrap_response": False}})
    monkeypatch.setattr(
        "gateway.config.load_gateway_config",
        lambda: GatewayConfig(platforms={Platform.TELEGRAM: PlatformConfig(enabled=True, token="tok")}),
    )
    monkeypatch.setattr("agent.async_utils.safe_schedule_threadsafe", lambda coro, loop: FakeFuture())
    monkeypatch.setattr(
        "gateway.mirror.mirror_to_session",
        lambda *args, **kwargs: mirrored.append((args, kwargs)) or True,
    )

    assert scheduler._deliver_result(
        job,
        "Body",
        adapters={Platform.TELEGRAM: FakeAdapter()},
        loop=FakeLoop(),
    ) is None

    assert len(mirrored) == 1
    args, kwargs = mirrored[0]
    assert args[:3] == ("telegram", "12345", "Body")
    assert kwargs["platform_message_id"] == "888"
