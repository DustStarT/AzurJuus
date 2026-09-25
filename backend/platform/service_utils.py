"""Formatting shared by API snapshots and domain services."""
from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any


def now_utc() -> datetime:
    return datetime.now(UTC)


def isoformat(value: datetime | None) -> str:
    value = value or now_utc()
    # SQLite discards timezone data; all application-written timestamps are UTC.
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat().replace('+00:00', 'Z')


def parse_iso_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(str(value).replace('Z', '+00:00')).astimezone(UTC)
    except ValueError:
        return None


def slugify(value: str) -> str:
    chunks = []
    for char in str(value or '').strip().lower():
        chunks.append(char if char.isalnum() or '\u4e00' <= char <= '\u9fff' else '-')
    slug = ''.join(chunks)
    while '--' in slug:
        slug = slug.replace('--', '-')
    return slug.strip('-') or 'actor'


def preview_text(value: str, limit: int = 40) -> str:
    normalized = ' '.join(str(value or '').replace('\n', ' ').split())
    return normalized if len(normalized) <= limit else f'{normalized[:limit]}...'


def json_safe(value: Any) -> Any:
    if isinstance(value, Path):
        return str(value)
    if isinstance(value, dict):
        return {str(key): json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [json_safe(item) for item in value]
    return value


def default_ui_session() -> dict[str, Any]:
    return {
        'view': 'chat',
        'activeConversationId': None,
        'activePostId': None,
        'composerMode': 'chat',
        'filters': {
            'reply': 'all', 'type': 'all', 'faction': 'all',
            'capability': 'all', 'favorite': 'all',
        },
    }
