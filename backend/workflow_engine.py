from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

try:  # pragma: no cover - optional runtime dependency
    from langgraph.graph import END, START, StateGraph
except Exception:  # pragma: no cover - optional runtime dependency
    END = None
    START = None
    StateGraph = None


def now_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


@dataclass
class WorkflowBlueprint:
    title: str
    summary: str
    stage_keys: list[str]
    team_actor_ids: list[str]
    parallel_groups: list[list[str]]
    assignment_briefs: dict[str, str]
    secretary_plan: str
    outsider_candidate_ids: list[str]


@dataclass
class WorkflowAdvanceResult:
    workflow_status: str
    current_stage_key: str
    next_stage_key: str | None
    next_review_role: str | None
    transition_kind: str
    mark_current_completed: bool = False
    mark_next_in_progress: bool = False
    requires_revision: bool = False
    is_completed: bool = False


@dataclass
class WorkflowActionPlan:
    transition_message: str | None = None
    transition_role: str | None = None
    trigger_execution_simulation: bool = False
    generate_review_summary_stage_key: str | None = None
    review_requests: list[dict[str, Any]] | None = None
    publish_completion_post: bool = False
    cancel_pending_stage_reviews: bool = False
    reopen_assignments: bool = False
    helper_target_actor_id: str | None = None
    helper_summary: str | None = None
    helper_join_role: str = "observer"
    helper_auto_reply: bool = False


class WorkflowEngine:
    DEFAULT_STAGE_KEYS = ["planning", "execution", "review"]

    def __init__(self) -> None:
        self._graph = self._compile_graph()

    def build_secretary_blueprint(
        self,
        *,
        title: str,
        description: str,
        secretary_actor_id: str | None,
        candidate_actors: list[dict[str, Any]],
        requested_actor_ids: list[str] | None = None,
    ) -> WorkflowBlueprint:
        summary = description.strip() or title.strip() or "new task"
        candidates = list(candidate_actors)
        requested = list(requested_actor_ids or [])

        if requested:
            chosen = [item for item in candidates if item["id"] in requested]
        else:
            chosen = sorted(candidates, key=lambda item: self._score_actor(item, summary), reverse=True)
            chosen = chosen[: max(2, min(4, len(chosen)))]

        chosen_ids = [item["id"] for item in chosen]
        if secretary_actor_id and secretary_actor_id not in chosen_ids:
            secretary = next((item for item in candidates if item["id"] == secretary_actor_id), None)
            if secretary is not None:
                chosen = [secretary, *chosen[:3]]
                deduped: list[dict[str, Any]] = []
                seen: set[str] = set()
                for item in chosen:
                    if item["id"] in seen:
                        continue
                    seen.add(item["id"])
                    deduped.append(item)
                chosen = deduped
                chosen_ids = [item["id"] for item in chosen]

        if len(chosen) < 2 and len(candidates) > len(chosen):
            fallback_candidates = sorted(candidates, key=lambda item: self._score_actor(item, summary), reverse=True)
            for item in fallback_candidates:
                if item["id"] in chosen_ids:
                    continue
                chosen.append(item)
                chosen_ids.append(item["id"])
                if len(chosen) >= 2:
                    break

        worker_ids = [item["id"] for item in chosen if item["id"] != secretary_actor_id]
        parallel_groups = [worker_ids] if worker_ids else ([[chosen_ids[0]]] if chosen_ids else [])

        assignment_briefs = {
            item["id"]: self._assignment_brief(item, summary, index)
            for index, item in enumerate(chosen)
        }

        secretary_name = next((item["name"] for item in chosen if item["id"] == secretary_actor_id), "Secretary")
        secretary_plan = (
            f"{secretary_name} completed the first pass of team formation for "
            f"'{title.strip() or 'new task'}'. Members keep their in-character methods while "
            "the secretary keeps direction, review, and risk alignment."
        )

        outsider_candidate_ids = [item["id"] for item in candidates if item["id"] not in chosen_ids]

        return WorkflowBlueprint(
            title=title.strip() or "new task",
            summary=summary,
            stage_keys=list(self.DEFAULT_STAGE_KEYS),
            team_actor_ids=chosen_ids,
            parallel_groups=parallel_groups,
            assignment_briefs=assignment_briefs,
            secretary_plan=secretary_plan,
            outsider_candidate_ids=outsider_candidate_ids,
        )

    def build_runtime_context(
        self,
        *,
        workflow_id: str,
        mode: str,
        origin_conversation_id: str,
        secretary_actor_id: str | None,
        blueprint: WorkflowBlueprint,
    ) -> dict[str, Any]:
        return {
            "originConversationId": origin_conversation_id,
            "teamActorIds": list(blueprint.team_actor_ids),
            "outsiderCandidateIds": list(blueprint.outsider_candidate_ids),
            "planningAcknowledged": False,
            "approvedHelpers": [],
            "helpRequests": [],
            "interruptions": [],
            "revisionCount": 0,
            "parallelGroups": [list(group) for group in blueprint.parallel_groups],
            "secretaryPlan": blueprint.secretary_plan,
            "orchestration": {
                "engine": "langgraph" if self._graph is not None else "builtin",
                "graphVersion": "v2",
                "workflowId": workflow_id,
                "mode": mode,
                "secretaryActorId": secretary_actor_id,
                "currentStageKey": "planning",
                "nextReviewRole": "user",
                "workflowStatus": "awaiting_plan_approval",
                "transitionKind": "created",
                "history": [
                    {
                        "type": "workflow-created",
                        "stageKey": "planning",
                        "workflowStatus": "awaiting_plan_approval",
                        "at": now_iso(),
                    }
                ],
            },
        }

    def build_interrupt_patch(self, *, workflow_id: str, body: str, actor_id: str = "commander") -> dict[str, Any]:
        return {
            "workflowId": workflow_id,
            "actorId": actor_id,
            "body": body.strip(),
            "kind": "human-interrupt",
            "at": now_iso(),
        }

    def apply_interrupt(self, context: dict[str, Any] | None, *, active_stage_key: str, patch: dict[str, Any]) -> dict[str, Any]:
        state = self._ensure_runtime_state(context or {})
        history = list(state.get("history") or [])
        history.append(
            {
                "type": "interrupt",
                "stageKey": active_stage_key,
                "body": patch.get("body") or "",
                "actorId": patch.get("actorId") or "commander",
                "at": patch.get("at") or now_iso(),
            }
        )
        state.update(
            {
                "currentStageKey": active_stage_key,
                "nextReviewRole": "user" if active_stage_key == "planning" else "secretary" if active_stage_key == "execution" else "user",
                "workflowStatus": "needs_revision",
                "transitionKind": "interrupt",
                "history": history,
                "lastInterrupt": patch,
            }
        )
        return state

    def resolve_help_request(
        self,
        context: dict[str, Any] | None,
        *,
        approval_id: str,
        target_actor_id: str,
        approved: bool,
    ) -> dict[str, Any]:
        state = self._ensure_runtime_state(context or {})
        history = list(state.get("history") or [])
        history.append(
            {
                "type": "help-request-resolved",
                "approvalId": approval_id,
                "targetActorId": target_actor_id,
                "approved": approved,
                "at": now_iso(),
            }
        )
        state["history"] = history
        return state

    def record_completion_post(
        self,
        context: dict[str, Any] | None,
        *,
        post_id: str,
        comment_count: int,
    ) -> dict[str, Any]:
        state = self._ensure_runtime_state(context or {})
        history = list(state.get("history") or [])
        history.append(
            {
                "type": "completion-post-created",
                "postId": post_id,
                "commentCount": comment_count,
                "at": now_iso(),
            }
        )
        state["history"] = history
        return state

    def resolve_stage_approval(
        self,
        context: dict[str, Any] | None,
        *,
        stage_key: str,
        review_role: str,
        decision: str,
    ) -> tuple[dict[str, Any], WorkflowAdvanceResult]:
        state = self._ensure_runtime_state(context or {})
        if self._graph is not None:
            graph_state = self._graph.invoke(
                {
                    "current_stage_key": stage_key,
                    "review_role": review_role,
                    "decision": decision,
                    "workflow_status": state.get("workflowStatus") or "active",
                }
            )
        else:
            graph_state = self._resolve_without_graph(
                stage_key=stage_key,
                review_role=review_role,
                decision=decision,
                workflow_status=state.get("workflowStatus") or "active",
            )

        result = WorkflowAdvanceResult(
            workflow_status=graph_state["workflow_status"],
            current_stage_key=graph_state["current_stage_key"],
            next_stage_key=graph_state.get("next_stage_key"),
            next_review_role=graph_state.get("next_review_role"),
            transition_kind=graph_state["transition_kind"],
            mark_current_completed=bool(graph_state.get("mark_current_completed")),
            mark_next_in_progress=bool(graph_state.get("mark_next_in_progress")),
            requires_revision=bool(graph_state.get("requires_revision")),
            is_completed=bool(graph_state.get("is_completed")),
        )

        history = list(state.get("history") or [])
        history.append(
            {
                "type": "approval-resolved",
                "stageKey": stage_key,
                "reviewRole": review_role,
                "decision": decision,
                "transitionKind": result.transition_kind,
                "workflowStatus": result.workflow_status,
                "nextStageKey": result.next_stage_key,
                "nextReviewRole": result.next_review_role,
                "at": now_iso(),
            }
        )
        state.update(
            {
                "currentStageKey": result.current_stage_key,
                "nextReviewRole": result.next_review_role,
                "workflowStatus": result.workflow_status,
                "transitionKind": result.transition_kind,
                "history": history,
            }
        )
        return state, result

    def build_graph_view(
        self,
        *,
        workflow_id: str,
        stages: list[dict[str, Any]],
        assignments: list[dict[str, Any]],
        orchestration: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        orchestration = orchestration or {}
        current_stage_key = orchestration.get("currentStageKey")
        nodes = [
            {
                "id": f"stage:{stage['key']}",
                "kind": "stage",
                "label": stage["title"],
                "stageKey": stage["key"],
                "status": stage["status"],
                "active": stage["key"] == current_stage_key,
                "position": stage.get("position"),
            }
            for stage in stages
        ]
        nodes.extend(
            {
                "id": f"assignment:{assignment['id']}",
                "kind": "assignment",
                "label": assignment["summary"],
                "stageId": assignment.get("stageId"),
                "agentId": assignment["agentId"],
                "status": assignment["status"],
                "skillLabel": assignment.get("skillLabel"),
                "allowParallel": assignment.get("allowParallel", True),
            }
            for assignment in assignments
        )

        edges = []
        ordered_stage_ids = [f"stage:{stage['key']}" for stage in stages]
        for index in range(len(ordered_stage_ids) - 1):
            edges.append(
                {
                    "id": f"edge:{ordered_stage_ids[index]}->{ordered_stage_ids[index + 1]}",
                    "from": ordered_stage_ids[index],
                    "to": ordered_stage_ids[index + 1],
                    "kind": "stage-flow",
                }
            )

        stage_key_by_id = {stage["id"]: stage["key"] for stage in stages}
        for assignment in assignments:
            stage_key = stage_key_by_id.get(assignment.get("stageId"))
            if stage_key:
                edges.append(
                    {
                        "id": f"edge:stage:{stage_key}->assignment:{assignment['id']}",
                        "from": f"stage:{stage_key}",
                        "to": f"assignment:{assignment['id']}",
                        "kind": "assignment-branch",
                    }
                )

        return {
            "workflowId": workflow_id,
            "engine": orchestration.get("engine") or ("langgraph" if self._graph is not None else "builtin"),
            "version": orchestration.get("graphVersion") or "v2",
            "currentStageKey": current_stage_key,
            "workflowStatus": orchestration.get("workflowStatus"),
            "nodes": nodes,
            "edges": edges,
        }

    def plan_after_approval(self, result: WorkflowAdvanceResult) -> WorkflowActionPlan:
        review_requests: list[dict[str, Any]] = []

        if result.transition_kind == "planning-approved":
            return WorkflowActionPlan(
                transition_message="规划阶段已确认，任务进入执行阶段。成员会保留各自的人设与方法逐步推进，并由秘书继续统筹方向与风险。",
                transition_role="stage-transition",
                trigger_execution_simulation=True,
                review_requests=review_requests,
            )

        if result.transition_kind == "execution-secretary-approved":
            review_requests.append(
                {
                    "stageKey": "execution",
                    "reviewRole": "user",
                    "title": "执行阶段待用户审查",
                    "summary": "秘书已完成执行阶段审查，请用户确认是否进入最终提交流程。",
                    "requiresUser": True,
                    "requiresSecretary": False,
                    "reviewerKind": "user",
                }
            )
            return WorkflowActionPlan(
                transition_message="秘书已完成执行阶段审查，当前等待用户确认是否进入最终提交流程。",
                transition_role="review-request",
                review_requests=review_requests,
            )

        if result.transition_kind == "execution-user-approved":
            review_requests.append(
                {
                    "stageKey": "review",
                    "reviewRole": "user",
                    "title": "最终结果待确认",
                    "summary": "秘书已整理最终汇总，请确认是否正式完成当前任务。",
                    "requiresUser": True,
                    "requiresSecretary": False,
                    "reviewerKind": "user",
                }
            )
            return WorkflowActionPlan(
                generate_review_summary_stage_key="review",
                review_requests=review_requests,
            )

        if result.transition_kind == "workflow-completed":
            return WorkflowActionPlan(
                transition_message="任务已完成。秘书会将本轮结果归档，并在需要时把后续感想同步到 JUUS 动态。",
                transition_role="workflow-complete",
                publish_completion_post=True,
            )

        return WorkflowActionPlan(review_requests=review_requests)

    def plan_after_rejection(self, *, stage_key: str, review_role: str) -> WorkflowActionPlan:
        if stage_key == "planning":
            return WorkflowActionPlan(
                transition_message="当前规划未通过确认，秘书会根据反馈重新整理组队与分工，再次提交规划方案。",
                transition_role="review-rejected",
                cancel_pending_stage_reviews=True,
                generate_review_summary_stage_key="planning",
                review_requests=[
                    {
                        "stageKey": "planning",
                        "reviewRole": "user",
                        "title": "规划修订待确认",
                        "summary": "秘书已根据反馈重新整理规划，请确认是否进入执行阶段。",
                        "requiresUser": True,
                        "requiresSecretary": False,
                        "reviewerKind": "user",
                    }
                ],
            )
        if stage_key == "execution":
            return WorkflowActionPlan(
                transition_message="当前执行阶段未通过审查，团队会按反馈继续调整，并在完成后重新提交秘书审查。",
                transition_role="review-rejected",
                cancel_pending_stage_reviews=True,
                reopen_assignments=True,
                trigger_execution_simulation=True,
            )
        if stage_key == "review":
            return WorkflowActionPlan(
                transition_message="最终结果尚未通过确认，秘书会根据反馈重新整理，再次提交给用户确认。",
                transition_role="review-rejected",
                cancel_pending_stage_reviews=True,
                generate_review_summary_stage_key="review",
                review_requests=[
                    {
                        "stageKey": "review",
                        "reviewRole": "user",
                        "title": "最终结果修订待确认",
                        "summary": "秘书已根据反馈重新整理最终结果，请确认是否正式完成当前任务。",
                        "requiresUser": True,
                        "requiresSecretary": False,
                        "reviewerKind": "user",
                    }
                ],
            )
        return WorkflowActionPlan(
            transition_message="当前阶段未通过审查，任务会停留在本阶段，等待补充说明或重新推进。",
            transition_role="review-rejected",
            cancel_pending_stage_reviews=True,
        )

    def build_transition_context_patch(self, result: WorkflowAdvanceResult) -> dict[str, Any]:
        timestamp = now_iso()
        if result.transition_kind == "planning-approved":
            return {
                "planningAcknowledged": True,
                "lastStageTransition": {"from": "planning", "to": "execution", "at": timestamp},
            }
        if result.transition_kind == "execution-user-approved":
            return {
                "lastStageTransition": {"from": "execution", "to": "review", "at": timestamp},
            }
        if result.transition_kind == "workflow-completed":
            return {
                "completedAt": timestamp,
                "lastStageTransition": {"from": "review", "to": "completed", "at": timestamp},
            }
        return {}

    def plan_after_interrupt(self, active_stage_key: str) -> WorkflowActionPlan:
        if active_stage_key == "planning":
            return WorkflowActionPlan(
                transition_message="用户补充了新的规划要求，当前先暂停推进，等待更新后的规划确认。",
                transition_role="interrupt",
                cancel_pending_stage_reviews=True,
                review_requests=[
                    {
                        "stageKey": "planning",
                        "reviewRole": "user",
                        "title": "规划修订待确认",
                        "summary": "用户插入了新的修改意见，请确认修订后的规划是否可以继续执行。",
                        "requiresUser": True,
                        "requiresSecretary": False,
                        "reviewerKind": "user",
                    }
                ],
            )
        if active_stage_key == "execution":
            return WorkflowActionPlan(
                transition_message="用户补充了新的执行要求，当前团队会先按修订方向调整，再重新进入阶段审查。",
                transition_role="interrupt",
                cancel_pending_stage_reviews=True,
                reopen_assignments=True,
                trigger_execution_simulation=True,
            )
        if active_stage_key == "review":
            return WorkflowActionPlan(
                transition_message="用户在最终确认前补充了新的要求，秘书会据此重新整理最终结果。",
                transition_role="interrupt",
                cancel_pending_stage_reviews=True,
                generate_review_summary_stage_key="review",
                review_requests=[
                    {
                        "stageKey": "review",
                        "reviewRole": "user",
                        "title": "最终结果修订待确认",
                        "summary": "用户插入了新的修改意见，秘书已重新整理最终结果，请确认是否完成。",
                        "requiresUser": True,
                        "requiresSecretary": False,
                        "reviewerKind": "user",
                    }
                ],
            )
        return WorkflowActionPlan(cancel_pending_stage_reviews=True)

    def plan_after_help_resolution(
        self,
        *,
        approved: bool,
        target_actor_id: str,
        summary: str | None = None,
    ) -> WorkflowActionPlan:
        if approved:
            return WorkflowActionPlan(
                transition_message="编外协助申请已获批准，新的协助成员将加入当前任务讨论。",
                transition_role="help-approved",
                helper_target_actor_id=target_actor_id,
                helper_summary=summary,
                helper_join_role="observer",
                helper_auto_reply=True,
            )
        return WorkflowActionPlan(
            transition_message="编外协助申请未获批准，当前团队需要先在现有成员范围内继续推进。",
            transition_role="help-rejected",
        )

    def plan_runtime_tick(
        self,
        *,
        current_stage_key: str | None,
        workflow_status: str | None,
    ) -> WorkflowActionPlan:
        if current_stage_key == "execution" and workflow_status in {"active", "needs_revision"}:
            return WorkflowActionPlan(trigger_execution_simulation=True)
        return WorkflowActionPlan()

    def _compile_graph(self):
        if StateGraph is None or START is None or END is None:
            return None

        graph = StateGraph(dict)
        graph.add_node("planning_user_approve", self._node_planning_user_approve)
        graph.add_node("execution_secretary_approve", self._node_execution_secretary_approve)
        graph.add_node("execution_user_approve", self._node_execution_user_approve)
        graph.add_node("review_user_approve", self._node_review_user_approve)
        graph.add_node("reject_or_revise", self._node_reject_or_revise)
        graph.add_node("noop", self._node_noop)

        graph.add_conditional_edges(
            START,
            self._route_event,
            {
                "planning_user_approve": "planning_user_approve",
                "execution_secretary_approve": "execution_secretary_approve",
                "execution_user_approve": "execution_user_approve",
                "review_user_approve": "review_user_approve",
                "reject_or_revise": "reject_or_revise",
                "noop": "noop",
            },
        )
        for node_name in (
            "planning_user_approve",
            "execution_secretary_approve",
            "execution_user_approve",
            "review_user_approve",
            "reject_or_revise",
            "noop",
        ):
            graph.add_edge(node_name, END)
        return graph.compile()

    def _route_event(self, state: dict[str, Any]) -> str:
        decision = str(state.get("decision") or "reject")
        stage_key = str(state.get("current_stage_key") or "")
        review_role = str(state.get("review_role") or "")
        if decision != "approve":
            return "reject_or_revise"
        if stage_key == "planning" and review_role == "user":
            return "planning_user_approve"
        if stage_key == "execution" and review_role == "secretary":
            return "execution_secretary_approve"
        if stage_key == "execution" and review_role == "user":
            return "execution_user_approve"
        if stage_key == "review" and review_role == "user":
            return "review_user_approve"
        return "noop"

    def _node_planning_user_approve(self, state: dict[str, Any]) -> dict[str, Any]:
        return {
            **state,
            "workflow_status": "active",
            "current_stage_key": "execution",
            "next_stage_key": "execution",
            "next_review_role": "secretary",
            "transition_kind": "planning-approved",
            "mark_current_completed": True,
            "mark_next_in_progress": True,
        }

    def _node_execution_secretary_approve(self, state: dict[str, Any]) -> dict[str, Any]:
        return {
            **state,
            "workflow_status": "active",
            "current_stage_key": "execution",
            "next_stage_key": "execution",
            "next_review_role": "user",
            "transition_kind": "execution-secretary-approved",
        }

    def _node_execution_user_approve(self, state: dict[str, Any]) -> dict[str, Any]:
        return {
            **state,
            "workflow_status": "active",
            "current_stage_key": "review",
            "next_stage_key": "review",
            "next_review_role": "user",
            "transition_kind": "execution-user-approved",
            "mark_current_completed": True,
            "mark_next_in_progress": True,
        }

    def _node_review_user_approve(self, state: dict[str, Any]) -> dict[str, Any]:
        return {
            **state,
            "workflow_status": "completed",
            "current_stage_key": "review",
            "next_stage_key": None,
            "next_review_role": None,
            "transition_kind": "workflow-completed",
            "mark_current_completed": True,
            "is_completed": True,
        }

    def _node_reject_or_revise(self, state: dict[str, Any]) -> dict[str, Any]:
        return {
            **state,
            "workflow_status": "needs_revision",
            "transition_kind": "revision-requested",
            "requires_revision": True,
            "next_stage_key": state.get("current_stage_key"),
            "next_review_role": state.get("review_role"),
        }

    def _node_noop(self, state: dict[str, Any]) -> dict[str, Any]:
        return {
            **state,
            "transition_kind": "noop",
            "next_stage_key": state.get("current_stage_key"),
            "next_review_role": state.get("review_role"),
        }

    def _resolve_without_graph(self, *, stage_key: str, review_role: str, decision: str, workflow_status: str) -> dict[str, Any]:
        state = {
            "current_stage_key": stage_key,
            "review_role": review_role,
            "decision": decision,
            "workflow_status": workflow_status,
        }
        route = self._route_event(state)
        handler = {
            "planning_user_approve": self._node_planning_user_approve,
            "execution_secretary_approve": self._node_execution_secretary_approve,
            "execution_user_approve": self._node_execution_user_approve,
            "review_user_approve": self._node_review_user_approve,
            "reject_or_revise": self._node_reject_or_revise,
            "noop": self._node_noop,
        }[route]
        return handler(state)

    def _ensure_runtime_state(self, context: dict[str, Any]) -> dict[str, Any]:
        orchestration = dict(context.get("orchestration") or {})
        orchestration.setdefault("engine", "langgraph" if self._graph is not None else "builtin")
        orchestration.setdefault("graphVersion", "v2")
        orchestration.setdefault("currentStageKey", "planning")
        orchestration.setdefault("nextReviewRole", "user")
        orchestration.setdefault("workflowStatus", "awaiting_plan_approval")
        orchestration.setdefault("transitionKind", "created")
        orchestration.setdefault("history", [])
        return orchestration

    def _score_actor(self, actor: dict[str, Any], task_summary: str) -> float:
        score = 0.4
        task_text = task_summary.lower()
        capabilities = [str(item).lower() for item in actor.get("capabilities") or []]
        keywords = str(actor.get("keywords") or "").lower()
        persona = str(actor.get("summary") or actor.get("persona") or "").lower()

        for token in capabilities:
            if token and token in task_text:
                score += 0.24

        for token in ("整理", "分析", "协调", "审查", "执行", "规划", "协作"):
            if token in task_summary and (token in keywords or token in persona or token in capabilities):
                score += 0.18

        if actor.get("favorite"):
            score += 0.08

        return score

    def _assignment_brief(self, actor: dict[str, Any], summary: str, index: int) -> str:
        priorities = [
            "负责先做结构拆解与重点判断",
            "负责推进主体执行与细节补充",
            "负责交叉检查与风险提醒",
            "负责收集零散信息与同步状态",
        ]
        capability_hint = "、".join((actor.get("capabilities") or [])[:2]) or actor.get("faction") or "当前专长"
        return f"{priorities[index % len(priorities)]}，并结合 {capability_hint} 完成“{summary[:28]}”这一轮处理。"
