"""Durable social topics and decisions; never grants tool permissions."""
from sqlalchemy import JSON, Integer, String
from sqlalchemy.orm import Mapped, mapped_column
from .models import Base


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
