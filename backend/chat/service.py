from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.orm import Session


from backend.models import (
    Actor,
    Conversation,
    ConversationMember,
    Message,
)
from backend.platform.service_utils import (
    preview_text,
)


class ChatService:
    """Chat operations shared by the application service."""

    def open_conversation(self, session: Session, conversation_id: str) -> dict[str, Any]:
        conversation = session.get(Conversation, conversation_id)
        if conversation is not None:
            conversation.unread_count = 0
            conversation.replied = True
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
                title="港区群聊",
                faction="港区日常",
                preview="",
                unread_count=0,
                replied=False,
                background_id="polar-bloom",
                bookmarked=True,
                announcement="港区成员日常聊天的地方。具体委托在任务或协作模式中处理。",
            )
            session.add(conversation)
            session.flush()
        else:
            # Migrate each former default independently; user edits stay theirs.
            if conversation.title == '港区协作频道':
                conversation.title = '港区群聊'
            if conversation.announcement == '秘书智能体会在这里协调多智能体任务、同步阶段进度和发起审批。':
                conversation.announcement = '港区成员日常聊天的地方。具体委托在任务或协作模式中处理。'
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
