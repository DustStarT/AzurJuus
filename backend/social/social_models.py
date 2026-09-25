"""Durable social topics and decisions; never grants tool permissions."""
from sqlalchemy import JSON, Float, Integer, String, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column
from backend.models import Base


class SocialTopic(Base):
    __tablename__ = 'social_topics'
    id: Mapped[str] = mapped_column(String(120), primary_key=True)
    conversation_id: Mapped[str] = mapped_column(String(120), index=True)
    version: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(24), default='active')
    data: Mapped[dict] = mapped_column(JSON, default=dict)


class SocialAction(Base):
    __tablename__ = 'social_actions'
    id: Mapped[str] = mapped_column(String(120), primary_key=True)
    topic_id: Mapped[str] = mapped_column(String(120), index=True)
    actor_id: Mapped[str] = mapped_column(String(80))
    status: Mapped[str] = mapped_column(String(24))
    data: Mapped[dict] = mapped_column(JSON, default=dict)


class SocialDeferredReply(Base):
    """A direct group mention waits for work to finish, never for app restart."""
    __tablename__ = 'social_deferred_replies'
    id: Mapped[str] = mapped_column(String(120), primary_key=True)
    conversation_id: Mapped[str] = mapped_column(String(120), index=True)
    actor_id: Mapped[str] = mapped_column(String(80), index=True)
    source_message_id: Mapped[str] = mapped_column(String(120))
    status: Mapped[str] = mapped_column(String(24), default='pending')
    data: Mapped[dict] = mapped_column(JSON, default=dict)


class SocialPublication(Base):
    """Publication metadata; absence means an old, observer-only archived row."""
    __tablename__ = 'social_publications'
    post_id: Mapped[str] = mapped_column(String(120), primary_key=True)
    status: Mapped[str] = mapped_column(String(24), default='published', index=True)
    data: Mapped[dict] = mapped_column(JSON, default=dict)


class SocialReaction(Base):
    __tablename__ = 'social_reactions'
    __table_args__ = (UniqueConstraint('post_id', 'actor_id', name='uq_social_reaction'),)
    id: Mapped[str] = mapped_column(String(120), primary_key=True)
    post_id: Mapped[str] = mapped_column(String(120), index=True)
    actor_id: Mapped[str] = mapped_column(String(80), index=True)
    at: Mapped[float] = mapped_column(Float)


class SocialShareConsent(Base):
    __tablename__ = 'social_share_consents'
    id: Mapped[str] = mapped_column(String(120), primary_key=True)
    event_id: Mapped[str] = mapped_column(String(80), index=True)
    actor_id: Mapped[str] = mapped_column(String(80))
    status: Mapped[str] = mapped_column(String(24))
    at: Mapped[float] = mapped_column(Float)
