import json
import time
from uuid import uuid4

from fastapi.testclient import TestClient

from backend.app import create_app
from backend.database import session_scope
from backend.models import Message, ActorSkill
from sqlalchemy import select
from test_backend_flows import configure_test_env


def test_real_background_admission_handoff_review_and_idempotency(monkeypatch, tmp_path):
    configure_test_env(monkeypatch, tmp_path)
    monkeypatch.setenv("AZURJUUS_WORKSPACE_STATE_PATH", str(tmp_path / "state" / "workspace-state.json"))
    from backend.config import get_settings
    get_settings.cache_clear()
    monkeypatch.setenv("AZURJUUS_EXECUTION_BACKEND", "hermes")
    app = create_app()
    c = app.state.runs
    histories = []

    class ScriptedBridge:
        session_id = "test"
        def __init__(self, home, settings, endpoint, token, event):
            self.phase, self.token, self.event = home.name, token, event
        async def start(self): pass
        async def close(self): pass
        async def steer(self, text): return {"queued": True}
        async def call(self, name, args):
            result = await c.tool(self.token, {"callId": uuid4().hex, "name": name, "args": args})
            assert result["status"] == "completed", result
            return result["callId"]
        async def prompt(self, prompt, workspace, history):
            histories.append((self.phase, prompt, history))
            rid = c.tokens[self.token][0]
            run = c.store.get(rid)
            if self.phase == "planner":
                actors = run["actors"]
                await self.call("delegate", {"tasks": [{"id":"draft","actorId":actors[0]["id"],"brief":"write source","dependsOn":[],"acceptance":["read back"]},{"id":"report","actorId":actors[-1]["id"],"brief":"consume draft","dependsOn":["draft"],"acceptance":["cite draft"]}]})
            else:
                if self.phase == "reviewer":
                    paths = [a["path"] for task in run["assignments"] for a in task["result"]["artifacts"]]
                else:
                    paths = [self.phase + ".txt"]
                    if self.phase == "report":
                        assert 'draft.txt' in prompt
                        await self.call("read_file", {"path":"draft.txt"})
                    await self.call("write_file", {"path":paths[0],"content":"Evidence from " + self.phase})
                checks = [{"callId":await self.call("read_file", {"path":p}),"description":"read current artifact"} for p in paths]
                await self.call("deliver", {"summary":"Verified report", "artifacts":paths,"checks":checks,"unresolved":[]})
            return "Verified report"

    c.bridge_factory = ScriptedBridge
    with TestClient(app) as client:
        workspace = client.get("/api/workspace/load").json()["workspace"]
        workspace["settings"]["llmApiKey"] = "local-provider-test"
        workspace["settings"]["authorizedWorkspaceRoot"] = str(tmp_path / "workspace")
        assert client.post("/api/workspace/save", json={"workspace":workspace}).status_code == 200
        cid = next(v["id"] for v in workspace["data"]["conversations"] if v["kind"] == "group")
        payload = {"conversationId":cid,"content":"Prepare a verified report","mode":"task","collaborative":True,"requestId":"unique-request"}
        accepted = client.post("/api/messages/send", json=payload)
        assert accepted.status_code == 200, accepted.text
        rid = accepted.json()["runId"]
        origin_cid = cid
        cid = accepted.json()["conversationId"]
        assert cid != origin_cid
        duplicate = client.post("/api/messages/send", json=payload)
        assert duplicate.json()["runId"] == rid
        for _ in range(200):
            run = client.get(f"/api/runs/{rid}").json()["run"]
            if run["status"] in {"completed","failed"}: break
            time.sleep(.025)
        assert run["status"] == "completed", run.get("error")
        assert len(run["artifacts"]) == 2
        assert client.get(f"/api/runs/{rid}/artifacts/0").content == b"Evidence from draft"
        assert client.get(f"/api/runs/{rid}/artifacts/-1").status_code == 404
        assert client.post(f"/api/runs/{rid}/control", json={"action":"cancel"}).status_code == 409
        assert client.post("/api/messages/send", json={**payload,"content":"conflicting request"}).status_code == 400
        events = client.get("/api/runtime/events").json()["events"]
        assert [e["seq"] for e in events] == sorted({e["seq"] for e in events})
        assert len([e for e in events if e["type"] == "run.created"]) == 1
        assert all("local-provider-test" not in json.dumps(client.get(path).json()) for path in ["/api/bootstrap","/api/runtime/diagnostics","/api/system/inspect"])
        assert [phase for phase,_,_ in histories] == ["planner","draft","report","reviewer"]
        assert all(history[0]["content"].startswith("角色设定") for _,_,history in histories)
        with session_scope() as session:
            messages = session.scalars(select(Message).where(Message.conversation_id == cid)).all()
            assert sum(m.body == payload["content"] for m in messages) == 1
            assert session.get(Message, rid + "-origin-result").conversation_id == origin_cid
            candidate = session.scalar(select(ActorSkill).where(ActorSkill.origin == 'learned_from_collaboration'))
            assert candidate is not None and not candidate.is_enabled
            assert candidate.visibility == 'private'
            assert candidate.extra_json['observedRunId'] == rid
            assert candidate.extra_json['validationCount'] == 0
            assert candidate.extra_json['lifecycle'] == 'candidate'
        assert c.store.get(rid)['learningCandidates']
        (tmp_path / "workspace" / "draft.txt").write_text("changed after review")
        assert client.get(f"/api/runs/{rid}/artifacts/0").status_code == 409


def test_local_http_boundary(monkeypatch, tmp_path):
    configure_test_env(monkeypatch, tmp_path)
    monkeypatch.setenv("AZURJUUS_EXECUTION_BACKEND", "hermes")
    with TestClient(create_app()) as client:
        assert client.get("/.env").status_code == 404
        assert client.get("/backend/services.py").status_code == 404
        assert client.get("/api/bootstrap", headers={"host":"untrusted.example"}).status_code == 400
        assert client.post("/api/workspace/save", json={}, headers={"origin":"https://untrusted.example"}).status_code == 403
        assert client.post("/api/internal/tool", json={"name":"command","args":{}}).status_code == 403
