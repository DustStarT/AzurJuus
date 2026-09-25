from __future__ import annotations

from typing import Any
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session


from backend.platform.constants import DEFAULT_USER
from backend.chat.message_order import message_order
from backend.models import (
    Actor,
    ActorSkill,
    ApprovalRequest,
    Message,
    SkillRun,
    SkillProposal,
    ToolExecutionLog,
    Workflow,
    WorkflowAssignment,
    WorkflowStage,
)
from backend.tasks.workflow_view import build_graph_view
from backend.platform.service_utils import (
    isoformat, preview_text,
)


class TaskService:
    """Tasks operations shared by the application service."""

    def serialize_workflow(self, session: Session, workflow: Workflow) -> dict[str, Any]:
        stages = session.scalars(
            select(WorkflowStage).where(WorkflowStage.workflow_id == workflow.id).order_by(WorkflowStage.position.asc())
        ).all()
        assignments = session.scalars(
            select(WorkflowAssignment).where(WorkflowAssignment.workflow_id == workflow.id).order_by(WorkflowAssignment.created_at.asc())
        ).all()
        approvals = session.scalars(
            select(ApprovalRequest).where(ApprovalRequest.workflow_id == workflow.id).order_by(ApprovalRequest.created_at.asc())
        ).all()
        skill_runs = session.scalars(
            select(SkillRun).where(SkillRun.workflow_id == workflow.id).order_by(SkillRun.created_at.asc())
        ).all()
        tool_logs = [
            item
            for item in session.scalars(select(ToolExecutionLog).order_by(ToolExecutionLog.created_at.asc())).all()
            if (item.request_json or {}).get("workflowId") == workflow.id
        ]
        workflow_messages = [
            item
            for item in (
                session.scalars(
                    select(Message).where(Message.conversation_id == workflow.conversation_id).order_by(*message_order(session))
                ).all()
                if workflow.conversation_id
                else []
            )
            if (item.metadata_json or {}).get("workflowId") == workflow.id
        ]
        skill_run_by_assignment: dict[str, SkillRun] = {}
        for run in skill_runs:
            if run.assignment_id:
                skill_run_by_assignment[run.assignment_id] = run
        serialized_stages = [
            {
                "id": stage.id,
                "key": stage.key,
                "title": stage.title,
                "status": stage.status,
                "position": stage.position,
                "requiresSecretaryReview": stage.requires_secretary_review,
                "requiresUserReview": stage.requires_user_review,
                "pendingApprovalIds": [
                    approval.id
                    for approval in approvals
                    if approval.payload_json.get("stageId") == stage.id and approval.status == "pending"
                ],
                "updatedAt": isoformat(stage.updated_at),
            }
            for stage in stages
        ]
        assignment_activity = dict((workflow.context_json or {}).get("assignmentActivity") or {})
        serialized_assignments = [
            {
                "id": assignment.id,
                "stageId": assignment.stage_id,
                "agentId": assignment.agent_id,
                "summary": assignment.summary,
                "status": assignment.status,
                "activity": assignment_activity.get(assignment.id),
                "dependencyIds": list(assignment.dependency_ids or []),
                "allowParallel": assignment.allow_parallel,
                "skillRunId": skill_run_by_assignment.get(assignment.id).id if skill_run_by_assignment.get(assignment.id) else None,
                "skillId": skill_run_by_assignment.get(assignment.id).primary_skill_id if skill_run_by_assignment.get(assignment.id) else None,
                "skillLabel": skill_run_by_assignment.get(assignment.id).primary_skill_name if skill_run_by_assignment.get(assignment.id) else None,
                "updatedAt": isoformat(assignment.updated_at),
            }
            for assignment in assignments
        ]
        orchestration = (workflow.context_json or {}).get("orchestration") or {}
        return {
            "id": workflow.id,
            "title": workflow.title,
            "description": workflow.description,
            "status": workflow.status,
            "ownerId": workflow.owner_id,
            "secretaryId": workflow.secretary_id,
            "conversationId": workflow.conversation_id,
            "mode": workflow.mode,
            "createdAt": isoformat(workflow.created_at),
            "updatedAt": isoformat(workflow.updated_at),
            "context": workflow.context_json or {},
            "orchestration": orchestration,
            "stages": serialized_stages,
            "assignments": serialized_assignments,
            "graph": build_graph_view(
                workflow_id=workflow.id,
                stages=serialized_stages,
                assignments=serialized_assignments,
                orchestration=orchestration,
            ),
            "timeline": self._build_workflow_timeline(
                workflow=workflow,
                stages=serialized_stages,
                approvals=approvals,
                tool_logs=tool_logs,
                skill_runs=skill_runs,
                messages=workflow_messages,
            ),
        }

    def serialize_approval(self, approval: ApprovalRequest) -> dict[str, Any]:
        progress = self._approval_progress(approval)
        return {
            "id": approval.id,
            "workflowId": approval.workflow_id,
            "toolExecutionId": approval.tool_execution_id,
            "requestedByActorId": approval.requested_by_actor_id,
            "reviewerActorId": approval.reviewer_actor_id,
            "targetKind": approval.target_kind,
            "title": approval.title,
            "summary": approval.summary,
            "payload": approval.payload_json or {},
            "status": approval.status,
            "requiresUser": approval.requires_user,
            "requiresSecretary": approval.requires_secretary,
            "approvedByUser": progress["approvedByUser"],
            "approvedBySecretary": progress["approvedBySecretary"],
            "pendingReviewers": progress["pendingReviewers"],
            "decisionTrail": progress["decisionTrail"],
            "createdAt": isoformat(approval.created_at),
            "resolvedAt": isoformat(approval.resolved_at) if approval.resolved_at else None,
        }

    def _approval_progress(self, approval: ApprovalRequest) -> dict[str, Any]:
        payload = approval.payload_json or {}
        approved_by_user = bool(payload.get("approvedByUser"))
        approved_by_secretary = bool(payload.get("approvedBySecretary"))
        decision_trail = list(payload.get("decisionTrail") or [])
        pending_reviewers: list[str] = []
        if approval.status == "pending":
            if approval.requires_secretary and not approved_by_secretary:
                pending_reviewers.append("secretary")
            if approval.requires_user and not approved_by_user:
                pending_reviewers.append("user")
        return {
            "approvedByUser": approved_by_user,
            "approvedBySecretary": approved_by_secretary,
            "pendingReviewers": pending_reviewers,
            "decisionTrail": decision_trail,
        }

    def serialize_tool_execution(self, execution: ToolExecutionLog) -> dict[str, Any]:
        return {
            "id": execution.id,
            "actorId": execution.actor_id,
            "workflowId": execution.request_json.get("workflowId") if execution.request_json else None,
            "assignmentId": execution.request_json.get("assignmentId") if execution.request_json else None,
            "requestedBySkillId": execution.request_json.get("requestedBySkillId") if execution.request_json else None,
            "origin": execution.request_json.get("origin") if execution.request_json else None,
            "autoExecuteOnApproval": bool(execution.request_json.get("autoExecuteOnApproval")) if execution.request_json else False,
            "toolName": execution.tool_name,
            "riskLevel": execution.risk_level,
            "status": execution.status,
            "request": execution.request_json or {},
            "result": execution.result_json or {},
            "snapshotRef": execution.snapshot_ref,
            "undoRef": execution.undo_ref,
            "approved": execution.approved,
            "createdAt": isoformat(execution.created_at),
            "completedAt": isoformat(execution.completed_at) if execution.completed_at else None,
        }

    def serialize_skill_run(self, skill_run: SkillRun) -> dict[str, Any]:
        return {
            "id": skill_run.id,
            "actorId": skill_run.actor_id,
            "workflowId": skill_run.workflow_id,
            "stageId": skill_run.stage_id,
            "assignmentId": skill_run.assignment_id,
            "conversationId": skill_run.conversation_id,
            "messageId": skill_run.message_id,
            "mode": skill_run.mode,
            "sourceKind": skill_run.source_kind,
            "status": skill_run.status,
            "primarySkillId": skill_run.primary_skill_id,
            "primarySkillName": skill_run.primary_skill_name,
            "selectedSkillIds": list(skill_run.selected_skill_ids or []),
            "selectedSkillNames": list(skill_run.selected_skill_names or []),
            "inputSummary": skill_run.input_summary,
            "outputSummary": skill_run.output_summary,
            "usedToolNames": list(skill_run.used_tool_names or []),
            "createdAt": isoformat(skill_run.created_at),
            "updatedAt": isoformat(skill_run.updated_at),
            "extra": skill_run.extra_json or {},
        }

    def serialize_skill_proposal(self, proposal: SkillProposal) -> dict[str, Any]:
        return {
            "id": proposal.id,
            "actorId": proposal.actor_id,
            "approvalId": proposal.approval_id,
            "baseSkillId": proposal.base_skill_id,
            "baseSkillName": proposal.base_skill_name,
            "targetVisibility": proposal.target_visibility,
            "title": proposal.title,
            "summary": proposal.summary,
            "promptPatch": proposal.prompt_patch,
            "status": proposal.status,
            "reviewerActorId": proposal.reviewer_actor_id,
            "decisionNote": proposal.decision_note,
            "createdAt": isoformat(proposal.created_at),
            "resolvedAt": isoformat(proposal.resolved_at) if proposal.resolved_at else None,
            "extra": proposal.extra_json or {},
        }

    def effective_skill_catalog(self, session: Session) -> list[dict[str, Any]]:
        proposals = self._approved_public_skill_proposals(session)
        return self.bundle.skills.registry.serialize_catalog(proposals)

    def submit_skill_proposal(
        self,
        session: Session,
        *,
        actor_id: str,
        base_skill_id: str,
        title: str,
        summary: str,
        prompt_patch: str,
    ) -> dict[str, Any]:
        actor = session.get(Actor, actor_id)
        if actor is None:
            raise ValueError("Actor not found.")
        base_skill = self.bundle.skills.registry.get(base_skill_id)
        if base_skill is None or base_skill.visibility != "public":
            raise ValueError("Only public skills can receive shared improvement proposals.")
        cleaned_title = str(title or "").strip() or f"{base_skill.name} 改进提案"
        cleaned_summary = str(summary or "").strip() or f"{actor.name} 提交了一份针对 {base_skill.name} 的流程改进说明。"
        cleaned_patch = str(prompt_patch or "").strip()
        if not cleaned_patch:
            raise ValueError("Proposal prompt patch is required.")

        proposal = SkillProposal(
            id=f"skillproposal-{uuid4().hex[:12]}",
            actor_id=actor.id,
            base_skill_id=base_skill.id,
            base_skill_name=base_skill.name,
            target_visibility="public",
            title=cleaned_title,
            summary=cleaned_summary,
            prompt_patch=cleaned_patch,
            status="pending",
            extra_json={"kind": "public-skill-improvement"},
        )
        session.add(proposal)
        session.flush()

        approval = ApprovalRequest(
            id=f"approval-{uuid4().hex[:12]}",
            requested_by_actor_id=actor.id,
            reviewer_actor_id=DEFAULT_USER["id"],
            target_kind="skill_proposal",
            title=f"Public skill proposal: {base_skill.name}",
            summary=f"{actor.name} 提交了公有 Skill「{base_skill.name}」的改进提案，等待用户确认是否纳入公共流程。",
            payload_json={
                "proposalId": proposal.id,
                "baseSkillId": base_skill.id,
                "baseSkillName": base_skill.name,
                "actorId": actor.id,
            },
            status="pending",
            requires_user=True,
            requires_secretary=False,
        )
        session.add(approval)
        session.flush()

        proposal.approval_id = approval.id
        session.add(proposal)
        session.flush()

        return {
            "proposal": self.serialize_skill_proposal(proposal),
            "approvalId": approval.id,
            "snapshot": self.build_snapshot(session),
        }

    def _actor_skill_rows(self, session: Session, actor_id: str) -> list[ActorSkill]:
        return session.scalars(
            select(ActorSkill).where(ActorSkill.actor_id == actor_id, ActorSkill.is_enabled.is_(True)).order_by(ActorSkill.updated_at.desc())
        ).all()

    def _skill_proposal_rows(
        self,
        session: Session,
        *,
        actor_id: str | None = None,
        status: str | None = None,
    ) -> list[SkillProposal]:
        proposals = session.scalars(select(SkillProposal).order_by(SkillProposal.created_at.desc())).all()
        matched: list[SkillProposal] = []
        for proposal in proposals:
            if actor_id and proposal.actor_id != actor_id:
                continue
            if status and proposal.status != status:
                continue
            matched.append(proposal)
        return matched

    def _approved_public_skill_proposals(
        self,
        session: Session,
        *,
        base_skill_id: str | None = None,
    ) -> list[SkillProposal]:
        proposals = self._skill_proposal_rows(session, status="approved")
        matched: list[SkillProposal] = []
        for proposal in proposals:
            if proposal.target_visibility != "public":
                continue
            if base_skill_id and proposal.base_skill_id != base_skill_id:
                continue
            matched.append(proposal)
        return matched

    def _ensure_actor_signature_skill(self, session: Session, actor: Actor) -> None:
        existing = session.scalar(
            select(ActorSkill).where(
                ActorSkill.actor_id == actor.id,
                ActorSkill.origin == "builtin_private",
            )
        )
        if existing is not None:
            return
        seed = self.bundle.skills.registry.build_signature_skill_seed(self.serialize_actor(actor))
        skill = ActorSkill(
            id=f"skill-{uuid4().hex[:12]}",
            actor_id=actor.id,
            skill_id=seed["skill_id"],
            name=seed["name"],
            visibility=seed["visibility"],
            origin=seed["origin"],
            base_skill_id=seed.get("base_skill_id"),
            summary=seed["summary"],
            prompt_patch=seed["prompt_patch"],
            mode_json=list(seed["mode_json"]),
            tool_allowlist_json=list(seed["tool_allowlist_json"]),
            tags_json=list(seed["tags_json"]),
            teachable=bool(seed["teachable"]),
            learnable=bool(seed["learnable"]),
            confidence=float(seed["confidence"]),
            is_enabled=True,
            extra_json=seed.get("extra_json") or {},
        )
        session.add(skill)
        session.flush()

    def _select_skill_execution(
        self,
        session: Session,
        *,
        actor: Actor,
        mode: str,
        prompt: str,
        conversation_kind: str,
        workflow_role: str | None = None,
        source_kind: str = "message",
    ):
        actor_skills = self._actor_skill_rows(session, actor.id)
        approved_public_proposals = self._approved_public_skill_proposals(session)
        return self.bundle.skills.choose_skills(
            actor=self.serialize_actor(actor, actor_skills),
            actor_skill_rows=actor_skills,
            mode=mode,
            prompt=prompt,
            conversation_kind=conversation_kind,
            workflow_role=workflow_role,
            source_kind=source_kind,
            approved_public_proposals=approved_public_proposals,
        )

    def _record_skill_run(
        self,
        session: Session,
        *,
        actor: Actor,
        selection,
        mode: str,
        source_kind: str,
        input_summary: str,
        output_summary: str,
        workflow_id: str | None = None,
        stage_id: str | None = None,
        assignment_id: str | None = None,
        conversation_id: str | None = None,
        message_id: str | None = None,
        used_tool_names: list[str] | None = None,
        extra: dict[str, Any] | None = None,
    ) -> SkillRun:
        extra_payload = dict(extra or {})
        extra_payload.setdefault("allowedToolNames", list(selection.tool_allowlist))
        skill_run = SkillRun(
            id=f"skillrun-{uuid4().hex[:12]}",
            actor_id=actor.id,
            workflow_id=workflow_id,
            stage_id=stage_id,
            assignment_id=assignment_id,
            conversation_id=conversation_id,
            message_id=message_id,
            mode=mode,
            source_kind=source_kind,
            status="completed",
            primary_skill_id=selection.primary_skill_id,
            primary_skill_name=selection.primary_skill_name,
            selected_skill_ids=[item["id"] for item in selection.selected_skills],
            selected_skill_names=[item["name"] for item in selection.selected_skills],
            input_summary=preview_text(input_summary, 180),
            output_summary=preview_text(output_summary, 220),
            used_tool_names=list(used_tool_names or []),
            extra_json=extra_payload,
        )
        session.add(skill_run)
        session.flush()
        return skill_run

    def _maybe_seed_learned_skill(
        self,
        session: Session,
        *,
        learner: Actor,
        teacher: Actor | None,
        teacher_skill_run: SkillRun | None,
        candidate_context: dict[str, Any] | None = None,
    ) -> ActorSkill | None:
        if teacher_skill_run is None or teacher is None or learner.id == teacher.id:
            return None
        if not teacher_skill_run.primary_skill_id.startswith("private."):
            return None
        existing = session.scalar(
            select(ActorSkill).where(
                ActorSkill.actor_id == learner.id,
                ActorSkill.base_skill_id == teacher_skill_run.primary_skill_id,
            )
        )
        if existing is not None:
            return None
        teacher_actor_skill = session.scalar(
            select(ActorSkill).where(
                ActorSkill.actor_id == teacher.id,
                ActorSkill.skill_id == teacher_skill_run.primary_skill_id,
            )
        )
        if teacher_actor_skill is None or not teacher_actor_skill.learnable:
            return None
        seed = self.bundle.skills.registry.build_learned_skill_seed(
            learner=self.serialize_actor(learner, self._actor_skill_rows(session, learner.id)),
            teacher=self.serialize_actor(teacher, self._actor_skill_rows(session, teacher.id)),
            source_skill={
                "id": teacher_actor_skill.skill_id,
                "name": teacher_actor_skill.name,
                "toolAllowlist": list(teacher_actor_skill.tool_allowlist_json or []),
                "tags": list(teacher_actor_skill.tags_json or []),
            },
        )
        learned = ActorSkill(
            id=f"skill-{uuid4().hex[:12]}",
            actor_id=learner.id,
            skill_id=seed["skill_id"],
            name=seed["name"],
            visibility=seed["visibility"],
            origin=seed["origin"],
            base_skill_id=seed.get("base_skill_id"),
            summary=seed["summary"],
            prompt_patch=seed["prompt_patch"],
            mode_json=list(seed["mode_json"]),
            tool_allowlist_json=list(seed["tool_allowlist_json"]),
            tags_json=list(seed["tags_json"]),
            teachable=bool(seed["teachable"]),
            learnable=bool(seed["learnable"]),
            confidence=float(seed["confidence"]),
            is_enabled=True,
            extra_json=seed.get("extra_json") or {},
        )
        session.add(learned)
        if candidate_context is not None:
            learned.is_enabled = False
            learned.summary = f"协作观察候选：{teacher.name} 的方法启发了 {learner.name}，尚未完成试用验证。"
            learned.extra_json = {**learned.extra_json, **candidate_context, "lifecycle": "candidate", "validationCount": 0}
        session.flush()
        return learned

    def _tool_result_preview(self, result: dict[str, Any]) -> str:
        if not result:
            return "工具已完成，但当前没有附带结果摘要。"
        for key in ("path", "dest", "src", "snapshot"):
            value = result.get(key)
            if value:
                return f"结果摘要：{key} = {value}。"
        if "entries" in result:
            return f"结果摘要：列出了 {len(result.get('entries') or [])} 个条目。"
        if "matches" in result:
            return f"结果摘要：命中了 {len(result.get('matches') or [])} 条搜索结果。"
        return f"结果摘要：{preview_text(str(result), 120)}"

    def _build_workflow_timeline(
        self,
        *,
        workflow: Workflow,
        stages: list[dict[str, Any]],
        approvals: list[ApprovalRequest],
        tool_logs: list[ToolExecutionLog],
        skill_runs: list[SkillRun],
        messages: list[Message],
    ) -> list[dict[str, Any]]:
        entries: list[dict[str, Any]] = []
        stage_title_by_key = {stage["key"]: stage["title"] for stage in stages}
        orchestration = (workflow.context_json or {}).get("orchestration") or {}
        for index, event in enumerate(orchestration.get("history") or []):
            stage_key = event.get("stageKey")
            entries.append(
                {
                    "id": f"orchestration-{workflow.id}-{index}",
                    "kind": "orchestration",
                    "title": event.get("type") or "workflow-event",
                    "summary": event.get("summary")
                    or event.get("body")
                    or event.get("transitionKind")
                    or event.get("workflowStatus")
                    or event.get("type")
                    or "workflow event",
                    "stageKey": stage_key,
                    "stageTitle": stage_title_by_key.get(stage_key),
                    "at": event.get("at") or isoformat(workflow.updated_at),
                    "status": event.get("workflowStatus") or orchestration.get("workflowStatus"),
                    "meta": event,
                }
            )

        for approval in approvals:
            entries.append(
                {
                    "id": approval.id,
                    "kind": "approval",
                    "title": approval.title,
                    "summary": approval.summary,
                    "stageKey": (approval.payload_json or {}).get("stageKey"),
                    "stageTitle": stage_title_by_key.get((approval.payload_json or {}).get("stageKey")),
                    "at": isoformat(approval.resolved_at or approval.created_at),
                    "status": approval.status,
                    "meta": self.serialize_approval(approval),
                }
            )

        for execution in tool_logs:
            entries.append(
                {
                    "id": execution.id,
                    "kind": "tool",
                    "title": execution.tool_name,
                    "summary": self._tool_result_preview((execution.result_json or {}).get("result") or {}),
                    "stageKey": None,
                    "stageTitle": None,
                    "at": isoformat(execution.completed_at or execution.created_at),
                    "status": execution.status,
                    "meta": self.serialize_tool_execution(execution),
                }
            )

        for run in skill_runs:
            entries.append(
                {
                    "id": run.id,
                    "kind": "skill-run",
                    "title": run.primary_skill_name,
                    "summary": run.output_summary,
                    "stageKey": next((stage["key"] for stage in stages if stage["id"] == run.stage_id), None),
                    "stageTitle": next((stage["title"] for stage in stages if stage["id"] == run.stage_id), None),
                    "at": isoformat(run.created_at),
                    "status": run.status,
                    "meta": self.serialize_skill_run(run),
                }
            )

        for message in messages:
            metadata = message.metadata_json or {}
            if message.type == "text" and metadata.get("role") is None:
                continue
            entries.append(
                {
                    "id": message.id,
                    "kind": "message",
                    "title": metadata.get("role") or message.type,
                    "summary": preview_text(message.body, 180),
                    "stageKey": metadata.get("stageKey"),
                    "stageTitle": stage_title_by_key.get(metadata.get("stageKey")),
                    "at": isoformat(message.created_at),
                    "status": "visible",
                    "meta": self.serialize_message(message),
                }
            )

        entries.sort(key=lambda item: item["at"])
        return entries[-48:]
