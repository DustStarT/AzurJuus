"""Additive tables; Experience remains the sole actor memory event projection."""
from sqlalchemy import JSON, String, Integer, Float
from sqlalchemy.orm import Mapped, mapped_column
from backend.models import Base


class MindProfile(Base):
    __tablename__ = 'mind_profiles'
    actor_id: Mapped[str] = mapped_column(String(80), primary_key=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    data: Mapped[dict] = mapped_column(JSON, default=dict)


class MindDecision(Base):
    __tablename__ = 'mind_decisions'
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    actor_id: Mapped[str] = mapped_column(String(80), index=True)
    version: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(24), default='pending')
    at: Mapped[float] = mapped_column(Float)
    data: Mapped[dict] = mapped_column(JSON, default=dict)


class PersonalGoal(Base):
    __tablename__ = 'personal_goals'
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    actor_id: Mapped[str] = mapped_column(String(80), index=True)
    status: Mapped[str] = mapped_column(String(24), default='active')
    data: Mapped[dict] = mapped_column(JSON, default=dict)


class LifeActivity(Base):
    __tablename__ = 'life_activities'
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    actor_id: Mapped[str] = mapped_column(String(80), index=True)
    status: Mapped[str] = mapped_column(String(24), default='active')
    updated: Mapped[float] = mapped_column(Float)
    data: Mapped[dict] = mapped_column(JSON, default=dict)


class LifeRecord(Base):
    __tablename__ = 'life_events'
    seq: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    id: Mapped[str] = mapped_column(String(80), unique=True)
    data: Mapped[dict] = mapped_column(JSON)


class MindControl(Base):
    __tablename__ = 'mind_controls'
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    data: Mapped[dict] = mapped_column(JSON, default=dict)


class MindCall(Base):
    __tablename__ = 'mind_calls'
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    actor_id: Mapped[str] = mapped_column(String(80), index=True)
    kind: Mapped[str] = mapped_column(String(40))
    at: Mapped[float] = mapped_column(Float, index=True)
    data: Mapped[dict] = mapped_column(JSON, default=dict)
