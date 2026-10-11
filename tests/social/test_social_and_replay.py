
import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from backend.app import create_app
from backend.tasks.run_store import RunStore
from conftest import configure_test_env


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
    from backend.social.social_runtime import SocialRuntime, SocialCandidate
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
        snapshot = client.get("/api/bootstrap").json()["snapshot"]
        client.post('/api/posts/publish',json={'body':'用于回放验收的独立动态。'}).raise_for_status()
        posts = client.get("/api/bootstrap").json()["snapshot"]["posts"]
        response = client.post("/api/posts/comment", json={"postId": posts[0]["id"], "body": "Replay fixture"})
        assert response.status_code == 200, response.text
        assert any(e["type"] == "workspace.changed" for e in store.events(after=second["seq"]))
