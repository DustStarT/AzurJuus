import asyncio
import threading
import time

import httpx
import pytest

from backend.hermes_bridge import HermesBridge
from backend.realtime import RealtimeHub
from conftest import configure_test_env


def test_server_shutdown_cancels_pending_http_approval(monkeypatch, tmp_path):
    configure_test_env(monkeypatch, tmp_path)
    monkeypatch.setenv("AZURJUUS_WORKSPACE_STATE_PATH", str(tmp_path / "state" / "workspace.json"))
    monkeypatch.setenv("AZURJUUS_EXECUTION_BACKEND", "hermes")
    from backend.config import get_settings
    get_settings.cache_clear()
    from server import create_server
    server = create_server(port=0)
    c = server.app.state.runs
    entered = threading.Event()

    class ApprovalBridge:
        session_id = None
        def __init__(self, home, settings, endpoint, token, event):
            self.endpoint, self.token = endpoint, token
        async def start(self):
            pass
        async def close(self):
            pass
        async def prompt(self, *args):
            async with httpx.AsyncClient(timeout=30) as client:
                entered.set()
                await client.post(self.endpoint, headers={"Authorization": "Bearer " + self.token},
                    json={"callId": "pendingapproval", "name": "command", "args": {"argv": ["echo", "synthetic"]}})
            raise AssertionError("Unapproved command must never finish")

    c.bridge_factory = ApprovalBridge
    c.settings_loader = lambda: {"llmApiKey": "synthetic"}
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    run = None
    try:
        for _ in range(200):
            if server._server.started:
                break
            time.sleep(.02)
        assert server._server.started
        run, _ = c.store.create({"prompt": "synthetic", "mode": "task", "collaborative": False, "conversationId": "test",
            "actors": [{"id": "a", "name": "A"}], "actorId": "a", "workspace": str(tmp_path), "grants": []}, "shutdown")
        server._loop.call_soon_threadsafe(c.launch, run["id"])
        assert entered.wait(5), c.store.get(run["id"])
        for _ in range(200):
            if c.store.get(run["id"])["status"] == "waiting_approval":
                break
            time.sleep(.02)
        assert c.store.get(run["id"])["status"] == "waiting_approval"
        server.shutdown()
        server.shutdown()  # concurrent lifecycle callers share one cleanup
        thread.join(5)
        assert not thread.is_alive(), "HTTP approval drain must not block lifespan shutdown"
        assert c.store.get(run["id"])["status"] == "paused"
        assert not c.tool_tasks
        assert not c.tasks
        assert c.store.call("pendingapproval")["status"] != "completed"
    finally:
        server.shutdown()
        thread.join(5)
        server.server_close()


@pytest.mark.asyncio
async def test_realtime_stop_handles_cancelled_listener():
    hub = RealtimeHub()
    hub._listener_task = asyncio.create_task(asyncio.Event().wait())
    await asyncio.sleep(0)
    await hub.stop()
    assert hub._listener_task.cancelled()


@pytest.mark.asyncio
async def test_transport_stages_and_disk_diagnostics_hide_credentials(tmp_path):
    events = []
    async def on_event(kind, payload):
        events.append((kind, payload))
    bridge = HermesBridge(tmp_path, {"llmApiKey": "synthetic-secret"}, "local", "synthetic-token", on_event)
    bridge.diagnostic("synthetic-secret synthetic-token")
    await bridge.stage("starting", "正在启动执行核心")
    log = (tmp_path / "bridge.log").read_text(encoding="utf-8")
    assert "synthetic-secret" not in log and "synthetic-token" not in log
    assert events == [("runtime.stage", {"stage": "starting", "label": "正在启动执行核心"})]


@pytest.mark.asyncio
async def test_empty_chat_reply_is_visible_failure(tmp_path):
    from backend.run_coordinator import RunCoordinator
    from backend.run_store import RunStore
    completed = []
    async def finished(*args):
        completed.append(args)
    class EmptyBridge:
        def __init__(self, *args):
            pass
        async def start(self):
            pass
        async def close(self):
            pass
        async def prompt(self, *args):
            return '  '
    store = RunStore(tmp_path / 'runs.db')
    c = RunCoordinator(store, lambda: {"llmApiKey": "synthetic"}, finished, "local", EmptyBridge)
    run, _ = store.create({"prompt": "synthetic", "mode": "chat", "conversationId": "test",
        "actors": [{"id": "a", "name": "A"}], "actorId": "a", "workspace": str(tmp_path)}, "empty")
    await c.run(run['id'])
    assert store.get(run['id'])['status'] == 'failed'
    assert '没有返回' in store.get(run['id'])['error']
    assert not completed
    await c.close()
