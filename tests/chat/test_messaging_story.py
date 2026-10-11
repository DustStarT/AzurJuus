"""The default port room behaves like an everyday messaging group."""
from sqlalchemy import select

from backend.database import session_scope
from backend.models import Actor, Conversation, Message


OLD_ANNOUNCEMENT='秘书智能体会在这里协调多智能体任务、同步阶段进度和发起审批。'


def test_default_room_and_independent_legacy_field_migration(world):
    coordinator,_,_=world
    service=coordinator.social_engine.service
    with session_scope() as session:
        room=session.get(Conversation,'port-hub')
        assert room.title=='港区群聊'
        assert '日常聊天' in room.announcement
        assert session.scalars(select(Message).where(Message.conversation_id=='port-hub')).all()==[]
        room.title='我改过的群名'
        room.announcement=OLD_ANNOUNCEMENT
        user=session.get(Actor,'commander')
        actors=session.scalars(select(Actor).where(Actor.kind=='agent',Actor.is_active.is_(True))).all()
        service._ensure_port_hub(session,user,actors)
        assert room.title=='我改过的群名'
        assert '日常聊天' in room.announcement
