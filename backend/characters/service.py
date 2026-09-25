from __future__ import annotations

from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from tools.blhx_character_import import resolve_personas

from backend.models import (
    Actor,
    AgentRelationship,
)
from backend.platform.service_utils import (
    slugify,
)


class CharacterService:
    """Characters operations shared by the application service."""

    def preview_personas(self, session: Session, names: list[str], participant_count: int) -> dict[str, Any]:
        if participant_count < 1 or participant_count > 24:
            raise ValueError("角色数量需要在 1–24 人之间。")
        cleaned = [str(item).strip() for item in names if str(item).strip()]
        if len(cleaned) != participant_count:
            raise ValueError(f"Expected {participant_count} character names, but received {len(cleaned)}.")
        personas, missing = resolve_personas(cleaned)
        if missing:
            raise FileNotFoundError(f"Missing character personas: {', '.join(missing)}")
        return {"personas": personas, "names": cleaned, "participantCount": participant_count}

    def compose_personas(self, session: Session, names: list[str], participant_count: int) -> dict[str, Any]:
        preview = self.preview_personas(session, names, participant_count)
        personas = preview["personas"]
        cleaned = preview["names"]
        snapshot = self.apply_personas(session, personas, cleaned, participant_count)
        return {"snapshot": snapshot, "personas": personas}

    def apply_personas(self, session: Session, personas: list[dict[str, Any]], names: list[str], participant_count: int) -> dict[str, Any]:
        workspace = self.get_workspace(session)
        user = self.get_or_create_user(session)
        existing_agents = session.scalars(select(Actor).where(Actor.kind == "agent")).all()
        by_source = {agent.source_character or agent.name: agent for agent in existing_agents}
        active_ids: list[str] = []
        new_agents: list[Actor] = []

        for spec in personas:
            source_name = spec.get("sourceName") or spec.get("displayName")
            agent = by_source.get(source_name)
            is_new = agent is None
            if agent is not None:
                # Reconnecting does not replace user persona, artwork or state.
                if spec.get('sourceMaterials'):
                    agent.extra_json={**(agent.extra_json or {}),'sourceMaterials':spec['sourceMaterials']}
                agent.is_active = True
                session.add(agent)
                active_ids.append(agent.id)
                continue
            if agent is None:
                agent = Actor(id=f"juus-{slugify(source_name)}", kind="agent", handle=spec.get("handle") or f"@{slugify(source_name)}.juus")
            agent.source_character = source_name
            agent.name = spec.get("displayName") or source_name
            agent.handle = spec.get("handle") or agent.handle
            agent.english_name = spec.get("englishName")
            agent.faction = spec.get("faction") or "未知阵营"
            agent.status = agent.status or "在线"
            agent.initials = (spec.get("displayName") or source_name)[:2]
            agent.palette = list(spec.get("palette") or agent.palette or ["#dce8f5", "#f7fbff"])
            agent.accent = spec.get("accent") or agent.accent or "#84caef"
            agent.tone = spec.get("tone") or agent.tone
            agent.persona = spec.get("persona") or agent.persona
            agent.summary = spec.get("summary") or agent.summary
            agent.keywords = spec.get("keywords") or agent.keywords
            agent.capabilities = list(spec.get("capabilities") or agent.capabilities or [])
            agent.tools = list(spec.get("tools") or agent.tools or [])
            agent.avatar_url = spec.get("avatarUrl") or agent.avatar_url
            agent.illustration_url = spec.get("illustrationUrl") or spec.get("avatarUrl") or agent.illustration_url
            agent.system_prompt = spec.get("promptSeed") or agent.system_prompt
            agent.character_url = spec.get("characterUrl") or agent.character_url
            agent.extra_json = {"aliases": spec.get("aliases") or [], "voiceSamples": spec.get("voiceSamples") or {}}
            if spec.get('sourceMaterials'):
                agent.extra_json['sourceMaterials']=spec['sourceMaterials']
            # The expanded social roster has the same boundary through the old
            # settings importer as through the additive character directory.
            if source_name in {'阿贺野', '武藏', '英王乔治五世', '约克公爵', '贾维斯', '七省'}:
                from backend.characters.terminal_characters import card, render, TERMINAL
                base = card(source_name)
                agent.persona = agent.tone = agent.summary = base['style']
                agent.system_prompt = TERMINAL + render(base)
                agent.tools = []
                agent.capabilities = []
                agent.extra_json = {'aliases': spec.get('aliases') or [], 'socialOnly': True,
                    'terminalCard': {'version': base['version'], 'text': render(base)}}
            agent.is_active = True
            session.add(agent)
            session.flush()
            self._ensure_actor_signature_skill(session, agent)
            active_ids.append(agent.id)
            if is_new:
                new_agents.append(agent)

        for agent in existing_agents:
            if agent.id not in active_ids:
                agent.is_active = False
                session.add(agent)

        workspace.max_connected_agents = participant_count
        workspace.connected_agent_ids = active_ids
        workspace.character_roster_text = "\n".join(names)
        if workspace.secretary_agent_id not in active_ids:
            workspace.secretary_agent_id = active_ids[0] if active_ids else ""
        session.add(workspace)
        session.flush()

        active_agents = (
            session.scalars(select(Actor).where(Actor.id.in_(active_ids)).order_by(Actor.name.asc())).all()
            if active_ids
            else []
        )
        for agent in active_agents:
            self._ensure_dm_conversation(session, user, agent)
        if active_agents:
            self._ensure_port_hub(session, user, active_agents)
        for agent in new_agents:
            from backend.characters.terminal_characters import card
            opening_card = card(agent.source_character or agent.name)
            greeting = opening_card['examples']['招呼'] if opening_card else '我在。'
            self._ensure_greeting_message(session, user, agent, greeting)
        self._sync_relationships(session, active_agents)
        session.flush()
        return self.build_snapshot(session)

    def _sync_relationships(self, session: Session, agents: list[Actor]) -> None:
        for agent in agents:
            changed=False
            for peer in agents:
                if agent.id == peer.id:
                    continue
                existing = session.scalar(
                    select(AgentRelationship).where(
                        AgentRelationship.agent_id == agent.id,
                        AgentRelationship.peer_agent_id == peer.id,
                    )
                )
                if existing is None:
                    changed=True
                    session.add(
                        AgentRelationship(
                            id=f"rel-{slugify(agent.id)}-{slugify(peer.id)}",
                            agent_id=agent.id,
                            peer_agent_id=peer.id,
                            affinity_score=0.6,
                            trust_score=0.6,
                            social_probability=0.55,
                            notes_json={},
                        )
                    )
            if changed:
                import time
                from backend.characters.relationship_research import RESEARCH_VERSION
                agent.extra_json={**(agent.extra_json or {}),'wikiResearch':{'version':RESEARCH_VERSION,'status':'pending','progress':5,
                    'message':'已加入后台资料队列','queuedAt':time.time(),'sourceUrls':[]}}
