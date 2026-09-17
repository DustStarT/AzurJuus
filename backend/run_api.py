from __future__ import annotations

import asyncio
import json
import hashlib
import os
from pathlib import Path

from fastapi import HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from sqlalchemy import select

from .database import session_scope
from .models import Actor, ActorSkill, Conversation, ConversationMember, Message, SkillRun, MemoryChunk
from .run_coordinator import RunCoordinator
from .run_store import RunStore
from .credentials import public_settings


def install_run_api(app, service, settings):
    store = RunStore(settings.workspace_state_path.parent / "runs.db")
    method_selections = {}

    def load_method(run_id, actor_id, phase):
        run = store.get(run_id)
        mode = "chat" if phase == "chat" or phase.startswith("discussion_") else "review" if phase == "reviewer" else "swarm" if run["collaborative"] else "task"
        with session_scope() as session:
            actor = session.get(Actor, actor_id)
            if actor is None:
                return ""
            selection = service._select_skill_execution(session, actor=actor, mode=mode, prompt=run["prompt"],
                conversation_kind="group" if run["collaborative"] else "dm", workflow_role="secretary" if phase in {"planner", "reviewer"} else "member", source_kind="hermes")
        method_selections[(run_id, phase)] = (actor_id, mode, selection)
        methods = run.get("methods", {})
        methods[phase] = {"actorId": actor_id, "names": [s["name"] for s in selection.selected_skills]}
        store.update(run_id, methods=methods)
        return selection.prompt_patch

    def sync_team(run_id, assignments):
        run = store.get(run_id)
        if not run.get("teamConversationId"):
            return
        selected = {"commander", run["actorId"], *(a["actorId"] for a in assignments)}
        with session_scope() as session:
            members = session.scalars(select(ConversationMember).where(ConversationMember.conversation_id == run["conversationId"])).all()
            present = {m.actor_id for m in members}
            for member in members:
                member.is_active = member.actor_id in selected
            for actor_id in selected - present:
                session.add(ConversationMember(conversation_id=run["conversationId"], actor_id=actor_id, role="member"))
        store.event(run_id, "workspace.changed", {})
        for assignment in assignments:
            store.event(run_id, 'mind.observation', {'key':run_id + ':assignment:' + assignment['id'],
                'actorIds':[assignment['actorId']], 'kind':'request', 'text':assignment['brief'],
                'data':{'scope':'personal_observation'}})
    def load_settings():
        with session_scope() as session:
            return {**service.serialize_settings(service.get_workspace(session)),
                    'userAddress': service.get_or_create_user(session).name}

    async def finished(run_id, text):
        run = store.get(run_id)
        expressed = coordinator.expression and coordinator.expression.enabled
        if expressed and run['mode'] != 'chat':
            await coordinator.expression.speak(run, run['actors'][0], 'result', '告诉用户任务的结果，只说必要结论。',
                facts={'status':run['status'], 'summary':text, 'artifacts':[a.get('path') if isinstance(a,dict) else a for a in run.get('artifacts', [])]})
        message_id = run_id + "-result"
        if expressed:
            phase = 'chat' if run['mode'] == 'chat' else 'result'
            message_id = 'speech-' + hashlib.sha256((run_id + ':' + phase).encode()).hexdigest()[:24]
        with session_scope() as session:
            conversation = session.get(Conversation, run["conversationId"])
            if not expressed and conversation and session.get(Message, message_id) is None:
                service._append_message(session, conversation, run["actorId"], "task" if run["mode"] != "chat" else "text", text, metadata={"runId": run_id, "artifacts": run.get("artifacts", [])}, message_id=message_id)
            origin = session.get(Conversation, run.get("originConversationId")) if run.get("originConversationId") else None
            if origin and origin.id != run["conversationId"] and session.get(Message, run_id + "-origin-result") is None:
                # A task notice is system UI, not the secretary speaking inside
                # someone else's private conversation.
                service._append_message(session, origin, "commander", "task_notice", "协作任务已完成，可在工作记录中查看成果。", metadata={"runId": run_id, "summary": text}, message_id=run_id + "-origin-result")
            for (rid, phase), (actor_id, mode, selection) in list(method_selections.items()):
                if rid != run_id:
                    continue
                record_id = "skillrun-" + hashlib.sha256((run_id + ":" + phase).encode()).hexdigest()[:24]
                if session.get(SkillRun, record_id) is None:
                    record = service._record_skill_run(session, actor=session.get(Actor, actor_id), selection=selection, mode=mode,
                        source_kind="hermes", conversation_id=run["conversationId"], input_summary=run["prompt"], output_summary=text,
                        used_tool_names=[c["name"] for c in store.calls(run_id) if c.get("phase") == phase and c["status"] == "completed"],
                        extra={"runId": run_id, "phase": phase, "verified": run["mode"] != "chat" and not phase.startswith("discussion_")})
                    record.id = record_id
                method_selections.pop((rid, phase), None)
            session.flush()
            candidates = list(run.get("learningCandidates", []))
            by_id = {a["id"]: a for a in run["assignments"]}
            if run["mode"] != "chat" and run["status"] == "completed":
                # Social generation reads business memories rather than the
                # execution journal. Bridge only verified participant outcomes.
                participants = {a["actorId"] for a in run["assignments"]}
                for actor_id in participants:
                    memory_id = "task-memory-" + hashlib.sha256((run_id + ":" + actor_id).encode()).hexdigest()[:24]
                    if session.get(MemoryChunk, memory_id) is None:
                        outcomes = [a.get("result", {}).get("summary", "") for a in run["assignments"] if a["actorId"] == actor_id]
                        session.add(MemoryChunk(id=memory_id, actor_id=actor_id, conversation_id=run["conversationId"],
                            source_kind="verified_task", summary="已验收的参与经历：" + "；".join(outcomes)[:600],
                            metadata_json={"runId":run_id, "verified":True}))
                for assignment in run["assignments"]:
                    for upstream_id in assignment["dependsOn"]:
                        upstream = by_id[upstream_id]
                        teacher_run_id = "skillrun-" + hashlib.sha256((run_id + ":" + upstream_id).encode()).hexdigest()[:24]
                        candidate = service._maybe_seed_learned_skill(session,
                            learner=session.get(Actor, assignment["actorId"]), teacher=session.get(Actor, upstream["actorId"]),
                            teacher_skill_run=session.get(SkillRun, teacher_run_id),
                            candidate_context={"observedRunId":run_id, "upstreamAssignmentId":upstream_id, "learnerAssignmentId":assignment["id"]})
                        if candidate is not None:
                            names = {c['name'] for c in store.calls(run_id) if c.get('phase') == upstream_id and c['status'] == 'completed'}
                            supported = names <= {'list_dir', 'read_file', 'search', 'write_file', 'move', 'mkdir', 'deliver', 'execution_history', 'discuss'}
                            family = 'organize' if supported and 'move' in names else 'inventory' if supported and 'list_dir' in names else None
                            candidate.extra_json = {**candidate.extra_json, 'trialFamily': family}
                            if family:
                                method = ('先列举源文件并记录相对目录，再执行分类移动；保留目录层级，对照源清单检查遗漏。' if family == 'organize'
                                    else '先递归枚举目录，区分文件和子目录，保留完整相对路径；输出清单后读回，对照输入检查漏项与重复。')
                                candidate.prompt_patch += '\n从可见工具行为提炼的待验证方法：' + method
                            candidates.append({"skillId":candidate.skill_id, "name":candidate.name, "actorId":candidate.actor_id, "status":"candidate"})
        if candidates:
            store.update(run_id, learningCandidates=candidates)
        store.update(run_id, resultMessageId=message_id)
        store.event(run_id, "conversation.changed", {"conversationId": run["conversationId"]})

    async def save_reply(run_id, payload):
        run = store.get(run_id)
        with session_scope() as session:
            conversation = session.get(Conversation, run["conversationId"])
            message = session.get(Message, payload["messageId"])
            if conversation and message is None:
                service._append_message(session, conversation, payload["actorId"], "text" if run["mode"] == "chat" else "task_progress", payload["text"],
                    metadata={"runId": run_id, "phase": payload["assignmentId"], 'expression':payload.get('expression',False),
                        'segments':payload.get('segments'), 'sourceIds':payload.get('sourceIds')}, message_id=payload["messageId"])
            elif message is not None:
                message.body = payload["text"]
        store.event(run_id, "conversation.changed", {"conversationId": run["conversationId"]})

    coordinator = RunCoordinator(store, load_settings, finished, "")
    coordinator.message_callback = save_reply
    coordinator.method_loader = load_method
    coordinator.plan_callback = sync_team
    from .cognition import Cognition
    from .cognition_api import install_cognition_api
    coordinator.cognition = Cognition(store)
    from .expression import ExpressionService
    from .terminal_api import install_terminal_api
    coordinator.expression = ExpressionService(coordinator)
    service.expression = coordinator.expression
    install_terminal_api(app, coordinator)
    from .skill_growth import SkillGrowth
    coordinator.growth = SkillGrowth(coordinator, service)
    def check_regression(run_id):
        run = store.get(run_id)
        if run['status'] not in {'failed', 'paused'}:
            return
        # Evidence of a rejected result, not a network outage, pauses a learned method.
        if not any(a.get('revisionNotes') for a in run.get('assignments', [])):
            return
        for (rid, phase), (aid, mode, selection) in list(method_selections.items()):
            if rid != run_id:
                continue
            for selected in selection.selected_skills:
                if selected.get('origin') == 'learned_from_collaboration':
                    coordinator.growth.control(aid, selected['id'], 'rollback')
    coordinator.regression_callback = check_regression
    service.cognition = coordinator.cognition
    install_cognition_api(app, coordinator)
    app.state.runs = coordinator

    @app.get("/api/actors/{actor_id}/methods")
    async def inspect_methods(actor_id: str):
        with session_scope() as session:
            if session.get(Actor, actor_id) is None:
                raise HTTPException(404, "角色不存在。")
            skills = session.scalars(select(ActorSkill).where(ActorSkill.actor_id == actor_id).order_by(ActorSkill.updated_at.desc())).all()
            records = session.scalars(select(SkillRun).where(SkillRun.actor_id == actor_id).order_by(SkillRun.created_at.desc()).limit(12)).all()
            return {"skills":[{"id":s.skill_id, "name":s.name, "summary":s.summary, "enabled":s.is_enabled,
                "origin":s.origin, "lifecycle":(s.extra_json or {}).get("lifecycle", "active"),
                "sourceRunId":(s.extra_json or {}).get("observedRunId"), "validationCount":(s.extra_json or {}).get("validationCount", 0),
                "trialFamily":(s.extra_json or {}).get('trialFamily'), "trialError":(s.extra_json or {}).get('trialError'),
                "revision":(s.extra_json or {}).get('revision', 0), "trialEvidence":(s.extra_json or {}).get('trialEvidence', []),
                "publicReview":(s.extra_json or {}).get('publicReview'), "publicProposalId":(s.extra_json or {}).get('publicProposalId')} for s in skills],
                "records":[{"id":r.id,"names":r.selected_skill_names,"summary":r.output_summary,
                    "verified":bool((r.extra_json or {}).get("verified")),"runId":(r.extra_json or {}).get("runId")} for r in records]}

    @app.post('/api/actors/{actor_id}/methods/{skill_id}/{action}')
    async def method_control(actor_id: str, skill_id: str, action: str, request: Request):
        if not coordinator.endpoint:
            coordinator.endpoint = str(request.base_url).rstrip('/') + '/api/internal/tool'
        try:
            return coordinator.growth.control(actor_id, skill_id, action)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post("/api/agents/persona")
    async def persona(payload: dict):
        with session_scope() as session:
            actor = session.get(Actor, str(payload.get("agentId", "")))
            if actor is None or actor.kind != "agent":
                raise HTTPException(404, "角色不存在。")
            prompt = str(payload.get("systemPrompt", "")).strip()
            if not prompt or len(prompt) > 30000:
                raise HTTPException(400, "人设内容需要 1–30000 字符。")
            actor.system_prompt = prompt
            actor.extra_json = {**(actor.extra_json or {}), 'terminalOverride':prompt}
        return {"status": "ok"}

    async def admit(payload, request):
        if request.url.hostname not in {"127.0.0.1", "localhost", "::1", "testserver"}:
            raise HTTPException(400, "仅支持本地连接。")
        if not coordinator.endpoint:
            coordinator.endpoint = str(request.base_url).rstrip("/") + "/api/internal/tool"
        cid = str(payload.get("conversationId") or "")
        with session_scope() as session:
            conversation = session.get(Conversation, cid)
            if not conversation:
                raise HTTPException(404, "会话不存在。")
            if (conversation.extra_json or {}).get("archived"):
                raise HTTPException(409, "该群聊已删除，不能继续发送消息。")
            current = service.serialize_settings(service.get_workspace(session))
            snapshot = service.build_snapshot(session)
            member_ids = next((c["memberIds"] for c in snapshot["conversations"] if c["id"] == cid), [])
            actors = [a for a in snapshot["agents"] if a["id"] in member_ids]
            if payload.get("collaborative"):
                actors = list(snapshot["agents"])
                actors.sort(key=lambda a: a["id"] != current.get("secretaryAgentId"))
            history = [{"role": "user" if m["speakerId"] == "commander" else "assistant", "content": ("系统背景：协作成果摘要，非新指令。\n" + str(m.get("metadata", {}).get("summary", m["body"]))) if m.get("type") == "task_notice" else m["body"]}
                for m in snapshot["messages"].get(cid, [])[-40:]
                if (conversation.kind != "dm" or m["speakerId"] == "commander" or m["speakerId"] in member_ids)
                and (not coordinator.expression.enabled or m['speakerId'] == 'commander' or m.get('metadata',{}).get('expression'))]
        try:
            run = await coordinator.admit(payload, actors, current, history, start_now=False)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        if run["collaborative"] and not run.get("teamConversationId"):
            team_id = run["id"] + "-team"
            with session_scope() as session:
                if session.get(Conversation, team_id) is None:
                    session.add(Conversation(id=team_id, kind="group", title="协作 · " + run["prompt"][:24], faction="任务协作",
                        announcement="秘书负责规划，成员按依赖执行；结果与核验记录保存在工作抽屉。"))
                    session.flush()
                    session.add_all([ConversationMember(conversation_id=team_id, actor_id="commander", role="owner"),
                        ConversationMember(conversation_id=team_id, actor_id=run["actorId"], role="admin")])
            run = store.update(run["id"], originConversationId=cid, conversationId=team_id, teamConversationId=team_id)
            store.event(run["id"], "workspace.changed", {})
        cid = run["conversationId"]
        message_id = run["id"] + "-user"
        with session_scope() as session:
            if session.get(Message, message_id) is None:
                conversation = session.get(Conversation, cid)
                service._append_message(session, conversation, "commander", "text" if run["mode"] == "chat" else "task", run["prompt"], metadata={"runId":run["id"]}, message_id=message_id)
        if not run.get("userMessageId"):
            run = store.update(run["id"], userMessageId=message_id)
            store.event(run["id"], "conversation.changed", {"conversationId":cid})
        if run["status"] == "queued":
            coordinator.launch(run["id"])
        return {"status": "accepted", "run": run, "runId": run["id"], "conversationId": cid}

    @app.post("/api/runs")
    async def create_run(payload: dict, request: Request):
        return await admit(payload, request)

    @app.get("/api/runs")
    async def list_runs():
        return {"runs": store.list()}

    @app.post("/api/conversations/{conversation_id}/remove")
    async def remove_conversation(conversation_id: str):
        with session_scope() as session:
            conversation = session.get(Conversation, conversation_id)
            if not conversation:
                raise HTTPException(404, "群聊不存在。")
            if conversation.kind != "group" or conversation_id == "port-hub":
                raise HTTPException(409, "仅支持删除任务协作群。")
        related = [r for r in store.list(all_rows=True) if r["conversationId"] == conversation_id]
        for run in related:
            if run["status"] not in {"completed", "cancelled"}:
                await coordinator.control(run["id"], "cancel")
            task = coordinator.tasks.get(run["id"])
            if task and not task.done():
                await asyncio.shield(task)
            await remove_run(run["id"])
        with session_scope() as session:
            conversation = session.get(Conversation, conversation_id)
            conversation.extra_json = {**(conversation.extra_json or {}), "archived": True}
        store.event(None, "workspace.changed", {})
        return {"removed": conversation_id}

    async def remove_run(run_id):
        run = store.get(run_id)
        task = coordinator.tasks.get(run_id)
        if task and not task.done():
            raise ValueError("任务仍在执行或退出，请先停止并等待结束。")
        if run["status"] == "paused":
            await coordinator.control(run_id, "cancel")
        store.remove(run_id)
        for key in list(method_selections):
            if key[0] == run_id:
                method_selections.pop(key, None)

    @app.post("/api/runs/clear")
    async def clear_runs():
        removed = []
        for run in store.list(all_rows=True):
            if run["status"] in {"completed", "failed", "cancelled"}:
                try:
                    await remove_run(run["id"])
                    removed.append(run["id"])
                except ValueError:
                    continue
        return {"removed": removed}

    @app.post("/api/runs/{run_id}/remove")
    async def remove_record(run_id: str):
        try:
            await remove_run(run_id)
        except KeyError:
            raise HTTPException(404, "任务不存在。")
        except ValueError as exc:
            raise HTTPException(409, str(exc))
        return {"removed": [run_id]}

    @app.get("/api/runs/{run_id}")
    async def get_run(run_id: str):
        try:
            return {"run": store.get(run_id), "calls": store.calls(run_id)}
        except KeyError:
            raise HTTPException(404, "任务不存在。")

    @app.post("/api/runs/{run_id}/control")
    async def control(run_id: str, payload: dict):
        try:
            return {"run": await coordinator.control(run_id, payload.get("action"), str(payload.get("text", "")), bool(payload.get("acknowledgeEffects")))}
        except (KeyError, ValueError) as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.post("/api/runs/{run_id}/approvals/{call_id}")
    async def approval(run_id: str, call_id: str, payload: dict):
        try:
            await coordinator.resolve(run_id, call_id, payload.get("decision"))
            return {"run": store.get(run_id), "calls": store.calls(run_id)}
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc

    @app.get("/api/runs/{run_id}/artifacts/{index}")
    async def artifact(run_id: str, index: int):
        try:
            run = store.get(run_id)
            if index < 0:
                raise ValueError("无效产物编号。")
            item = run["artifacts"][index]
            path = coordinator.tools.path(run["workspace"], item["path"])
            if not path.is_file():
                raise ValueError("产物已移除。")
            if hashlib.sha256(path.read_bytes()).hexdigest() != item["sha256"]:
                raise HTTPException(409, "产物在验收后已改变，请重新核验。")
            return FileResponse(path, filename=path.name)
        except (KeyError, ValueError, IndexError) as exc:
            raise HTTPException(404, str(exc)) from exc

    @app.get("/api/runtime/events")
    async def events(after: int = 0, runId: str | None = None):
        return {"events": store.events(max(0, after), runId)}

    @app.get("/api/runs/{run_id}/calls/{call_id}/log")
    async def command_log(run_id: str, call_id: str):
        call = store.call(call_id)
        if not call or call["run_id"] != run_id or call["name"] != "command":
            raise HTTPException(404, "命令记录不存在。")
        path = store.path.parent / "command-logs" / (call["id"] + ".log")
        if not path.is_file():
            raise HTTPException(404, "此调用没有产生输出日志。")
        return FileResponse(path, filename=call["id"] + ".log")

    @app.get("/api/runtime/state")
    async def runtime_state():
        return store.state()

    @app.get("/api/runtime/diagnostics")
    async def diagnostics():
        from .hermes_bridge import ROOT
        from .sqlite_policy import journal_mode
        cfg = load_settings()
        return {"engine": "hermes", "installed": (ROOT / ".vendor/hermes-agent/tui_gateway/entry.py").exists(), "pin": json.loads((ROOT / "hermes.lock.json").read_text()), "settings": public_settings(cfg), "activeRuns": len(coordinator.tasks), "tools": list(__import__("backend.capabilities", fromlist=["CATALOG"]).CATALOG), "database": "SQLite " + journal_mode(), "desktop": os.name == "nt"}

    @app.post("/api/internal/tool")
    async def internal_tool(payload: dict, request: Request):
        token = request.headers.get("authorization", "").removeprefix("Bearer ")
        try:
            return await coordinator.tool(token, payload)
        except PermissionError as exc:
            raise HTTPException(403, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc

    @app.websocket("/ws/runtime")
    async def socket(websocket: WebSocket):
        origin = websocket.headers.get("origin")
        if origin:
            from urllib.parse import urlparse
            if urlparse(origin).netloc != websocket.headers.get("host"):
                await websocket.close(code=1008)
                return
        await websocket.accept()
        try:
            cursor = max(0, int(websocket.query_params.get("after", "0")))
        except ValueError:
            await websocket.close(code=1008, reason="Invalid event cursor")
            return
        try:
            latest = store.state()["cursor"]
            if cursor > latest:
                await websocket.send_json({"seq": latest, "type": "runtime.sync", "payload": {}, "runId": None})
                cursor = latest
            while True:
                batch = store.events(cursor)
                for event in batch:
                    await websocket.send_json(event)
                    cursor = event["seq"]
                if not batch:
                    # Receive doubles as a disconnect detector. Clients send a heartbeat.
                    try:
                        await asyncio.wait_for(websocket.receive_text(), 0.25)
                    except TimeoutError:
                        pass
        except (WebSocketDisconnect, RuntimeError):
            pass

    # These compatibility routes are installed before the legacy implementations.
    # Legacy flow tests must explicitly opt into the old fixture runtime.
    if os.getenv("AZURJUUS_EXECUTION_BACKEND") != "legacy-test":
        @app.post("/api/messages/send")
        async def message(payload: dict, request: Request):
            normalized = {**payload, "prompt": payload.get("content", ""), "mode": payload.get("mode", "chat")}
            return await admit(normalized, request)

        @app.post("/api/workflows/dispatch")
        async def dispatch(payload: dict, request: Request):
            return await admit({**payload, "prompt": payload.get("description") or payload.get("title"), "mode": "task", "collaborative": True}, request)

    return coordinator
