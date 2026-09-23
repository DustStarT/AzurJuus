from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, JSON, String, Text, UniqueConstraint, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    pass


class TimestampMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=lambda:datetime.now(UTC), server_default=func.now(), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class Actor(TimestampMixin, Base):
    __tablename__ = "actors"

    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    kind: Mapped[str] = mapped_column(String(24), nullable=False)
    source_character: Mapped[str | None] = mapped_column(String(120))
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    handle: Mapped[str] = mapped_column(String(120), nullable=False, unique=True)
    english_name: Mapped[str | None] = mapped_column(String(120))
    faction: Mapped[str] = mapped_column(String(120), default="未知阵营", nullable=False)
    status: Mapped[str] = mapped_column(String(40), default="在线", nullable=False)
    initials: Mapped[str] = mapped_column(String(12), default="AZ", nullable=False)
    palette: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    accent: Mapped[str | None] = mapped_column(String(40))
    tone: Mapped[str | None] = mapped_column(Text)
    persona: Mapped[str | None] = mapped_column(Text)
    summary: Mapped[str | None] = mapped_column(Text)
    keywords: Mapped[str | None] = mapped_column(Text)
    capabilities: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    tools: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    favorite: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    avatar_url: Mapped[str | None] = mapped_column(Text)
    illustration_url: Mapped[str | None] = mapped_column(Text)
    system_prompt: Mapped[str | None] = mapped_column(Text)
    character_url: Mapped[str | None] = mapped_column(Text)
    extra_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class Conversation(TimestampMixin, Base):
    __tablename__ = "conversations"

    id: Mapped[str] = mapped_column(String(120), primary_key=True)
    kind: Mapped[str] = mapped_column(String(24), nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    faction: Mapped[str] = mapped_column(String(120), default="联合频道", nullable=False)
    preview: Mapped[str] = mapped_column(Text, default="", nullable=False)
    unread_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    replied: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    background_id: Mapped[str] = mapped_column(String(80), default="signal-blue", nullable=False)
    bookmarked: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    announcement: Mapped[str] = mapped_column(Text, default="", nullable=False)
    workflow_id: Mapped[str | None] = mapped_column(String(120), ForeignKey("workflows.id"))
    extra_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)

    members: Mapped[list["ConversationMember"]] = relationship(back_populates="conversation", cascade="all, delete-orphan")
    messages: Mapped[list["Message"]] = relationship(back_populates="conversation", cascade="all, delete-orphan")


class ConversationMember(Base):
    __tablename__ = "conversation_members"
    __table_args__ = (UniqueConstraint("conversation_id", "actor_id", name="uq_conversation_member"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    conversation_id: Mapped[str] = mapped_column(String(120), ForeignKey("conversations.id"), nullable=False)
    actor_id: Mapped[str] = mapped_column(String(80), ForeignKey("actors.id"), nullable=False)
    role: Mapped[str] = mapped_column(String(32), default="member", nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    conversation: Mapped[Conversation] = relationship(back_populates="members")
    actor: Mapped[Actor] = relationship()


class Message(TimestampMixin, Base):
    __tablename__ = "messages"

    id: Mapped[str] = mapped_column(String(120), primary_key=True)
    conversation_id: Mapped[str] = mapped_column(String(120), ForeignKey("conversations.id"), nullable=False, index=True)
    speaker_id: Mapped[str] = mapped_column(String(80), ForeignKey("actors.id"), nullable=False)
    type: Mapped[str] = mapped_column(String(24), default="text", nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)

    conversation: Mapped[Conversation] = relationship(back_populates="messages")
    speaker: Mapped[Actor] = relationship()


class SocialPost(TimestampMixin, Base):
    __tablename__ = "social_posts"

    id: Mapped[str] = mapped_column(String(120), primary_key=True)
    author_id: Mapped[str] = mapped_column(String(80), ForeignKey("actors.id"), nullable=False)
    excerpt: Mapped[str] = mapped_column(Text, nullable=False)
    likes: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    liked_by_user: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    art_label: Mapped[str | None] = mapped_column(String(255))
    art_mark: Mapped[str | None] = mapped_column(String(64))
    art_palette: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    media_url: Mapped[str | None] = mapped_column(Text)
    following: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    author: Mapped[Actor] = relationship()
    comments: Mapped[list["SocialComment"]] = relationship(back_populates="post", cascade="all, delete-orphan")


class SocialComment(TimestampMixin, Base):
    __tablename__ = "social_comments"

    id: Mapped[str] = mapped_column(String(120), primary_key=True)
    post_id: Mapped[str] = mapped_column(String(120), ForeignKey("social_posts.id"), nullable=False, index=True)
    author_id: Mapped[str] = mapped_column(String(80), ForeignKey("actors.id"), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)

    post: Mapped[SocialPost] = relationship(back_populates="comments")
    author: Mapped[Actor] = relationship()


class WorkspaceSetting(TimestampMixin, Base):
    __tablename__ = "workspace_settings"

    id: Mapped[int] = mapped_column(primary_key=True, default=1)
    resolution_preset: Mapped[str] = mapped_column(String(32), default="balanced", nullable=False)
    max_connected_agents: Mapped[int] = mapped_column(Integer, default=5, nullable=False)
    connected_agent_ids: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    character_roster_text: Mapped[str] = mapped_column(Text, default="", nullable=False)
    llm_provider: Mapped[str] = mapped_column(String(120), default="OpenAI Compatible", nullable=False)
    llm_model: Mapped[str] = mapped_column(String(255), default="gpt-4.1-mini", nullable=False)
    llm_base_url: Mapped[str] = mapped_column(Text, default="https://api.openai.com/v1", nullable=False)
    llm_api_key: Mapped[str] = mapped_column(Text, default="", nullable=False)
    tool_profile: Mapped[str] = mapped_column(String(80), default="local-python", nullable=False)
    tool_base_url: Mapped[str] = mapped_column(Text, default="http://127.0.0.1:8000", nullable=False)
    tool_api_key: Mapped[str] = mapped_column(Text, default="", nullable=False)
    authorized_workspace_root: Mapped[str] = mapped_column(Text, default="", nullable=False)
    secretary_agent_id: Mapped[str] = mapped_column(String(80), default="", nullable=False)
    allow_idle_social: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    social_interval_minutes: Mapped[int] = mapped_column(Integer, default=60, nullable=False)
    ui_session_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)


class ModelConnection(Base):
    __tablename__ = "model_connections"

    id: Mapped[str] = mapped_column(String(64), primary_key=True)
    base_url: Mapped[str] = mapped_column(Text, nullable=False)
    model: Mapped[str] = mapped_column(String(255), nullable=False)
    api_key: Mapped[str] = mapped_column(Text, default="", nullable=False)
    last_used_at: Mapped[float] = mapped_column(Float, nullable=False)
    last_checked_at: Mapped[float | None] = mapped_column(Float)
    last_check: Mapped[str] = mapped_column(String(32), default="unchecked", nullable=False)


class Workflow(TimestampMixin, Base):
    __tablename__ = "workflows"

    id: Mapped[str] = mapped_column(String(120), primary_key=True)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    description: Mapped[str] = mapped_column(Text, default="", nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="draft", nullable=False)
    owner_id: Mapped[str] = mapped_column(String(80), ForeignKey("actors.id"), nullable=False)
    secretary_id: Mapped[str | None] = mapped_column(String(80), ForeignKey("actors.id"))
    conversation_id: Mapped[str | None] = mapped_column(String(120), ForeignKey("conversations.id"))
    mode: Mapped[str] = mapped_column(String(32), default="task", nullable=False)
    context_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)


class WorkflowStage(TimestampMixin, Base):
    __tablename__ = "workflow_stages"

    id: Mapped[str] = mapped_column(String(120), primary_key=True)
    workflow_id: Mapped[str] = mapped_column(String(120), ForeignKey("workflows.id"), nullable=False)
    key: Mapped[str] = mapped_column(String(64), nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="pending", nullable=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    requires_secretary_review: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    requires_user_review: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class WorkflowAssignment(TimestampMixin, Base):
    __tablename__ = "workflow_assignments"

    id: Mapped[str] = mapped_column(String(120), primary_key=True)
    workflow_id: Mapped[str] = mapped_column(String(120), ForeignKey("workflows.id"), nullable=False)
    stage_id: Mapped[str | None] = mapped_column(String(120), ForeignKey("workflow_stages.id"))
    agent_id: Mapped[str] = mapped_column(String(80), ForeignKey("actors.id"), nullable=False)
    summary: Mapped[str] = mapped_column(Text, default="", nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="pending", nullable=False)
    dependency_ids: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    allow_parallel: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)


class ApprovalRequest(TimestampMixin, Base):
    __tablename__ = "approval_requests"

    id: Mapped[str] = mapped_column(String(120), primary_key=True)
    workflow_id: Mapped[str | None] = mapped_column(String(120), ForeignKey("workflows.id"))
    tool_execution_id: Mapped[str | None] = mapped_column(String(120), ForeignKey("tool_execution_logs.id"))
    requested_by_actor_id: Mapped[str] = mapped_column(String(80), ForeignKey("actors.id"), nullable=False)
    reviewer_actor_id: Mapped[str | None] = mapped_column(String(80), ForeignKey("actors.id"))
    target_kind: Mapped[str] = mapped_column(String(64), nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    summary: Mapped[str] = mapped_column(Text, default="", nullable=False)
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="pending", nullable=False)
    requires_user: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    requires_secretary: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ToolExecutionLog(TimestampMixin, Base):
    __tablename__ = "tool_execution_logs"

    id: Mapped[str] = mapped_column(String(120), primary_key=True)
    actor_id: Mapped[str] = mapped_column(String(80), ForeignKey("actors.id"), nullable=False)
    tool_name: Mapped[str] = mapped_column(String(80), nullable=False)
    risk_level: Mapped[str] = mapped_column(String(32), default="low", nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="planned", nullable=False)
    request_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    result_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
    snapshot_ref: Mapped[str | None] = mapped_column(Text)
    undo_ref: Mapped[str | None] = mapped_column(Text)
    approved: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class MemoryChunk(TimestampMixin, Base):
    __tablename__ = "memory_chunks"

    id: Mapped[str] = mapped_column(String(120), primary_key=True)
    actor_id: Mapped[str | None] = mapped_column(String(80), ForeignKey("actors.id"))
    workflow_id: Mapped[str | None] = mapped_column(String(120), ForeignKey("workflows.id"))
    conversation_id: Mapped[str | None] = mapped_column(String(120), ForeignKey("conversations.id"))
    source_kind: Mapped[str] = mapped_column(String(64), nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    embedding_ref: Mapped[str | None] = mapped_column(String(255))
    metadata_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)


class AgentRelationship(TimestampMixin, Base):
    __tablename__ = "agent_relationships"
    __table_args__ = (UniqueConstraint("agent_id", "peer_agent_id", name="uq_agent_peer"),)

    id: Mapped[str] = mapped_column(String(120), primary_key=True)
    agent_id: Mapped[str] = mapped_column(String(80), ForeignKey("actors.id"), nullable=False)
    peer_agent_id: Mapped[str] = mapped_column(String(80), ForeignKey("actors.id"), nullable=False)
    affinity_score: Mapped[float] = mapped_column(default=0.5, nullable=False)
    trust_score: Mapped[float] = mapped_column(default=0.5, nullable=False)
    social_probability: Mapped[float] = mapped_column(default=0.5, nullable=False)
    notes_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)


class ActorSkill(TimestampMixin, Base):
    __tablename__ = "actor_skills"
    __table_args__ = (UniqueConstraint("actor_id", "skill_id", name="uq_actor_skill"),)

    id: Mapped[str] = mapped_column(String(120), primary_key=True)
    actor_id: Mapped[str] = mapped_column(String(80), ForeignKey("actors.id"), nullable=False)
    skill_id: Mapped[str] = mapped_column(String(120), nullable=False)
    name: Mapped[str] = mapped_column(String(120), nullable=False)
    visibility: Mapped[str] = mapped_column(String(24), default="private", nullable=False)
    origin: Mapped[str] = mapped_column(String(40), default="builtin", nullable=False)
    base_skill_id: Mapped[str | None] = mapped_column(String(120))
    summary: Mapped[str] = mapped_column(Text, default="", nullable=False)
    prompt_patch: Mapped[str] = mapped_column(Text, default="", nullable=False)
    mode_json: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    tool_allowlist_json: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    tags_json: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    teachable: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    learnable: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    confidence: Mapped[float] = mapped_column(default=0.55, nullable=False)
    is_enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    extra_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)


class SkillProposal(TimestampMixin, Base):
    __tablename__ = "skill_proposals"

    id: Mapped[str] = mapped_column(String(120), primary_key=True)
    actor_id: Mapped[str] = mapped_column(String(80), ForeignKey("actors.id"), nullable=False)
    approval_id: Mapped[str | None] = mapped_column(String(120))
    base_skill_id: Mapped[str] = mapped_column(String(120), nullable=False)
    base_skill_name: Mapped[str] = mapped_column(String(120), nullable=False)
    target_visibility: Mapped[str] = mapped_column(String(24), default="public", nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    summary: Mapped[str] = mapped_column(Text, default="", nullable=False)
    prompt_patch: Mapped[str] = mapped_column(Text, default="", nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="pending", nullable=False)
    reviewer_actor_id: Mapped[str | None] = mapped_column(String(80), ForeignKey("actors.id"))
    decision_note: Mapped[str | None] = mapped_column(Text)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    extra_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)


class SkillRun(TimestampMixin, Base):
    __tablename__ = "skill_runs"

    id: Mapped[str] = mapped_column(String(120), primary_key=True)
    actor_id: Mapped[str] = mapped_column(String(80), ForeignKey("actors.id"), nullable=False)
    workflow_id: Mapped[str | None] = mapped_column(String(120), ForeignKey("workflows.id"))
    stage_id: Mapped[str | None] = mapped_column(String(120), ForeignKey("workflow_stages.id"))
    assignment_id: Mapped[str | None] = mapped_column(String(120), ForeignKey("workflow_assignments.id"))
    conversation_id: Mapped[str | None] = mapped_column(String(120), ForeignKey("conversations.id"))
    message_id: Mapped[str | None] = mapped_column(String(120), ForeignKey("messages.id"))
    mode: Mapped[str] = mapped_column(String(32), default="chat", nullable=False)
    source_kind: Mapped[str] = mapped_column(String(64), default="message", nullable=False)
    status: Mapped[str] = mapped_column(String(32), default="completed", nullable=False)
    primary_skill_id: Mapped[str] = mapped_column(String(120), nullable=False)
    primary_skill_name: Mapped[str] = mapped_column(String(120), nullable=False)
    selected_skill_ids: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    selected_skill_names: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    input_summary: Mapped[str] = mapped_column(Text, default="", nullable=False)
    output_summary: Mapped[str] = mapped_column(Text, default="", nullable=False)
    used_tool_names: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    extra_json: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict, nullable=False)
