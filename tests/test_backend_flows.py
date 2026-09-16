from __future__ import annotations

import asyncio
import sys
import time
from pathlib import Path

import httpx
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.app import create_app
from backend.config import get_settings
from backend.database import session_scope


def configure_test_env(monkeypatch, tmp_path: Path, *, social_enabled: bool = False) -> None:
    # Legacy protocol fixtures explicitly exercise the pre-expression path.
    monkeypatch.setenv('AZURJUUS_EXPRESSION_ENABLED', '0')
    monkeypatch.setenv("AZURJUUS_EXECUTION_BACKEND", "legacy-test")
    workspace_root = tmp_path / "workspace"
    workspace_root.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("AZURJUUS_DATABASE_URL", f"sqlite+pysqlite:///{(tmp_path / 'azurjuus.db').as_posix()}")
    monkeypatch.setenv("AZURJUUS_REDIS_URL", "")
    monkeypatch.setenv("AZURJUUS_CHROMA_URL", "http://127.0.0.1:65535")
    monkeypatch.setenv("AZURJUUS_CHROMA_PATH", str(tmp_path / "chroma"))
    monkeypatch.setenv("AZURJUUS_WORKSPACE_ROOT", str(workspace_root))
    monkeypatch.setenv("AZURJUUS_WORKSPACE_STATE_PATH", str(tmp_path / "workspace-state.json"))
    monkeypatch.setenv("AZURJUUS_SOCIAL_ENABLED", "1" if social_enabled else "0")
    monkeypatch.setenv("AZURJUUS_SOCIAL_TICK_SECONDS", "9999")
    monkeypatch.setenv("AZURJUUS_WORKFLOW_RUNTIME_ENABLED", "0")
    monkeypatch.setenv("AZURJUUS_WORKFLOW_TICK_SECONDS", "9999")
    get_settings.cache_clear()


def open_client(monkeypatch, tmp_path: Path, *, social_enabled: bool = False) -> TestClient:
    configure_test_env(monkeypatch, tmp_path, social_enabled=social_enabled)
    return TestClient(create_app())


def latest_workflow(snapshot: dict, title: str) -> dict:
    matches = [item for item in snapshot["workflows"] if item["title"] == title]
    assert matches, f"workflow {title!r} not found"
    return sorted(matches, key=lambda item: item["updatedAt"], reverse=True)[0]


def pending_approval(snapshot: dict, workflow_id: str, *, stage_key: str | None = None, review_role: str | None = None, target_kind: str | None = None) -> dict:
    for approval in snapshot["approvals"]:
        if approval["workflowId"] != workflow_id or approval["status"] != "pending":
            continue
        if target_kind and approval["targetKind"] != target_kind:
            continue
        payload = approval["payload"] or {}
        if stage_key and payload.get("stageKey") != stage_key:
            continue
        if review_role and payload.get("reviewRole") != review_role:
            continue
        return approval
    raise AssertionError(f"pending approval not found for workflow={workflow_id}, stage={stage_key}, role={review_role}, target={target_kind}")


def tick_until_pending_approval(
    client: TestClient,
    snapshot: dict,
    workflow_id: str,
    *,
    stage_key: str,
    review_role: str,
    target_kind: str = "workflow_stage_review",
    attempts: int = 12,
) -> dict:
    current_snapshot = snapshot
    for _ in range(attempts):
        try:
            return pending_approval(current_snapshot, workflow_id, stage_key=stage_key, review_role=review_role, target_kind=target_kind)
        except AssertionError:
            tick = client.post("/api/workflows/tick", json={"workflowId": workflow_id})
            assert tick.status_code == 200
            payload = tick.json()
            if payload.get("snapshot"):
                current_snapshot = payload["snapshot"]
    return pending_approval(current_snapshot, workflow_id, stage_key=stage_key, review_role=review_role, target_kind=target_kind)


def wait_for_snapshot(client: TestClient, predicate, *, attempts: int = 20, delay: float = 0.05) -> dict:
    last_snapshot = {}
    for _ in range(attempts):
        last_snapshot = client.get("/api/bootstrap").json()["snapshot"]
        if predicate(last_snapshot):
            return last_snapshot
        time.sleep(delay)
    raise AssertionError("snapshot condition not met in time")


def test_phase_one_dm_message_persists_and_skill_preview(monkeypatch, tmp_path: Path) -> None:
    with open_client(monkeypatch, tmp_path) as client:
        bootstrap = client.get("/api/bootstrap").json()["snapshot"]
        dm_conversation = next(item for item in bootstrap["conversations"] if item["kind"] == "dm")
        agent_id = next(member_id for member_id in dm_conversation["memberIds"] if member_id != "commander")
        skill_catalog = client.get("/api/skills/catalog")
        assert skill_catalog.status_code == 200
        skills = skill_catalog.json()["skills"]
        assert any(item["id"] == "public.secretary.route" for item in skills)
        assert any(item["name"] == "陪伴式对话" for item in skills)

        preview = client.post(
            "/api/skills/preview",
            json={
                "actorId": agent_id,
                "mode": "chat",
                "prompt": "今天想和你聊聊最近的状态。",
                "conversationKind": "dm",
            },
        ).json()
        assert preview["status"] == "ok"
        assert preview["primarySkillId"]
        assert preview["selectedSkills"]

        secretary_preview = client.post(
            "/api/skills/preview",
            json={
                "actorId": agent_id,
                "mode": "swarm",
                "prompt": "请先负责统筹这轮协作，并把阶段目标同步给大家。",
                "conversationKind": "group",
                "workflowRole": "secretary",
            },
        ).json()
        assert secretary_preview["status"] == "ok"
        assert any(item["id"] == "public.secretary.route" for item in secretary_preview["selectedSkills"])

        response = client.post(
            "/api/messages/send",
            json={
                "conversationId": dm_conversation["id"],
                "content": "今天想和你聊聊最近的状态。",
                "mode": "chat",
            },
        )
        assert response.status_code == 200
        snapshot = response.json()["snapshot"]
        assert len(snapshot["messages"][dm_conversation["id"]]) >= 2

    with open_client(monkeypatch, tmp_path) as client:
        bootstrap = client.get("/api/bootstrap").json()["snapshot"]
        assert len(bootstrap["messages"][dm_conversation["id"]]) >= 2


def test_contracts_expose_routes_and_realtime_events(monkeypatch, tmp_path: Path) -> None:
    with open_client(monkeypatch, tmp_path) as client:
        contracts = client.get("/api/contracts")
        assert contracts.status_code == 200
        payload = contracts.json()
        assert payload["routes"]
        assert payload["realtimeEvents"]
        assert "conversation.typing" in payload["realtimeEvents"]
        assert "workflow.skill.completed" in payload["realtimeEvents"]
        assert "skill.proposal.created" in payload["realtimeEvents"]
        assert any(item["path"] == "/api/skills/proposals" for item in payload["routes"])


def test_workspace_settings_and_ui_session_persist(monkeypatch, tmp_path: Path) -> None:
    with open_client(monkeypatch, tmp_path) as client:
        workspace = client.get("/api/workspace/load").json()["workspace"]
        assert workspace["session"]["actorId"] == "commander"
        assert workspace["session"]["authMode"] == "local_cookie"
        workspace["settings"]["llmModel"] = "gpt-4.1"
        workspace["settings"]["llmApiKey"] = "test-key"
        workspace["settings"]["authorizedWorkspaceRoot"] = str(tmp_path / "workspace")
        workspace["settings"]["searchApiKey"] = "search-key"
        workspace["settings"]["mapApiKey"] = "map-key"
        workspace["settings"]["recommendationApiKey"] = "recommend-key"
        workspace["settings"]["weatherApiKey"] = "weather-key"
        workspace["uiSession"]["view"] = "circle"
        workspace["uiSession"]["activeConversationId"] = "port-hub"

        saved = client.post("/api/workspace/save", json={"workspace": workspace})
        assert saved.status_code == 200

    with open_client(monkeypatch, tmp_path) as client:
        workspace = client.get("/api/workspace/load").json()["workspace"]
        assert workspace["settings"]["llmModel"] == "gpt-4.1"
        for key in ("llmApiKey", "searchApiKey", "mapApiKey", "recommendationApiKey", "weatherApiKey"):
            assert key not in workspace["settings"]
            assert workspace["settings"][key + "Configured"] is True
        with session_scope() as session:
            private = client.app.state.service.serialize_settings(client.app.state.service.get_workspace(session))
            assert private["llmApiKey"] == "test-key"
            assert private["searchApiKey"] == "search-key"
        assert workspace["uiSession"]["view"] == "circle"
        assert workspace["uiSession"]["activeConversationId"] == "port-hub"
        assert workspace["session"]["actorId"] == "commander"
        bootstrap = client.get("/api/bootstrap").json()["workspace"]
        assert bootstrap["session"]["actorId"] == "commander"
        assert bootstrap["session"]["approvalMode"] == "server_managed_secretary"


def test_system_inspect_and_tool_catalog(monkeypatch, tmp_path: Path) -> None:
    with open_client(monkeypatch, tmp_path) as client:
        workspace = client.get("/api/workspace/load").json()["workspace"]
        workspace["settings"]["authorizedWorkspaceRoot"] = str(tmp_path / "workspace")
        saved = client.post("/api/workspace/save", json={"workspace": workspace})
        assert saved.status_code == 200

        tool_catalog = client.get("/api/tools/catalog")
        assert tool_catalog.status_code == 200
        tool_payload = tool_catalog.json()
        assert tool_payload["authorizedWorkspaceRoot"] == str(tmp_path / "workspace")
        assert any(item["name"] == "list_dir" for item in tool_payload["localTools"])
        assert any(item["name"] == "weather_lookup" for item in tool_payload["externalTools"])

        system_payload = client.get("/api/system/inspect")
        assert system_payload.status_code == 200
        system_data = system_payload.json()
        assert "skillVsTool" in system_data["notes"]
        assert "authorizedWorkspace" in system_data["notes"]


def test_actor_tool_access_is_differentiated_and_enforced(monkeypatch, tmp_path: Path) -> None:
    with open_client(monkeypatch, tmp_path) as client:
        system_payload = client.get("/api/system/inspect")
        assert system_payload.status_code == 200
        actor_access = system_payload.json()["toolCatalog"]["actorAccess"]
        assert actor_access

        actor_with_create_folder = next(
            (item for item in actor_access if "create_folder" in item["manualPlanRequiredTools"]),
            None,
        )
        actor_without_create_folder = next(
            (item for item in actor_access if "create_folder" not in item["manualPlanRequiredTools"]),
            None,
        )
        assert actor_with_create_folder is not None
        assert actor_without_create_folder is not None
        assert actor_with_create_folder["actorId"] != actor_without_create_folder["actorId"]

        denied = client.post(
            "/api/tools/plan",
            json={
                "actorId": actor_without_create_folder["actorId"],
                "toolName": "create_folder",
                "args": {"path": "role-guarded-folder"},
            },
        )
        assert denied.status_code == 422
        assert "not currently allowed" in denied.json()["detail"]

        allowed = client.post(
            "/api/tools/plan",
            json={
                "actorId": actor_with_create_folder["actorId"],
                "toolName": "create_folder",
                "args": {"path": "role-guarded-folder"},
            },
        )
        assert allowed.status_code == 200
        allowed_payload = allowed.json()
        assert allowed_payload["plan"]["tool"] == "create_folder"
        assert allowed_payload["executionId"]


def test_auto_tool_reply_uses_authorized_workspace(monkeypatch, tmp_path: Path) -> None:
    with open_client(monkeypatch, tmp_path) as client:
        service = client.app.state.service
        workspace_root = tmp_path / "workspace"
        (workspace_root / "inside.txt").write_text("hello", encoding="utf-8")
        calls = {"count": 0}

        async def fake_generate_reply(**kwargs):
            calls["count"] += 1
            if calls["count"] == 1:
                return "", [{"name": "list_dir", "args": {"path": "."}}]
            return "done", []

        original = service.bundle.runtime.generate_reply
        service.bundle.runtime.generate_reply = fake_generate_reply
        try:
            reply, used_tools = asyncio.run(
                service._generate_reply_with_tools_v2(
                    None,
                    settings={"authorizedWorkspaceRoot": str(workspace_root)},
                    prompt="请先看看工作区里有什么",
                    mode="task",
                    conversation_kind="dm",
                    recent_messages=[],
                    memory_snippets=[],
                    skill_context="",
                    tool_allowlist=["list_dir"],
                )
            )
        finally:
            service.bundle.runtime.generate_reply = original

        assert reply == "done"
        assert used_tools == ["list_dir"]


def test_realtime_websocket_typing_and_incremental_replay(monkeypatch, tmp_path: Path) -> None:
    with open_client(monkeypatch, tmp_path) as client:
        bootstrap = client.get("/api/bootstrap").json()["snapshot"]
        dm_conversation = next(item for item in bootstrap["conversations"] if item["kind"] == "dm")
        first_agent_id = next(member_id for member_id in dm_conversation["memberIds"] if member_id != "commander")

        with client.websocket_connect("/ws") as websocket:
            connected = websocket.receive_json()
            assert connected["event"] == "runtime.connected"
            assert isinstance(connected["payload"]["cursor"], int)

            response = client.post(
                "/api/messages/send",
                json={
                    "conversationId": dm_conversation["id"],
                    "content": "请用你的方式回一句简短问候。",
                    "mode": "chat",
                },
            )
            assert response.status_code == 200

            received = [
                websocket.receive_json(),
                websocket.receive_json(),
                websocket.receive_json(),
                websocket.receive_json(),
            ]
            names = [item["event"] for item in received]
            assert "conversation.typing" in names
            assert names.count("conversation.message.created") >= 2
            assert "workflow.skill.completed" in names
            cursors = [item["cursor"] for item in received if isinstance(item.get("cursor"), int)]
            assert cursors
            last_cursor = max(cursors)

        publish = client.post(
            "/api/posts/publish",
            json={"authorId": first_agent_id, "body": "任务之外，也想顺手留一条动态。"},
        )
        assert publish.status_code == 200

        with client.websocket_connect(f"/ws?after={last_cursor}") as websocket:
            connected = websocket.receive_json()
            assert connected["event"] == "runtime.connected"
            replayed = websocket.receive_json()
            assert replayed["event"] == "post.created"
            assert replayed["cursor"] > last_cursor


def test_tool_security_blocks_escape_and_unapproved_execute(monkeypatch, tmp_path: Path) -> None:
    with open_client(monkeypatch, tmp_path) as client:
        escaped = client.post(
            "/api/tools/plan",
            json={
                "actorId": "commander",
                "toolName": "create_folder",
                "args": {"path": "..\\outside-root"},
            },
        )
        assert escaped.status_code == 422
        assert "authorized workspace" in escaped.json()["detail"]

        planned = client.post(
            "/api/tools/plan",
            json={
                "actorId": "commander",
                "toolName": "create_folder",
                "args": {"path": "safe-output"},
            },
        )
        assert planned.status_code == 200
        payload = planned.json()
        execution_id = payload["executionId"]
        approval_id = payload["approvalId"]
        assert approval_id
        approval = next(item for item in payload["snapshot"]["approvals"] if item["id"] == approval_id)
        assert approval["status"] == "approved"
        assert approval["approvedBySecretary"] is True

        executed = client.post("/api/tools/execute", json={"executionId": execution_id})
        assert executed.status_code == 200
        assert (tmp_path / "workspace" / "safe-output").exists()


def test_high_risk_tool_requires_dual_approval(monkeypatch, tmp_path: Path) -> None:
    with open_client(monkeypatch, tmp_path) as client:
        workspace = client.get("/api/workspace/load").json()["workspace"]
        secretary_actor_id = workspace["settings"]["secretaryAgentId"]
        planned = client.post(
            "/api/tools/plan",
            json={
                "actorId": "commander",
                "toolName": "write_text_file",
                "args": {"path": "notes\\report.txt", "content": "phase-ready"},
            },
        )
        assert planned.status_code == 200
        payload = planned.json()
        approval_id = payload["approvalId"]
        execution_id = payload["executionId"]
        approval = next(item for item in payload["snapshot"]["approvals"] if item["id"] == approval_id)
        assert approval["approvedBySecretary"] is True
        assert approval["approvedByUser"] is False
        assert approval["pendingReviewers"] == ["user"]

        denied = client.post("/api/tools/execute", json={"executionId": execution_id})
        assert denied.status_code == 403

        user_pass = client.post(
            "/api/approvals/resolve",
            json={"approvalId": approval_id, "decision": "approve", "reviewerActorId": secretary_actor_id},
        )
        assert user_pass.status_code == 200
        user_payload = user_pass.json()
        assert user_payload["status"] == "approved"
        assert user_payload["approvalProgress"]["approvedBySecretary"] is True
        assert user_payload["approvalProgress"]["approvedByUser"] is True
        assert user_payload["approvalProgress"]["pendingReviewers"] == []
        resolved_approval = next(item for item in user_payload["snapshot"]["approvals"] if item["id"] == approval_id)
        assert resolved_approval["reviewerActorId"] == "commander"

        executed = client.post("/api/tools/execute", json={"executionId": execution_id})
        assert executed.status_code == 200
        report_path = tmp_path / "workspace" / "notes" / "report.txt"
        assert report_path.exists()
        assert report_path.read_text(encoding="utf-8") == "phase-ready"


def test_phase_two_social_publish_and_comment(monkeypatch, tmp_path: Path) -> None:
    with open_client(monkeypatch, tmp_path) as client:
        bootstrap = client.get("/api/bootstrap").json()["snapshot"]
        author_id = bootstrap["agents"][0]["id"]
        publish = client.post(
            "/api/posts/publish",
            json={"authorId": author_id, "body": "任务之后，先在这里记下一点此刻的想法。"},
        )
        assert publish.status_code == 200
        snapshot = publish.json()["snapshot"]
        post = snapshot["posts"][0]

        comment = client.post(
            "/api/posts/comment",
            json={"postId": post["id"], "body": "指挥官已阅，这条感想我收到了。"},
        )
        assert comment.status_code == 200
        snapshot = comment.json()["snapshot"]
        post = next(item for item in snapshot["posts"] if item["id"] == post["id"])
        assert any(item["authorId"] == "commander" for item in post["comments"])


def test_skill_proposal_roundtrip_updates_effective_catalog_and_preview(monkeypatch, tmp_path: Path) -> None:
    with open_client(monkeypatch, tmp_path) as client:
        bootstrap = client.get("/api/bootstrap").json()["snapshot"]
        actor_id = bootstrap["agents"][0]["id"]

        created = client.post(
            "/api/skills/proposals",
            json={
                "actorId": actor_id,
                "baseSkillId": "public.task.breakdown",
                "title": "补充交付格式确认",
                "summary": "建议在公共拆解流程里补一句先确认交付格式与验收方式。",
                "promptPatch": "执行前先显式确认交付格式、验收方式和完成标准，再继续拆解。",
            },
        )
        assert created.status_code == 200
        created_payload = created.json()
        assert created_payload["proposal"]["status"] == "pending"

        proposals = client.get("/api/skills/proposals")
        assert proposals.status_code == 200
        assert any(item["id"] == created_payload["proposal"]["id"] for item in proposals.json()["proposals"])

        resolved = client.post(
            "/api/approvals/resolve",
            json={
                "approvalId": created_payload["approvalId"],
                "decision": "approve",
                "reviewerActorId": "commander",
            },
        )
        assert resolved.status_code == 200
        resolved_payload = resolved.json()
        assert resolved_payload["targetKind"] == "skill_proposal"
        assert resolved_payload["proposal"]["status"] == "approved"

        catalog = client.get("/api/skills/catalog")
        assert catalog.status_code == 200
        breakdown = next(item for item in catalog.json()["skills"] if item["id"] == "public.task.breakdown")
        assert breakdown["revisionCount"] >= 1
        assert created_payload["proposal"]["id"] in breakdown["proposalIds"]

        preview = client.post(
            "/api/skills/preview",
            json={
                "actorId": actor_id,
                "mode": "task",
                "prompt": "请先整理今日任务列表并给出明确交付格式。",
                "conversationKind": "dm",
            },
        )
        assert preview.status_code == 200
        assert "执行前先显式确认交付格式" in preview.json()["promptPatch"]


def test_idle_social_tick_generates_post_and_auto_comments(monkeypatch, tmp_path: Path) -> None:
    with open_client(monkeypatch, tmp_path, social_enabled=True) as client:
        client.get("/api/bootstrap")
        service = client.app.state.service
        with session_scope() as session:
            from datetime import UTC, datetime, timedelta
            from sqlalchemy import update
            from backend.models import SocialPost
            # Seed greetings are new posts, so they must age before an idle tick
            # becomes due. Do not depend on SQLite UTC being read as local time.
            session.execute(update(SocialPost).values(created_at=datetime.now(UTC) - timedelta(days=1)))
            result = asyncio.run(service.run_idle_social_tick(session))
        assert result is not None
        assert result["postId"]
        assert result["snapshot"]["posts"]
        assert result["commentCount"] >= 1


def test_phase_three_workflow_help_interrupt_tool_and_completion(monkeypatch, tmp_path: Path) -> None:
    with open_client(monkeypatch, tmp_path) as client:
        bootstrap = client.get("/api/bootstrap").json()["snapshot"]
        active_agents = bootstrap["agents"]
        selected_actor_ids = [item["id"] for item in active_agents[:3]]

        dispatch = client.post(
            "/api/workflows/dispatch",
            json={
                "conversationId": "port-hub",
                "title": "港区资料整理",
                "content": "请大家协作整理今天的港区任务记录，并输出阶段性总结。",
                "mode": "swarm",
                "actorIds": selected_actor_ids,
            },
        )
        assert dispatch.status_code == 200
        snapshot = dispatch.json()["snapshot"]
        workflow = latest_workflow(snapshot, "港区资料整理")
        assert workflow["orchestration"]["currentStageKey"] == "planning"
        assert workflow["orchestration"]["workflowStatus"] == "awaiting_plan_approval"

        planning = pending_approval(snapshot, workflow["id"], stage_key="planning", review_role="user", target_kind="workflow_stage_review")
        approve_planning = client.post(
            "/api/approvals/resolve",
            json={"approvalId": planning["id"], "decision": "approve", "reviewerActorId": "commander"},
        )
        assert approve_planning.status_code == 200
        snapshot = approve_planning.json()["snapshot"]
        workflow = latest_workflow(snapshot, "港区资料整理")
        assert workflow["status"] == "active"
        assert workflow["orchestration"]["currentStageKey"] == "execution"

        outsider_candidates = workflow["context"].get("outsiderCandidateIds") or []
        assert outsider_candidates
        requester_actor_id = workflow["assignments"][0]["agentId"]
        help_request = client.post(
            "/api/workflows/help-request",
            json={
                "workflowId": workflow["id"],
                "requesterActorId": requester_actor_id,
                "targetActorId": outsider_candidates[0],
                "summary": "当前需要补充一位编外成员从旁整理遗漏风险点。",
            },
        )
        assert help_request.status_code == 200
        snapshot = help_request.json()["snapshot"]
        help_approval = pending_approval(snapshot, workflow["id"], target_kind="workflow_help_request")

        help_resolve = client.post(
            "/api/approvals/resolve",
            json={"approvalId": help_approval["id"], "decision": "approve", "reviewerActorId": "commander"},
        )
        assert help_resolve.status_code == 200
        snapshot = help_resolve.json()["snapshot"]
        workflow = latest_workflow(snapshot, "港区资料整理")
        assert outsider_candidates[0] in workflow["context"].get("approvedHelpers", [])
        assert any(item["agentId"] == outsider_candidates[0] for item in workflow["assignments"])

        tool_plan = client.post(
            "/api/tools/plan",
            json={
                "actorId": workflow["secretaryId"],
                "toolName": "create_folder",
                "args": {"path": "task-output"},
                "workflowId": workflow["id"],
            },
        )
        assert tool_plan.status_code == 200
        tool_payload = tool_plan.json()
        assert tool_payload["status"] == "approved"
        assert tool_payload["approvalId"] is None
        tool_execute = client.post("/api/tools/execute", json={"executionId": tool_payload["executionId"]})
        assert tool_execute.status_code == 200
        tool_snapshot = tool_execute.json()["snapshot"]
        assert any(item["id"] == tool_payload["executionId"] and item["status"] == "completed" for item in tool_snapshot["toolExecutions"])

        interrupt = client.post(
            "/api/workflows/interrupt",
            json={"workflowId": workflow["id"], "body": "补充要求：把风险点单独列出来，并优先保证可审查性。"},
        )
        assert interrupt.status_code == 200
        snapshot = interrupt.json()["snapshot"]
        workflow = latest_workflow(snapshot, "港区资料整理")
        assert workflow["status"] == "active"
        assert workflow["context"].get("revisionCount", 0) >= 1
        assert workflow["orchestration"]["currentStageKey"] == "execution"
        assert workflow["orchestration"].get("lastInterrupt", {}).get("body")

        execution_user = tick_until_pending_approval(
            client,
            snapshot,
            workflow["id"],
            stage_key="execution",
            review_role="user",
        )
        approve_execution_user = client.post(
            "/api/approvals/resolve",
            json={"approvalId": execution_user["id"], "decision": "approve", "reviewerActorId": "commander"},
        )
        assert approve_execution_user.status_code == 200
        snapshot = approve_execution_user.json()["snapshot"]

        final_review = pending_approval(snapshot, workflow["id"], stage_key="review", review_role="user", target_kind="workflow_stage_review")
        approve_final = client.post(
            "/api/approvals/resolve",
            json={"approvalId": final_review["id"], "decision": "approve", "reviewerActorId": "commander"},
        )
        assert approve_final.status_code == 200
        snapshot = approve_final.json()["snapshot"]
        workflow = latest_workflow(snapshot, "港区资料整理")
        assert workflow["status"] == "completed"
        assert workflow["context"].get("completionPostId")
        assert workflow["orchestration"]["workflowStatus"] == "completed"
        assert workflow["graph"]["nodes"]
        assert workflow["timeline"]
        assert any(post["id"] == workflow["context"]["completionPostId"] for post in snapshot["posts"])

        inspected = client.get(f"/api/workflows/{workflow['id']}")
        assert inspected.status_code == 200
        inspected_workflow = inspected.json()["workflow"]
        assert inspected_workflow["id"] == workflow["id"]
        assert inspected_workflow["graph"]["nodes"]
        assert inspected_workflow["timeline"]

        timeline = client.get(f"/api/workflows/{workflow['id']}/timeline")
        assert timeline.status_code == 200
        timeline_payload = timeline.json()
        assert timeline_payload["timeline"]
        assert timeline_payload["graph"]["nodes"]
        orchestration_entries = [item for item in timeline_payload["timeline"] if item["kind"] == "orchestration"]
        assert orchestration_entries
        assert any(item["title"] == "assignment-completed" for item in orchestration_entries)


def test_actor_skill_inspection_includes_public_and_learned_skills(monkeypatch, tmp_path: Path) -> None:
    with open_client(monkeypatch, tmp_path) as client:
        bootstrap = client.get("/api/bootstrap").json()["snapshot"]
        selected_actor_ids = [item["id"] for item in bootstrap["agents"][:3]]

        dispatch = client.post(
            "/api/workflows/dispatch",
            json={
                "conversationId": "port-hub",
                "title": "协作学习测试",
                "content": "请三位成员共同整理信息，并在过程中互相借鉴做法。",
                "mode": "swarm",
                "actorIds": selected_actor_ids,
            },
        )
        assert dispatch.status_code == 200
        snapshot = dispatch.json()["snapshot"]
        workflow = latest_workflow(snapshot, "协作学习测试")

        planning = pending_approval(snapshot, workflow["id"], stage_key="planning", review_role="user", target_kind="workflow_stage_review")
        approve_planning = client.post(
            "/api/approvals/resolve",
            json={"approvalId": planning["id"], "decision": "approve", "reviewerActorId": "commander"},
        )
        assert approve_planning.status_code == 200
        snapshot = approve_planning.json()["snapshot"]
        workflow = latest_workflow(snapshot, "协作学习测试")

        execution_user = tick_until_pending_approval(
            client,
            snapshot,
            workflow["id"],
            stage_key="execution",
            review_role="user",
        )
        assert execution_user["status"] == "pending"

        learned_detected = False
        for actor_id in selected_actor_ids:
            inspected = client.get(f"/api/actors/{actor_id}/skills")
            assert inspected.status_code == 200
            payload = inspected.json()
            assert payload["publicSkills"]
            if any(item["origin"] == "learned_from_collaboration" for item in payload["skills"]):
                learned_detected = True
        assert learned_detected


def test_rejected_stage_review_keeps_workflow_in_revision(monkeypatch, tmp_path: Path) -> None:
    with open_client(monkeypatch, tmp_path) as client:
        bootstrap = client.get("/api/bootstrap").json()["snapshot"]
        selected_actor_ids = [item["id"] for item in bootstrap["agents"][:3]]

        dispatch = client.post(
            "/api/workflows/dispatch",
            json={
                "conversationId": "port-hub",
                "title": "阶段驳回测试",
                "content": "先推进，再在执行阶段由秘书驳回。",
                "mode": "swarm",
                "actorIds": selected_actor_ids,
            },
        )
        snapshot = dispatch.json()["snapshot"]
        workflow = latest_workflow(snapshot, "阶段驳回测试")

        planning = pending_approval(snapshot, workflow["id"], stage_key="planning", review_role="user", target_kind="workflow_stage_review")
        approve_planning = client.post(
            "/api/approvals/resolve",
            json={"approvalId": planning["id"], "decision": "approve", "reviewerActorId": "commander"},
        )
        snapshot = approve_planning.json()["snapshot"]
        workflow = latest_workflow(snapshot, "阶段驳回测试")
        execution_user = tick_until_pending_approval(
            client,
            snapshot,
            workflow["id"],
            stage_key="execution",
            review_role="user",
        )

        rejected = client.post(
            "/api/approvals/resolve",
            json={"approvalId": execution_user["id"], "decision": "reject", "reviewerActorId": "commander"},
        )
        assert rejected.status_code == 200
        snapshot = rejected.json()["snapshot"]
        workflow = latest_workflow(snapshot, "阶段驳回测试")
        assert workflow["status"] == "active"
        assert workflow["orchestration"]["currentStageKey"] == "execution"
        assert any(
            item.get("transitionKind") == "revision-requested"
            and item.get("workflowStatus") == "needs_revision"
            for item in workflow["orchestration"].get("history", [])
        )
        retried_execution_user = tick_until_pending_approval(
            client,
            snapshot,
            workflow["id"],
            stage_key="execution",
            review_role="user",
        )
        assert retried_execution_user["status"] == "pending"
        assert retried_execution_user["id"] != execution_user["id"]


def test_rejected_planning_review_requeues_planning_confirmation(monkeypatch, tmp_path: Path) -> None:
    with open_client(monkeypatch, tmp_path) as client:
        bootstrap = client.get("/api/bootstrap").json()["snapshot"]
        selected_actor_ids = [item["id"] for item in bootstrap["agents"][:3]]

        dispatch = client.post(
            "/api/workflows/dispatch",
            json={
                "conversationId": "port-hub",
                "title": "规划驳回重提测试",
                "content": "先生成规划，再由用户驳回并要求重提。",
                "mode": "swarm",
                "actorIds": selected_actor_ids,
            },
        )
        assert dispatch.status_code == 200
        snapshot = dispatch.json()["snapshot"]
        workflow = latest_workflow(snapshot, "规划驳回重提测试")

        planning = pending_approval(snapshot, workflow["id"], stage_key="planning", review_role="user", target_kind="workflow_stage_review")
        rejected = client.post(
            "/api/approvals/resolve",
            json={"approvalId": planning["id"], "decision": "reject", "reviewerActorId": "commander"},
        )
        assert rejected.status_code == 200
        snapshot = rejected.json()["snapshot"]
        workflow = latest_workflow(snapshot, "规划驳回重提测试")
        assert workflow["status"] == "needs_revision"
        assert workflow["orchestration"]["currentStageKey"] == "planning"
        assert workflow["orchestration"]["workflowStatus"] == "needs_revision"

        replanned = pending_approval(snapshot, workflow["id"], stage_key="planning", review_role="user", target_kind="workflow_stage_review")
        assert replanned["id"] != planning["id"]


def test_public_approval_route_ignores_spoofed_reviewer_actor_id(monkeypatch, tmp_path: Path) -> None:
    with open_client(monkeypatch, tmp_path) as client:
        bootstrap = client.get("/api/bootstrap").json()["snapshot"]
        selected_actor_ids = [item["id"] for item in bootstrap["agents"][:4]]

        dispatch = client.post(
            "/api/workflows/dispatch",
            json={
                "conversationId": "port-hub",
                "title": "审批身份校验测试",
                "content": "推进到执行审查阶段，并验证只有秘书本人可以处理秘书审批。",
                "mode": "swarm",
                "actorIds": selected_actor_ids,
            },
        )
        assert dispatch.status_code == 200
        snapshot = dispatch.json()["snapshot"]
        workflow = latest_workflow(snapshot, "审批身份校验测试")

        planning = pending_approval(snapshot, workflow["id"], stage_key="planning", review_role="user", target_kind="workflow_stage_review")
        approved_planning = client.post(
            "/api/approvals/resolve",
            json={"approvalId": planning["id"], "decision": "approve", "reviewerActorId": "commander"},
        )
        assert approved_planning.status_code == 200
        snapshot = approved_planning.json()["snapshot"]
        resolved_planning = next(item for item in snapshot["approvals"] if item["id"] == planning["id"])
        assert resolved_planning["reviewerActorId"] == "commander"


def test_execution_tick_completes_parallel_assignments_in_one_batch(monkeypatch, tmp_path: Path) -> None:
    with open_client(monkeypatch, tmp_path) as client:
        bootstrap = client.get("/api/bootstrap").json()["snapshot"]
        selected_actor_ids = [item["id"] for item in bootstrap["agents"][:4]]

        dispatch = client.post(
            "/api/workflows/dispatch",
            json={
                "conversationId": "port-hub",
                "title": "并行执行批次测试",
                "content": "让多个协作成员在同一执行阶段并行推进，然后提交秘书审查。",
                "mode": "swarm",
                "actorIds": selected_actor_ids,
            },
        )
        assert dispatch.status_code == 200
        snapshot = dispatch.json()["snapshot"]
        workflow = latest_workflow(snapshot, "并行执行批次测试")

        planning = pending_approval(snapshot, workflow["id"], stage_key="planning", review_role="user", target_kind="workflow_stage_review")
        approved_planning = client.post(
            "/api/approvals/resolve",
            json={"approvalId": planning["id"], "decision": "approve", "reviewerActorId": "commander"},
        )
        assert approved_planning.status_code == 200
        snapshot = approved_planning.json()["snapshot"]
        workflow = latest_workflow(snapshot, "并行执行批次测试")

        started_assignments = [
            item for item in workflow["assignments"]
            if item["status"] in {"in_progress", "waiting_retry"}
        ]
        assert len(started_assignments) >= 2

        execution_user = tick_until_pending_approval(
            client,
            snapshot,
            workflow["id"],
            stage_key="execution",
            review_role="user",
        )
        assert execution_user["status"] == "pending"

        idle_tick = client.post("/api/workflows/tick", json={"workflowId": workflow["id"]})
        assert idle_tick.status_code == 200
        assert idle_tick.json()["status"] == "idle"


def test_workflow_dispatch_survives_llm_provider_failure(monkeypatch, tmp_path: Path) -> None:
    async def failing_post(self, *args, **kwargs):
        raise httpx.ConnectError("provider unavailable")

    monkeypatch.setattr(httpx.AsyncClient, "post", failing_post, raising=True)

    with open_client(monkeypatch, tmp_path) as client:
        workspace = client.get("/api/workspace/load").json()["workspace"]
        workspace["settings"]["llmApiKey"] = "live-key"
        workspace["settings"]["llmModel"] = "gpt-4.1"
        saved = client.post("/api/workspace/save", json={"workspace": workspace})
        assert saved.status_code == 200

        bootstrap = client.get("/api/bootstrap").json()["snapshot"]
        selected_actor_ids = [item["id"] for item in bootstrap["agents"][:3]]
        dispatch = client.post(
            "/api/workflows/dispatch",
            json={
                "conversationId": "port-hub",
                "title": "LLM 故障回退测试",
                "content": "即使外部模型失败，也应该还能创建任务并走本地兜底。",
                "mode": "swarm",
                "actorIds": selected_actor_ids,
            },
        )
        assert dispatch.status_code == 200
        snapshot = dispatch.json()["snapshot"]
        workflow = latest_workflow(snapshot, "LLM 故障回退测试")
        assert workflow["title"] == "LLM 故障回退测试"
        assert workflow["conversationId"]
        task_room_messages = snapshot["messages"][workflow["conversationId"]]
        assert len(task_room_messages) >= 3


def test_task_message_can_request_collaborative_launch_before_group_creation(monkeypatch, tmp_path: Path) -> None:
    with open_client(monkeypatch, tmp_path) as client:
        bootstrap = client.get("/api/bootstrap").json()["snapshot"]
        dm_conversation = next(item for item in bootstrap["conversations"] if item["kind"] == "dm")

        response = client.post(
            "/api/messages/send",
            json={
                "conversationId": dm_conversation["id"],
                "content": "这项任务需要多人协作、分工推进和阶段审查，请先判断并准备协作方案。",
                "mode": "task",
            },
        )
        assert response.status_code == 200
        snapshot = wait_for_snapshot(
            client,
            lambda current: any(
                item["targetKind"] == "workflow_launch_request"
                and (item["payload"] or {}).get("originConversationId") == dm_conversation["id"]
                and item["status"] == "pending"
                for item in current["approvals"]
            ),
        )
        approval = next(
            item
            for item in snapshot["approvals"]
            if item["targetKind"] == "workflow_launch_request"
            and (item["payload"] or {}).get("originConversationId") == dm_conversation["id"]
            and item["status"] == "pending"
        )
        assert approval["requiresUser"] is True

        approved = client.post(
            "/api/approvals/resolve",
            json={"approvalId": approval["id"], "decision": "approve", "reviewerActorId": "commander"},
        )
        assert approved.status_code == 200
        payload = approved.json()
        assert payload["targetKind"] == "workflow_launch_request"
        assert payload["createdWorkflowId"]
        assert payload["createdConversationId"]

        snapshot = payload["snapshot"]
        workflow = next(item for item in snapshot["workflows"] if item["id"] == payload["createdWorkflowId"])
        conversation = next(item for item in snapshot["conversations"] if item["id"] == payload["createdConversationId"])
        assert conversation["taskPurpose"]
        assert conversation["channelState"] == "workflow"
        assert workflow["conversationId"] == conversation["id"]
        assert len(conversation["memberIds"]) >= 3
        assert len(workflow["context"].get("teamActorIds") or []) >= 2
        assert any(item["agentId"] != workflow["secretaryId"] for item in workflow["assignments"])


def test_system_reset_succeeds_even_with_pending_memory_and_reply_tasks(monkeypatch, tmp_path: Path) -> None:
    with open_client(monkeypatch, tmp_path) as client:
        bootstrap = client.get("/api/bootstrap").json()["snapshot"]
        dm_conversation = next(item for item in bootstrap["conversations"] if item["kind"] == "dm")

        send = client.post(
            "/api/messages/send",
            json={
                "conversationId": dm_conversation["id"],
                "content": "请先简单回应这条消息，然后我会立即执行系统重置。",
                "mode": "chat",
            },
        )
        assert send.status_code == 200

        reset = client.post("/api/system/reset")
        assert reset.status_code == 200
        payload = reset.json()
        assert payload["status"] == "ok"
        assert payload["snapshot"]["agents"]
        assert payload["snapshot"]["conversations"]


def test_group_lifecycle_can_keep_or_disband_completed_room(monkeypatch, tmp_path: Path) -> None:
    with open_client(monkeypatch, tmp_path) as client:
        bootstrap = client.get("/api/bootstrap").json()["snapshot"]
        selected_actor_ids = [item["id"] for item in bootstrap["agents"][:3]]

        dispatch = client.post(
            "/api/workflows/dispatch",
            json={
                "conversationId": "port-hub",
                "title": "协作群保留测试",
                "content": "完成任务后测试协作群保留与解散。",
                "mode": "swarm",
                "actorIds": selected_actor_ids,
            },
        )
        snapshot = dispatch.json()["snapshot"]
        workflow = latest_workflow(snapshot, "协作群保留测试")
        conversation_id = workflow["conversationId"]

        planning = pending_approval(snapshot, workflow["id"], stage_key="planning", review_role="user", target_kind="workflow_stage_review")
        snapshot = client.post(
            "/api/approvals/resolve",
            json={"approvalId": planning["id"], "decision": "approve", "reviewerActorId": "commander"},
        ).json()["snapshot"]
        execution_user = tick_until_pending_approval(
            client,
            snapshot,
            workflow["id"],
            stage_key="execution",
            review_role="user",
        )
        snapshot = client.post(
            "/api/approvals/resolve",
            json={"approvalId": execution_user["id"], "decision": "approve", "reviewerActorId": "commander"},
        ).json()["snapshot"]
        final_review = pending_approval(snapshot, workflow["id"], stage_key="review", review_role="user", target_kind="workflow_stage_review")
        snapshot = client.post(
            "/api/approvals/resolve",
            json={"approvalId": final_review["id"], "decision": "approve", "reviewerActorId": "commander"},
        ).json()["snapshot"]

        kept = client.post("/api/groups/lifecycle", json={"conversationId": conversation_id, "action": "keep"})
        assert kept.status_code == 200
        kept_snapshot = kept.json()["snapshot"]
        kept_conversation = next(item for item in kept_snapshot["conversations"] if item["id"] == conversation_id)
        assert kept_conversation["channelState"] == "chat"
        assert kept_conversation["workflowId"] is None

        disbanded = client.post("/api/groups/lifecycle", json={"conversationId": conversation_id, "action": "disband"})
        assert disbanded.status_code == 200
        disbanded_snapshot = disbanded.json()["snapshot"]
        disbanded_conversation = next(item for item in disbanded_snapshot["conversations"] if item["id"] == conversation_id)
        assert disbanded_conversation["archived"] is True
        assert disbanded_conversation["channelState"] == "archived"
