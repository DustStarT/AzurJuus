"""Business-side cognitive projections; execution truth stays in RunStore."""
from sqlalchemy import JSON, Boolean, Integer, String, Text, Float, ForeignKey
from sqlalchemy.orm import Mapped, mapped_column
from .models import Base


class MindState(Base):
    __tablename__ = 'mind_states'
    actor_id: Mapped[str] = mapped_column(String(80), ForeignKey('actors.id'), primary_key=True)
    version: Mapped[int] = mapped_column(Integer, default=0)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    data: Mapped[dict] = mapped_column(JSON, default=dict)


class Experience(Base):
    __tablename__ = 'mind_experiences'
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    actor_id: Mapped[str] = mapped_column(String(80), ForeignKey('actors.id'), index=True)
    source_seq: Mapped[int] = mapped_column(Integer)
    run_id: Mapped[str | None] = mapped_column(String(120), index=True)
    kind: Mapped[str] = mapped_column(String(40))
    text: Mapped[str] = mapped_column(Text)
    at: Mapped[float] = mapped_column(Float)
    forgotten: Mapped[bool] = mapped_column(Boolean, default=False)
    data: Mapped[dict] = mapped_column(JSON, default=dict)


class MindCursor(Base):
    __tablename__ = 'mind_cursors'
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    seq: Mapped[int] = mapped_column(Integer, default=0)


class MindReceipt(Base):
    __tablename__ = 'mind_receipts'
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    source_seq: Mapped[int] = mapped_column(Integer)


class MindOutbox(Base):
    __tablename__ = 'mind_outbox'
    id: Mapped[str] = mapped_column(String(80), primary_key=True)
    payload: Mapped[dict] = mapped_column(JSON)
    sent: Mapped[bool] = mapped_column(Boolean, default=False)
