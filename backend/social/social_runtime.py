from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any


@dataclass
class IdleSocialPlan:
    actor_id: str
    prompt: str
    due_at: datetime


@dataclass
class SocialCandidate:
    actor_id: str
    last_post_at: datetime | None
    affinity_score: float = 0.5
    trust_score: float = 0.5
    social_probability: float = 0.5


class SocialRuntime:
    """Idle-agent social planner for JUUS posts and in-character comments."""

    def __init__(self, enabled: bool = True):
        self.enabled = enabled

    def should_schedule(self, *, last_post_at: datetime | None, interval_minutes: int) -> bool:
        if not self.enabled:
            return False
        if last_post_at is None:
            return True
        # SQLite returns naive UTC values; interpreting them as local time can
        # make every post immediately overdue on Windows in UTC+8.
        last_post_at = last_post_at.replace(tzinfo=UTC) if last_post_at.tzinfo is None else last_post_at.astimezone(UTC)
        next_due = last_post_at + timedelta(minutes=max(interval_minutes, 1))
        return datetime.now(UTC) >= next_due

    def build_idle_post_plan(
        self,
        *,
        actor_id: str,
        actor_name: str,
        faction: str,
        summary: str,
        recent_memories: list[str],
        interval_minutes: int,
    ) -> IdleSocialPlan:
        memory_block = "\n".join(f"- {item}" for item in recent_memories if item)
        prompt = (
            f"You are {actor_name} from {faction}. Write one short JUUS circle post that feels in character. "
            "It may reflect on a finished task, a daily moment, or a stray thought. Keep it concise and social."
        )
        if summary:
            prompt += f" Persona reminder: {summary}."
        if memory_block:
            prompt += f"\nRecent memories:\n{memory_block}"
        due_at = datetime.now(UTC) + timedelta(minutes=max(interval_minutes, 1))
        return IdleSocialPlan(actor_id=actor_id, prompt=prompt, due_at=due_at)

    def pick_due_author(self, candidates: list[SocialCandidate]) -> SocialCandidate | None:
        if not self.enabled or not candidates:
            return None
        return sorted(
            candidates,
            key=lambda candidate: (
                candidate.last_post_at is not None,
                (candidate.last_post_at.replace(tzinfo=UTC) if candidate.last_post_at and candidate.last_post_at.tzinfo is None else candidate.last_post_at) or datetime.min.replace(tzinfo=UTC),
                candidate.actor_id,
            ),
        )[0]

    def pick_commenters(self, candidates: list[SocialCandidate], limit: int = 2) -> list[SocialCandidate]:
        if not self.enabled or not candidates or limit <= 0:
            return []
        ranked = sorted(
            candidates,
            key=lambda candidate: (
                candidate.social_probability,
                candidate.trust_score,
                candidate.affinity_score,
                candidate.actor_id,
            ),
            reverse=True,
        )
        selected = [candidate for candidate in ranked if candidate.social_probability >= 0.45]
        return selected[:limit]

    def build_relationship_hint(self, *, affinity_score: float, trust_score: float, notes: dict[str, Any] | None = None) -> str:
        notes = notes or {}
        mood = []
        if affinity_score >= 0.72:
            mood.append("关系亲近")
        elif affinity_score >= 0.56:
            mood.append("相处自然")
        else:
            mood.append("关系普通")
        if trust_score >= 0.72:
            mood.append("较为信任")
        elif trust_score >= 0.56:
            mood.append("基本信任")
        else:
            mood.append("仍在观察")
        if notes:
            mood.append(", ".join(str(value) for value in notes.values() if value))
        return " / ".join(item for item in mood if item)

    def build_comment_prompt(self, *, author_name: str, post_excerpt: str, responder_name: str, relationship_hint: str) -> str:
        return (
            f"{responder_name} is replying to {author_name}'s JUUS post. "
            f"Post excerpt: {post_excerpt}. Relationship hint: {relationship_hint}. "
            "Write one short in-character comment."
        )
