"""Stable chat order, including legacy SQLite rows sharing one-second timestamps."""
from sqlalchemy import literal_column
from .models import Message


def message_order(session, *, newest_first=False):
    direction='desc' if newest_first else 'asc'
    # Old SQLite messages used CURRENT_TIMESTAMP with one-second precision.
    # Their random IDs cannot be used as a chronological tie-breaker.
    tie=literal_column('messages.rowid') if session.get_bind().dialect.name=='sqlite' else Message.id
    return (getattr(Message.created_at,direction)(),getattr(tie,direction)())
