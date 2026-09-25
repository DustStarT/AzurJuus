from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import uuid4

from sqlalchemy import delete, select, update
from sqlalchemy.orm import Session

from tools.blhx_character_import import resolve_personas

from backend.platform.constants import APP_META, DEFAULT_CHARACTERS, DEFAULT_SETTINGS, DEFAULT_USER, WALLPAPERS
from backend.tasks.llm_runtime import AgentRuntime
from backend.credentials import protect, reveal, public_settings
from backend.mind.memory import MemoryStore
from backend.chat.message_order import message_order
from backend.models import (
    Actor,
    ActorSkill,
    AgentRelationship,
    ApprovalRequest,
    Conversation,
    ConversationMember,
    MemoryChunk,
    Message,
    ModelConnection,
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
from backend.tasks.skill_runtime import SkillRuntime
from backend.social.social_runtime import SocialRuntime
from backend.tasks.tool_gateway import ToolGateway
from backend.platform.service_utils import (
    default_ui_session, isoformat, now_utc, preview_text,
)


from backend.characters.service import CharacterService
from backend.chat.service import ChatService
from backend.social.service import SocialService
from backend.tasks.service import TaskService

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


class AzurJuusService(CharacterService, ChatService, SocialService, TaskService):
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
                from backend.characters.relationship_research import RESEARCH_VERSION
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
        from backend.characters.character_identity import roster_actors
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
            from backend.tasks.model_connections import remember
            if workspace.llm_api_key:
                remember(session,workspace)
            workspace.resolution_preset = str(settings.get("resolutionPreset") or workspace.resolution_preset)
            workspace.max_connected_agents = int(settings.get("maxConnectedAgents") or workspace.max_connected_agents)
            workspace.connected_agent_ids = [str(item) for item in settings.get("connectedAgentIds") or workspace.connected_agent_ids]
            workspace.character_roster_text = str(settings.get("characterRosterText") or workspace.character_roster_text)
            workspace.llm_provider = str(settings.get("llmProvider") or workspace.llm_provider)
            workspace.llm_model = str(settings.get("llmModel") or workspace.llm_model)
            workspace.llm_base_url = str(settings.get("llmBaseUrl") or workspace.llm_base_url)
            if not settings.get("llmApiKey") and not settings.get("clearLlmApiKey"):
                from backend.tasks.model_connections import connection_id, normalize
                try:
                    target=session.get(ModelConnection,connection_id(*normalize(workspace.llm_base_url,workspace.llm_model)))
                except ValueError:
                    target=None
                if target and target.api_key:
                    workspace.llm_api_key=target.api_key
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
            remember(session,workspace)
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
        from backend.characters.character_identity import roster_actors
        agents=sorted(roster_actors(session),key=lambda a:a.name)
        active_agent_ids=[a.id for a in agents]
        allowed_ids={user.id,*active_agent_ids}
        from backend.characters.character_identity import references_hidden
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

        posts = [post for post in session.scalars(
            select(SocialPost).where(SocialPost.author_id.in_([user.id,*active_agent_ids]))
                .order_by(SocialPost.created_at.desc()).limit(100)).all()
            if self.post_visible(session,post,user.id)][:30]
        from backend.social.social_models import SocialDeferredReply
        deferred = session.scalars(select(SocialDeferredReply).where(
            SocialDeferredReply.status == 'pending',
            SocialDeferredReply.actor_id.in_(active_agent_ids))).all()
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
            "posts": [self.serialize_post(session, post) for post in posts],
            "pendingReplies": [{'actorId': row.actor_id, 'conversationId': row.conversation_id,
                'sourceMessageId': row.source_message_id} for row in deferred],
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
        from backend.social.social_models import SocialPublication, SocialReaction
        from backend.characters.character_identity import roster_actors
        allowed_ids={'commander',*(a.id for a in roster_actors(session))}
        publication=session.get(SocialPublication,post.id)
        audience=(publication.data.get('audience') if publication else None) or {'kind':'observer','actorIds':[]}
        reactions=session.scalars(select(SocialReaction).where(SocialReaction.post_id==post.id)).all()
        comments = session.scalars(
            select(SocialComment).where(SocialComment.post_id == post.id).order_by(SocialComment.created_at.asc())
        ).all()
        return {
            "id": post.id,
            "authorId": post.author_id,
            "excerpt": post.excerpt,
            "body": publication.data.get('body',post.excerpt) if publication else post.excerpt,
            "audience": audience,
            "sourceKind": publication.data.get('sourceKind','legacy') if publication else 'legacy',
            "legacyArchived": publication is None,
            "likes": len(reactions) if publication else post.likes,
            "likedByUser": any(r.actor_id=='commander' for r in reactions) if publication else post.liked_by_user,
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
                ModelConnection,
                WorkspaceSetting,
            ]

            from backend.mind.cognition_models import MindState, Experience, MindReceipt, MindCursor, MindOutbox
            deletion_order = [MindOutbox, MindReceipt, Experience, MindState, MindCursor, *deletion_order]
            from backend.mind.mind_models import MindProfile, MindDecision, PersonalGoal, LifeActivity, LifeRecord, MindControl, MindCall
            from backend.social.social_models import (SocialTopic, SocialAction,
                SocialDeferredReply, SocialPublication, SocialReaction, SocialShareConsent)
            deletion_order = [MindCall, MindControl, LifeRecord, LifeActivity, PersonalGoal, MindDecision, MindProfile,
                SocialShareConsent, SocialReaction, SocialPublication, SocialDeferredReply,
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





















    def _recent_memory_summaries(self, session: Session, actor_id: str, limit: int = 4) -> list[str]:
        chunks = session.scalars(
            select(MemoryChunk).where(MemoryChunk.actor_id == actor_id).order_by(MemoryChunk.created_at.desc())
        ).all()
        return [chunk.summary for chunk in chunks[:limit] if chunk.summary]




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
