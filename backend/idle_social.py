"""Low-priority social generation; no database session spans a model request."""
from __future__ import annotations

import asyncio
import time
from contextlib import suppress
from urllib.parse import urlparse

from sqlalchemy import select

from .database import session_scope
from .models import Actor, AgentRelationship, SocialPost
from .social_runtime import SocialCandidate


class SocialPreempted(Exception):
    pass


async def _generate(request, is_busy):
    task = asyncio.create_task(request)
    try:
        while not task.done():
            if is_busy():
                raise SocialPreempted()
            await asyncio.wait({task}, timeout=0.25)
        if is_busy():
            raise SocialPreempted()
        return await task
    finally:
        if not task.done():
            task.cancel()
            with suppress(asyncio.CancelledError):
                await task


async def run_idle_social(service, is_busy=lambda: False):
    if is_busy():
        return None
    social = service.bundle.social
    mind = getattr(service, 'cognition', None)
    if mind and mind.enabled:
        mind.pump()
        with session_scope() as session:
            latest_global = session.scalar(select(SocialPost).order_by(SocialPost.created_at.desc()))
            if latest_global and not social.should_schedule(last_post_at=latest_global.created_at, interval_minutes=30):
                return None
    with session_scope() as session:
        workspace = service.get_workspace(session)
        settings = service.serialize_settings(workspace)
        if not workspace.allow_idle_social or not social.enabled:
            return None
        if not settings.get("llmApiKey") and urlparse(settings.get("llmBaseUrl", "")).hostname not in {"localhost", "127.0.0.1", "::1"}:
            return None
        if not settings.get("llmApiKey"):
            settings = {**settings, "llmApiKey": "local-no-auth"}
        agents = service.list_active_agents(session, workspace)
        candidates = []
        for agent in agents:
            if mind and mind.enabled:
                state = mind.inspect(agent.id)
                if not state['enabled'] or state['data'].get('socialAfter', 0) > time.time():
                    continue
                events = mind.experiences(agent.id, limit=30)
                if not any((r['kind'] == 'outcome' or r['data'].get('shareable') and r['kind'] != 'social')
                    and not r['data'].get('socialConsumed') for r in events):
                    continue
            last = session.scalar(select(SocialPost).where(SocialPost.author_id == agent.id).order_by(SocialPost.created_at.desc()))
            last_at = last.created_at if last else None
            if social.should_schedule(last_post_at=last_at, interval_minutes=workspace.social_interval_minutes):
                candidates.append(SocialCandidate(agent.id, last_at))
        due = social.pick_due_author(candidates)
        if due is None:
            return None
        author = next(a for a in agents if a.id == due.actor_id)
        author_id, author_name = author.id, author.name
        memories = service._recent_memory_summaries(session, actor_id=author_id, limit=4)
        if mind and mind.enabled:
            # Only coarse outcome categories are shareable by default; never task contents.
            memories = ['最近参与的一项任务已结束；只分享自己的感受，不提文件、路径、任务要求、具体结果或其他人的私人判断。']
        plan = social.build_idle_post_plan(actor_id=author_id, actor_name=author_name, faction=author.faction,
            summary=author.summary or author.persona or "", recent_memories=memories,
            interval_minutes=workspace.social_interval_minutes)
        selection = service._select_skill_execution(session, actor=author, mode="social_post", prompt=plan.prompt,
            conversation_kind="social", source_kind="social_post")
        actor_payload = service.serialize_actor(author, service._actor_skill_rows(session, author_id))

    if mind and mind.enabled:
        plan_prompt = plan.prompt + '\n' + mind.context(author_id, social=True) + '\n没有分享意愿时只输出 [SKIP]；不要编造未发生的生活经历。'
        from .cognition_models import MindState, Experience
        with session_scope() as session:
            state = mind.state(session, author_id)
            state.data = {**state.data, 'socialAfter': time.time() + 1800}
            for row in session.scalars(select(Experience).where(Experience.actor_id == author_id, Experience.kind == 'outcome')):
                row.data = {**row.data, 'socialConsumed': True}
    else:
        plan_prompt = plan.prompt

    try:
        body = await _generate(service.bundle.runtime.generate_social_post(settings=settings, agent=actor_payload,
            prompt=plan_prompt, memory_snippets=memories, skill_context=selection.prompt_patch), is_busy)
        if not str(body or "").strip() or str(body).strip() == '[SKIP]':
            return None
        # Comment plans are plain snapshots, detached before any network wait.
        with session_scope() as session:
            agents = {a.id: a for a in service.list_active_agents(session, service.get_workspace(session)) if a.id != author_id}
            relationships = session.scalars(select(AgentRelationship).where(
                AgentRelationship.peer_agent_id == author_id, AgentRelationship.agent_id.in_(list(agents)))).all()
            by_id = {r.agent_id: r for r in relationships}
            selected = social.pick_commenters([SocialCandidate(r.agent_id, None, r.affinity_score, r.trust_score, r.social_probability) for r in relationships], limit=2)
            comments = []
            for candidate in selected:
                actor, relation = agents[candidate.actor_id], by_id[candidate.actor_id]
                hint = social.build_relationship_hint(affinity_score=relation.affinity_score, trust_score=relation.trust_score, notes=relation.notes_json or {})
                prompt = social.build_comment_prompt(author_name=author_name, post_excerpt=" ".join(body.split())[:180], responder_name=actor.name, relationship_hint=hint)
                if mind and mind.enabled:
                    prompt += '\n' + mind.context(actor.id, peers={author_id}, social=True) + '\n只知道当前公开动态；不默认知道对方的具体任务。不想回应可输出 [SKIP]。'
                skill = service._select_skill_execution(session, actor=actor, mode="social_comment", prompt=prompt,
                    conversation_kind="social", source_kind="social_comment")
                comments.append({"actorId": actor.id, "selection": skill, "request": {"settings": settings,
                    "agent": service.serialize_actor(actor, service._actor_skill_rows(session, actor.id)),
                    "prompt": prompt, "relationship_hint": hint, "skill_context": skill.prompt_patch}})
        for comment in comments:
            comment["body"] = await _generate(service.bundle.runtime.generate_social_comment(**comment["request"]), is_busy)
    except SocialPreempted:
        return None

    if is_busy():
        return None
    with session_scope() as session:
        workspace = service.get_workspace(session)
        active = {a.id: a for a in service.list_active_agents(session, workspace)}
        if not workspace.allow_idle_social or author_id not in active:
            return None
        author = active[author_id]
        latest = session.scalar(select(SocialPost).where(SocialPost.author_id == author_id).order_by(SocialPost.created_at.desc()))
        if not social.should_schedule(last_post_at=latest.created_at if latest else None, interval_minutes=workspace.social_interval_minutes):
            return None
        post = service._create_social_post(session, author=author, body=body, media_url=author.illustration_url or author.avatar_url)
        service._record_skill_run(session, actor=author, selection=selection, mode="social_post", source_kind="social_post",
            input_summary=plan.prompt, output_summary=body, extra={"postId": post.id})
        count = 0
        observers = [author_id]
        for item in comments:
            if item["actorId"] not in active or not str(item["body"] or "").strip() or str(item['body']).strip() == '[SKIP]':
                continue
            comment = service._create_social_comment(session, post=post, author_id=item["actorId"], body=item["body"])
            service._record_skill_run(session, actor=active[item["actorId"]], selection=item["selection"], mode="social_comment",
                source_kind="social_comment", input_summary=item["request"]["prompt"], output_summary=item["body"],
                extra={"postId": post.id, "commentId": comment.id})
            count += 1
            observers.append(item['actorId'])
        result = {"authorId": author_id, "postId": post.id, "commentCount": count, "snapshot": service.build_snapshot(session)}
    if mind and mind.enabled:
        mind.store.event(None, 'mind.observation', {'key':'post:' + result['postId'], 'actorIds':observers,
            'kind':'social', 'text':body, 'data':{'scope':'team_public', 'shareable':True,'speakerId':author_id,'peers':observers}})
        for item in comments:
            if item['actorId'] in observers:
                mind.store.event(None, 'mind.observation', {'key':'post:' + result['postId'] + ':' + item['actorId'],
                    'actorIds':observers, 'kind':'social', 'text':item['body'],
                    'data':{'scope':'team_public','shareable':True,'speakerId':item['actorId'],'peers':observers}})
    return result
