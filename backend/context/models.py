from sqlalchemy import JSON, Boolean, Float, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column
from backend.models import Base


class ContextSource(Base):
    __tablename__ = 'context_sources'
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    version: Mapped[str] = mapped_column(String(80))
    kind: Mapped[str] = mapped_column(String(40), index=True)
    title: Mapped[str] = mapped_column(Text)
    text: Mapped[str] = mapped_column(Text)
    at: Mapped[float] = mapped_column(Float)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    data: Mapped[dict] = mapped_column(JSON, default=dict)


class ContextGrant(Base):
    __tablename__ = 'context_grants'
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    source_id: Mapped[str] = mapped_column(String(80), index=True)
    actor_id: Mapped[str] = mapped_column(String(80), index=True)
    conversation_id: Mapped[str] = mapped_column(String(100), default='')
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    data: Mapped[dict] = mapped_column(JSON, default=dict)


class ContextJob(Base):
    __tablename__ = 'context_jobs'
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    status: Mapped[str] = mapped_column(String(40), default='pending', index=True)
    data: Mapped[dict] = mapped_column(JSON, default=dict)


class ContextTombstone(Base):
    __tablename__ = 'context_tombstones'
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    data: Mapped[dict] = mapped_column(JSON, default=dict)


class ContextReference(Base):
    __tablename__ = 'context_references'
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    actor_id: Mapped[str] = mapped_column(String(80), index=True)
    conversation_id: Mapped[str] = mapped_column(String(100))
    data: Mapped[dict] = mapped_column(JSON, default=dict)


class ContextTrace(Base):
    __tablename__ = 'context_traces'
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    at: Mapped[float] = mapped_column(Float)
    actor_id: Mapped[str] = mapped_column(String(80), index=True)
    conversation_id: Mapped[str] = mapped_column(String(100))
    data: Mapped[dict] = mapped_column(JSON, default=dict)


class ContextSetting(Base):
    __tablename__ = 'context_settings'
    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    data: Mapped[dict] = mapped_column(JSON, default=dict)
