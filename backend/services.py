from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import uuid4

from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from tools.blhx_character_import import resolve_personas

from .constants import APP_META, DEFAULT_CHARACTERS, DEFAULT_SETTINGS, DEFAULT_USER, WALLPAPERS
from .llm_runtime import AgentRuntime
from .credentials import protect, reveal, public_settings
from .memory import MemoryStore
from .models import (
    Actor,
    ActorSkill,
    AgentRelationship,
    ApprovalRequest,
    Conversation,
    ConversationMember,
    MemoryChunk,
    Message,
    SkillRun,
    SkillProposal,
    SocialComment,
    SocialPost,
    ToolExecutionLog,
    Workflow,
    WorkflowAssignment,
    WorkflowStage,
    WorkspaceSetting,
)
from .skill_runtime import SkillRuntime
from .social_runtime import SocialCandidate, SocialRuntime
from .tool_gateway import EXTERNAL_TOOL_SPECS, LOCAL_TOOL_SPECS, READ_ONLY_TOOLS, ToolGateway, WRITE_TOOLS
from .workflow_engine import WorkflowEngine


def now_utc() -> datetime:
    return datetime.now(UTC)


def isoformat(value: datetime | None) -> str:
    value = value or now_utc()
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


def parse_iso_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00")).astimezone(UTC)
    except ValueError:
        return None


def slugify(value: str) -> str:
    chunks = []
    for char in str(value or "").strip().lower():
        if char.isalnum() or "\u4e00" <= char <= "\u9fff":
            chunks.append(char)
        else:
            chunks.append("-")
    slug = "".join(chunks)
    while "--" in slug:
        slug = slug.replace("--", "-")
    return slug.strip("-") or "actor"


def preview_text(value: str, limit: int = 40) -> str:
    normalized = " ".join(str(value or "").replace("\n", " ").split())
    return normalized if len(normalized) <= limit else f"{normalized[:limit]}..."


def json_safe(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [json_safe(item) for item in value]
    return value


EXECUTION_LLM_WINDOW = 2
EXECUTION_COMPLETION_BATCH = 1
ASSIGNMENT_RETRY_DELAY_SECONDS = 18


def default_ui_session() -> dict[str, Any]:
    return {
        "view": "chat",
        "activeConversationId": None,
        "activePostId": None,
        "composerMode": "chat",
        "filters": {
            "reply": "all",
            "type": "all",
            "faction": "all",
            "capability": "all",
            "favorite": "all",
        },
    }


EXTRA_SETTINGS_KEYS = (
    "visionEnabled",
    "searchApiKey",
    "mapApiKey",
    "recommendationApiKey",
    "weatherApiKey",
)


@dataclass
class ServiceBundle:
    runtime: AgentRuntime
    memory: MemoryStore
    tools: ToolGateway
    social: SocialRuntime
    workflow: WorkflowEngine
    skills: SkillRuntime
    runtime_context: dict[str, Any]


class AzurJuusService:
    def __init__(self, bundle: ServiceBundle):
        self.bundle = bundle

    def ensure_seed(self, session: Session) -> None:
        workspace = self.get_workspace(session)
        self.get_or_create_user(session)
        has_agent = session.scalar(select(Actor).where(Actor.kind == "agent", Actor.is_active.is_(True)).limit(1))
        if has_agent is None:
            personas, missing = resolve_personas(DEFAULT_CHARACTERS)
            if missing:
                raise RuntimeError(f"Missing default personas: {', '.join(missing)}")
            self.apply_personas(session, personas, DEFAULT_CHARACTERS, len(DEFAULT_CHARACTERS))
        else:
            for agent in session.scalars(select(Actor).where(Actor.kind == "agent", Actor.is_active.is_(True))).all():
                self._ensure_actor_signature_skill(session, agent)
        if not workspace.secretary_agent_id:
            first_agent = session.scalar(
                select(Actor).where(Actor.kind == "agent", Actor.is_active.is_(True)).order_by(Actor.name.asc())
            )
            if first_agent is not None:
                workspace.secretary_agent_id = first_agent.id
                session.add(workspace)
                session.flush()

        chunks = session.scalars(select(MemoryChunk).order_by(MemoryChunk.created_at.asc())).all()
        self.bundle.memory.clear()
        for chunk in chunks:
            self.bundle.memory.add(
                chunk.id,
                chunk.summary,
                chunk.metadata_json or {},
            )

    def get_workspace(self, session: Session) -> WorkspaceSetting:
        workspace = session.get(WorkspaceSetting, 1)
        if workspace is not None:
            return workspace
        workspace = WorkspaceSetting(
            id=1,
            resolution_preset=DEFAULT_SETTINGS["resolutionPreset"],
            max_connected_agents=DEFAULT_SETTINGS["maxConnectedAgents"],
            connected_agent_ids=list(DEFAULT_SETTINGS["connectedAgentIds"]),
            character_roster_text=DEFAULT_SETTINGS["characterRosterText"],
            llm_provider=DEFAULT_SETTINGS["llmProvider"],
            llm_model=DEFAULT_SETTINGS["llmModel"],
            llm_base_url=DEFAULT_SETTINGS["llmBaseUrl"],
            llm_api_key=DEFAULT_SETTINGS["llmApiKey"],
            tool_profile=DEFAULT_SETTINGS["toolProfile"],
            tool_base_url=DEFAULT_SETTINGS["toolBaseUrl"],
            tool_api_key=DEFAULT_SETTINGS["toolApiKey"],
            authorized_workspace_root=DEFAULT_SETTINGS["authorizedWorkspaceRoot"],
            secretary_agent_id=DEFAULT_SETTINGS["secretaryAgentId"],
            allow_idle_social=DEFAULT_SETTINGS["allowIdleSocial"],
            social_interval_minutes=DEFAULT_SETTINGS["socialIntervalMinutes"],
            ui_session_json=default_ui_session(),
        )
        session.add(workspace)
        session.flush()
        return workspace

    def get_or_create_user(self, session: Session) -> Actor:
        actor = session.get(Actor, DEFAULT_USER["id"])
        if actor is not None:
            return actor
        actor = Actor(
            id=DEFAULT_USER["id"],
            kind="human",
            source_character=None,
            name=DEFAULT_USER["name"],
            handle=DEFAULT_USER["handle"],
            faction=DEFAULT_USER["faction"],
            status=DEFAULT_USER["status"],
            initials=DEFAULT_USER["initials"],
            palette=list(DEFAULT_USER["palette"]),
            accent=DEFAULT_USER["accent"],
            capabilities=list(DEFAULT_USER["capabilities"]),
            tools=list(DEFAULT_USER["tools"]),
            favorite=False,
            is_active=True,
        )
        session.add(actor)
        session.flush()
        return actor









    def list_active_agents(self, session: Session, workspace: WorkspaceSetting | None = None) -> list[Actor]:
        workspace = workspace or self.get_workspace(session)
        active_agent_ids = list(workspace.connected_agent_ids or [])
        if not active_agent_ids:
            seeded_agents = session.scalars(
                select(Actor).where(Actor.kind == "agent", Actor.is_active.is_(True)).order_by(Actor.name.asc())
            ).all()

            active_agent_ids = [agent.id for agent in seeded_agents[: workspace.max_connected_agents]]
        if not active_agent_ids:
            return []
        return session.scalars(select(Actor).where(Actor.id.in_(active_agent_ids)).order_by(Actor.name.asc())).all()

    def _workspace_settings_extras(self, workspace: WorkspaceSetting) -> dict[str, Any]:
        ui_payload = dict(workspace.ui_session_json or {})
        extras = ui_payload.get("settingsExtras")
        return dict(extras) if isinstance(extras, dict) else {}

    def _build_workspace_ui_session(
        self,
        workspace: WorkspaceSetting,
        ui_session: dict[str, Any] | None = None,
        settings_extras: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        existing_payload = dict(workspace.ui_session_json or {})
        base_keys = set(default_ui_session().keys())
        preserved_extras = {key: value for key, value in existing_payload.items() if key not in base_keys}
        next_payload = {
            **default_ui_session(),
            **{key: existing_payload.get(key) for key in base_keys if key in existing_payload},
        }
        if isinstance(ui_session, dict):
            next_payload.update(ui_session)
        next_payload.update(preserved_extras)
        if settings_extras:
            next_payload["settingsExtras"] = dict(settings_extras)
        elif "settingsExtras" in next_payload and not next_payload["settingsExtras"]:
            next_payload.pop("settingsExtras", None)
        return next_payload

    def serialize_settings(self, workspace: WorkspaceSetting) -> dict[str, Any]:
        settings = {
            "resolutionPreset": workspace.resolution_preset,
            "maxConnectedAgents": workspace.max_connected_agents,
            "connectedAgentIds": list(workspace.connected_agent_ids or []),
            "characterRosterText": workspace.character_roster_text,
            "llmProvider": workspace.llm_provider,
            "llmModel": workspace.llm_model,
            "llmBaseUrl": workspace.llm_base_url,
            "llmApiKey": reveal(workspace.llm_api_key),
            "toolProfile": workspace.tool_profile,
            "toolBaseUrl": workspace.tool_base_url,
            "toolApiKey": reveal(workspace.tool_api_key),
            "authorizedWorkspaceRoot": workspace.authorized_workspace_root,
            "secretaryAgentId": workspace.secretary_agent_id,
            "allowIdleSocial": workspace.allow_idle_social,
            "socialIntervalMinutes": workspace.social_interval_minutes,
        }
        settings.update(self._workspace_settings_extras(workspace))
        for key in EXTRA_SETTINGS_KEYS:
            if key.lower().endswith("apikey") and settings.get(key):
                settings[key] = reveal(settings[key])
        settings["visionEnabled"] = str(settings.get("visionEnabled", "")).lower() in {"true", "1"}
        return settings

    def serialize_session(self, session: Session, actor_id: str | None = None) -> dict[str, Any]:
        actor = session.get(Actor, actor_id or DEFAULT_USER["id"])
        if actor is None:
            actor = self.get_or_create_user(session)
        return {
            "actorId": actor.id,
            "actorName": actor.name,
            "actorHandle": actor.handle,
            "actorKind": actor.kind,
            "authMode": "local_cookie",
            "isLocalSession": True,
            "canApproveAsUser": actor.kind == "human",
            "canApproveAsSecretary": False,
            "approvalMode": "server_managed_secretary",
        }

    def build_workspace_payload(self, session: Session, actor_id: str | None = None) -> dict[str, Any]:
        self.ensure_seed(session)
        workspace = self.get_workspace(session)
        return {
            "data": self.build_snapshot(session),
            "settings": public_settings(self.serialize_settings(self.get_workspace(session))),
            "uiSession": {k: v for k, v in (workspace.ui_session_json or default_ui_session()).items() if k != "settingsExtras"},
            "session": self.serialize_session(session, actor_id),
        }

    def save_workspace_payload(
        self,
        session: Session,
        workspace_payload: dict[str, Any],
        actor_id: str | None = None,
    ) -> dict[str, Any]:
        workspace = self.get_workspace(session)
        settings_extras = self._workspace_settings_extras(workspace)
        settings = workspace_payload.get("settings") if isinstance(workspace_payload, dict) else None
        if isinstance(settings, dict):
            workspace.resolution_preset = str(settings.get("resolutionPreset") or workspace.resolution_preset)
            workspace.max_connected_agents = int(settings.get("maxConnectedAgents") or workspace.max_connected_agents)
            workspace.connected_agent_ids = [str(item) for item in settings.get("connectedAgentIds") or workspace.connected_agent_ids]
            workspace.character_roster_text = str(settings.get("characterRosterText") or workspace.character_roster_text)
            workspace.llm_provider = str(settings.get("llmProvider") or workspace.llm_provider)
            workspace.llm_model = str(settings.get("llmModel") or workspace.llm_model)
            workspace.llm_base_url = str(settings.get("llmBaseUrl") or workspace.llm_base_url)
            if settings.get("llmApiKey"):
                workspace.llm_api_key = protect(str(settings["llmApiKey"]))
            if settings.get("clearLlmApiKey"):
                workspace.llm_api_key = ""
            workspace.tool_profile = str(settings.get("toolProfile") or workspace.tool_profile)
            workspace.tool_base_url = str(settings.get("toolBaseUrl") or workspace.tool_base_url)
            if settings.get("toolApiKey"):
                workspace.tool_api_key = protect(str(settings["toolApiKey"]))
            workspace.authorized_workspace_root = str(settings.get("authorizedWorkspaceRoot") or workspace.authorized_workspace_root)
            workspace.secretary_agent_id = str(settings.get("secretaryAgentId") or workspace.secretary_agent_id)
            workspace.allow_idle_social = bool(settings.get("allowIdleSocial", workspace.allow_idle_social))
            workspace.social_interval_minutes = int(settings.get("socialIntervalMinutes") or workspace.social_interval_minutes)
            for key in EXTRA_SETTINGS_KEYS:
                if key in settings:
                    value = str(settings.get(key) or "")
                    if key.lower().endswith("apikey"):
                        if value:
                            settings_extras[key] = protect(value)
                    else:
                        settings_extras[key] = value
        ui_session = workspace_payload.get("uiSession") if isinstance(workspace_payload, dict) else None
        workspace.ui_session_json = self._build_workspace_ui_session(
            workspace,
            ui_session=ui_session if isinstance(ui_session, dict) else None,
            settings_extras=settings_extras,
        )
        session.add(workspace)
        session.flush()
        return self.build_workspace_payload(session, actor_id=actor_id)

    def build_snapshot(self, session: Session) -> dict[str, Any]:
        workspace = self.get_workspace(session)
        user = self.get_or_create_user(session)
        active_agent_ids = list(workspace.connected_agent_ids or [])
        if not active_agent_ids:
            active_agents = session.scalars(
                select(Actor).where(Actor.kind == "agent", Actor.is_active.is_(True)).order_by(Actor.name.asc())
            ).all()
            active_agent_ids = [agent.id for agent in active_agents[: workspace.max_connected_agents]]
        agents = (
            session.scalars(select(Actor).where(Actor.id.in_(active_agent_ids)).order_by(Actor.name.asc())).all()
            if active_agent_ids
            else []
        )
        allowed_ids = {user.id, *active_agent_ids}

        conversations = []
        for conversation in session.scalars(select(Conversation).order_by(Conversation.updated_at.desc())).all():
            members = session.scalars(
                select(ConversationMember).where(ConversationMember.conversation_id == conversation.id)
            ).all()
            member_ids = [member.actor_id for member in members if member.is_active]
            if not member_ids or any(member_id not in allowed_ids for member_id in member_ids):
                continue
            conversations.append((conversation, members))

        posts = (
            session.scalars(
                select(SocialPost).where(SocialPost.author_id.in_(active_agent_ids)).order_by(SocialPost.created_at.desc())
            ).all()
            if active_agent_ids
            else []
        )
        skill_rows = (
            session.scalars(select(ActorSkill).where(ActorSkill.actor_id.in_(active_agent_ids)).order_by(ActorSkill.updated_at.desc())).all()
            if active_agent_ids
            else []
        )
        skill_map: dict[str, list[ActorSkill]] = {}
        for row in skill_rows:
            skill_map.setdefault(row.actor_id, []).append(row)
        workflows = session.scalars(select(Workflow).order_by(Workflow.updated_at.desc())).all()
        approvals = session.scalars(select(ApprovalRequest).order_by(ApprovalRequest.created_at.desc())).all()
        tool_logs = session.scalars(select(ToolExecutionLog).order_by(ToolExecutionLog.created_at.desc())).all()
        skill_runs = session.scalars(select(SkillRun).order_by(SkillRun.created_at.desc())).all()
        skill_proposals = session.scalars(select(SkillProposal).order_by(SkillProposal.created_at.desc())).all()

        return {
            "meta": {
                **APP_META,
                "connection": "Local FastAPI Runtime",
                "runtimeHint": f"PostgreSQL/Redis-ready 路 {len(active_agent_ids)} agents online",
            },
            "user": self.serialize_actor(user),
            "wallpapers": WALLPAPERS,
            "agents": [self.serialize_actor(agent, skill_map.get(agent.id, [])) for agent in agents],
            "conversations": [self.serialize_conversation(conversation, members) for conversation, members in conversations],
            "messages": {
                conversation.id: [
                    self.serialize_message(message)
                    for message in session.scalars(
                        select(Message).where(Message.conversation_id == conversation.id).order_by(Message.created_at.asc())
                    ).all()
                ]
                for conversation, _members in conversations
            },
            "posts": [self.serialize_post(session, post) for post in posts],
            "workflows": [self.serialize_workflow(session, workflow) for workflow in workflows],
            "approvals": [self.serialize_approval(approval) for approval in approvals],
            "toolExecutions": [self.serialize_tool_execution(execution) for execution in tool_logs],
            "skillCatalog": self.effective_skill_catalog(session),
            "skillRuns": [self.serialize_skill_run(skill_run) for skill_run in skill_runs],
            "skillProposals": [self.serialize_skill_proposal(skill_proposal) for skill_proposal in skill_proposals],
            "roster": {
                "generatedAt": isoformat(now_utc()),
                "names": [agent.source_character or agent.name for agent in agents],
                "prompts": {agent.source_character or agent.name: agent.system_prompt or "" for agent in agents},
            },
        }

    def serialize_actor(self, actor: Actor, actor_skills: list[ActorSkill] | None = None) -> dict[str, Any]:
        visible_skills = self.bundle.skills.serialize_actor_skills(actor_skills or [])
        return {
            "id": actor.id,
            "sourceCharacter": actor.source_character,
            "name": actor.name,
            "handle": actor.handle,
            "englishName": actor.english_name,
            "faction": actor.faction,
            "status": actor.status,
            "initials": actor.initials,
            "palette": list(actor.palette or []),
            "accent": actor.accent,
            "tone": actor.tone,
            "persona": actor.persona,
            "summary": actor.summary or "",
            "keywords": actor.keywords or "",
            "capabilities": list(actor.capabilities or []),
            "tools": list(actor.tools or []),
            "favorite": bool(actor.favorite),
            "avatarUrl": actor.avatar_url,
            "illustrationUrl": actor.illustration_url,
            "systemPrompt": actor.system_prompt,
            "characterUrl": actor.character_url,
            "skills": visible_skills,
            "juusAccount": {
                "handle": actor.handle,
                "englishName": actor.english_name,
                "keywords": actor.keywords or "",
            },
        }

    def serialize_conversation(self, conversation: Conversation, members: list[ConversationMember]) -> dict[str, Any]:
        roles = {member.actor_id: member.role for member in members if member.is_active}
        member_ids = [member.actor_id for member in members if member.is_active]
        extra = conversation.extra_json or {}
        return {
            "id": conversation.id,
            "kind": conversation.kind,
            "title": conversation.title,
            "faction": conversation.faction,
            "memberIds": member_ids,
            "preview": conversation.preview,
            "unreadCount": conversation.unread_count,
            "replied": conversation.replied,
            "backgroundId": conversation.background_id,
            "bookmarked": conversation.bookmarked,
            "announcement": conversation.announcement,
            "workflowId": conversation.workflow_id,
            "archived": bool(extra.get("archived")),
            "taskPurpose": extra.get("taskPurpose") or "",
            "channelState": extra.get("channelState") or ("workflow" if conversation.workflow_id else "chat"),
            "roles": roles,
            "updatedAt": isoformat(conversation.updated_at),
        }

    def serialize_message(self, message: Message) -> dict[str, Any]:
        return {
            "id": message.id,
            "speakerId": message.speaker_id,
            "type": message.type,
            "body": message.body,
            "createdAt": isoformat(message.created_at),
            "metadata": message.metadata_json or {},
        }

    def serialize_post(self, session: Session, post: SocialPost) -> dict[str, Any]:
        comments = session.scalars(
            select(SocialComment).where(SocialComment.post_id == post.id).order_by(SocialComment.created_at.asc())
        ).all()
        return {
            "id": post.id,
            "authorId": post.author_id,
            "excerpt": post.excerpt,
            "likes": post.likes,
            "likedByUser": post.liked_by_user,
            "createdAt": isoformat(post.created_at),
            "artLabel": post.art_label,
            "artMark": post.art_mark,
            "artPalette": list(post.art_palette or []),
            "mediaUrl": post.media_url,
            "following": post.following,
            "comments": [self.serialize_comment(comment) for comment in comments],
        }

    def serialize_comment(self, comment: SocialComment) -> dict[str, Any]:
        return {
            "id": comment.id,
            "authorId": comment.author_id,
            "body": comment.body,
            "createdAt": isoformat(comment.created_at),
        }

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
                    select(Message).where(Message.conversation_id == workflow.conversation_id).order_by(Message.created_at.asc())
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
            "graph": self.bundle.workflow.build_graph_view(
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

    def _reviewer_role_for_approval(
        self,
        session: Session,
        approval: ApprovalRequest,
        reviewer_actor_id: str | None,
    ) -> str | None:
        reviewer_id = reviewer_actor_id or approval.reviewer_actor_id
        if not reviewer_id:
            return None
        if reviewer_id == DEFAULT_USER["id"]:
            return "user"
        workflow = session.get(Workflow, approval.workflow_id) if approval.workflow_id else None
        if workflow is not None and workflow.secretary_id and reviewer_id == workflow.secretary_id:
            return "secretary"
        if approval.requires_secretary:
            workspace = self.get_workspace(session)
            if workspace.secretary_agent_id and reviewer_id == workspace.secretary_agent_id:
                return "secretary"
        return None

    def _expected_reviewer_actor_id(
        self,
        session: Session,
        approval: ApprovalRequest,
        reviewer_role: str,
    ) -> str | None:
        if reviewer_role == "user":
            return DEFAULT_USER["id"]
        if reviewer_role != "secretary":
            return None
        workflow = session.get(Workflow, approval.workflow_id) if approval.workflow_id else None
        if workflow is not None and workflow.secretary_id:
            return workflow.secretary_id
        workspace = self.get_workspace(session)
        if approval.requires_secretary and workspace.secretary_agent_id:
            return workspace.secretary_agent_id
        return approval.reviewer_actor_id

    def _validate_approval_reviewer(
        self,
        session: Session,
        approval: ApprovalRequest,
        reviewer_actor_id: str | None,
    ) -> tuple[str, str]:
        reviewer_id = reviewer_actor_id or approval.reviewer_actor_id
        if not reviewer_id:
            raise PermissionError("Reviewer actor is required.")
            
        reviewer_role = self._reviewer_role_for_approval(session, approval, reviewer_id)
        if reviewer_role is None:
            raise PermissionError(f"Reviewer {reviewer_id} is not authorized for this approval.")

        progress = self._approval_progress(approval)
        pending_roles = progress["pendingReviewers"]
        
        # 允许逻辑：如果当前角色在待审批列表中，或者（用户审批且该审批确实需要用户参与）
        is_pending = reviewer_role in pending_roles
        can_intervene = (reviewer_role == "user" and approval.requires_user and not progress.get("approvedByUser"))
        
        if pending_roles and not is_pending and not can_intervene:
            expected = " / ".join("秘书" if item == "secretary" else "用户" for item in pending_roles)
            raise PermissionError(f"此审批当前正在等待: {expected}。")

        expected_actor_id = self._expected_reviewer_actor_id(session, approval, reviewer_role)
        if expected_actor_id and reviewer_id != expected_actor_id:
            raise PermissionError("Reviewer does not match the current approval assignee.")

        return reviewer_id, reviewer_role

    def _auto_approve_secretary_tool_review(
        self,
        session: Session,
        *,
        approval: ApprovalRequest | None,
        reviewer_actor_id: str | None,
    ) -> None:
        if approval is None or approval.status != "pending" or not approval.requires_secretary:
            return
        payload = approval.payload_json or {}
        if payload.get("approvedBySecretary"):
            return
        self._resolve_tool_execution_approval(
            session,
            approval=approval,
            decision="approve",
            reviewer_actor_id=reviewer_actor_id,
        )

    async def _auto_approve_stage_review_request(
        self,
        session: Session,
        *,
        workflow: Workflow,
        approval: ApprovalRequest | None,
    ) -> None:
        if approval is None or approval.status != "pending":
            return
        reviewer_actor_id = approval.reviewer_actor_id or workflow.secretary_id or workflow.owner_id
        approval.status = "approved"
        approval.resolved_at = now_utc()
        approval.reviewer_actor_id = reviewer_actor_id
        session.add(approval)
        await self._resolve_workflow_stage_review(
            session,
            workflow=workflow,
            approval=approval,
            decision="approve",
            reviewer_actor_id=reviewer_actor_id,
        )

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

    def _matching_tool_execution(
        self,
        execution: ToolExecutionLog,
        *,
        actor_id: str,
        tool_name: str,
        safe_args: dict[str, Any],
        workflow_id: str | None = None,
        assignment_id: str | None = None,
    ) -> bool:
        if execution.actor_id != actor_id or execution.tool_name != tool_name:
            return False
        if execution.status not in {"pending_approval", "approved", "completed"}:
            return False
        request = execution.request_json or {}
        if workflow_id and request.get("workflowId") != workflow_id:
            return False
        if assignment_id and request.get("assignmentId") != assignment_id:
            return False
        for key, value in safe_args.items():
            if request.get(key) != value:
                return False
        return True

    def _find_existing_tool_execution(
        self,
        session: Session,
        *,
        actor_id: str,
        tool_name: str,
        safe_args: dict[str, Any],
        workflow_id: str | None = None,
        assignment_id: str | None = None,
    ) -> tuple[ToolExecutionLog | None, ApprovalRequest | None]:
        candidates = session.scalars(
            select(ToolExecutionLog)
            .where(
                ToolExecutionLog.actor_id == actor_id,
                ToolExecutionLog.tool_name == tool_name,
            )
            .order_by(ToolExecutionLog.created_at.desc())
        ).all()
        for execution in candidates:
            if not self._matching_tool_execution(
                execution,
                actor_id=actor_id,
                tool_name=tool_name,
                safe_args=safe_args,
                workflow_id=workflow_id,
                assignment_id=assignment_id,
            ):
                continue
            approval = session.scalar(
                select(ApprovalRequest)
                .where(ApprovalRequest.tool_execution_id == execution.id)
                .order_by(ApprovalRequest.created_at.desc())
            )
            return execution, approval
        return None, None

    def _tool_plan_payload(
        self,
        session: Session,
        *,
        execution: ToolExecutionLog,
        approval: ApprovalRequest | None,
        plan,
    ) -> dict[str, Any]:
        approval_progress = self._approval_progress(approval) if approval is not None else {
            "approvedByUser": True,
            "approvedBySecretary": True,
            "pendingReviewers": [],
            "decisionTrail": [],
        }
        return {
            "status": execution.status,
            "executionId": execution.id,
            "approvalId": approval.id if approval else None,
            "plan": {
                "tool": plan.tool_name,
                "riskLevel": plan.risk_level,
                "requiresSecretaryApproval": bool(plan.requires_secretary_approval),
                "requiresUserApproval": bool(plan.requires_user_approval),
                "supportsUndo": plan.supports_undo,
                "args": json_safe(plan.normalized_args),
            },
            "snapshot": self.build_snapshot(session),
            "approvedByUser": approval_progress["approvedByUser"],
            "approvedBySecretary": approval_progress["approvedBySecretary"],
            "pendingReviewers": approval_progress["pendingReviewers"],
        }

    def _assignment_open_tool_executions(
        self,
        session: Session,
        *,
        workflow_id: str,
        assignment_id: str,
    ) -> list[ToolExecutionLog]:
        executions = session.scalars(select(ToolExecutionLog).order_by(ToolExecutionLog.created_at.asc())).all()
        return [
            execution
            for execution in executions
            if (execution.request_json or {}).get("workflowId") == workflow_id
            and (execution.request_json or {}).get("assignmentId") == assignment_id
            and execution.status in {"pending_approval", "approved"}
        ]

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

    def inspect_workflow(self, session: Session, workflow_id: str) -> dict[str, Any]:
        workflow = session.get(Workflow, workflow_id)
        if workflow is None:
            raise ValueError("Workflow not found.")
        return self.serialize_workflow(session, workflow)

    def inspect_actor_skills(self, session: Session, actor_id: str) -> dict[str, Any]:
        actor = session.get(Actor, actor_id)
        if actor is None:
            raise ValueError("Actor not found.")
        actor_skills = self._actor_skill_rows(session, actor.id)
        public_skills = self.effective_skill_catalog(session)
        return {
            "actor": self.serialize_actor(actor, actor_skills),
            "publicSkills": public_skills,
            "skills": self.bundle.skills.serialize_actor_skills(actor_skills),
        }

    def preview_skill_selection(
        self,
        session: Session,
        *,
        actor_id: str,
        mode: str,
        prompt: str,
        conversation_kind: str,
        workflow_role: str | None = None,
        source_kind: str = "message",
    ) -> dict[str, Any]:
        actor = session.get(Actor, actor_id)
        if actor is None:
            raise ValueError("Actor not found.")
        actor_skills = self._actor_skill_rows(session, actor.id)
        approved_public_proposals = self._approved_public_skill_proposals(session)
        selection = self.bundle.skills.choose_skills(
            actor=self.serialize_actor(actor, actor_skills),
            actor_skill_rows=actor_skills,
            mode=mode,
            prompt=prompt,
            conversation_kind=conversation_kind,
            workflow_role=workflow_role,
            source_kind=source_kind,
            approved_public_proposals=approved_public_proposals,
        )
        return {
            "actorId": actor.id,
            "actorName": actor.name,
            "mode": mode,
            "conversationKind": conversation_kind,
            "sourceKind": source_kind,
            "primarySkillId": selection.primary_skill_id,
            "primarySkillName": selection.primary_skill_name,
            "selectedSkills": selection.selected_skills,
            "promptPatch": selection.prompt_patch,
            "toolAllowlist": selection.tool_allowlist,
        }

    def effective_skill_catalog(self, session: Session) -> list[dict[str, Any]]:
        proposals = self._approved_public_skill_proposals(session)
        return self.bundle.skills.registry.serialize_catalog(proposals)

    def inspect_tool_catalog(self, session: Session) -> dict[str, Any]:
        workspace = self.get_workspace(session)
        settings = self.serialize_settings(workspace)
        active_agents = self.list_active_agents(session, workspace)
        actor_rows = {
            actor.id: self._actor_skill_rows(session, actor.id)
            for actor in active_agents
        }
        local_tools = self.bundle.tools.local_catalog()
        external_tools = self.bundle.tools.external_catalog(settings)
        actor_access = []
        for actor in active_agents:
            skill_selection = self.bundle.skills.choose_skills(
                actor=self.serialize_actor(actor, actor_rows.get(actor.id, [])),
                actor_skill_rows=actor_rows.get(actor.id, []),
                mode="task",
                prompt="系统检查当前工具配置",
                conversation_kind="dm",
                source_kind="inspection",
                approved_public_proposals=self._approved_public_skill_proposals(session),
            )
            actor_access.append(
                {
                    "actorId": actor.id,
                    "actorName": actor.name,
                    "declaredTools": list(actor.tools or []),
                    "skillSuggestedTools": list(skill_selection.tool_allowlist),
                    "autoCallableReadOnlyTools": self._read_only_tool_allowlist(skill_selection.tool_allowlist),
                    "manualPlanRequiredTools": self._actor_manual_tool_allowlist(actor, actor_rows.get(actor.id, [])),
                }
            )
        return {
            "authorizedWorkspaceRoot": workspace.authorized_workspace_root,
            "effectiveDefaultWorkspaceRoot": str(self.bundle.tools._default_workspace_root),
            "toolProfile": workspace.tool_profile,
            "localTools": local_tools,
            "externalTools": external_tools,
            "actorAccess": actor_access,
        }

    def inspect_system(self, session: Session) -> dict[str, Any]:
        workspace = self.get_workspace(session)
        active_agents = self.list_active_agents(session, workspace)
        return {
            "settings": public_settings(self.serialize_settings(workspace)),
            "authorizedWorkspaceRoot": workspace.authorized_workspace_root,
            "effectiveDefaultWorkspaceRoot": str(self.bundle.tools._default_workspace_root),
            "toolCatalog": self.inspect_tool_catalog(session),
            "skillCatalog": self.effective_skill_catalog(session),
            "actors": [
                {
                    "id": actor.id,
                    "name": actor.name,
                    "faction": actor.faction,
                    "capabilities": list(actor.capabilities or []),
                    "declaredTools": list(actor.tools or []),
                }
                for actor in active_agents
            ],
            "notes": {
                "skillVsTool": "Skill 决定方法与提示词补丁，Tool 是具体能力调用。自动 LLM 工具调用只开放低风险只读工具；写操作必须走计划与审批。",
                "authorizedWorkspace": "设置中的授权工作区用于工具路径校验；若自动工具调用没有显式 root，则会回退到该设置，再回退到后端默认工作区。",
            },
        }

    def _manual_tool_allowlist_for_actor(
        self,
        session: Session,
        *,
        actor_id: str,
        workflow: Workflow | None = None,
    ) -> list[str]:
        if actor_id == DEFAULT_USER["id"]:
            return list(LOCAL_TOOL_SPECS.keys())
        actor = session.get(Actor, actor_id)
        if actor is None:
            return []
        return self._actor_manual_tool_allowlist(actor, self._actor_skill_rows(session, actor.id))

    def _read_only_tool_allowlist(self, tool_names: list[str] | tuple[str, ...]) -> list[str]:
        return [
            name
            for name in dict.fromkeys(tool_names or [])
            if name in READ_ONLY_TOOLS
        ]

    def _actor_manual_tool_allowlist(self, actor: Actor, actor_skill_rows: list[ActorSkill]) -> list[str]:
        allowed: list[str] = []
        for name in actor.tools or []:
            if name in LOCAL_TOOL_SPECS:
                allowed.append(name)
        for row in actor_skill_rows:
            if row.visibility == "public":
                continue
            for name in row.tool_allowlist_json or []:
                if name in LOCAL_TOOL_SPECS:
                    allowed.append(name)
        return list(dict.fromkeys(allowed))

    def list_skill_proposals(
        self,
        session: Session,
        *,
        actor_id: str | None = None,
        status: str | None = None,
    ) -> list[dict[str, Any]]:
        proposals = self._skill_proposal_rows(session, actor_id=actor_id, status=status)
        return [self.serialize_skill_proposal(item) for item in proposals]

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

    def reload_skill_catalog(self, session: Session | None = None) -> list[dict[str, Any]]:
        if session is None:
            return self.bundle.skills.registry.serialize_catalog()
        return self.effective_skill_catalog(session)

    def preview_personas(self, session: Session, names: list[str], participant_count: int) -> dict[str, Any]:
        if participant_count < 5 or participant_count > 10:
            raise ValueError("Participant count must be between 5 and 10.")
        cleaned = [str(item).strip() for item in names if str(item).strip()]
        if len(cleaned) != participant_count:
            raise ValueError(f"Expected {participant_count} character names, but received {len(cleaned)}.")
        personas, missing = resolve_personas(cleaned)
        if missing:
            raise FileNotFoundError(f"Missing character personas: {', '.join(missing)}")
        return {"personas": personas, "names": cleaned, "participantCount": participant_count}

    def compose_personas(self, session: Session, names: list[str], participant_count: int) -> dict[str, Any]:
        preview = self.preview_personas(session, names, participant_count)
        personas = preview["personas"]
        cleaned = preview["names"]
        snapshot = self.apply_personas(session, personas, cleaned, participant_count)
        return {"snapshot": snapshot, "personas": personas}

    def apply_persona_preview(self, session: Session, personas: list[dict[str, Any]], names: list[str], participant_count: int) -> dict[str, Any]:
        cleaned = [str(item).strip() for item in names if str(item).strip()]
        if participant_count < 5 or participant_count > 10:
            raise ValueError("Participant count must be between 5 and 10.")
        if len(cleaned) != participant_count:
            raise ValueError(f"Expected {participant_count} character names, but received {len(cleaned)}.")
        if len(personas) != participant_count:
            raise ValueError("Persona draft count does not match participant count.")
        snapshot = self.apply_personas(session, personas, cleaned, participant_count)
        return {"snapshot": snapshot, "personas": personas}

    def apply_personas(self, session: Session, personas: list[dict[str, Any]], names: list[str], participant_count: int) -> dict[str, Any]:
        workspace = self.get_workspace(session)
        user = self.get_or_create_user(session)
        existing_agents = session.scalars(select(Actor).where(Actor.kind == "agent")).all()
        by_source = {agent.source_character or agent.name: agent for agent in existing_agents}
        active_ids: list[str] = []
        new_agents: list[Actor] = []

        for spec in personas:
            source_name = spec.get("sourceName") or spec.get("displayName")
            agent = by_source.get(source_name)
            is_new = agent is None
            if agent is None:
                agent = Actor(id=f"juus-{slugify(source_name)}", kind="agent", handle=spec.get("handle") or f"@{slugify(source_name)}.juus")
            agent.source_character = source_name
            agent.name = spec.get("displayName") or source_name
            agent.handle = spec.get("handle") or agent.handle
            agent.english_name = spec.get("englishName")
            agent.faction = spec.get("faction") or "未知阵营"
            agent.status = agent.status or "在线"
            agent.initials = (spec.get("displayName") or source_name)[:2]
            agent.palette = list(spec.get("palette") or agent.palette or ["#dce8f5", "#f7fbff"])
            agent.accent = spec.get("accent") or agent.accent or "#84caef"
            agent.tone = spec.get("tone") or agent.tone
            agent.persona = spec.get("persona") or agent.persona
            agent.summary = spec.get("summary") or agent.summary
            agent.keywords = spec.get("keywords") or agent.keywords
            agent.capabilities = list(spec.get("capabilities") or agent.capabilities or [])
            agent.tools = list(spec.get("tools") or agent.tools or [])
            agent.avatar_url = spec.get("avatarUrl") or agent.avatar_url
            agent.illustration_url = spec.get("illustrationUrl") or spec.get("avatarUrl") or agent.illustration_url
            agent.system_prompt = spec.get("promptSeed") or agent.system_prompt
            agent.character_url = spec.get("characterUrl") or agent.character_url
            agent.extra_json = {"aliases": spec.get("aliases") or [], "voiceSamples": spec.get("voiceSamples") or {}}
            agent.is_active = True
            session.add(agent)
            session.flush()
            self._ensure_actor_signature_skill(session, agent)
            active_ids.append(agent.id)
            if is_new:
                new_agents.append(agent)

        for agent in existing_agents:
            if agent.id not in active_ids:
                agent.is_active = False
                session.add(agent)

        workspace.max_connected_agents = participant_count
        workspace.connected_agent_ids = active_ids
        workspace.character_roster_text = "\n".join(names)
        if workspace.secretary_agent_id not in active_ids:
            workspace.secretary_agent_id = active_ids[0] if active_ids else ""
        session.add(workspace)
        session.flush()

        active_agents = (
            session.scalars(select(Actor).where(Actor.id.in_(active_ids)).order_by(Actor.name.asc())).all()
            if active_ids
            else []
        )
        for agent in active_agents:
            self._ensure_dm_conversation(session, user, agent)
        if active_agents:
            self._ensure_port_hub(session, user, active_agents)
        for agent in new_agents:
            greeting = agent.summary or f"{agent.name} 已接入 AzurJuus，之后请多指教。"
            self._ensure_greeting_message(session, user, agent, greeting)
            self._ensure_greeting_post(session, agent)
        self._sync_relationships(session, active_agents)
        session.flush()
        return self.build_snapshot(session)

    def reset_system(self, session: Session, actor_id: str | None = None) -> dict[str, Any]:
        """彻底清空系统数据并恢复种子状态"""
        try:
            # 1. 首先断开所有可能导致循环引用的外键
            session.execute(update(MemoryChunk).values(actor_id=None, workflow_id=None, conversation_id=None))
            session.execute(update(SkillRun).values(workflow_id=None, stage_id=None, assignment_id=None, conversation_id=None, message_id=None))
            session.execute(update(Conversation).values(workflow_id=None))
            session.execute(update(Workflow).values(conversation_id=None))
            session.execute(update(ApprovalRequest).values(workflow_id=None, tool_execution_id=None))
            session.execute(update(WorkflowAssignment).values(stage_id=None))
            session.flush()

            # 2. 按照依赖关系从子表到主表依次删除
            # 顺序说明：先删除最底层的关联记录，最后删除基础实体（Actor/Workspace）
            deletion_order = [
                SkillRun,
                MemoryChunk,
                SkillProposal,
                ApprovalRequest,
                ToolExecutionLog,
                WorkflowAssignment,
                WorkflowStage,
                Message,
                ConversationMember,
                SocialComment,
                SocialPost,
                AgentRelationship,
                ActorSkill,
                Workflow,
                Conversation,
                Actor,
                WorkspaceSetting,
            ]

            from .cognition_models import MindState, Experience, MindReceipt, MindCursor, MindOutbox
            deletion_order = [MindOutbox, MindReceipt, Experience, MindState, MindCursor, *deletion_order]

            for model in deletion_order:
                session.execute(delete(model))
                session.flush()

            # 3. 清理内存缓存与向量库
            session.expunge_all()
            self.bundle.memory.clear()
            
            # 4. 重新刷入初始种子数据
            self.ensure_seed(session)
            session.flush()
            
            print(f"[Services] System reset successful (triggered by {actor_id or 'unknown'})")
            return self.build_workspace_payload(session, actor_id=actor_id)
            
        except Exception as e:
            print(f"[Services] System reset failed: {str(e)}")
            session.rollback()
            raise

    async def run_idle_social_tick(self, session: Session) -> dict[str, Any] | None:
        workspace = self.get_workspace(session)
        if not workspace.allow_idle_social or not self.bundle.social.enabled:
            return None

        agents = self.list_active_agents(session, workspace)
        if not agents:
            return None

        candidates: list[SocialCandidate] = []
        for agent in agents:
            last_post = session.scalar(
                select(SocialPost).where(SocialPost.author_id == agent.id).order_by(SocialPost.created_at.desc())
            )
            last_post_at = last_post.created_at if last_post is not None else None
            if self.bundle.social.should_schedule(
                last_post_at=last_post_at,
                interval_minutes=workspace.social_interval_minutes,
            ):
                candidates.append(SocialCandidate(actor_id=agent.id, last_post_at=last_post_at))

        due_author = self.bundle.social.pick_due_author(candidates)
        if due_author is None:
            return None

        author = next((agent for agent in agents if agent.id == due_author.actor_id), None)
        if author is None:
            return None

        recent_memories = self._recent_memory_summaries(session, actor_id=author.id, limit=4)
        plan = self.bundle.social.build_idle_post_plan(
            actor_id=author.id,
            actor_name=author.name,
            faction=author.faction,
            summary=author.summary or author.persona or "",
            recent_memories=recent_memories,
            interval_minutes=workspace.social_interval_minutes,
        )
        selection = self._select_skill_execution(
            session,
            actor=author,
            mode="social_post",
            prompt=plan.prompt,
            conversation_kind="social",
            source_kind="social_post",
        )
        body = await self.bundle.runtime.generate_social_post(
            settings=self.serialize_settings(self.get_workspace(session)),
            agent=self.serialize_actor(author, self._actor_skill_rows(session, author.id)),
            prompt=plan.prompt,
            memory_snippets=recent_memories,
            skill_context=selection.prompt_patch,
        )
        post = self._create_social_post(
            session,
            author=author,
            body=body,
            media_url=author.illustration_url or author.avatar_url,
        )
        self._record_skill_run(
            session,
            actor=author,
            selection=selection,
            mode="social_post",
            source_kind="social_post",
            input_summary=plan.prompt,
            output_summary=body,
            extra={"postId": post.id},
        )
        comment_count = await self._auto_comment_on_post(
            session,
            post=post,
            author=author,
            agents=agents,
            settings_payload=self.serialize_settings(self.get_workspace(session)),
        )
        session.flush()
        snapshot = self.build_snapshot(session)
        return {
            "authorId": author.id,
            "postId": post.id,
            "commentCount": comment_count,
            "snapshot": snapshot,
        }

    def open_conversation(self, session: Session, conversation_id: str) -> dict[str, Any]:
        conversation = session.get(Conversation, conversation_id)
        if conversation is not None:
            conversation.unread_count = 0
            conversation.replied = True
            session.add(conversation)
            session.flush()
        return self.build_snapshot(session)

    def accept_user_message(self, session: Session, conversation_id: str, content: str, mode: str) -> dict[str, Any]:
        conversation = session.get(Conversation, conversation_id)
        if conversation is None:
            raise ValueError("Conversation not found.")
        trimmed = str(content or "").strip()
        if not trimmed:
            return {
                "snapshot": self.build_snapshot(session),
                "conversationId": conversation.id,
                "workflowId": None,
                "messageId": None,
                "content": trimmed,
                "mode": mode,
            }
        user = self.get_or_create_user(session)
        workflow = self._current_workflow_for_conversation(session, conversation.id)
        user_message = self._append_message(session, conversation, user.id, "task" if mode != "chat" else "text", trimmed)
        if workflow is not None:
            user_message.metadata_json = {
                **(user_message.metadata_json or {}),
                "workflowId": workflow.id,
                "mode": mode,
            }
            session.add(user_message)
        self._record_memory(session, actor_id=user.id, conversation_id=conversation.id, source_kind="message.user", text=trimmed)
        session.flush()
        return {
            "snapshot": self.build_snapshot(session),
            "conversationId": conversation.id,
            "workflowId": workflow.id if workflow else None,
            "messageId": user_message.id,
            "content": trimmed,
            "mode": mode,
        }

    async def send_message(
        self,
        session: Session,
        conversation_id: str,
        content: str,
        mode: str,
        user_message_id: str | None = None,
    ) -> dict[str, Any]:
        conversation = session.get(Conversation, conversation_id)
        if conversation is None:
            raise ValueError("Conversation not found.")
        trimmed = str(content or "").strip()
        if not trimmed:
            return {"snapshot": self.build_snapshot(session), "conversationId": conversation.id, "skillRuns": [], "responderActorIds": []}
        workspace = self.get_workspace(session)
        user = self.get_or_create_user(session)
        workflow = self._current_workflow_for_conversation(session, conversation.id)
        user_message = session.get(Message, user_message_id) if user_message_id else None
        if user_message is None:
            user_message = self._append_message(session, conversation, user.id, "task" if mode != "chat" else "text", trimmed)
            if workflow is not None:
                user_message.metadata_json = {
                    **(user_message.metadata_json or {}),
                    "workflowId": workflow.id,
                    "mode": mode,
                }
                session.add(user_message)
            self._record_memory(session, actor_id=user.id, conversation_id=conversation.id, source_kind="message.user", text=trimmed)
            session.flush()

        member_ids = [
            member.actor_id
            for member in session.scalars(
                select(ConversationMember).where(
                    ConversationMember.conversation_id == conversation.id,
                    ConversationMember.is_active.is_(True),
                )
            ).all()
        ]
        agent_ids = [item for item in member_ids if item != user.id]
        agents = session.scalars(select(Actor).where(Actor.id.in_(agent_ids)).order_by(Actor.name.asc())).all() if agent_ids else []
        recent_messages = session.scalars(
            select(Message).where(Message.conversation_id == conversation.id).order_by(Message.created_at.asc())
        ).all()
        speaker_id_set = {item.speaker_id for item in recent_messages}
        speaker_id_set.add(user.id)
        speaker_names = {
            actor.id: actor.name
            for actor in session.scalars(select(Actor).where(Actor.id.in_(speaker_id_set))).all()
        }
        serialized_recent = [
            {
                "speakerId": item.speaker_id,
                "speakerName": speaker_names.get(item.speaker_id, item.speaker_id),
                "body": item.body,
            }
            for item in recent_messages[-12:]
        ]

        if mode == "task" and workflow is None and agents:
            lead_agent = self._pick_task_lead_agent(agents, workspace.secretary_agent_id)
            route = await self.bundle.runtime.decide_task_route(
                settings=self.serialize_settings(self.get_workspace(session)),
                agent=self.serialize_actor(lead_agent, self._actor_skill_rows(session, lead_agent.id)),
                prompt=trimmed,
                recent_messages=serialized_recent,
                memory_snippets=self._recent_memory_summaries(session, lead_agent.id),
            )
            if route.get("route") == "collaborative":
                secretary = session.get(Actor, workspace.secretary_agent_id) if workspace.secretary_agent_id else None
                if secretary is None:
                    secretary = next((agent for agent in agents if agent.id != lead_agent.id), lead_agent)
                self._append_message(
                    session,
                    conversation,
                    lead_agent.id,
                    "task",
                    f"这项任务我先判断为更适合多人协作。{route.get('reason') or ''}".strip(),
                    metadata={"mode": "task-routing", "route": "collaborative"},
                )
                approval = self._create_workflow_launch_request(
                    session,
                    conversation=conversation,
                    requester_actor=lead_agent,
                    secretary_actor=secretary,
                    body=trimmed,
                    mode=mode,
                    summary=route.get("reason") or "",
                )
                self._append_message(
                    session,
                    conversation,
                    secretary.id,
                    "system",
                    f"{secretary.name} 认为这项任务更适合建立协作频道，是否现在启动多人任务？",
                    metadata={
                        "approvalId": approval.id,
                        "targetKind": "workflow_launch_request",
                        "role": "workflow-launch-request",
                    },
                )
                session.flush()
                return {
                    "snapshot": self.build_snapshot(session),
                    "conversationId": conversation.id,
                    "workflowId": None,
                    "pendingApprovalId": approval.id,
                    "skillRuns": [],
                    "responderActorIds": [lead_agent.id, secretary.id],
                }

            workflow = self._ensure_solo_task_workflow(session, conversation, trimmed, lead_agent.id)
            if user_message is not None:
                user_message.metadata_json = {
                    **(user_message.metadata_json or {}),
                    "workflowId": workflow.id,
                    "mode": mode,
                    "taskRoute": "solo",
                }
                session.add(user_message)

        if workflow is not None and conversation.kind == "group":
            secretary_agent = next((agent for agent in agents if agent.id == workflow.secretary_id), None)
            other_agents = [agent for agent in agents if agent.id != workflow.secretary_id]
            targets = [item for item in [secretary_agent, *other_agents[:1]] if item is not None]
        elif mode == "swarm" or conversation.kind == "group":
            targets = agents[: max(1, min(2, len(agents)))]
        else:
            targets = agents[:1]

        created_skill_runs: list[dict[str, Any]] = []
        responder_actor_ids: list[str] = []
        for index, agent in enumerate(targets):
            memories = self.bundle.memory.query(trimmed, actor_id=agent.id, conversation_id=conversation.id, limit=4)
            assignment = self._assignment_for_agent(session, workflow.id, agent.id) if workflow else None
            selection = self._select_skill_execution(
                session,
                actor=agent,
                mode=mode,
                prompt=trimmed,
                conversation_kind=conversation.kind,
                workflow_role="secretary" if workflow and agent.id == workflow.secretary_id else None,
                source_kind="message",
            )
            reply, used_tool_names = await self._generate_reply_with_tools_v2(
                None,
                session=session, actor_id=agent.id, workflow_id=workflow.id if workflow else None, assignment_id=assignment.id if assignment else None,
                tool_allowlist=selection.tool_allowlist,
                settings=self.serialize_settings(self.get_workspace(session)),
                agent=self.serialize_actor(agent, self._actor_skill_rows(session, agent.id)),
                prompt=trimmed,
                mode=mode,
                conversation_kind=conversation.kind,
                recent_messages=serialized_recent,
                memory_snippets=[item.text for item in memories],
                skill_context=selection.prompt_patch,
            )
            message_type = "task" if mode != "chat" and index == 0 else "text"
            message = self._append_message(session, conversation, agent.id, message_type, reply)
            skill_run = self._record_skill_run(
                session,
                actor=agent,
                selection=selection,
                mode=mode,
                source_kind="message",
                input_summary=trimmed,
                output_summary=reply,
                workflow_id=workflow.id if workflow else None,
                stage_id=self._active_stage_id(session, workflow.id) if workflow else None,
                assignment_id=assignment.id if assignment else None,
                conversation_id=conversation.id,
                message_id=message.id,
                used_tool_names=used_tool_names,
                extra={"conversationKind": conversation.kind},
            )
            message.metadata_json = {
                **(message.metadata_json or {}),
                "skillRunId": skill_run.id,
                "skillId": selection.primary_skill_id,
                "skillLabel": selection.primary_skill_name,
                "skillStack": selection.selected_skills,
                "workflowId": workflow.id if workflow else None,
            }
            session.add(message)
            self._record_memory(session, actor_id=agent.id, conversation_id=conversation.id, source_kind="message.agent", text=reply)
            responder_actor_ids.append(agent.id)
            created_skill_runs.append(self.serialize_skill_run(skill_run))

        if mode in {"task", "swarm"}:
            if workflow is None:
                self._ensure_workflow_stub(session, conversation, trimmed, mode)
            else:
                notes = list(workflow.context_json.get("liveChatNotes") or [])
                notes.append({"body": trimmed, "at": isoformat(now_utc()), "conversationId": conversation.id})
                workflow.context_json = {**workflow.context_json, "liveChatNotes": notes}
                session.add(workflow)
        session.flush()
        return {
            "snapshot": self.build_snapshot(session),
            "conversationId": conversation.id,
            "workflowId": workflow.id if workflow else None,
            "responderActorIds": responder_actor_ids,
            "skillRuns": created_skill_runs,
        }


    async def _generate_reply_with_tools_v2(
        self,
        workspace_root: str | None,
        *,
        tool_allowlist: list[str] | None = None,
        **kwargs,
    ) -> tuple[str, list[str]]:
        loop_count = 0
        prompt = kwargs.get("prompt", "")
        recent_messages = kwargs.get("recent_messages", [])
        current_messages = list(recent_messages)
        final_reply = ""
        settings_payload = kwargs.get("settings") or {}
        effective_workspace_root = workspace_root or settings_payload.get("authorizedWorkspaceRoot") or None
        allowed_tool_names = [
            name
            for name in (tool_allowlist or [])
            if name in READ_ONLY_TOOLS or name in WRITE_TOOLS
        ]
        used_tool_names: list[str] = []

        session = kwargs.get("session")
        actor_id = kwargs.get("actor_id")
        workflow_id = kwargs.get("workflow_id")
        assignment_id = kwargs.get("assignment_id")

        while loop_count < 5:
            loop_count += 1
            kwargs["prompt"] = prompt
            kwargs["recent_messages"] = current_messages

            runtime_kwargs = {
                k: v for k, v in kwargs.items()
                if k not in {"session", "actor_id", "workflow_id", "assignment_id"}
            }
            if "agent" in runtime_kwargs:
                reply_text, parsed_tools, meta = await self.bundle.runtime.generate_reply_details(
                    **runtime_kwargs,
                    available_tool_names=allowed_tool_names,
                )
            else:
                reply_text, parsed_tools = await self.bundle.runtime.generate_reply(
                    **runtime_kwargs,
                    available_tool_names=allowed_tool_names,
                )
                meta = {}
            raw_tool_calls = meta.get("raw_tool_calls") or []
            if parsed_tools and not raw_tool_calls:
                raw_tool_calls = [
                    {
                        "id": call.get("id") or f"call-{loop_count}-{index}",
                        "type": "function",
                        "function": {
                            "name": call.get("name") or "",
                            "arguments": json.dumps(call.get("args") or {}, ensure_ascii=False),
                        },
                    }
                    for index, call in enumerate(parsed_tools, start=1)
                    if isinstance(call, dict)
                ]

            if reply_text:
                final_reply = reply_text

            if not parsed_tools and not raw_tool_calls:
                return reply_text or final_reply or "（正在处理，请稍候……）", used_tool_names

            if loop_count == 1 and prompt:
                current_messages.append({"role": "user", "content": prompt})
                
            assistant_msg = {"role": "assistant", "content": reply_text or ""}
            if raw_tool_calls:
                assistant_msg["tool_calls"] = raw_tool_calls
            current_messages.append(assistant_msg)

            for call in raw_tool_calls:
                if call.get("type") != "function":
                    continue
                func_info = call.get("function") or {}
                tool_name = func_info.get("name", "")
                tool_call_id = call.get("id", "")
                
                if tool_name not in allowed_tool_names:
                    current_messages.append({
                        "role": "tool",
                        "tool_call_id": tool_call_id,
                        "name": tool_name,
                        "content": f"Error: tool '{tool_name}' is not allowed by the current skill."
                    })
                    continue

                try:
                    args = json.loads(func_info.get("arguments", "{}"))
                    if tool_name in WRITE_TOOLS and session is not None and actor_id is not None:
                        plan_res = self.plan_tool(
                            session,
                            actor_id=actor_id,
                            tool_name=tool_name,
                            args=args,
                            workflow_id=workflow_id,
                            assignment_id=assignment_id,
                            origin="agent",
                            auto_execute_if_ready=True,
                        )
                        status = plan_res.get("status")
                        execution_id = plan_res.get("executionId")
                        approval_id = plan_res.get("approvalId")
                        if status == "completed":
                            execution = session.get(ToolExecutionLog, execution_id)
                            result = execution.result_json if execution else {}
                        else:
                            if assignment_id:
                                assignment = session.get(WorkflowAssignment, assignment_id)
                                if assignment:
                                    assignment.status = "waiting_approval"
                                    session.add(assignment)
                                    session.flush()
                            result = {
                                "status": "pending_approval",
                                "executionId": execution_id,
                                "approvalId": approval_id,
                                "message": (
                                    f"工具执行计划已提交，等待审批（审批 ID: {approval_id}，执行 ID: {execution_id}）。"
                                    "由于安全策略，你无法直接写入该文件/目录。在用户或秘书审批通过前，操作不会生效。"
                                    "请在群聊中向用户说明此操作意图，告知用户需要进行审批。"
                                )
                            }
                    else:
                        result = self.bundle.tools.execute(
                            tool_name,
                            args,
                            effective_workspace_root,
                        )

                    if tool_name not in used_tool_names:
                        used_tool_names.append(tool_name)
                        
                    if tool_name == "list_dir":
                        entries = result.get("result", {}).get("entries", [])
                        preview = f"目录包含 {len(entries)} 个条目，前 30 项：{', '.join(item['name'] for item in entries[:30])}"
                        current_messages.append({"role": "tool", "tool_call_id": tool_call_id, "name": tool_name, "content": preview})
                    elif tool_name == "read_text_file":
                        file_content = result.get("result", {}).get("content", "")
                        current_messages.append({"role": "tool", "tool_call_id": tool_call_id, "name": tool_name, "content": f"已读取文件前 4000 字：\n{file_content[:4000]}"})
                    elif tool_name == "search_text":
                        matches = result.get("result", {}).get("matches", [])
                        preview = f"命中 {len(matches)} 处：{', '.join(match.get('path', '') for match in matches[:10])}"
                        current_messages.append({"role": "tool", "tool_call_id": tool_call_id, "name": tool_name, "content": preview})
                    else:
                        current_messages.append({"role": "tool", "tool_call_id": tool_call_id, "name": tool_name, "content": self._tool_result_preview(result.get("result") or result)})
                except Exception as error:
                    current_messages.append({"role": "tool", "tool_call_id": tool_call_id, "name": tool_name, "content": f"Error: {str(error)}"})

            prompt = ""

        return final_reply or "（正在处理，请稍候……）", used_tool_names

    async def _generate_reply_with_tools(self, workspace_root: str | None, **kwargs) -> str:
        loop_count = 0
        prompt = kwargs.get('prompt', '')
        recent_messages = kwargs.get('recent_messages', [])
        current_messages = list(recent_messages)
        final_reply = ""
        
        while loop_count < 3:
            loop_count += 1
            kwargs['prompt'] = prompt
            kwargs['recent_messages'] = current_messages
            
            reply_text, tool_calls = await self.bundle.runtime.generate_reply(**kwargs)
            if reply_text:
                final_reply = reply_text
            
            if not tool_calls:
                return reply_text or final_reply
            
            tool_results = []
            for call in tool_calls:
                try:
                    res = self.bundle.tools.execute(call['name'], call['args'], workspace_root)
                    if call['name'] == 'list_dir':
                        entries = res.get('result', {}).get('entries', [])
                        preview = f"目录含有 {len(entries)} 个条目，列出前30：" + ", ".join(e['name'] for e in entries[:30])
                        tool_results.append(f"[{call['name']}] {preview}")
                    elif call['name'] == 'read_text_file':
                        file_content = res.get('result', {}).get('content', '')
                        tool_results.append(f"[{call['name']}] 成功读取（前1000字）：\n{file_content[:1000]}")
                    elif call['name'] == 'search_text':
                        matches = res.get('result', {}).get('matches', [])
                        preview = f"命中 {len(matches)} 处：" + ", ".join(m.get('path', '') for m in matches[:10])
                        tool_results.append(f"[{call['name']}] {preview}")
                    else:
                        tool_results.append(f"[{call['name']}] " + self._tool_result_preview(res.get('result') or res))
                except Exception as e:
                    tool_results.append(f"[{call['name']}] Error: {str(e)}")
            
            if loop_count == 1:
                current_messages.append({'speakerName': 'user', 'body': prompt})
            current_messages.append({'speakerName': 'system', 'body': '【系统回传本地只读工具执行结果】\n' + '\n'.join(tool_results)})
            prompt = '请根据以上工具执行结果继续。如果是任务模式，请给出结论。'
            
        return reply_text or final_reply or '（正在进行后台处理，请稍后...）'

    async def dispatch_workflow(
        self,
        session: Session,
        conversation_id: str,
        title: str,
        content: str,
        mode: str,
        actor_ids: list[str] | None = None,
    ) -> dict[str, Any]:
        origin_conversation = session.get(Conversation, conversation_id)
        if origin_conversation is None:
            raise ValueError("Conversation not found.")
        workspace = self.get_workspace(session)
        self.get_or_create_user(session)
        active_agents = self.list_active_agents(session, workspace)
        if not active_agents:
            raise ValueError("No active agents are available.")
        secretary = next((agent for agent in active_agents if agent.id == workspace.secretary_agent_id), None) or active_agents[0]
        candidate_agents = [agent for agent in active_agents if agent.id != DEFAULT_USER["id"]]
        blueprint = self.bundle.workflow.build_secretary_blueprint(
            title=title,
            description=content,
            secretary_actor_id=secretary.id,
            candidate_actors=[self.serialize_actor(agent, self._actor_skill_rows(session, agent.id)) for agent in candidate_agents],
            requested_actor_ids=actor_ids,
        )
        workflow_id = f"wf-{uuid4().hex[:12]}"
        workflow = Workflow(
            id=workflow_id,
            title=title or preview_text(content, 40),
            description=content or title,
            status="awaiting_plan_approval",
            owner_id=DEFAULT_USER["id"],
            secretary_id=secretary.id,
            conversation_id=None,
            mode=mode,
            context_json=self.bundle.workflow.build_runtime_context(
                workflow_id=workflow_id,
                mode=mode,
                origin_conversation_id=origin_conversation.id,
                secretary_actor_id=secretary.id,
                blueprint=blueprint,
            ),
        )
        session.add(workflow)
        session.flush()
        self._push_workflow_event(
            workflow,
            event_type="team-blueprint-created",
            stage_key="planning",
            summary=blueprint.secretary_plan,
            extra={
                "teamActorIds": list(blueprint.team_actor_ids),
                "outsiderCandidateIds": list(blueprint.outsider_candidate_ids),
            },
        )
        workflow_room = self._ensure_workflow_conversation(
            session,
            workflow_id=workflow.id,
            title=f"{secretary.name} · {workflow.title}",
            faction=origin_conversation.faction or secretary.faction,
            team_actor_ids=blueprint.team_actor_ids,
            secretary_id=secretary.id,
            task_purpose=content or title,
        )
        workflow.conversation_id = workflow_room.id
        session.add(workflow)

        stage_specs = [
            ("planning", "规划与组队", "in_progress"),
            ("execution", "执行与协作", "pending"),
            ("review", "阶段审查与提交", "pending"),
        ]
        for position, (key, stage_title, status) in enumerate(stage_specs, start=1):
            session.add(
                WorkflowStage(
                    id=f"stage-{uuid4().hex[:12]}",
                    workflow_id=workflow.id,
                    key=key,
                    title=stage_title,
                    status=status,
                    position=position,
                    requires_secretary_review=True,
                    requires_user_review=True,
                )
            )
        session.flush()

        for actor_id in blueprint.team_actor_ids:
            if actor_id == secretary.id:
                continue
            session.add(
                WorkflowAssignment(
                    id=f"assign-{uuid4().hex[:12]}",
                    workflow_id=workflow.id,
                    stage_id=self._stage_by_key(session, workflow.id, "execution").id if self._stage_by_key(session, workflow.id, "execution") else None,
                    agent_id=actor_id,
                    summary=blueprint.assignment_briefs.get(actor_id, preview_text(content, 80)),
                    status="pending",
                    dependency_ids=[],
                    allow_parallel=True,
                )
            )
        session.flush()

        self._append_message(
            session,
            workflow_room,
            DEFAULT_USER["id"],
            "task",
            content or title or "请大家协作处理这项任务。",
            metadata={"workflowId": workflow.id, "stageKey": "planning", "role": "owner-request"},
        )
        self._append_message(
            session,
            origin_conversation,
            secretary.id,
            "system",
            f"已为“{workflow.title}”创建专门协作群聊，接下来会在任务群里同步组队、阶段推进和审批。",
            metadata={"workflowId": workflow.id, "taskConversationId": workflow_room.id},
        )
        await self._generate_secretary_plan_message(
            session,
            workflow=workflow,
            conversation=workflow_room,
            secretary=secretary,
            blueprint=blueprint,
        )

        for actor_id in blueprint.team_actor_ids:
            if actor_id == secretary.id:
                continue
            agent = session.get(Actor, actor_id)
            assignment = self._assignment_for_agent(session, workflow.id, actor_id)
            if agent is None or assignment is None:
                continue
            selection = self._select_skill_execution(
                session,
                actor=agent,
                mode="swarm" if mode == "swarm" else "task",
                prompt=assignment.summary,
                conversation_kind="group",
                source_kind="planning",
            )
            response, used_tool_names = await self._generate_reply_with_tools_v2(
                None,
                session=session, actor_id=agent.id, workflow_id=workflow.id, assignment_id=assignment.id,
                tool_allowlist=selection.tool_allowlist,
                settings=self.serialize_settings(self.get_workspace(session)),
                agent=self.serialize_actor(agent, self._actor_skill_rows(session, agent.id)),
                prompt=f"秘书已经把你的任务概括为：{assignment.summary}\n请先在群里做一句简短回应，说明你会如何接手。",
                mode="swarm" if mode == "swarm" else "task",
                conversation_kind="group",
                recent_messages=[],
                memory_snippets=self._recent_memory_summaries(session, agent.id),
                skill_context=selection.prompt_patch,
            )
            message = self._append_message(
                session,
                workflow_room,
                agent.id,
                "task",
                response,
                metadata={"workflowId": workflow.id, "stageKey": "planning", "assignmentId": assignment.id},
            )
            skill_run = self._record_skill_run(
                session,
                actor=agent,
                selection=selection,
                mode=mode,
                source_kind="planning",
                input_summary=assignment.summary,
                output_summary=response,
                workflow_id=workflow.id,
                stage_id=self._stage_by_key(session, workflow.id, "planning").id if self._stage_by_key(session, workflow.id, "planning") else None,
                assignment_id=assignment.id,
                conversation_id=workflow_room.id,
                message_id=message.id,
                used_tool_names=used_tool_names,
                extra={"workflowRole": "assignee"},
            )
            message.metadata_json = {
                **(message.metadata_json or {}),
                "skillRunId": skill_run.id,
                "skillId": selection.primary_skill_id,
                "skillLabel": selection.primary_skill_name,
                "skillStack": selection.selected_skills,
            }
            session.add(message)

        planning_stage = self._stage_by_key(session, workflow.id, "planning")
        if planning_stage is not None:
            self._create_stage_review_request(
                session,
                workflow=workflow,
                stage=planning_stage,
                reviewer_actor_id=DEFAULT_USER["id"],
                review_role="user",
                title="规划阶段待确认",
                summary="秘书已完成组队和初步分工，请确认是否进入执行阶段。",
                requires_user=True,
                requires_secretary=False,
            )
        return {
            "snapshot": self.build_snapshot(session),
            "workflowId": workflow.id,
            "conversationId": workflow_room.id,
        }

    def toggle_favorite_agent(self, session: Session, agent_id: str) -> dict[str, Any]:
        agent = session.get(Actor, agent_id)
        if agent is None:
            raise ValueError("Agent not found.")
        agent.favorite = not agent.favorite
        session.add(agent)
        posts = session.scalars(select(SocialPost).where(SocialPost.author_id == agent_id)).all()
        for post in posts:
            post.following = agent.favorite
            session.add(post)
        session.flush()
        return self.build_snapshot(session)

    def set_conversation_background(self, session: Session, conversation_id: str, background_id: str) -> dict[str, Any]:
        conversation = session.get(Conversation, conversation_id)
        if conversation is None:
            raise ValueError("Conversation not found.")
        conversation.background_id = background_id
        session.add(conversation)
        session.flush()
        return self.build_snapshot(session)

    def update_group_role(self, session: Session, conversation_id: str, agent_id: str, role: str) -> dict[str, Any]:
        member = session.scalar(
            select(ConversationMember).where(
                ConversationMember.conversation_id == conversation_id,
                ConversationMember.actor_id == agent_id,
            )
        )
        if member is None:
            raise ValueError("Conversation member not found.")
        member.role = role
        session.add(member)
        session.flush()
        return self.build_snapshot(session)

    def set_group_lifecycle(self, session: Session, conversation_id: str, action: str) -> dict[str, Any]:
        conversation = session.get(Conversation, conversation_id)
        if conversation is None or conversation.kind != "group":
            raise ValueError("Conversation group not found.")
        workflow = session.get(Workflow, conversation.workflow_id) if conversation.workflow_id else None
        if action == "disband":
            conversation.extra_json = {**(conversation.extra_json or {}), "archived": True, "channelState": "archived"}
            conversation.preview = "该协作频道已解散。"
            conversation.announcement = "该协作频道已结束并解散。"
        else:
            conversation.extra_json = {**(conversation.extra_json or {}), "archived": False, "channelState": "chat"}
            conversation.preview = "该协作频道已转为闲聊群聊。"
            conversation.announcement = "该协作频道任务已完成，当前作为闲聊群保留。"
        if workflow is not None:
            workflow.conversation_id = None
            session.add(workflow)
        conversation.workflow_id = None
        session.add(conversation)
        session.flush()
        return self.build_snapshot(session)

    def toggle_post_like(self, session: Session, post_id: str) -> dict[str, Any]:
        post = session.get(SocialPost, post_id)
        if post is None:
            raise ValueError("Post not found.")
        post.liked_by_user = not post.liked_by_user
        post.likes = max(0, post.likes + (1 if post.liked_by_user else -1))
        session.add(post)
        session.flush()
        return self.build_snapshot(session)

    def add_comment(self, session: Session, post_id: str, body: str) -> dict[str, Any]:
        post = session.get(SocialPost, post_id)
        if post is None:
            raise ValueError("Post not found.")
        content = str(body or "").strip()
        if not content:
            return self.build_snapshot(session)
        self._create_social_comment(session, post=post, author_id=DEFAULT_USER["id"], body=content)
        session.flush()
        return self.build_snapshot(session)

    def publish_post(self, session: Session, author_id: str | None, body: str, media_url: str | None = None) -> dict[str, Any]:
        content = str(body or "").strip()
        if not content:
            return self.build_snapshot(session)
        author = session.get(Actor, author_id) if author_id else None
        if author is None:
            author = session.scalar(
                select(Actor).where(Actor.kind == "agent", Actor.favorite.is_(True), Actor.is_active.is_(True)).order_by(Actor.name.asc())
            )
        if author is None:
            author = session.scalar(select(Actor).where(Actor.kind == "agent", Actor.is_active.is_(True)).order_by(Actor.name.asc()))
        if author is None:
            raise ValueError("No available agent author.")
        self._create_social_post(
            session,
            author=author,
            body=content,
            media_url=media_url or author.illustration_url or author.avatar_url,
        )
        session.flush()
        return self.build_snapshot(session)

    def plan_tool(
        self,
        session: Session,
        actor_id: str,
        tool_name: str,
        args: dict[str, Any],
        workflow_id: str | None = None,
        *,
        assignment_id: str | None = None,
        origin: str = "manual",
        auto_execute_if_ready: bool = False,
    ) -> dict[str, Any]:
        workspace = self.get_workspace(session)
        workflow = session.get(Workflow, workflow_id) if workflow_id else None
        allowed_tool_names = self._manual_tool_allowlist_for_actor(
            session,
            actor_id=actor_id,
            workflow=workflow,
        )
        if actor_id != DEFAULT_USER["id"] and tool_name not in allowed_tool_names:
            raise ValueError(f"Actor {actor_id} is not currently allowed to use tool {tool_name}.")
        plan = self.bundle.tools.plan(tool_name, args or {}, workspace.authorized_workspace_root)
        safe_args = json_safe(plan.normalized_args)
        existing_execution, existing_approval = self._find_existing_tool_execution(
            session,
            actor_id=actor_id,
            tool_name=tool_name,
            safe_args=safe_args,
            workflow_id=workflow_id,
            assignment_id=assignment_id,
        )
        if existing_execution is not None:
            if auto_execute_if_ready and existing_execution.status == "approved":
                self.execute_tool(session, existing_execution.id)
                existing_execution = session.get(ToolExecutionLog, existing_execution.id) or existing_execution
            return self._tool_plan_payload(
                session,
                execution=existing_execution,
                approval=existing_approval,
                plan=plan,
            )
        secretary_actor_id = workspace.secretary_agent_id or None
        secretary_self_approved = bool(
            secretary_actor_id
            and actor_id == secretary_actor_id
            and plan.requires_secretary_approval
        )
        requires_secretary_approval = bool(plan.requires_secretary_approval and not secretary_self_approved)
        requires_user_approval = bool(plan.requires_user_approval)
        needs_approval = requires_secretary_approval or requires_user_approval
        latest_skill_run = None
        conversation = session.get(Conversation, workflow.conversation_id) if workflow and workflow.conversation_id else None
        actor = session.get(Actor, actor_id)
        if workflow_id:
            latest_skill_run = session.scalar(
                select(SkillRun)
                .where(SkillRun.workflow_id == workflow_id, SkillRun.actor_id == actor_id)
                .order_by(SkillRun.created_at.desc())
            )
        execution = ToolExecutionLog(
            id=f"tool-{uuid4().hex[:12]}",
            actor_id=actor_id,
            tool_name=tool_name,
            risk_level=plan.risk_level,
            status="pending_approval" if needs_approval else "approved",
            request_json={
                **safe_args,
                **({"workflowId": workflow_id} if workflow_id else {}),
                **({"assignmentId": assignment_id} if assignment_id else {}),
                **({"requestedBySkillId": latest_skill_run.primary_skill_id} if latest_skill_run else {}),
                "origin": origin,
                "autoExecuteOnApproval": bool(auto_execute_if_ready),
            },
            approved=not needs_approval,
        )
        session.add(execution)
        session.flush([execution])
        approval = None
        pending_reviewers = (
            (["secretary"] if requires_secretary_approval else [])
            + (["user"] if requires_user_approval else [])
        )
        if needs_approval:
            approval = ApprovalRequest(
                id=f"approval-{uuid4().hex[:12]}",
                workflow_id=workflow_id,
                tool_execution_id=execution.id,
                requested_by_actor_id=actor_id,
                reviewer_actor_id=(
                    secretary_actor_id
                    if requires_secretary_approval
                    else DEFAULT_USER["id"] if requires_user_approval else None
                ),
                target_kind="tool_execution",
                title=f"Tool execution approval: {tool_name}",
                summary=f"Risk level: {plan.risk_level}. Awaiting remaining approvals before execution.",
                payload_json={
                    **safe_args,
                    "approvedByUser": False,
                    "approvedBySecretary": secretary_self_approved,
                    "pendingReviewers": pending_reviewers,
                    "decisionTrail": [],
                },
                status="pending",
                requires_user=requires_user_approval,
                requires_secretary=requires_secretary_approval,
            )
            session.add(approval)
        session.flush()
        if approval is not None and requires_secretary_approval:
            self._auto_approve_secretary_tool_review(
                session,
                approval=approval,
                reviewer_actor_id=secretary_actor_id,
            )
            session.flush()
        if conversation is not None:
            approval_label = "等待审批" if approval is not None else "可直接执行"
            tool_body = (
                f"{actor.name if actor else actor_id} 提交了工具计划：{tool_name}。"
                f" 风险等级：{plan.risk_level}，当前状态：{approval_label}。"
            )
            self._append_message(
                session,
                conversation,
                actor_id,
                "system",
                tool_body,
                metadata={
                    "workflowId": workflow_id,
                    "role": "tool-plan",
                    "toolExecutionId": execution.id,
                    "approvalId": approval.id if approval else None,
                    "toolName": tool_name,
                    "riskLevel": plan.risk_level,
                },
            )
            self._record_memory(
                session,
                actor_id=actor_id,
                conversation_id=conversation.id,
                source_kind="tool.plan",
                text=tool_body,
            )
        if workflow is not None:
            self._push_workflow_event(
                workflow,
                event_type="tool-plan-created",
                summary=f"{tool_name} / {plan.risk_level}",
                extra={
                    "toolExecutionId": execution.id,
                    "approvalId": approval.id if approval else None,
                    "toolName": tool_name,
                    "riskLevel": plan.risk_level,
                },
            )
        if auto_execute_if_ready and execution.status == "approved":
            self.execute_tool(session, execution.id)
            execution = session.get(ToolExecutionLog, execution.id) or execution
        return self._tool_plan_payload(
            session,
            execution=execution,
            approval=approval,
            plan=plan,
        )

    def request_workflow_help(
        self,
        session: Session,
        *,
        workflow_id: str,
        requester_actor_id: str,
        target_actor_id: str,
        summary: str,
    ) -> dict[str, Any]:
        workflow = session.get(Workflow, workflow_id)
        if workflow is None:
            raise ValueError("Workflow not found.")
        target_actor = session.get(Actor, target_actor_id)
        if target_actor is None:
            raise ValueError("Target helper not found.")
        requires_secretary = False
        requires_user = True
        approval = ApprovalRequest(
            id=f"approval-{uuid4().hex[:12]}",
            workflow_id=workflow.id,
            tool_execution_id=None,
            requested_by_actor_id=requester_actor_id,
            reviewer_actor_id=DEFAULT_USER["id"],
            target_kind="workflow_help_request",
            title=f"编外求助申请：{target_actor.name}",
            summary=summary or f"申请让 {target_actor.name} 作为编外协助者加入当前任务。",
            payload_json={
                "workflowId": workflow.id,
                "targetActorId": target_actor.id,
                "requestedByActorId": requester_actor_id,
                "summary": summary,
            },
            status="pending",
            requires_user=requires_user,
            requires_secretary=requires_secretary,
        )
        session.add(approval)
        help_requests = list(workflow.context_json.get("helpRequests") or [])
        help_requests.append(
            {
                "approvalId": approval.id,
                "requestedByActorId": requester_actor_id,
                "targetActorId": target_actor.id,
                "summary": summary,
                "status": "pending",
                "createdAt": isoformat(now_utc()),
            }
        )
        workflow.context_json = {**workflow.context_json, "helpRequests": help_requests}
        self._push_workflow_event(
            workflow,
            event_type="help-request-created",
            stage_key=self._active_stage(session, workflow.id).key if self._active_stage(session, workflow.id) is not None else None,
            summary=summary or f"请求 {target_actor.name} 作为编外协助者加入当前任务。",
            extra={"approvalId": approval.id, "targetActorId": target_actor.id, "requesterActorId": requester_actor_id},
        )
        session.add(workflow)
        conversation = session.get(Conversation, workflow.conversation_id) if workflow.conversation_id else None
        if conversation is not None:
            requester = session.get(Actor, requester_actor_id)
            self._append_message(
                session,
                conversation,
                requester_actor_id,
                "system",
                f"{requester.name if requester else requester_actor_id} 申请向编外成员 {target_actor.name} 请求帮助，等待秘书与用户确认。",
                metadata={"workflowId": workflow.id, "targetActorId": target_actor.id, "approvalId": approval.id, "role": "help-request"},
            )
        session.flush()
        return {
            "status": "pending_approval",
            "approvalId": approval.id,
            "snapshot": self.build_snapshot(session),
        }

    async def resolve_approval(self, session: Session, approval_id: str, decision: str, reviewer_actor_id: str | None = None) -> dict[str, Any]:
        approval = session.get(ApprovalRequest, approval_id)
        if approval is None:
            raise ValueError("Approval request not found.")
        if approval.status in {"approved", "rejected", "superseded"}:
            raise ValueError("Approval request is already resolved.")
        normalized_decision = "approve" if decision == "approve" else "reject"
        resolved_reviewer_actor_id, _reviewer_role = self._validate_approval_reviewer(session, approval, reviewer_actor_id)
        approval.reviewer_actor_id = resolved_reviewer_actor_id
        proposal_payload = None
        if approval.tool_execution_id and approval.target_kind == "tool_execution":
            self._resolve_tool_execution_approval(
                session,
                approval=approval,
                decision=normalized_decision,
                reviewer_actor_id=resolved_reviewer_actor_id,
            )
        else:
            approval.status = "approved" if normalized_decision == "approve" else "rejected"
            approval.resolved_at = now_utc()
            session.add(approval)
        if approval.target_kind == "skill_proposal":
            proposal_id = str(approval.payload_json.get("proposalId") or "")
            proposal = session.get(SkillProposal, proposal_id) if proposal_id else None
            if proposal is None and approval.id:
                proposal = session.scalar(select(SkillProposal).where(SkillProposal.approval_id == approval.id))
            if proposal is not None:
                proposal.status = approval.status
                proposal.reviewer_actor_id = approval.reviewer_actor_id
                proposal.decision_note = (
                    "The user approved this public skill improvement proposal."
                    if approval.status == "approved"
                    else "The user rejected this public skill improvement proposal."
                )
                proposal.resolved_at = approval.resolved_at
                session.add(proposal)
                proposal_payload = self.serialize_skill_proposal(proposal)
        if approval.target_kind == "workflow_stage_review" and approval.workflow_id:
            workflow = session.get(Workflow, approval.workflow_id)
            if workflow is not None:
                await self._resolve_workflow_stage_review(
                    session,
                    workflow=workflow,
                    approval=approval,
                    decision=normalized_decision,
                    reviewer_actor_id=resolved_reviewer_actor_id,
                )
        if approval.target_kind == "workflow_launch_request":
            payload = approval.payload_json or {}
            origin_conversation_id = str(payload.get("originConversationId") or "port-hub")
            secretary_actor_id = str(payload.get("secretaryActorId") or "")
            if normalized_decision == "approve":
                dispatch_result = await self.dispatch_workflow(
                    session,
                    conversation_id=origin_conversation_id,
                    title=str(payload.get("title") or "多人任务"),
                    content=str(payload.get("content") or ""),
                    mode="swarm",
                    actor_ids=None,
                )
                approval.payload_json = {
                    **payload,
                    "createdWorkflowId": dispatch_result.get("workflowId"),
                    "createdConversationId": dispatch_result.get("conversationId"),
                }
                session.add(approval)
                origin_conversation = session.get(Conversation, origin_conversation_id)
                if origin_conversation is not None and secretary_actor_id:
                    secretary = session.get(Actor, secretary_actor_id)
                    self._append_message(
                        session,
                        origin_conversation,
                        secretary_actor_id,
                        "system",
                        f"{secretary.name if secretary else '秘书智能体'} 已启动多人任务频道，接下来会在协作频道里继续推进。",
                        metadata={
                            "approvalId": approval.id,
                            "workflowId": dispatch_result.get("workflowId"),
                            "taskConversationId": dispatch_result.get("conversationId"),
                            "role": "workflow-launch-approved",
                        },
                    )
            else:
                origin_conversation = session.get(Conversation, origin_conversation_id)
                if origin_conversation is not None and secretary_actor_id:
                    secretary = session.get(Actor, secretary_actor_id)
                    self._append_message(
                        session,
                        origin_conversation,
                        secretary_actor_id,
                        "system",
                        f"{secretary.name if secretary else '秘书智能体'} 已记录你的决定，本轮先不启动多人任务。",
                        metadata={
                            "approvalId": approval.id,
                            "role": "workflow-launch-rejected",
                        },
                    )
        if approval.target_kind == "workflow_help_request" and approval.status == "approved":
            workflow = session.get(Workflow, approval.workflow_id) if approval.workflow_id else None
            if workflow is not None:
                orchestration = self.bundle.workflow.resolve_help_request(
                    workflow.context_json,
                    approval_id=approval.id,
                    target_actor_id=str(approval.payload_json.get("targetActorId") or ""),
                    approved=True,
                )
                help_requests = []
                for item in list(workflow.context_json.get("helpRequests") or []):
                    if item.get("approvalId") == approval.id:
                        help_requests.append({**item, "status": "approved", "resolvedAt": isoformat(now_utc())})
                    else:
                        help_requests.append(item)
                target_actor_id = approval.payload_json.get("targetActorId")
                conversation = session.get(Conversation, workflow.conversation_id) if workflow.conversation_id else None
                workflow.context_json = {
                    **workflow.context_json,
                    "helpRequests": help_requests,
                    "orchestration": orchestration,
                }
                session.add(workflow)
                if conversation is not None and target_actor_id:
                    action_plan = self.bundle.workflow.plan_after_help_resolution(
                        approved=True,
                        target_actor_id=str(target_actor_id),
                        summary=approval.payload_json.get("summary"),
                    )
                    await self._execute_workflow_action_plan(
                        session,
                        workflow=workflow,
                        conversation=conversation,
                        action_plan=action_plan,
                        default_stage_key=((workflow.context_json.get("orchestration") or {}).get("currentStageKey")),
                        transition_actor_id=workflow.secretary_id or workflow.owner_id,
                    )
        elif approval.target_kind == "workflow_help_request":
            workflow = session.get(Workflow, approval.workflow_id) if approval.workflow_id else None
            conversation = session.get(Conversation, workflow.conversation_id) if workflow and workflow.conversation_id else None
            target_actor_id = approval.payload_json.get("targetActorId")
            target_actor = session.get(Actor, target_actor_id) if target_actor_id else None
            if workflow is not None:
                orchestration = self.bundle.workflow.resolve_help_request(
                    workflow.context_json,
                    approval_id=approval.id,
                    target_actor_id=str(target_actor_id or ""),
                    approved=False,
                )
                help_requests = []
                for item in list(workflow.context_json.get("helpRequests") or []):
                    if item.get("approvalId") == approval.id:
                        help_requests.append({**item, "status": "rejected", "resolvedAt": isoformat(now_utc())})
                    else:
                        help_requests.append(item)
                workflow.context_json = {
                    **workflow.context_json,
                    "helpRequests": help_requests,
                    "orchestration": orchestration,
                }
                session.add(workflow)
            if conversation is not None:
                action_plan = self.bundle.workflow.plan_after_help_resolution(
                    approved=False,
                    target_actor_id=str(target_actor_id or ""),
                    summary=approval.payload_json.get("summary"),
                )
                if target_actor is not None:
                    action_plan.transition_message = f"对 {target_actor.name} 的编外求助申请未获批准，当前团队需要先在现有成员范围内继续推进。"
                await self._execute_workflow_action_plan(
                    session,
                    workflow=workflow,
                    conversation=conversation,
                    action_plan=action_plan,
                    default_stage_key=((workflow.context_json.get("orchestration") or {}).get("currentStageKey")) if workflow else None,
                    transition_actor_id=resolved_reviewer_actor_id or DEFAULT_USER["id"],
                )
        session.flush()
        created_workflow_id = None
        created_conversation_id = None
        if approval.target_kind == "workflow_launch_request":
            created_workflow_id = (approval.payload_json or {}).get("createdWorkflowId")
            created_conversation_id = (approval.payload_json or {}).get("createdConversationId")
        return {
            "status": approval.status,
            "approvalId": approval.id,
            "workflowId": approval.workflow_id,
            "targetKind": approval.target_kind,
            "createdWorkflowId": created_workflow_id,
            "createdConversationId": created_conversation_id,
            "proposal": proposal_payload,
            "approvalProgress": self._approval_progress(approval),
            "snapshot": self.build_snapshot(session),
        }

    def _resolve_tool_execution_approval(
        self,
        session: Session,
        *,
        approval: ApprovalRequest,
        decision: str,
        reviewer_actor_id: str | None,
    ) -> None:
        execution = session.get(ToolExecutionLog, approval.tool_execution_id) if approval.tool_execution_id else None
        if execution is None:
            return
        payload = dict(approval.payload_json or {})
        decision_trail = list(payload.get("decisionTrail") or [])
        reviewer_id, reviewer_role = self._validate_approval_reviewer(session, approval, reviewer_actor_id)
        reviewer = session.get(Actor, reviewer_id) if reviewer_id else None
        role_flag = "approvedByUser" if reviewer_role == "user" else "approvedBySecretary"
        already_approved = bool(payload.get(role_flag))
        if decision == "reject" or not already_approved:
            decision_trail.append(
                {
                    "actorId": reviewer_id,
                    "role": reviewer_role,
                    "decision": "approved" if decision == "approve" else "rejected",
                    "at": isoformat(now_utc()),
                }
            )
        if decision == "approve":
            payload[role_flag] = True
        payload["decisionTrail"] = decision_trail

        awaiting_roles: list[str] = []
        if decision == "approve":
            if approval.requires_secretary and not payload.get("approvedBySecretary"):
                awaiting_roles.append("secretary")
            if approval.requires_user and not payload.get("approvedByUser"):
                awaiting_roles.append("user")
        payload["pendingReviewers"] = awaiting_roles
        approval.payload_json = payload

        workflow_id = execution.request_json.get("workflowId") if execution.request_json else None
        workflow = session.get(Workflow, workflow_id) if workflow_id else None
        conversation = session.get(Conversation, workflow.conversation_id) if workflow and workflow.conversation_id else None

        if decision == "reject":
            approval.status = "rejected"
            approval.resolved_at = now_utc()
            execution.approved = False
            execution.status = "rejected"
            assignment_id = (execution.request_json or {}).get("assignmentId")
            if assignment_id:
                assignment = session.get(WorkflowAssignment, assignment_id)
                if assignment:
                    assignment.status = "pending"
                    session.add(assignment)
                    if workflow is not None:
                        self._clear_assignment_activity(workflow, assignment.id)
        elif awaiting_roles:
            approval.status = "pending"
            approval.resolved_at = None
            next_role = awaiting_roles[0]
            approval.reviewer_actor_id = self._expected_reviewer_actor_id(session, approval, next_role)
            execution.approved = False
            execution.status = "pending_approval"
        else:
            approval.status = "approved"
            approval.resolved_at = now_utc()
            execution.approved = True
            execution.status = "approved"
        session.add(approval)
        session.add(execution)

        waiting_label = "、".join("用户" if role == "user" else "秘书" for role in awaiting_roles)
        if workflow is not None:
            self._push_workflow_event(
                workflow,
                event_type="tool-approval-resolved" if approval.status != "pending" else "tool-approval-partial",
                summary=(
                    f"{execution.tool_name} / waiting {','.join(awaiting_roles)}"
                    if approval.status == "pending"
                    else f"{execution.tool_name} / {approval.status}"
                ),
                extra={
                    "approvalId": approval.id,
                    "toolExecutionId": execution.id,
                    "toolName": execution.tool_name,
                    "decision": approval.status,
                    "reviewerRole": reviewer_role,
                    "pendingReviewers": awaiting_roles,
                },
            )
        if conversation is not None:
            if approval.status == "pending":
                body = f"工具计划 {execution.tool_name} 已获得{'用户' if reviewer_role == 'user' else '秘书'}审批，仍等待{waiting_label}确认后才能执行。"
            elif approval.status == "approved":
                body = f"工具计划 {execution.tool_name} 已完成全部审批，可以继续执行。"
            else:
                body = f"工具计划 {execution.tool_name} 已被{'用户' if reviewer_role == 'user' else '秘书'}拒绝，当前需重新评估执行方案。"
            self._append_message(
                session,
                conversation,
                reviewer.id if reviewer is not None else DEFAULT_USER["id"],
                "system",
                body,
                metadata={
                    "workflowId": workflow.id,
                    "role": "tool-approval",
                    "toolExecutionId": execution.id,
                    "approvalId": approval.id,
                    "toolName": execution.tool_name,
                    "decision": approval.status,
                    "reviewerRole": reviewer_role,
                    "pendingReviewers": awaiting_roles,
                },
            )

    async def _resolve_workflow_stage_review(
        self,
        session: Session,
        *,
        workflow: Workflow,
        approval: ApprovalRequest,
        decision: str,
        reviewer_actor_id: str | None,
    ) -> None:
        stage_id = approval.payload_json.get("stageId")
        stage_key = str(approval.payload_json.get("stageKey") or "")
        review_role = str(approval.payload_json.get("reviewRole") or "")
        conversation = session.get(Conversation, workflow.conversation_id) if workflow.conversation_id else None
        if conversation is None:
            return
        orchestration, advance = self.bundle.workflow.resolve_stage_approval(
            workflow.context_json,
            stage_key=stage_key,
            review_role=review_role,
            decision=decision,
        )
        action_plan = (
            self.bundle.workflow.plan_after_approval(advance)
            if decision == "approve"
            else self.bundle.workflow.plan_after_rejection(stage_key=stage_key, review_role=review_role)
        )
        self._apply_workflow_advance(
            session,
            workflow=workflow,
            orchestration=orchestration,
            advance=advance,
            stage_id=stage_id,
            fallback_stage_key=stage_key,
        )
        await self._execute_workflow_action_plan(
            session,
            workflow=workflow,
            conversation=conversation,
            action_plan=action_plan,
            default_stage_key=advance.next_stage_key or stage_key,
            transition_actor_id=reviewer_actor_id or workflow.secretary_id or workflow.owner_id,
        )

    def _apply_workflow_advance(
        self,
        session: Session,
        *,
        workflow: Workflow,
        orchestration: dict[str, Any],
        advance,
        stage_id: str | None,
        fallback_stage_key: str | None = None,
    ) -> None:
        workflow.context_json = {**workflow.context_json, "orchestration": orchestration}
        workflow.status = advance.workflow_status
        session.add(workflow)

        current_stage = session.get(WorkflowStage, stage_id) if stage_id else (
            self._stage_by_key(session, workflow.id, fallback_stage_key) if fallback_stage_key else None
        )
        if current_stage is not None:
            if advance.mark_current_completed:
                current_stage.status = "completed"
            else:
                current_stage.status = "in_progress"
            session.add(current_stage)
        if advance.mark_next_in_progress and advance.next_stage_key:
            next_stage = self._stage_by_key(session, workflow.id, advance.next_stage_key)
            if next_stage is not None:
                next_stage.status = "in_progress"
                session.add(next_stage)

        transition_updates = self.bundle.workflow.build_transition_context_patch(advance)
        if transition_updates:
            workflow.context_json = {**workflow.context_json, **transition_updates}
            session.add(workflow)

    def execute_tool(self, session: Session, execution_id: str) -> dict[str, Any]:
        execution = session.get(ToolExecutionLog, execution_id)
        if execution is None:
            raise ValueError("Tool execution not found.")
        if not execution.approved:
            raise PermissionError("Tool execution is not approved.")
        workspace = self.get_workspace(session)
        result = self.bundle.tools.execute(execution.tool_name, execution.request_json or {}, workspace.authorized_workspace_root)
        execution.status = "completed"
        execution.result_json = result
        execution.snapshot_ref = result.get("result", {}).get("snapshot")
        execution.completed_at = now_utc()
        session.add(execution)
        workflow_id = execution.request_json.get("workflowId") if execution.request_json else None
        assignment_id = (execution.request_json or {}).get("assignmentId")
        if assignment_id:
            assignment = session.get(WorkflowAssignment, assignment_id)
            if assignment:
                assignment.status = "completed"
                session.add(assignment)
                workflow = session.get(Workflow, workflow_id) if workflow_id else None
                if workflow is not None:
                    self._clear_assignment_activity(workflow, assignment.id)
                    self._push_workflow_event(
                        workflow,
                        event_type="assignment-completed",
                        stage_key="execution",
                        summary=assignment.summary,
                        extra={"assignmentId": assignment.id, "agentId": assignment.agent_id},
                    )
        workflow = session.get(Workflow, workflow_id) if workflow_id else None
        conversation = session.get(Conversation, workflow.conversation_id) if workflow and workflow.conversation_id else None
        actor = session.get(Actor, execution.actor_id)
        if conversation is None and actor is not None:
            dm_conversation_id = f"dm-{actor.id}"
            conversation = session.get(Conversation, dm_conversation_id)
        if workflow is not None:
            self._push_workflow_event(
                workflow,
                event_type="tool-executed",
                summary=f"{execution.tool_name} completed",
                extra={"toolExecutionId": execution.id, "toolName": execution.tool_name},
            )
        if conversation is not None:
            result_data = result.get("result") or {}
            if execution.tool_name == "list_dir":
                entries = result_data.get("entries") or []
                detail = "目录内容：\n" + "\n".join(
                    f"{'[目录]' if e['isDir'] else '[文件]'} {e['name']}" 
                    for e in entries[:40]
                )
            elif execution.tool_name == "read_text_file":
                content = result_data.get("content") or ""
                detail = f"文件内容（前 800 字）：\n{content[:800]}"
            elif execution.tool_name == "search_text":
                matches = result_data.get("matches") or []
                detail = f"搜索命中 {len(matches)} 处：\n" + "\n".join(
                    m.get("path", "") for m in matches[:10]
                )
            else:
                detail = self._tool_result_preview(result_data)
            tool_body = f"{actor.name if actor else execution.actor_id} 已执行 {execution.tool_name}。\n{detail}"
            self._append_message(
                session,
                conversation,
                execution.actor_id,
                "system",
                tool_body,
                metadata={
                    "workflowId": workflow.id if workflow else None,
                    "role": "tool-executed",
                    "toolExecutionId": execution.id,
                    "toolName": execution.tool_name,
                },
            )
            self._record_memory(
                session,
                actor_id=execution.actor_id,
                conversation_id=conversation.id,
                source_kind="tool.execution",
                text=tool_body,
            )
        session.flush()
        return {
            "status": execution.status,
            "executionId": execution.id,
            "result": result,
            "snapshot": self.build_snapshot(session),
        }

    def undo_tool(self, session: Session, execution_id: str) -> dict[str, Any]:
        execution = session.get(ToolExecutionLog, execution_id)
        if execution is None or not execution.snapshot_ref:
            raise ValueError("No undo snapshot is available for this execution.")
        workspace = self.get_workspace(session)
        path_value = execution.request_json.get("path") or execution.request_json.get("src") or execution.result_json.get("result", {}).get("path")
        result = self.bundle.tools.execute("restore_snapshot", {"path": path_value}, workspace.authorized_workspace_root)
        workflow_id = execution.request_json.get("workflowId") if execution.request_json else None
        workflow = session.get(Workflow, workflow_id) if workflow_id else None
        conversation = session.get(Conversation, workflow.conversation_id) if workflow and workflow.conversation_id else None
        if workflow is not None:
            self._push_workflow_event(
                workflow,
                event_type="tool-undo",
                summary=f"{execution.tool_name} restored from snapshot",
                extra={"toolExecutionId": execution.id, "toolName": execution.tool_name},
            )
        if conversation is not None:
            undo_body = f"{execution.tool_name} 的最近一次执行已回滚，相关工作区内容恢复到快照状态。"
            self._append_message(
                session,
                conversation,
                DEFAULT_USER["id"],
                "system",
                undo_body,
                metadata={
                    "workflowId": workflow.id,
                    "role": "tool-undo",
                    "toolExecutionId": execution.id,
                    "toolName": execution.tool_name,
                },
            )
            self._record_memory(
                session,
                actor_id=DEFAULT_USER["id"],
                conversation_id=conversation.id,
                source_kind="tool.undo",
                text=undo_body,
            )
        return {
            "status": "restored",
            "executionId": execution.id,
            "result": result,
            "snapshot": self.build_snapshot(session),
        }

    async def interrupt_workflow(self, session: Session, workflow_id: str, body: str) -> dict[str, Any]:
        workflow = session.get(Workflow, workflow_id)
        if workflow is None:
            raise ValueError("Workflow not found.")
        active_stage = self._active_stage(session, workflow.id)
        patch = self.bundle.workflow.build_interrupt_patch(workflow_id=workflow_id, body=body, actor_id=DEFAULT_USER["id"])
        notes = list(workflow.context_json.get("interruptions") or [])
        notes.append(patch)
        revision_count = int(workflow.context_json.get("revisionCount") or 0) + 1
        orchestration = self.bundle.workflow.apply_interrupt(
            workflow.context_json,
            active_stage_key=active_stage.key if active_stage is not None else "planning",
            patch=patch,
        )
        workflow.context_json = {
            **workflow.context_json,
            "interruptions": notes,
            "lastInterrupt": patch,
            "revisionCount": revision_count,
            "orchestration": orchestration,
        }
        workflow.status = "needs_revision"
        session.add(workflow)
        if active_stage is not None:
            active_stage.status = "in_progress"
            session.add(active_stage)
        conversation = session.get(Conversation, workflow.conversation_id) if workflow.conversation_id else None
        if conversation is not None:
            self._append_message(
                session,
                conversation,
                DEFAULT_USER["id"],
                "task",
                body,
                metadata={"workflowId": workflow.id, "role": "human-interrupt"},
            )
            interrupt_plan = self.bundle.workflow.plan_after_interrupt(active_stage.key if active_stage is not None else "planning")
            secretary = session.get(Actor, workflow.secretary_id) if workflow.secretary_id else None
            if secretary is not None:
                selection = self._select_skill_execution(
                    session,
                    actor=secretary,
                    mode="swarm",
                    prompt=body,
                    conversation_kind="group",
                    workflow_role="secretary",
                    source_kind="interrupt",
                )
                reply, used_tool_names = await self._generate_reply_with_tools_v2(
                    None,
                    session=session, actor_id=secretary.id, workflow_id=workflow.id,
                    tool_allowlist=selection.tool_allowlist,
                    settings=self.serialize_settings(self.get_workspace(session)),
                    agent=self.serialize_actor(secretary, self._actor_skill_rows(session, secretary.id)),
                    prompt=f"用户刚刚补充了新的修改意见：{body}\n请在群聊中说明这会如何影响当前执行方向。",
                    mode="swarm",
                    conversation_kind="group",
                    recent_messages=[],
                    memory_snippets=self._recent_memory_summaries(session, secretary.id),
                    skill_context=selection.prompt_patch,
                    instructions_override="你是秘书智能体，需要在任务群里同步用户刚刚插入的修改意见，并告诉大家接下来如何调整。",
                )
                message = self._append_message(
                    session,
                    conversation,
                    secretary.id,
                    "task",
                    reply,
                    metadata={"workflowId": workflow.id, "role": "secretary-adjustment"},
                )
                skill_run = self._record_skill_run(
                    session,
                    actor=secretary,
                    selection=selection,
                    mode="swarm",
                    source_kind="interrupt",
                    input_summary=body,
                    output_summary=reply,
                    workflow_id=workflow.id,
                    stage_id=self._active_stage_id(session, workflow.id),
                    conversation_id=conversation.id,
                    message_id=message.id,
                    used_tool_names=used_tool_names,
                    extra={"interrupt": True},
                )
                message.metadata_json = {
                    **(message.metadata_json or {}),
                    "skillRunId": skill_run.id,
                    "skillId": selection.primary_skill_id,
                    "skillLabel": selection.primary_skill_name,
                    "skillStack": selection.selected_skills,
                }
                session.add(message)
                assignees = session.scalars(
                    select(WorkflowAssignment)
                    .where(WorkflowAssignment.workflow_id == workflow.id)
                    .order_by(WorkflowAssignment.updated_at.desc())
                ).all()
                for assignment in assignees[:2]:
                    agent = session.get(Actor, assignment.agent_id)
                    if agent is None or agent.id == secretary.id:
                        continue
                    member_selection = self._select_skill_execution(
                        session,
                        actor=agent,
                        mode="swarm" if workflow.mode == "swarm" else "task",
                        prompt=body,
                        conversation_kind="group",
                        source_kind="interrupt",
                    )
                    member_reply, member_used_tool_names = await self._generate_reply_with_tools_v2(
                        None,
                        session=session, actor_id=agent.id, workflow_id=workflow.id, assignment_id=assignment.id,
                        tool_allowlist=member_selection.tool_allowlist,
                        settings=self.serialize_settings(self.get_workspace(session)),
                        agent=self.serialize_actor(agent, self._actor_skill_rows(session, agent.id)),
                        prompt=(
                            f"用户刚补充的新要求：{body}\n"
                            f"你原本负责：{assignment.summary}\n"
                            "请在群聊中同步你将如何调整当前处理方向或补充哪些信息。"
                        ),
                        mode="swarm" if workflow.mode == "swarm" else "task",
                        conversation_kind="group",
                        recent_messages=[],
                        memory_snippets=self._recent_memory_summaries(session, agent.id),
                        skill_context=member_selection.prompt_patch,
                    )
                    member_message = self._append_message(
                        session,
                        conversation,
                        agent.id,
                        "task",
                        member_reply,
                        metadata={"workflowId": workflow.id, "assignmentId": assignment.id, "role": "interrupt-response"},
                    )
                    member_skill_run = self._record_skill_run(
                        session,
                        actor=agent,
                        selection=member_selection,
                        mode=workflow.mode,
                        source_kind="interrupt",
                        input_summary=body,
                        output_summary=member_reply,
                        workflow_id=workflow.id,
                        stage_id=self._active_stage_id(session, workflow.id),
                        assignment_id=assignment.id,
                        conversation_id=conversation.id,
                        message_id=member_message.id,
                        used_tool_names=member_used_tool_names,
                        extra={"workflowRole": "assignee", "interrupt": True},
                    )
                    member_message.metadata_json = {
                        **(member_message.metadata_json or {}),
                        "skillRunId": member_skill_run.id,
                        "skillId": member_selection.primary_skill_id,
                        "skillLabel": member_selection.primary_skill_name,
                        "skillStack": member_selection.selected_skills,
                    }
                    session.add(member_message)
            await self._execute_workflow_action_plan(
                session,
                workflow=workflow,
                conversation=conversation,
                action_plan=interrupt_plan,
                default_stage_key=active_stage.key if active_stage is not None else "planning",
                transition_actor_id=(secretary.id if secretary is not None else workflow.owner_id),
            )
        session.flush()
        return {
            "snapshot": self.build_snapshot(session),
            "workflowId": workflow.id,
            "stageKey": active_stage.key if active_stage is not None else "planning",
        }

    async def abort_workflow(self, session: Session, workflow_id: str) -> dict[str, Any]:
        workflow = session.get(Workflow, workflow_id)
        if workflow is None:
            raise ValueError("Workflow not found.")
        workflow.status = "cancelled"
        session.add(workflow)
        pending_approvals = session.scalars(
            select(ApprovalRequest).where(
                ApprovalRequest.workflow_id == workflow.id,
                ApprovalRequest.status == "pending"
            )
        ).all()
        for approval in pending_approvals:
            approval.status = "superseded"
            approval.resolved_at = now_utc()
            session.add(approval)
        conversation = session.get(Conversation, workflow.conversation_id)
        if conversation:
            self._append_message(
                session,
                conversation,
                workflow.secretary_id or workflow.owner_id,
                "system",
                f"任务“{workflow.title}”已被中止。",
                metadata={"workflowId": workflow.id, "role": "workflow-cancelled"}
            )
        session.flush()
        return {"status": "ok", "snapshot": self.build_snapshot(session), "workflowId": workflow.id}

    def _append_message(
        self,
        session: Session,
        conversation: Conversation,
        speaker_id: str,
        message_type: str,
        body: str,
        metadata: dict[str, Any] | None = None,
        message_id: str | None = None,
    ) -> Message:
        message = Message(
            id=message_id or f"msg-{uuid4().hex[:12]}",
            conversation_id=conversation.id,
            speaker_id=speaker_id,
            type=message_type,
            body=body,
            metadata_json=metadata or {},
        )
        session.add(message)
        session.flush()
        conversation.preview = preview_text(body, 56)
        conversation.unread_count = 0
        conversation.replied = True
        session.add(conversation)
        return message

    def _ensure_dm_conversation(self, session: Session, user: Actor, agent: Actor) -> Conversation:
        conversation_id = f"dm-{agent.id}"
        conversation = session.get(Conversation, conversation_id)
        if conversation is None:
            conversation = Conversation(
                id=conversation_id,
                kind="dm",
                title=agent.name,
                faction=agent.faction,
                preview=preview_text(agent.summary or agent.persona or ""),
                unread_count=0,
                replied=False,
                background_id=self._default_background_for(agent.faction),
                bookmarked=bool(agent.favorite),
                announcement="",
            )
            session.add(conversation)
            session.flush()
            session.add_all(
                [
                    ConversationMember(conversation_id=conversation.id, actor_id=user.id, role="owner"),
                    ConversationMember(conversation_id=conversation.id, actor_id=agent.id, role="member"),
                ]
            )
        conversation.title = agent.name
        conversation.faction = agent.faction
        conversation.background_id = conversation.background_id or self._default_background_for(agent.faction)
        session.add(conversation)
        session.flush()
        return conversation

    def _ensure_port_hub(self, session: Session, user: Actor, agents: list[Actor]) -> Conversation:
        conversation = session.get(Conversation, "port-hub")
        if conversation is None:
            conversation = Conversation(
                id="port-hub",
                kind="group",
                title="港区协作频道",
                faction="联合频道",
                preview="角色设定已刷新，新的频道成员正在向你问候。",
                unread_count=min(len(agents), 3),
                replied=False,
                background_id="polar-bloom",
                bookmarked=True,
                announcement="秘书智能体会在这里协调多智能体任务、同步阶段进度和发起审批。",
            )
            session.add(conversation)
            session.flush()
        existing_members = session.scalars(select(ConversationMember).where(ConversationMember.conversation_id == conversation.id)).all()
        existing_ids = {member.actor_id for member in existing_members}
        required_ids = [user.id, *[agent.id for agent in agents]]
        admin_ids = {agent.id for agent in agents[:2]}
        for actor_id in required_ids:
            role = "owner" if actor_id in {user.id, agents[0].id} else "admin" if actor_id in admin_ids else "member"
            if actor_id in existing_ids:
                member = next(item for item in existing_members if item.actor_id == actor_id)
                member.is_active = True
                member.role = role
                session.add(member)
            else:
                session.add(ConversationMember(conversation_id=conversation.id, actor_id=actor_id, role=role, is_active=True))
        for member in existing_members:
            if member.actor_id not in required_ids:
                member.is_active = False
                session.add(member)
        session.flush()
        return conversation

    def _ensure_greeting_message(self, session: Session, user: Actor, agent: Actor, greeting: str) -> None:
        conversation = self._ensure_dm_conversation(session, user, agent)
        existing = session.scalar(select(Message).where(Message.conversation_id == conversation.id).limit(1))
        if existing is None:
            self._append_message(session, conversation, agent.id, "text", greeting)
        hub = session.get(Conversation, "port-hub")
        if hub is not None:
            hub_existing = session.scalar(
                select(Message).where(Message.conversation_id == hub.id, Message.speaker_id == agent.id)
            )
            if hub_existing is None:
                self._append_message(session, hub, agent.id, "text", f"{agent.name} 已加入港区协作频道。")

    def _ensure_greeting_post(self, session: Session, agent: Actor) -> None:
        existing = session.scalar(select(SocialPost).where(SocialPost.author_id == agent.id).limit(1))
        if existing is not None:
            return
        self._create_social_post(
            session,
            author=agent,
            body=agent.summary or agent.persona or f"{agent.name} 已接入 AzurJuus。",
            media_url=agent.illustration_url or agent.avatar_url,
            likes=200,
        )

    def _sync_relationships(self, session: Session, agents: list[Actor]) -> None:
        for agent in agents:
            for peer in agents:
                if agent.id == peer.id:
                    continue
                existing = session.scalar(
                    select(AgentRelationship).where(
                        AgentRelationship.agent_id == agent.id,
                        AgentRelationship.peer_agent_id == peer.id,
                    )
                )
                if existing is None:
                    session.add(
                        AgentRelationship(
                            id=f"rel-{slugify(agent.id)}-{slugify(peer.id)}",
                            agent_id=agent.id,
                            peer_agent_id=peer.id,
                            affinity_score=0.6,
                            trust_score=0.6,
                            social_probability=0.55,
                            notes_json={},
                        )
                    )

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

    def _recent_memory_summaries(self, session: Session, actor_id: str, limit: int = 4) -> list[str]:
        chunks = session.scalars(
            select(MemoryChunk).where(MemoryChunk.actor_id == actor_id).order_by(MemoryChunk.created_at.desc())
        ).all()
        return [chunk.summary for chunk in chunks[:limit] if chunk.summary]

    def _create_social_post(
        self,
        session: Session,
        *,
        author: Actor,
        body: str,
        media_url: str | None = None,
        likes: int = 0,
    ) -> SocialPost:
        content = " ".join(str(body or "").split())
        if not content:
            content = author.summary or author.persona or f"{author.name} 记录了一条新的动态。"
        post = SocialPost(
            id=f"post-{uuid4().hex[:12]}",
            author_id=author.id,
            excerpt=preview_text(content, 96),
            likes=max(0, likes),
            liked_by_user=False,
            art_label=author.name,
            art_mark=author.initials,
            art_palette=list(author.palette or ["#b8c5ef", "#f0f4ff", "#ffffff"]),
            media_url=media_url,
            following=author.favorite,
        )
        session.add(post)
        session.flush()
        self._record_memory(session, actor_id=author.id, conversation_id=None, source_kind="social.post", text=content)
        return post

    def _create_social_comment(self, session: Session, *, post: SocialPost, author_id: str, body: str) -> SocialComment:
        content = " ".join(str(body or "").split())
        comment = SocialComment(
            id=f"comment-{uuid4().hex[:12]}",
            post_id=post.id,
            author_id=author_id,
            body=content,
        )
        session.add(comment)
        session.flush()
        self._record_memory(session, actor_id=author_id, conversation_id=None, source_kind="social.comment", text=content)
        return comment

    async def _auto_comment_on_post(
        self,
        session: Session,
        *,
        post: SocialPost,
        author: Actor,
        agents: list[Actor],
        settings_payload: dict[str, Any],
    ) -> int:
        agent_by_id = {agent.id: agent for agent in agents if agent.id != author.id}
        if not agent_by_id:
            return 0

        relationships = session.scalars(
            select(AgentRelationship).where(
                AgentRelationship.peer_agent_id == author.id,
                AgentRelationship.agent_id.in_(list(agent_by_id)),
            )
        ).all()
        candidates = [
            SocialCandidate(
                actor_id=relationship.agent_id,
                last_post_at=None,
                affinity_score=relationship.affinity_score,
                trust_score=relationship.trust_score,
                social_probability=relationship.social_probability,
            )
            for relationship in relationships
            if relationship.agent_id in agent_by_id
        ]
        selected = self.bundle.social.pick_commenters(candidates, limit=2)
        if not selected:
            return 0

        relationship_by_agent = {relationship.agent_id: relationship for relationship in relationships}
        created = 0
        for candidate in selected:
            commenter = agent_by_id.get(candidate.actor_id)
            relationship = relationship_by_agent.get(candidate.actor_id)
            if commenter is None or relationship is None:
                continue
            relationship_hint = self.bundle.social.build_relationship_hint(
                affinity_score=relationship.affinity_score,
                trust_score=relationship.trust_score,
                notes=relationship.notes_json or {},
            )
            prompt = self.bundle.social.build_comment_prompt(
                author_name=author.name,
                post_excerpt=post.excerpt,
                responder_name=commenter.name,
                relationship_hint=relationship_hint,
            )
            selection = self._select_skill_execution(
                session,
                actor=commenter,
                mode="social_comment",
                prompt=prompt,
                conversation_kind="social",
                source_kind="social_comment",
            )
            body = await self.bundle.runtime.generate_social_comment(
                settings=settings_payload,
                agent=self.serialize_actor(commenter, self._actor_skill_rows(session, commenter.id)),
                prompt=prompt,
                relationship_hint=relationship_hint,
                skill_context=selection.prompt_patch,
            )
            if not str(body or "").strip():
                continue
            comment = self._create_social_comment(session, post=post, author_id=commenter.id, body=body)
            self._record_skill_run(
                session,
                actor=commenter,
                selection=selection,
                mode="social_comment",
                source_kind="social_comment",
                input_summary=prompt,
                output_summary=body,
                extra={"postId": post.id, "commentId": comment.id},
            )
            created += 1
        return created

    def _record_memory(self, session: Session, actor_id: str | None, conversation_id: str | None, source_kind: str, text: str) -> None:
        chunk = MemoryChunk(
            id=f"mem-{uuid4().hex[:12]}",
            actor_id=actor_id,
            conversation_id=conversation_id,
            source_kind=source_kind,
            summary=preview_text(text, 200),
            metadata_json={"actor_id": actor_id, "conversation_id": conversation_id},
        )
        session.add(chunk)
        session.flush()
        self.bundle.memory.add(
            chunk.id,
            chunk.summary,
            {"actor_id": actor_id, "conversation_id": conversation_id, "source_kind": source_kind},
        )

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

    def _push_workflow_event(
        self,
        workflow: Workflow,
        *,
        event_type: str,
        stage_key: str | None = None,
        summary: str | None = None,
        extra: dict[str, Any] | None = None,
    ) -> None:
        context = workflow.context_json or {}
        orchestration = dict((context.get("orchestration") or {}))
        history = list(orchestration.get("history") or [])
        event = {
            "type": event_type,
            "at": isoformat(now_utc()),
        }
        if stage_key:
            event["stageKey"] = stage_key
        if summary:
            event["summary"] = summary
        if extra:
            event.update(extra)
        history.append(event)
        orchestration["history"] = history
        workflow.context_json = {**context, "orchestration": orchestration}

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
                    "at": event.get("at") or workflow.updated_at.astimezone(UTC).isoformat().replace("+00:00", "Z"),
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

    async def _publish_workflow_wrapup_post(self, session: Session, *, workflow: Workflow) -> SocialPost | None:
        if workflow.context_json.get("completionPostId"):
            return session.get(SocialPost, workflow.context_json.get("completionPostId"))
        secretary = session.get(Actor, workflow.secretary_id) if workflow.secretary_id else None
        if secretary is None:
            return None
        workspace = self.get_workspace(session)
        prompt = (
            f"任务“{workflow.title}”已经完成。\n"
            f"任务摘要：{workflow.description}\n"
            "请以 JUUS 动态的方式发一条简短收尾感想，像任务结束后的角色化碎碎念，不要写成正式汇报。"
        )
        selection = self._select_skill_execution(
            session,
            actor=secretary,
            mode="social_post",
            prompt=prompt,
            conversation_kind="social",
            workflow_role="secretary",
            source_kind="workflow_wrapup_post",
        )
        body = await self.bundle.runtime.generate_social_post(
            settings=self.serialize_settings(self.get_workspace(session)),
            agent=self.serialize_actor(secretary, self._actor_skill_rows(session, secretary.id)),
            prompt=prompt,
            memory_snippets=self._recent_memory_summaries(session, actor_id=secretary.id, limit=4),
            skill_context=selection.prompt_patch,
        )
        post = self._create_social_post(
            session,
            author=secretary,
            body=body,
            media_url=secretary.illustration_url or secretary.avatar_url,
        )
        self._record_skill_run(
            session,
            actor=secretary,
            selection=selection,
            mode="social_post",
            source_kind="workflow_wrapup_post",
            input_summary=prompt,
            output_summary=body,
            workflow_id=workflow.id,
            conversation_id=workflow.conversation_id,
            extra={"postId": post.id},
        )
        team_actor_ids = [
            actor_id
            for actor_id in workflow.context_json.get("teamActorIds") or []
            if actor_id != secretary.id
        ]
        if team_actor_ids:
            team_agents = session.scalars(select(Actor).where(Actor.id.in_(team_actor_ids))).all()
            comment_count = await self._auto_comment_on_post(
                session,
                post=post,
                author=secretary,
                agents=[secretary, *team_agents],
                settings_payload=self.serialize_settings(self.get_workspace(session)),
            )
        else:
            comment_count = 0
        orchestration = self.bundle.workflow.record_completion_post(
            workflow.context_json,
            post_id=post.id,
            comment_count=comment_count,
        )
        workflow.context_json = {
            **workflow.context_json,
            "completionPostId": post.id,
            "completionCommentCount": comment_count,
            "orchestration": orchestration,
        }
        session.add(workflow)
        conversation = session.get(Conversation, workflow.conversation_id) if workflow.conversation_id else None
        if conversation is not None:
            self._append_message(
                session,
                conversation,
                secretary.id,
                "system",
                f"{secretary.name} 已将本轮任务收尾感想同步到 JUUS 动态。",
                metadata={"workflowId": workflow.id, "postId": post.id, "role": "workflow-wrapup-post"},
            )
        return post

    def _current_workflow_for_conversation(self, session: Session, conversation_id: str) -> Workflow | None:
        return session.scalar(
            select(Workflow)
            .where(
                Workflow.conversation_id == conversation_id,
                Workflow.status.in_(["awaiting_plan_approval", "active", "needs_revision"]),
            )
            .order_by(Workflow.updated_at.desc())
        )

    def _pick_task_lead_agent(self, agents: list[Actor], secretary_actor_id: str | None) -> Actor:
        if not agents:
            raise ValueError("No available agents for this conversation.")
        if len(agents) == 1:
            return agents[0]
        return next((agent for agent in agents if agent.id != secretary_actor_id), agents[0])

    def _create_workflow_launch_request(
        self,
        session: Session,
        *,
        conversation: Conversation,
        requester_actor: Actor,
        secretary_actor: Actor,
        body: str,
        mode: str,
        summary: str,
    ) -> ApprovalRequest:
        approval = ApprovalRequest(
            id=f"approval-{uuid4().hex[:12]}",
            requested_by_actor_id=secretary_actor.id,
            reviewer_actor_id=DEFAULT_USER["id"],
            target_kind="workflow_launch_request",
            title=f"多人任务启动确认：{preview_text(body, 24)}",
            summary=summary or f"{requester_actor.name} 认为当前任务更适合交给秘书组织协作频道处理。",
            payload_json={
                "originConversationId": conversation.id,
                "requesterActorId": requester_actor.id,
                "secretaryActorId": secretary_actor.id,
                "title": preview_text(body, 32),
                "content": body,
                "mode": "task",
            },
            status="pending",
            requires_user=True,
            requires_secretary=False,
        )
        session.add(approval)
        session.flush()
        return approval

    def _active_stage(self, session: Session, workflow_id: str) -> WorkflowStage | None:
        stages = session.scalars(
            select(WorkflowStage)
            .where(WorkflowStage.workflow_id == workflow_id)
            .order_by(WorkflowStage.position.asc())
        ).all()
        return next((stage for stage in stages if stage.status == "in_progress"), None) or next(
            (stage for stage in stages if stage.status == "pending"),
            None,
        ) or (stages[0] if stages else None)

    def _active_stage_id(self, session: Session, workflow_id: str) -> str | None:
        stage = self._active_stage(session, workflow_id)
        return stage.id if stage is not None else None

    def _stage_by_key(self, session: Session, workflow_id: str, key: str) -> WorkflowStage | None:
        return session.scalar(
            select(WorkflowStage).where(WorkflowStage.workflow_id == workflow_id, WorkflowStage.key == key)
        )

    def _assignment_for_agent(self, session: Session, workflow_id: str, agent_id: str) -> WorkflowAssignment | None:
        return session.scalar(
            select(WorkflowAssignment).where(
                WorkflowAssignment.workflow_id == workflow_id,
                WorkflowAssignment.agent_id == agent_id,
            )
        )

    def _assignment_dependencies_met(
        self,
        assignments_by_id: dict[str, WorkflowAssignment],
        assignment: WorkflowAssignment,
    ) -> bool:
        dependency_ids = list(assignment.dependency_ids or [])
        if not dependency_ids:
            return True
        for dependency_id in dependency_ids:
            dependency = assignments_by_id.get(dependency_id)
            if dependency is None or dependency.status != "completed":
                return False
        return True

    def _select_execution_batch(
        self,
        workflow: Workflow,
        assignments: list[WorkflowAssignment],
        runnable_statuses: set[str] | None = None,
    ) -> list[WorkflowAssignment]:
        active_assignments = [
            item
            for item in assignments
            if item.status != "completed"
            and (runnable_statuses is None or item.status in runnable_statuses)
        ]
        if not active_assignments:
            return []

        assignments_by_id = {item.id: item for item in assignments}
        assignments_by_actor = {item.agent_id: item for item in active_assignments}
        context_groups = [
            [str(actor_id) for actor_id in group if str(actor_id).strip()]
            for group in list((workflow.context_json or {}).get("parallelGroups") or [])
            if isinstance(group, list)
        ]

        for group in context_groups:
            batch = [
                assignments_by_actor[actor_id]
                for actor_id in group
                if actor_id in assignments_by_actor
                and self._assignment_dependencies_met(assignments_by_id, assignments_by_actor[actor_id])
            ]
            if batch:
                return batch

        ready_assignments = [
            item
            for item in active_assignments
            if self._assignment_dependencies_met(assignments_by_id, item)
        ]
        if not ready_assignments:
            return []
        if any(not item.allow_parallel for item in ready_assignments):
            return [ready_assignments[0]]
        return ready_assignments

    def _assignment_activity_map(self, workflow: Workflow) -> dict[str, Any]:
        return dict((workflow.context_json or {}).get("assignmentActivity") or {})

    def _store_assignment_activity(self, workflow: Workflow, activity_map: dict[str, Any]) -> None:
        workflow.context_json = {**(workflow.context_json or {}), "assignmentActivity": activity_map}

    def _set_assignment_activity(
        self,
        workflow: Workflow,
        assignment: WorkflowAssignment,
        *,
        state: str,
        label: str,
        detail: str | None = None,
        next_retry_at: datetime | None = None,
        attempts: int | None = None,
    ) -> None:
        activity_map = self._assignment_activity_map(workflow)
        previous = dict(activity_map.get(assignment.id) or {})
        activity_map[assignment.id] = {
            **previous,
            "state": state,
            "label": label,
            "detail": detail or "",
            "updatedAt": isoformat(now_utc()),
            "attempts": attempts if attempts is not None else int(previous.get("attempts") or 0),
            "nextRetryAt": isoformat(next_retry_at) if next_retry_at else None,
        }
        self._store_assignment_activity(workflow, activity_map)

    def _clear_assignment_activity(self, workflow: Workflow, assignment_id: str) -> None:
        activity_map = self._assignment_activity_map(workflow)
        if assignment_id not in activity_map:
            return
        activity_map.pop(assignment_id, None)
        self._store_assignment_activity(workflow, activity_map)

    def _assignment_attempts(self, workflow: Workflow, assignment_id: str) -> int:
        return int((self._assignment_activity_map(workflow).get(assignment_id) or {}).get("attempts") or 0)

    def _assignment_retry_due(self, workflow: Workflow, assignment_id: str, *, now: datetime | None = None) -> bool:
        activity = self._assignment_activity_map(workflow).get(assignment_id) or {}
        next_retry_at = parse_iso_datetime(activity.get("nextRetryAt"))
        if next_retry_at is None:
            return True
        return next_retry_at <= (now or now_utc())

    def _assignment_live_label(self, assignment: WorkflowAssignment) -> str:
        summary = preview_text(assignment.summary or "当前子任务", 28)
        return f"正在处理：{summary}"

    async def _admit_workflow_helper(
        self,
        session: Session,
        *,
        workflow: Workflow,
        conversation: Conversation,
        target_actor: Actor,
        summary: str | None = None,
        join_role: str = "observer",
        auto_reply: bool = True,
    ) -> WorkflowAssignment:
        existing = session.scalar(
            select(ConversationMember).where(
                ConversationMember.conversation_id == conversation.id,
                ConversationMember.actor_id == target_actor.id,
            )
        )
        if existing is None:
            session.add(
                ConversationMember(
                    conversation_id=conversation.id,
                    actor_id=target_actor.id,
                    role=join_role,
                    is_active=True,
                )
            )
        else:
            existing.is_active = True
            existing.role = join_role
            session.add(existing)

        assignment = self._assignment_for_agent(session, workflow.id, target_actor.id)
        if assignment is None:
            assignment = WorkflowAssignment(
                id=f"assign-{uuid4().hex[:12]}",
                workflow_id=workflow.id,
                stage_id=self._active_stage_id(session, workflow.id),
                agent_id=target_actor.id,
                summary=summary or f"作为编外协助者，为“{workflow.title}”补充思路、风险判断或缺失信息。",
                status="pending",
                dependency_ids=[],
                allow_parallel=True,
            )
            session.add(assignment)
            session.flush()

        approved_helpers = list(workflow.context_json.get("approvedHelpers") or [])
        if target_actor.id not in approved_helpers:
            approved_helpers.append(target_actor.id)
            workflow.context_json = {
                **workflow.context_json,
                "approvedHelpers": approved_helpers,
            }
            session.add(workflow)

        if auto_reply:
            selection = self._select_skill_execution(
                session,
                actor=target_actor,
                mode="swarm",
                prompt=assignment.summary,
                conversation_kind="group",
                source_kind="help_request",
            )
            helper_reply, helper_used_tool_names = await self._generate_reply_with_tools_v2(
                None,
                session=session, actor_id=target_actor.id, workflow_id=workflow.id, assignment_id=assignment.id,
                tool_allowlist=selection.tool_allowlist,
                settings=self.serialize_settings(self.get_workspace(session)),
                agent=self.serialize_actor(target_actor, self._actor_skill_rows(session, target_actor.id)),
                prompt=(
                    f"你刚获准以编外协助者身份加入任务“{workflow.title}”。\n"
                    f"总任务：{workflow.description}\n"
                    f"当前希望你补充的部分：{assignment.summary}\n"
                    "请先在群聊里说明你会从什么角度协助，以及你现在最需要哪些上下文。"
                ),
                mode="swarm",
                conversation_kind="group",
                recent_messages=[],
                memory_snippets=self._recent_memory_summaries(session, target_actor.id),
                skill_context=selection.prompt_patch,
            )
            helper_message = self._append_message(
                session,
                conversation,
                target_actor.id,
                "task",
                helper_reply,
                metadata={"workflowId": workflow.id, "assignmentId": assignment.id, "role": "help-response"},
            )
            helper_skill_run = self._record_skill_run(
                session,
                actor=target_actor,
                selection=selection,
                mode="swarm",
                source_kind="help_request",
                input_summary=assignment.summary,
                output_summary=helper_reply,
                workflow_id=workflow.id,
                stage_id=self._active_stage_id(session, workflow.id),
                assignment_id=assignment.id,
                conversation_id=conversation.id,
                message_id=helper_message.id,
                used_tool_names=helper_used_tool_names,
                extra={"workflowRole": "helper"},
            )
            helper_message.metadata_json = {
                **(helper_message.metadata_json or {}),
                "skillRunId": helper_skill_run.id,
                "skillId": selection.primary_skill_id,
                "skillLabel": selection.primary_skill_name,
                "skillStack": selection.selected_skills,
            }
            session.add(helper_message)

        return assignment

    def _ensure_workflow_conversation(
        self,
        session: Session,
        *,
        workflow_id: str,
        title: str,
        faction: str,
        team_actor_ids: list[str],
        secretary_id: str | None,
        task_purpose: str | None = None,
    ) -> Conversation:
        conversation_id = f"wf-room-{workflow_id}"
        conversation = session.get(Conversation, conversation_id)
        task_purpose = str(task_purpose or "").strip()
        announcement = f"为完成“{task_purpose}”而建立的协作频道。" if task_purpose else "为当前任务建立的协作频道。"
        if conversation is None:
            conversation = Conversation(
                id=conversation_id,
                kind="group",
                title=title,
                faction=faction or "联合频道",
                preview="秘书智能体正在创建协作小组。",
                unread_count=1,
                replied=False,
                background_id="signal-blue",
                bookmarked=True,
                announcement=announcement,
                workflow_id=workflow_id,
                extra_json={"workflowRoom": True, "taskPurpose": task_purpose, "channelState": "workflow"},
            )
            session.add(conversation)
            session.flush()
        else:
            conversation.announcement = announcement
            conversation.extra_json = {
                **(conversation.extra_json or {}),
                "workflowRoom": True,
                "taskPurpose": task_purpose,
                "channelState": "workflow",
            }
            session.add(conversation)
        existing_members = session.scalars(
            select(ConversationMember).where(ConversationMember.conversation_id == conversation.id)
        ).all()
        existing_ids = {member.actor_id for member in existing_members}
        required_ids = [DEFAULT_USER["id"], *team_actor_ids]
        for actor_id in required_ids:
            role = "owner" if actor_id == DEFAULT_USER["id"] else "admin" if actor_id == secretary_id else "member"
            if actor_id in existing_ids:
                member = next(item for item in existing_members if item.actor_id == actor_id)
                member.role = role
                member.is_active = True
                session.add(member)
            else:
                session.add(
                    ConversationMember(
                        conversation_id=conversation.id,
                        actor_id=actor_id,
                        role=role,
                        is_active=True,
                    )
                )
        for member in existing_members:
            if member.actor_id not in required_ids:
                member.is_active = False
                session.add(member)
        session.flush()
        return conversation

    def _create_stage_review_request(
        self,
        session: Session,
        *,
        workflow: Workflow,
        stage: WorkflowStage,
        reviewer_actor_id: str | None,
        review_role: str,
        title: str,
        summary: str,
        requires_user: bool,
        requires_secretary: bool,
        payload_extra: dict[str, Any] | None = None,
    ) -> ApprovalRequest:
        existing = session.scalar(
            select(ApprovalRequest).where(
                ApprovalRequest.workflow_id == workflow.id,
                ApprovalRequest.target_kind == "workflow_stage_review",
                ApprovalRequest.status == "pending",
            )
        )
        if existing is not None and existing.payload_json.get("stageId") == stage.id and existing.payload_json.get("reviewRole") == review_role:
            return existing
        approval = ApprovalRequest(
            id=f"approval-{uuid4().hex[:12]}",
            workflow_id=workflow.id,
            tool_execution_id=None,
            requested_by_actor_id=workflow.secretary_id or workflow.owner_id,
            reviewer_actor_id=reviewer_actor_id,
            target_kind="workflow_stage_review",
            title=title,
            summary=summary,
            payload_json={
                "workflowId": workflow.id,
                "stageId": stage.id,
                "stageKey": stage.key,
                "reviewRole": review_role,
                **(payload_extra or {}),
            },
            status="pending",
            requires_user=requires_user,
            requires_secretary=requires_secretary,
        )
        session.add(approval)
        session.flush()
        return approval

    async def _apply_review_requests(
        self,
        session: Session,
        *,
        workflow: Workflow,
        requests: list[dict[str, Any]] | None,
    ) -> None:
        for request in requests or []:
            request_stage = self._stage_by_key(session, workflow.id, request["stageKey"])
            if request_stage is None:
                continue
            reviewer_actor_id = workflow.secretary_id if request["reviewerKind"] == "secretary" else DEFAULT_USER["id"]
            approval = self._create_stage_review_request(
                session,
                workflow=workflow,
                stage=request_stage,
                reviewer_actor_id=reviewer_actor_id,
                review_role=request["reviewRole"],
                title=request["title"],
                summary=request["summary"],
                requires_user=request["requiresUser"],
                requires_secretary=request["requiresSecretary"],
            )
            if request.get("reviewerKind") == "secretary":
                await self._auto_approve_stage_review_request(
                    session,
                    workflow=workflow,
                    approval=approval,
                )

    async def _execute_workflow_action_plan(
        self,
        session: Session,
        *,
        workflow: Workflow,
        conversation: Conversation,
        action_plan,
        default_stage_key: str | None = None,
        transition_actor_id: str | None = None,
    ) -> dict[str, Any] | None:
        execution_result: dict[str, Any] | None = None
        if getattr(action_plan, "cancel_pending_stage_reviews", False):
            pending_reviews = session.scalars(
                select(ApprovalRequest).where(
                    ApprovalRequest.workflow_id == workflow.id,
                    ApprovalRequest.target_kind == "workflow_stage_review",
                    ApprovalRequest.status == "pending",
                )
            ).all()
            for item in pending_reviews:
                item.status = "superseded"
                item.resolved_at = now_utc()
                session.add(item)

        if getattr(action_plan, "reopen_assignments", False):
            assignments = session.scalars(
                select(WorkflowAssignment)
                .where(WorkflowAssignment.workflow_id == workflow.id)
                .order_by(WorkflowAssignment.updated_at.desc())
            ).all()
            for assignment in assignments:
                if assignment.agent_id == workflow.secretary_id:
                    continue
                assignment.status = "pending"
                session.add(assignment)
                self._clear_assignment_activity(workflow, assignment.id)

        if action_plan.transition_message:
            self._append_message(
                session,
                conversation,
                transition_actor_id or workflow.secretary_id or workflow.owner_id,
                "system",
                action_plan.transition_message,
                metadata={
                    "workflowId": workflow.id,
                    "stageKey": default_stage_key,
                    "role": action_plan.transition_role or "workflow-transition",
                },
            )

        if (
            default_stage_key == "execution"
            and (getattr(action_plan, "reopen_assignments", False) or action_plan.trigger_execution_simulation)
            and workflow.status != "completed"
        ):
            workflow.status = "active"
            session.add(workflow)

        if action_plan.trigger_execution_simulation:
            execution_result = await self._simulate_execution_stage(session, workflow=workflow, conversation=conversation)

        if action_plan.generate_review_summary_stage_key:
            await self._generate_review_summary(
                session,
                workflow=workflow,
                conversation=conversation,
                stage_key=action_plan.generate_review_summary_stage_key,
            )

        await self._apply_review_requests(
            session,
            workflow=workflow,
            requests=action_plan.review_requests,
        )

        if action_plan.publish_completion_post:
            self._record_memory(
                session,
                actor_id=workflow.secretary_id or workflow.owner_id,
                conversation_id=conversation.id,
                source_kind="workflow.complete",
                text=f"{workflow.title} 已完成，当前任务归档并准备同步后续感想。",
            )
            await self._publish_workflow_wrapup_post(session, workflow=workflow)

        helper_actor_id = getattr(action_plan, "helper_target_actor_id", None)
        if helper_actor_id:
            target_actor = session.get(Actor, helper_actor_id)
            if target_actor is not None:
                await self._admit_workflow_helper(
                    session,
                    workflow=workflow,
                    conversation=conversation,
                    target_actor=target_actor,
                    summary=getattr(action_plan, "helper_summary", None),
                    join_role=getattr(action_plan, "helper_join_role", "observer"),
                    auto_reply=bool(getattr(action_plan, "helper_auto_reply", False)),
                )
        return execution_result

    def _pending_stage_review_requests(
        self,
        session: Session,
        workflow_id: str,
        stage_key: str,
        review_role: str | None = None,
    ) -> list[ApprovalRequest]:
        approvals = session.scalars(
            select(ApprovalRequest).where(
                ApprovalRequest.workflow_id == workflow_id,
                ApprovalRequest.target_kind == "workflow_stage_review",
                ApprovalRequest.status == "pending",
            )
        ).all()
        matched = []
        for approval in approvals:
            payload = approval.payload_json or {}
            if payload.get("stageKey") != stage_key:
                continue
            if review_role and payload.get("reviewRole") != review_role:
                continue
            matched.append(approval)
        return matched

    async def run_workflow_tick(self, session: Session, workflow_id: str | None = None) -> dict[str, Any] | None:
        if workflow_id:
            workflows = [session.get(Workflow, workflow_id)]
        else:
            workflows = session.scalars(
                select(Workflow)
                .where(Workflow.status.in_(["active", "needs_revision"]))
                .order_by(Workflow.updated_at.asc())
            ).all()

        for workflow in workflows:
            if workflow is None:
                continue
            orchestration = (workflow.context_json or {}).get("orchestration") or {}
            action_plan = self.bundle.workflow.plan_runtime_tick(
                current_stage_key=orchestration.get("currentStageKey"),
                workflow_status=workflow.status,
            )
            if not action_plan.trigger_execution_simulation:
                continue
            conversation = session.get(Conversation, workflow.conversation_id) if workflow.conversation_id else None
            if conversation is None:
                continue
            result = await self._execute_workflow_action_plan(
                session,
                workflow=workflow,
                conversation=conversation,
                action_plan=action_plan,
                default_stage_key=orchestration.get("currentStageKey"),
                transition_actor_id=workflow.secretary_id or workflow.owner_id,
            )
            if result is not None:
                return result
        return None

    async def _generate_secretary_plan_message(
        self,
        session: Session,
        *,
        workflow: Workflow,
        conversation: Conversation,
        secretary: Actor,
        blueprint,
    ) -> Message:
        selection = self._select_skill_execution(
            session,
            actor=secretary,
            mode="swarm",
            prompt=workflow.description,
            conversation_kind="group",
            workflow_role="secretary",
            source_kind="planning",
        )
        reply, used_tool_names = await self._generate_reply_with_tools_v2(
            None,
            session=session, actor_id=secretary.id, workflow_id=workflow.id,
            tool_allowlist=selection.tool_allowlist,
            settings=self.serialize_settings(self.get_workspace(session)),
            agent=self.serialize_actor(secretary, self._actor_skill_rows(session, secretary.id)),
            prompt=(
                f"当前任务：{workflow.description}\n"
                f"已选成员：{'、'.join(blueprint.team_actor_ids)}\n"
                "请以秘书智能体身份向群聊同步初步组队思路、分工原则、阶段安排和审查方式。"
            ),
            mode="swarm",
            conversation_kind="group",
            recent_messages=[],
            memory_snippets=self._recent_memory_summaries(session, secretary.id),
            skill_context=selection.prompt_patch,
            instructions_override="你是秘书智能体，当前需要在任务群聊里说明组队思路、阶段安排、审查方式和当前需要用户确认的地方。",
        )
        message = self._append_message(
            session,
            conversation,
            secretary.id,
            "task",
            reply,
            metadata={"workflowId": workflow.id, "stageKey": "planning", "role": "secretary-plan"},
        )
        skill_run = self._record_skill_run(
            session,
            actor=secretary,
            selection=selection,
            mode="swarm",
            source_kind="planning",
            input_summary=workflow.description,
            output_summary=reply,
            workflow_id=workflow.id,
            stage_id=self._stage_by_key(session, workflow.id, "planning").id if self._stage_by_key(session, workflow.id, "planning") else None,
            conversation_id=conversation.id,
            message_id=message.id,
            used_tool_names=used_tool_names,
            extra={"role": "secretary"},
        )
        message.metadata_json = {
            **(message.metadata_json or {}),
            "skillRunId": skill_run.id,
            "skillId": selection.primary_skill_id,
            "skillLabel": selection.primary_skill_name,
            "skillStack": selection.selected_skills,
        }
        session.add(message)
        return message

    async def _simulate_execution_stage(self, session: Session, *, workflow: Workflow, conversation: Conversation) -> dict[str, Any] | None:
        execution_stage = self._stage_by_key(session, workflow.id, "execution")
        if execution_stage is None or execution_stage.status != "in_progress":
            return None
        if self._pending_stage_review_requests(session, workflow.id, "execution"):
            return None

        assignments = session.scalars(
            select(WorkflowAssignment).where(WorkflowAssignment.workflow_id == workflow.id).order_by(WorkflowAssignment.created_at.asc())
        ).all()
        if not assignments:
            return None

        if all(item.status == "completed" for item in assignments):
            created_skill_runs: list[dict[str, Any]] = []
            actor_ids: list[str] = []
            if len(assignments) > 1:
                teacher_run = session.scalar(
                    select(SkillRun)
                    .where(
                        SkillRun.workflow_id == workflow.id,
                        SkillRun.assignment_id == assignments[0].id,
                    )
                    .order_by(SkillRun.created_at.desc())
                )
                for assignment in assignments[1:]:
                    learner = session.get(Actor, assignment.agent_id)
                    if learner is not None and teacher_run is not None:
                        learned_skill = self._maybe_seed_learned_skill(
                            session,
                            learner=learner,
                            teacher=session.get(Actor, teacher_run.actor_id),
                            teacher_skill_run=teacher_run,
                        )
                        if learned_skill is not None:
                            self._push_workflow_event(
                                workflow,
                                event_type="learned-skill-seeded",
                                stage_key="execution",
                                summary=learned_skill.name,
                                extra={"actorId": learner.id, "skillId": learned_skill.skill_id},
                            )

            review_message, review_skill_run = await self._generate_review_summary(
                session,
                workflow=workflow,
                conversation=conversation,
                stage_key="execution",
            )
            approval = self._create_stage_review_request(
                session,
                workflow=workflow,
                stage=execution_stage,
                reviewer_actor_id=workflow.secretary_id,
                review_role="secretary",
                title="Execution stage pending secretary review",
                summary="All assignees have completed their execution updates.",
                requires_user=False,
                requires_secretary=True,
            )
            await self._auto_approve_stage_review_request(
                session,
                workflow=workflow,
                approval=approval,
            )
            self._push_workflow_event(
                workflow,
                event_type="execution-awaiting-user-review",
                stage_key="execution",
                summary="Secretary review is complete and awaiting user confirmation.",
            )
            session.add(workflow)
            session.flush()
            if review_skill_run is not None:
                created_skill_runs.append(self.serialize_skill_run(review_skill_run))
            if workflow.secretary_id:
                actor_ids.append(workflow.secretary_id)
            return {
                "snapshot": self.build_snapshot(session),
                "workflowId": workflow.id,
                "conversationId": conversation.id,
                "stageKey": "execution",
                "messageCreated": review_message is not None,
                "reviewRequested": True,
                "skillRuns": created_skill_runs,
                "actorIds": actor_ids,
            }

        now = now_utc()
        in_progress_assignments = [item for item in assignments if item.status == "in_progress"]
        if not in_progress_assignments:
            retry_ready = [
                item for item in assignments
                if item.status == "waiting_retry" and self._assignment_retry_due(workflow, item.id, now=now)
            ]
            for assignment in retry_ready[:EXECUTION_LLM_WINDOW]:
                assignment.status = "in_progress"
                session.add(assignment)
                attempts = self._assignment_attempts(workflow, assignment.id)
                self._set_assignment_activity(
                    workflow,
                    assignment,
                    state="thinking",
                    label=f"正在继续：{preview_text(assignment.summary or workflow.title, 28)}",
                    detail="上一次请求被限速或暂时失败，正在重新排队。",
                    attempts=attempts,
                )
            if retry_ready:
                session.add(workflow)
                session.flush()
                return {
                    "snapshot": self.build_snapshot(session),
                    "workflowId": workflow.id,
                    "conversationId": conversation.id,
                    "stageKey": "execution",
                    "messageCreated": False,
                    "reviewRequested": False,
                    "skillRuns": [],
                    "actorIds": [item.agent_id for item in retry_ready[:EXECUTION_LLM_WINDOW]],
                    "activityUpdated": True,
                }

            execution_batch = self._select_execution_batch(workflow, assignments, runnable_statuses={"pending"})
            if not execution_batch:
                return None
            started = execution_batch[:EXECUTION_LLM_WINDOW]
            for assignment in started:
                assignment.status = "in_progress"
                session.add(assignment)
                self._set_assignment_activity(
                    workflow,
                    assignment,
                    state="thinking",
                    label=self._assignment_live_label(assignment),
                    detail="已接手子任务，正在整理信息与思路。",
                    attempts=max(1, self._assignment_attempts(workflow, assignment.id)),
                )
                self._push_workflow_event(
                    workflow,
                    event_type="assignment-started",
                    stage_key="execution",
                    summary=assignment.summary,
                    extra={"assignmentId": assignment.id, "agentId": assignment.agent_id},
                )
            session.add(workflow)
            session.flush()
            return {
                "snapshot": self.build_snapshot(session),
                "workflowId": workflow.id,
                "conversationId": conversation.id,
                "stageKey": "execution",
                "messageCreated": False,
                "reviewRequested": False,
                "skillRuns": [],
                "actorIds": [item.agent_id for item in started],
                "activityUpdated": True,
            }

        workspace_payload = self.serialize_settings(self.get_workspace(session))
        llm_enabled = bool(str(workspace_payload.get("llmApiKey") or "").strip())
        prepared_runs: list[dict[str, Any]] = []
        for assignment in in_progress_assignments[:EXECUTION_COMPLETION_BATCH]:
            agent = session.get(Actor, assignment.agent_id)
            if agent is None:
                assignment.status = "completed"
                session.add(assignment)
                self._clear_assignment_activity(workflow, assignment.id)
                continue
            selection = self._select_skill_execution(
                session,
                actor=agent,
                mode="swarm" if workflow.mode == "swarm" else "task",
                prompt=assignment.summary or workflow.description,
                conversation_kind="group",
                source_kind="execution",
            )
            prepared_runs.append(
                {
                    "assignment": assignment,
                    "agent": agent,
                    "selection": selection,
                    "agentPayload": self.serialize_actor(agent, self._actor_skill_rows(session, agent.id)),
                    "memorySnippets": self._recent_memory_summaries(session, agent.id),
                }
            )

        created_skill_runs: list[dict[str, Any]] = []
        actor_ids: list[str] = []
        message_created = False
        activity_updated = False

        for item in prepared_runs:
            assignment = item["assignment"]
            agent = item["agent"]
            selection = item["selection"]
            attempts = max(1, self._assignment_attempts(workflow, assignment.id))
            try:
                body, used_tool_names = await self._generate_reply_with_tools_v2(
                    None,
                    session=session, actor_id=agent.id, workflow_id=workflow.id, assignment_id=assignment.id,
                    tool_allowlist=selection.tool_allowlist,
                    settings=workspace_payload,
                    agent=item["agentPayload"],
                    prompt=(
                        f"当前总任务：{workflow.description}\n"
                        f"你当前负责：{assignment.summary}\n"
                        "请用符合人设的简短口吻，在群里同步你此刻已确认的内容、正在推进的重点，或下一步打算。"
                    ),
                    mode="swarm" if workflow.mode == "swarm" else "task",
                    conversation_kind="group",
                    recent_messages=[],
                    memory_snippets=item["memorySnippets"],
                    skill_context=selection.prompt_patch,
                )
                fallback_body = self.bundle.runtime._fallback_reply(
                    agent=item["agentPayload"],
                    prompt=(
                        f"当前总任务：{workflow.description}\n"
                        f"你当前负责：{assignment.summary}\n"
                        "请用符合人设的简短口吻，在群里同步你此刻已确认的内容、正在推进的重点，或下一步打算。"
                    ),
                    mode="swarm" if workflow.mode == "swarm" else "task",
                )
                normalized_body = str(body or "").strip()
                if llm_enabled and normalized_body == fallback_body:
                    next_retry_at = now_utc() + timedelta(seconds=ASSIGNMENT_RETRY_DELAY_SECONDS)
                    assignment.status = "waiting_retry"
                    session.add(assignment)
                    self._set_assignment_activity(
                        workflow,
                        assignment,
                        state="waiting_retry",
                        label="模型响应较慢，稍后继续",
                        detail="当前保留进行中状态，等待下一轮重试。",
                        next_retry_at=next_retry_at,
                        attempts=attempts + 1,
                    )
                    self._push_workflow_event(
                        workflow,
                        event_type="assignment-waiting-retry",
                        stage_key="execution",
                        summary=assignment.summary,
                        extra={"assignmentId": assignment.id, "agentId": assignment.agent_id},
                    )
                    activity_updated = True
                    continue
                message = self._append_message(
                    session,
                    conversation,
                    agent.id,
                    "task",
                    normalized_body or f"{agent.name} 正在继续推进这一项，我会很快补上下一轮结果。",
                    metadata={"workflowId": workflow.id, "stageKey": "execution", "assignmentId": assignment.id},
                )
                skill_run = self._record_skill_run(
                    session,
                    actor=agent,
                    selection=selection,
                    mode=workflow.mode,
                    source_kind="execution",
                    input_summary=assignment.summary or workflow.description,
                    output_summary=normalized_body or assignment.summary or workflow.description,
                    workflow_id=workflow.id,
                    stage_id=execution_stage.id,
                    assignment_id=assignment.id,
                    conversation_id=conversation.id,
                    message_id=message.id,
                    used_tool_names=used_tool_names,
                    extra={"workflowRole": "executor"},
                )
                message.metadata_json = {
                    **(message.metadata_json or {}),
                    "skillRunId": skill_run.id,
                    "skillId": selection.primary_skill_id,
                    "skillLabel": selection.primary_skill_name,
                    "skillStack": selection.selected_skills,
                }
                session.add(message)
                assignment.status = "completed"
                session.add(assignment)
                self._clear_assignment_activity(workflow, assignment.id)
                self._push_workflow_event(
                    workflow,
                    event_type="assignment-completed",
                    stage_key="execution",
                    summary=assignment.summary,
                    extra={"assignmentId": assignment.id, "agentId": assignment.agent_id, "skillId": selection.primary_skill_id},
                )
                self._record_memory(session, actor_id=agent.id, conversation_id=conversation.id, source_kind="workflow.execution", text=normalized_body)
                created_skill_runs.append(self.serialize_skill_run(skill_run))
                actor_ids.append(agent.id)
                message_created = True
            except Exception:
                next_retry_at = now_utc() + timedelta(seconds=ASSIGNMENT_RETRY_DELAY_SECONDS)
                assignment.status = "waiting_retry"
                session.add(assignment)
                self._set_assignment_activity(
                    workflow,
                    assignment,
                    state="waiting_retry",
                    label="暂时没有拿到结果，稍后重试",
                    detail="这名成员还在处理当前子任务。",
                    next_retry_at=next_retry_at,
                    attempts=attempts + 1,
                )
                self._push_workflow_event(
                    workflow,
                    event_type="assignment-waiting-retry",
                    stage_key="execution",
                    summary=assignment.summary,
                    extra={"assignmentId": assignment.id, "agentId": assignment.agent_id},
                )
                activity_updated = True

        session.add(workflow)
        session.flush()
        return {
            "snapshot": self.build_snapshot(session),
            "workflowId": workflow.id,
            "conversationId": conversation.id,
            "stageKey": "execution",
            "messageCreated": message_created,
            "reviewRequested": False,
            "skillRuns": created_skill_runs,
            "actorIds": list(dict.fromkeys(actor_ids)),
            "activityUpdated": activity_updated,
        }
    async def _generate_review_summary(
        self,
        session: Session,
        *,
        workflow: Workflow,
        conversation: Conversation,
        stage_key: str,
    ) -> tuple[Message | None, SkillRun | None]:
        secretary = session.get(Actor, workflow.secretary_id) if workflow.secretary_id else None
        if secretary is None:
            return None, None
        selection = self._select_skill_execution(
            session,
            actor=secretary,
            mode="review",
            prompt=workflow.description,
            conversation_kind="group",
            workflow_role="secretary",
            source_kind="review",
        )
        stage = self._stage_by_key(session, workflow.id, stage_key)
        assignment_summaries = [
            assignment.summary
            for assignment in session.scalars(select(WorkflowAssignment).where(WorkflowAssignment.workflow_id == workflow.id)).all()
        ]
        body, used_tool_names = await self._generate_reply_with_tools_v2(
            None,
            session=session, actor_id=secretary.id, workflow_id=workflow.id,
            tool_allowlist=selection.tool_allowlist,
            settings=self.serialize_settings(self.get_workspace(session)),
            agent=self.serialize_actor(secretary, self._actor_skill_rows(session, secretary.id)),
            prompt=(
                f"当前任务：{workflow.description}\n"
                f"阶段：{stage_key}\n"
                f"分工摘要：{'；'.join(assignment_summaries[:4])}\n"
                "请以秘书身份输出当前阶段审查意见，说明是否通过、风险是否收口、下一步是什么。"
            ),
            mode="task",
            conversation_kind="group",
            recent_messages=[],
            memory_snippets=self._recent_memory_summaries(session, secretary.id),
            skill_context=selection.prompt_patch,
            instructions_override="你是秘书智能体，当前要在任务群里给出阶段审查意见，既要清晰，也要保持角色化表达。",
        )
        message = self._append_message(
            session,
            conversation,
            secretary.id,
            "task",
            body,
            metadata={"workflowId": workflow.id, "stageKey": stage_key, "role": "review"},
        )
        skill_run = self._record_skill_run(
            session,
            actor=secretary,
            selection=selection,
            mode="review",
            source_kind="review",
            input_summary=workflow.description,
            output_summary=body,
            workflow_id=workflow.id,
            stage_id=stage.id if stage else None,
            conversation_id=conversation.id,
            message_id=message.id,
            used_tool_names=used_tool_names,
            extra={"stageKey": stage_key},
        )
        message.metadata_json = {
            **(message.metadata_json or {}),
            "skillRunId": skill_run.id,
            "skillId": selection.primary_skill_id,
            "skillLabel": selection.primary_skill_name,
            "skillStack": selection.selected_skills,
        }
        session.add(message)
        self._push_workflow_event(
            workflow,
            event_type="review-summary-generated",
            stage_key=stage_key,
            summary=preview_text(body, 120),
            extra={"messageId": message.id, "skillRunId": skill_run.id},
        )
        return message, skill_run

    async def _advance_workflow_after_stage_approval(
        self,
        session: Session,
        *,
        workflow: Workflow,
        approval: ApprovalRequest,
    ) -> None:
        await self._resolve_workflow_stage_review(
            session,
            workflow=workflow,
            approval=approval,
            decision="approve",
            reviewer_actor_id=approval.reviewer_actor_id,
        )

    def _ensure_workflow_stub(
        self,
        session: Session,
        conversation: Conversation,
        body: str,
        mode: str,
        actor_ids: list[str] | None = None,
    ) -> Workflow:
        workflow = Workflow(
            id=f"wf-{uuid4().hex[:12]}",
            title=preview_text(body, 40),
            description=body,
            status="active",
            owner_id=DEFAULT_USER["id"],
            secretary_id=self.get_workspace(session).secretary_agent_id or None,
            conversation_id=conversation.id,
            mode=mode,
            context_json={"originConversationId": conversation.id},
        )
        session.add(workflow)
        session.flush()
        stages = [
            ("planning", "规划与组队"),
            ("execution", "执行与同步"),
            ("review", "审查与提交"),
        ]
        for position, (key, title) in enumerate(stages, start=1):
            session.add(
                WorkflowStage(
                    id=f"stage-{uuid4().hex[:12]}",
                    workflow_id=workflow.id,
                    key=key,
                    title=title,
                    status="in_progress" if position == 1 else "pending",
                    position=position,
                    requires_secretary_review=True,
                    requires_user_review=position == len(stages),
                )
            )
        target_agent_ids = actor_ids or [
            member.actor_id
            for member in session.scalars(
                select(ConversationMember).where(ConversationMember.conversation_id == conversation.id)
            ).all()
            if member.actor_id != DEFAULT_USER["id"]
        ]
        for agent_id in target_agent_ids:
            session.add(
                WorkflowAssignment(
                    id=f"assign-{uuid4().hex[:12]}",
                    workflow_id=workflow.id,
                    agent_id=agent_id,
                    summary=preview_text(body, 80),
                    status="pending",
                    dependency_ids=[],
                    allow_parallel=True,
                )
            )
        return workflow

    def _ensure_solo_task_workflow(
        self,
        session: Session,
        conversation: Conversation,
        body: str,
        actor_id: str,
    ) -> Workflow:
        workflow = self._ensure_workflow_stub(session, conversation, body, "task", [actor_id])
        workflow.title = preview_text(body, 32)
        workflow.description = body
        workflow.status = "active"
        workflow.context_json = {
            **(workflow.context_json or {}),
            "originConversationId": conversation.id,
            "soloTask": True,
            "teamActorIds": [actor_id],
            "orchestration": {
                **((workflow.context_json or {}).get("orchestration") or {}),
                "engine": "builtin",
                "currentStageKey": "execution",
                "nextReviewRole": "secretary",
                "workflowStatus": "active",
                "transitionKind": "solo-task-created",
            },
        }
        planning_stage = self._stage_by_key(session, workflow.id, "planning")
        execution_stage = self._stage_by_key(session, workflow.id, "execution")
        review_stage = self._stage_by_key(session, workflow.id, "review")
        if planning_stage is not None:
            planning_stage.status = "completed"
            session.add(planning_stage)
        if execution_stage is not None:
            execution_stage.status = "in_progress"
            session.add(execution_stage)
        if review_stage is not None:
            review_stage.status = "pending"
            session.add(review_stage)
        assignment = self._assignment_for_agent(session, workflow.id, actor_id)
        if assignment is not None:
            assignment.stage_id = execution_stage.id if execution_stage is not None else assignment.stage_id
            assignment.summary = f"独立完成：{preview_text(body, 80)}"
            assignment.status = "in_progress"
            session.add(assignment)
        self._push_workflow_event(
            workflow,
            event_type="solo-task-created",
            stage_key="execution",
            summary=f"当前任务先由 {actor_id} 单独推进。",
            extra={"teamActorIds": [actor_id]},
        )
        session.add(workflow)
        session.flush()
        return workflow

    def _default_background_for(self, faction: str) -> str:
        mapping = {
            "重樱": "sakura-net",
            "皇家": "harbor-lounge",
            "北方联合": "polar-bloom",
            "铁血": "signal-blue",
            "白鹰": "signal-blue",
            "撒丁帝国": "harbor-lounge",
        }
        return mapping.get(faction, "signal-blue")







