import asyncio

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from starlette.websockets import WebSocketDisconnect

from backend.app import create_app
from backend.database import get_engine, session_scope
from backend.idle_social import run_idle_social
from backend.models import SocialPost
from backend.run_store import RunStore
from conftest import configure_test_env


@pytest.mark.asyncio
async def test_social_model_waits_without_database_connection_and_can_be_preempted(monkeypatch, tmp_path):
    configure_test_env(monkeypatch, tmp_path)
    app = create_app()
    with TestClient(app):
        service = app.state.service
        service.bundle.social.enabled = True
        monkeypatch.setattr(service.bundle.social, "should_schedule", lambda **kw: True)
        with session_scope() as session:
            workspace = service.get_workspace(session)
            workspace.allow_idle_social = True
            workspace.llm_api_key = "synthetic-model-key"
        requests = []

        async def generate(**kw):
            assert get_engine().pool.checkedout() == 0
            # An independent writer can commit while the model is waiting.
            with session_scope() as session:
                service.get_workspace(session).social_interval_minutes = 7
            requests.append(kw["prompt"])
            await asyncio.sleep(0)
            return "合成社交内容"

        monkeypatch.setattr(service.bundle.runtime, "generate_social_post", generate)
        monkeypatch.setattr(service.bundle.runtime, "generate_social_comment", generate)
        assert await run_idle_social(service) is None, 'No experience must not invent a scheduled life event'
        with session_scope() as session:
            aids = [a.id for a in service.list_active_agents(session, service.get_workspace(session))]
        def seed_outcomes(key):
            app.state.runs.store.event(None, 'mind.observation', {'key':key, 'actorIds':aids,
                'kind':'outcome', 'text':'合成任务已完成', 'data':{'verified':True}})
            app.state.runs.cognition.pump()
        seed_outcomes('first-social-event')
        result = await run_idle_social(service)
        assert result and result["commentCount"] > 0
        assert len(requests) == result["commentCount"] + 1
        with session_scope() as session:
            before = session.scalar(select(func.count()).select_from(SocialPost))
            assert session.get(SocialPost, result["postId"]).excerpt == "合成社交内容"

        busy, entered, cancelled = False, asyncio.Event(), asyncio.Event()
        async def slow(**kw):
            assert get_engine().pool.checkedout() == 0
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        monkeypatch.setattr(service.bundle.runtime, "generate_social_post", slow)
        seed_outcomes('second-social-event')
        pending = asyncio.create_task(run_idle_social(service, lambda: busy))
        await asyncio.wait_for(entered.wait(), 2)
        busy = True
        assert await asyncio.wait_for(pending, 2) is None
        assert cancelled.is_set()
        with session_scope() as session:
            assert session.scalar(select(func.count()).select_from(SocialPost)) == before


def test_recovery_includes_old_runs_and_preserves_event_association(tmp_path):
    store = RunStore(tmp_path / "runs.db")
    old, _ = store.create({"prompt": "old"}, "old")
    store.put_call("pending", old["id"], "move", {}, "waiting_approval", phase="worker")
    for index in range(205):
        recent, _ = store.create({"prompt": str(index)}, str(index))
        store.update(recent["id"], status="completed")
    assert old["id"] not in {r["id"] for r in store.list()}
    store.recover()
    assert store.get(old["id"])["status"] == "paused"
    assert store.call("pending")["status"] == "expired"
    events = store.events(run_id=old["id"])
    assert next(e for e in events if e["type"] == "tool.expired")["payload"]["assignmentId"] == "worker"


def test_sqlite_naive_utc_post_does_not_become_immediately_due():
    from datetime import UTC, datetime, timedelta
    from backend.social_runtime import SocialRuntime, SocialCandidate
    social = SocialRuntime()
    now = datetime.now(UTC)
    assert not social.should_schedule(last_post_at=now.replace(tzinfo=None), interval_minutes=30)
    assert social.should_schedule(last_post_at=(now - timedelta(hours=1)).replace(tzinfo=None), interval_minutes=30)
    assert social.pick_due_author([SocialCandidate("a", now), SocialCandidate("b", (now - timedelta(hours=1)).replace(tzinfo=None))]).actor_id == "b"


def test_websocket_replay_cursor_reset_and_social_notifications(monkeypatch, tmp_path):
    configure_test_env(monkeypatch, tmp_path)
    app = create_app()
    store = app.state.runs.store
    with TestClient(app) as client:
        cursor = store.state()["cursor"]
        first = store.event(None, "test.first", {})
        second = store.event(None, "test.second", {})
        with client.websocket_connect(f"/ws/runtime?after={cursor}") as ws:
            assert ws.receive_json()["seq"] == first["seq"]
        with client.websocket_connect(f"/ws/runtime?after={first['seq']}") as ws:
            assert ws.receive_json()["seq"] == second["seq"]
        with client.websocket_connect("/ws/runtime?after=99999999") as ws:
            assert ws.receive_json()["type"] == "runtime.sync"
        with client.websocket_connect("/ws/runtime?after=broken") as ws:
            with pytest.raises(WebSocketDisconnect) as exc:
                ws.receive_json()
            assert exc.value.code == 1008
        posts = client.get("/api/bootstrap").json()["snapshot"]["posts"]
        response = client.post("/api/posts/comment", json={"postId": posts[0]["id"], "body": "Replay fixture"})
        assert response.status_code == 200, response.text
        assert any(e["type"] == "workspace.changed" for e in store.events(after=second["seq"]))
