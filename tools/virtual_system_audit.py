from __future__ import annotations

import argparse
import json
import sys
import shutil
import tempfile
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Any, Callable

from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.app import create_app
from backend.config import get_settings


@dataclass
class AuditStep:
    name: str
    ok: bool
    detail: str


def configure_isolated_env(base_dir: Path) -> Path:
    workspace_root = base_dir / "workspace"
    workspace_root.mkdir(parents=True, exist_ok=True)
    import os

    os.environ["AZURJUUS_DATABASE_URL"] = f"sqlite+pysqlite:///{(base_dir / 'azurjuus.db').as_posix()}"
    os.environ["AZURJUUS_REDIS_URL"] = ""
    os.environ["AZURJUUS_CHROMA_URL"] = "http://127.0.0.1:65535"
    os.environ["AZURJUUS_CHROMA_PATH"] = str(base_dir / "chroma")
    os.environ["AZURJUUS_WORKSPACE_ROOT"] = str(workspace_root)
    os.environ["AZURJUUS_WORKSPACE_STATE_PATH"] = str(base_dir / "workspace-state.json")
    os.environ["AZURJUUS_SOCIAL_ENABLED"] = "0"
    os.environ["AZURJUUS_SOCIAL_TICK_SECONDS"] = "9999"
    os.environ["AZURJUUS_WORKFLOW_RUNTIME_ENABLED"] = "0"
    os.environ["AZURJUUS_WORKFLOW_TICK_SECONDS"] = "9999"
    get_settings.cache_clear()
    return workspace_root


def wait_for_snapshot(client: TestClient, predicate: Callable[[dict[str, Any]], bool], attempts: int = 20, delay: float = 0.1) -> dict[str, Any]:
    snapshot: dict[str, Any] = {}
    for _ in range(attempts):
        snapshot = client.get("/api/bootstrap").json()["snapshot"]
        if predicate(snapshot):
            return snapshot
        time.sleep(delay)
    raise AssertionError("snapshot condition not met in time")


def find_dm(snapshot: dict[str, Any]) -> dict[str, Any]:
    return next(item for item in snapshot["conversations"] if item["kind"] == "dm")


def find_pending_approval(snapshot: dict[str, Any], *, target_kind: str | None = None) -> dict[str, Any]:
    for approval in snapshot["approvals"]:
        if approval["status"] != "pending":
            continue
        if target_kind and approval["targetKind"] != target_kind:
            continue
        return approval
    raise AssertionError(f"pending approval not found for target_kind={target_kind}")


def record(steps: list[AuditStep], name: str, fn: Callable[[], str]) -> None:
    try:
        detail = fn()
        steps.append(AuditStep(name=name, ok=True, detail=detail))
    except Exception as error:
        steps.append(AuditStep(name=name, ok=False, detail=str(error)))


def run_audit() -> dict[str, Any]:
    steps: list[AuditStep] = []
    base_dir = Path(tempfile.mkdtemp(prefix="azurjuus-audit-"))
    try:
        workspace_root = configure_isolated_env(base_dir)
        with TestClient(create_app()) as client:
            bootstrap_payload = client.get("/api/bootstrap").json()
            snapshot = bootstrap_payload["snapshot"]
            workspace = bootstrap_payload["workspace"]

            record(
                steps,
                "bootstrap",
                lambda: f"会话 {len(snapshot['conversations'])} 个，智能体 {len(snapshot['agents'])} 个。",
            )

            def inspect_system_step() -> str:
                inspected = client.get("/api/system/inspect")
                assert inspected.status_code == 200
                payload = inspected.json()
                assert payload["authorizedWorkspaceRoot"] == workspace["settings"]["authorizedWorkspaceRoot"]
                assert payload["toolCatalog"]["effectiveDefaultWorkspaceRoot"]
                return (
                    f"授权工作区={payload['authorizedWorkspaceRoot'] or '(未设置)'}，"
                    f"本地工具 {len(payload['toolCatalog']['localTools'])} 个，"
                    f"外部工具 {len(payload['toolCatalog']['externalTools'])} 个。"
                )

            record(steps, "system-inspect", inspect_system_step)

            dm = find_dm(snapshot)

            def chat_step() -> str:
                accepted = client.post(
                    "/api/messages/send",
                    json={"conversationId": dm["id"], "content": "今天天气如何？先简单闲聊一下。", "mode": "chat"},
                )
                assert accepted.status_code == 200
                updated = wait_for_snapshot(
                    client,
                    lambda snap: len(snap["messages"][dm["id"]]) >= len(snapshot["messages"][dm["id"]]) + 2,
                )
                return f"私聊消息已往返，当前消息数 {len(updated['messages'][dm['id']])}。"

            record(steps, "chat-roundtrip", chat_step)

            def solo_task_step() -> str:
                accepted = client.post(
                    "/api/messages/send",
                    json={"conversationId": dm["id"], "content": "请帮我整理一下今天的工作要点。", "mode": "task"},
                )
                assert accepted.status_code == 200
                updated = wait_for_snapshot(
                    client,
                    lambda snap: any(item["conversationId"] == dm["id"] and item["mode"] == "task" for item in snap["workflows"]),
                )
                workflow = next(item for item in updated["workflows"] if item["conversationId"] == dm["id"] and item["mode"] == "task")
                return f"单人任务已生成，workflow={workflow['id']}，状态={workflow['status']}。"

            record(steps, "solo-task", solo_task_step)

            collaborative_prompt = (
                "整理授权工作区内多份文档，需要明确分工、并行处理、阶段审批、群聊同步和风险确认。"
                "这个任务包含多文件检查、分类建议、执行计划、回滚说明、协作记录和总结。"
            )

            def collaborative_task_step() -> str:
                dispatched = client.post(
                    "/api/workflows/dispatch",
                    json={
                        "conversationId": dm["id"],
                        "title": "虚拟协作任务",
                        "content": collaborative_prompt,
                        "mode": "swarm",
                    },
                )
                assert dispatched.status_code == 200
                snap = dispatched.json()["snapshot"]
                workflow = next(item for item in reversed(snap["workflows"]) if item["title"] == "虚拟协作任务")
                approval = next(
                    item
                    for item in snap["approvals"]
                    if item["workflowId"] == workflow["id"] and item["status"] == "pending"
                )
                approved = client.post(
                    "/api/approvals/resolve",
                    json={"approvalId": approval["id"], "decision": "approve"},
                )
                assert approved.status_code == 200
                final_snapshot = approved.json()["snapshot"]
                conversation = next(item for item in final_snapshot["conversations"] if item["id"] == workflow["conversationId"])
                agent_member_count = len([item for item in conversation["memberIds"] if item != "commander"])
                assert agent_member_count >= 2
                return f"多人任务已启动，协作频道={conversation['title']}，智能体成员 {agent_member_count} 人。"

            record(steps, "collaborative-task-approve", collaborative_task_step)

            def collaborative_reject_step() -> str:
                dispatched = client.post(
                    "/api/workflows/dispatch",
                    json={
                        "conversationId": dm["id"],
                        "title": "虚拟协作任务-拒绝",
                        "content": collaborative_prompt,
                        "mode": "swarm",
                    },
                )
                assert dispatched.status_code == 200
                snap = dispatched.json()["snapshot"]
                workflow = next(item for item in reversed(snap["workflows"]) if item["title"] == "虚拟协作任务-拒绝")
                approval = next(
                    item
                    for item in snap["approvals"]
                    if item["workflowId"] == workflow["id"] and item["status"] == "pending"
                )
                rejected = client.post(
                    "/api/approvals/resolve",
                    json={"approvalId": approval["id"], "decision": "reject"},
                )
                assert rejected.status_code == 200
                return "多人任务启动确认已成功拒绝。"

            record(steps, "collaborative-task-reject", collaborative_reject_step)

            def tool_flow_step() -> str:
                saved = client.post(
                    "/api/workspace/save",
                    json={
                        "workspace": {
                            **workspace,
                            "settings": {
                                **workspace["settings"],
                                "authorizedWorkspaceRoot": str(workspace_root),
                            },
                        }
                    },
                )
                assert saved.status_code == 200
                planned = client.post(
                    "/api/tools/plan",
                    json={
                        "actorId": "commander",
                        "toolName": "list_dir",
                        "args": {"path": "."},
                    },
                )
                assert planned.status_code == 200
                execution_id = planned.json()["executionId"]
                executed = client.post("/api/tools/execute", json={"executionId": execution_id})
                assert executed.status_code == 200
                return f"工具计划与执行成功，execution={execution_id}。"

            record(steps, "tool-plan-execute", tool_flow_step)

            def social_flow_step() -> str:
                refreshed = client.get("/api/bootstrap").json()["snapshot"]
                author_id = refreshed["agents"][0]["id"]
                published = client.post(
                    "/api/posts/publish",
                    json={"authorId": author_id, "body": "今天先记录一条虚拟动态，用于系统巡检。"},
                )
                assert published.status_code == 200
                post_id = published.json()["snapshot"]["posts"][0]["id"]
                commented = client.post(
                    "/api/posts/comment",
                    json={"postId": post_id, "body": "收到，这里做一条回帖检查。"},
                )
                assert commented.status_code == 200
                return f"朋友圈动态与评论成功，post={post_id}。"

            record(steps, "social-post-comment", social_flow_step)

            def reset_step() -> str:
                reset = client.post("/api/system/reset")
                assert reset.status_code == 200
                payload = reset.json()["workspace"]
                assert payload["data"]["workflows"] == []
                return "系统重置成功，任务与动态已清空。"

            record(steps, "system-reset", reset_step)

        summary = {
            "ok": all(step.ok for step in steps),
            "stepCount": len(steps),
            "passed": len([step for step in steps if step.ok]),
            "failed": len([step for step in steps if not step.ok]),
            "steps": [asdict(step) for step in steps],
            "tempDir": str(base_dir),
        }
        return summary
    finally:
        try:
            shutil.rmtree(base_dir, ignore_errors=True)
        except Exception:
            pass


def main() -> int:
    parser = argparse.ArgumentParser(description="Run a virtual end-to-end audit of the AzurJuus system.")
    parser.add_argument("--json", action="store_true", help="Print the summary as JSON.")
    args = parser.parse_args()

    summary = run_audit()
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    else:
        print("AzurJuus virtual audit")
        print(f"Passed: {summary['passed']} / {summary['stepCount']}")
        for step in summary["steps"]:
            marker = "PASS" if step["ok"] else "FAIL"
            print(f"[{marker}] {step['name']}: {step['detail']}")
    return 0 if summary["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
