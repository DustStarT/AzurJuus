from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
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
from .message_order import message_order
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
from .social_runtime import SocialRuntime
from .tool_gateway import ToolGateway
from .workflow_view import build_graph_view


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
    skills: SkillRuntime
    runtime_context: dict[str, Any]


class AzurJuusService:
    def __init__(self, bundle: ServiceBundle):
        self.bundle = bundle

    def ensure_seed(self, session: Session) -> None:
        workspace = self.get_workspace(session)
        self.get_or_create_user(session)
        existing = session.scalars(select(Actor).where(Actor.kind == 'agent')).all()
        roster_names = {n.strip() for n in workspace.character_roster_text.splitlines() if n.strip()}
        excluded = []
        for actor in existing:
            if (actor.source_character or actor.name) not in roster_names:
                actor.is_active = False
                excluded.append(actor.id)
        if excluded:
            workspace.connected_agent_ids = [aid for aid in (workspace.connected_agent_ids or []) if aid not in excluded]
            if workspace.secretary_agent_id in excluded:
                workspace.secretary_agent_id = ''
            for member in session.scalars(select(ConversationMember).where(ConversationMember.actor_id.in_(excluded))).all():
                member.is_active = False
            session.flush()
        has_agent = session.scalar(select(Actor).where(Actor.kind == "agent", Actor.is_active.is_(True)).limit(1))
        if has_agent is None and not existing:
            personas, missing = resolve_personas(DEFAULT_CHARACTERS)
            if missing:
                raise RuntimeError(f"Missing default personas: {', '.join(missing)}")
            self.apply_personas(session, personas, DEFAULT_CHARACTERS, len(DEFAULT_CHARACTERS))
        else:
            for agent in session.scalars(select(Actor).where(Actor.kind == "agent", Actor.is_active.is_(True))).all():
                self._ensure_actor_signature_skill(session, agent)
                from .relationship_research import RESEARCH_VERSION
                research=(agent.extra_json or {}).get('wikiResearch',{})
                if research.get('version')!=RESEARCH_VERSION:
                    import time
                    agent.extra_json={**(agent.extra_json or {}),'wikiResearch':{'version':RESEARCH_VERSION,
                        'status':'pending','progress':5,'message':'等待补查官方剧情资料',
                        'queuedAt':time.time(),'sourceUrls':research.get('sourceUrls',[])}}
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
        from .character_identity import roster_actors
        return sorted(roster_actors(session),key=lambda a:a.name)

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
        from .character_identity import roster_actors
        agents=sorted(roster_actors(session),key=lambda a:a.name)
        active_agent_ids=[a.id for a in agents]
        allowed_ids={user.id,*active_agent_ids}
        from .character_identity import references_hidden
        hidden=set(session.scalars(select(Actor.id).where(Actor.kind=='agent')).all())-set(active_agent_ids)
        def shown(items):return [item for item in items if not references_hidden(item,hidden)]

        conversations = []
        for conversation in session.scalars(select(Conversation).order_by(Conversation.updated_at.desc())).all():
            members = session.scalars(
                select(ConversationMember).where(ConversationMember.conversation_id == conversation.id)
            ).all()
            member_ids = [member.actor_id for member in members if member.is_active]
            if not any(member_id in active_agent_ids for member_id in member_ids) or any(member_id not in allowed_ids for member_id in member_ids):
                continue
            conversations.append((conversation, members))

        posts = (
            session.scalars(
                select(SocialPost).where(SocialPost.author_id.in_(active_agent_ids)).order_by(SocialPost.created_at.desc())
            ).all()
            if active_agent_ids
            else []
        )
        from .idle_social import MOMENTS_ENABLED
        if not MOMENTS_ENABLED:posts=[]
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
                        select(Message).where(Message.conversation_id == conversation.id,Message.speaker_id.in_(allowed_ids)).order_by(*message_order(session))
                    ).all()
                ]
                for conversation, _members in conversations
            },
            "posts": [self.serialize_post(session, post) for post in posts
                if not any(post.author_id == a.id and post.excerpt == preview_text(' '.join((a.summary or a.persona or '').split()), 96) for a in agents)],
            "workflows": shown([self.serialize_workflow(session, workflow) for workflow in workflows]),
            "approvals": shown([self.serialize_approval(approval) for approval in approvals]),
            "toolExecutions": shown([self.serialize_tool_execution(execution) for execution in tool_logs]),
            "skillCatalog": self.effective_skill_catalog(session),
            "skillRuns": shown([self.serialize_skill_run(skill_run) for skill_run in skill_runs]),
            "skillProposals": shown([self.serialize_skill_proposal(skill_proposal) for skill_proposal in skill_proposals]),
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
            "socialOnly": bool((actor.extra_json or {}).get('socialOnly')),
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
            "sourceMaterials": (actor.extra_json or {}).get('sourceMaterials', []),
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
        from .character_identity import roster_actors
        allowed_ids={'commander',*(a.id for a in roster_actors(session))}
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
            "comments": [self.serialize_comment(comment) for comment in comments if comment.author_id in allowed_ids],
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

    def preview_personas(self, session: Session, names: list[str], participant_count: int) -> dict[str, Any]:
        if participant_count < 1 or participant_count > 24:
            raise ValueError("角色数量需要在 1–24 人之间。")
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
            if agent is not None:
                # Reconnecting does not replace user persona, artwork or state.
                if spec.get('sourceMaterials'):
                    agent.extra_json={**(agent.extra_json or {}),'sourceMaterials':spec['sourceMaterials']}
                agent.is_active = True
                session.add(agent)
                active_ids.append(agent.id)
                continue
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
            if spec.get('sourceMaterials'):
                agent.extra_json['sourceMaterials']=spec['sourceMaterials']
            # The expanded social roster has the same boundary through the old
            # settings importer as through the additive character directory.
            if source_name in {'阿贺野', '武藏', '英王乔治五世', '约克公爵', '贾维斯', '标枪', '七省'}:
                from .terminal_characters import card, render, TERMINAL
                base = card(source_name)
                agent.persona = agent.tone = agent.summary = base['style']
                agent.system_prompt = TERMINAL + render(base)
                agent.tools = []
                agent.capabilities = []
                agent.extra_json = {'aliases': spec.get('aliases') or [], 'socialOnly': True,
                    'terminalCard': {'version': base['version'], 'text': render(base)}}
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
            from .terminal_characters import card
            opening_card = card(agent.source_character or agent.name)
            greeting = opening_card['examples']['招呼'] if opening_card else '我在。'
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
            from .mind_models import MindProfile, MindDecision, PersonalGoal, LifeActivity, LifeRecord, MindControl, MindCall
            from .social_models import SocialTopic, SocialAction
            deletion_order = [MindCall, MindControl, LifeRecord, LifeActivity, PersonalGoal, MindDecision, MindProfile,
                SocialAction, SocialTopic, *deletion_order]

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

    def open_conversation(self, session: Session, conversation_id: str) -> dict[str, Any]:
        conversation = session.get(Conversation, conversation_id)
        if conversation is not None:
            conversation.unread_count = 0
            conversation.replied = True
            session.add(conversation)
            session.flush()
        return self.build_snapshot(session)


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
            created_at=datetime.now(UTC),
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
        # Connecting an account is not a personal post or a social experience.
        return

    def _sync_relationships(self, session: Session, agents: list[Actor]) -> None:
        for agent in agents:
            changed=False
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
                    changed=True
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
            if changed:
                import time
                from .relationship_research import RESEARCH_VERSION
                agent.extra_json={**(agent.extra_json or {}),'wikiResearch':{'version':RESEARCH_VERSION,'status':'pending','progress':5,
                    'message':'已加入后台资料队列','queuedAt':time.time(),'sourceUrls':[]}}

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
            raise ValueError('动态内容不能为空。')
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
