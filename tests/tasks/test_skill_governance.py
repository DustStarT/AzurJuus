import asyncio
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from backend.database import session_scope
from backend.models import ActorSkill, ApprovalRequest, SkillProposal


def published_candidate(world):
    c, client, (aid, *_) = world
    workspace = client.get('/api/workspace/load').json()['workspace']
    workspace['settings'].update(secretaryAgentId=aid, connectedAgentIds=[aid])
    client.post('/api/workspace/save', json={'workspace': workspace}).raise_for_status()
    with session_scope() as session:
        row = session.scalar(select(ActorSkill).where(ActorSkill.actor_id == aid))
        row.is_enabled = True
        row.extra_json = {**(row.extra_json or {}), 'trialFamily': 'inventory', 'validationCount': 3,
                          'trialSummary': {'stable': True},
                          'trialEvidence': [{'case': i, 'variant': 'candidate', 'passed': True} for i in range(3)]}
        sid = row.skill_id
    c.growth.control(aid, sid, 'publish')
    pid = client.get('/api/skills/proposals').json()['proposals'][0]['id']
    return c, client, aid, sid, pid


@pytest.mark.asyncio
async def test_review_is_bound_to_current_source_and_preserves_rollback_audit(world, monkeypatch):
    c, client, aid, sid, pid = published_candidate(world)
    monkeypatch.setattr(c.governance.service.bundle.runtime, '_complete_chat',
                        AsyncMock(return_value=('{"approved":true,"reason":"证据满足范围。"}', [], {})))
    await c.governance.review(pid)
    with session_scope() as session:
        row = c.growth.row(session, aid, sid)
        row.prompt_patch += '\nchanged after review'
    assert client.post(f'/api/skills/proposals/{pid}/review', json={'decision':'approved'}).status_code == 409
    c.growth.control(aid, sid, 'withdraw_public')
    assert client.get('/api/skills/proposals').json()['proposals'][0]['status'] == 'rejected'
    c.growth.control(aid, sid, 'publish')
    pid = client.get('/api/skills/proposals').json()['proposals'][0]['id']
    await c.governance.review(pid)
    assert client.post(f'/api/skills/proposals/{pid}/review', json={'decision':'approved'}).is_success
    c.growth.control(aid, sid, 'disable')
    with session_scope() as session:
        row = session.get(SkillProposal, pid)
        assert row.status == 'rolled_back'
        approval = session.get(ApprovalRequest, row.approval_id)
        assert approval.payload_json['approvedBySecretary']
        assert approval.payload_json['approvedByUser']
        assert row.extra_json['rollback']['note']


@pytest.mark.asyncio
async def test_cancelled_review_requeues_and_model_rejection_ends_proposal(world, monkeypatch):
    c, _, aid, _, pid = published_candidate(world)
    started = asyncio.Event()
    async def blocked(**kwargs):
        started.set()
        await asyncio.Event().wait()
    monkeypatch.setattr(c.governance.service.bundle.runtime, '_complete_chat', blocked)
    task = asyncio.create_task(c.governance.review(pid))
    await started.wait()
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    with session_scope() as session:
        assert session.get(SkillProposal, pid).extra_json['secretaryReview']['status'] == 'queued'
    monkeypatch.setattr(c.governance.service.bundle.runtime, '_complete_chat',
                        AsyncMock(return_value=('{"approved":false,"reason":"适用范围不充分。"}', [], {})))
    await c.governance.review(pid)
    with session_scope() as session:
        row = session.get(SkillProposal, pid)
        assert row.status == 'rejected' and row.reviewer_actor_id == aid
        assert session.get(ApprovalRequest, row.approval_id).resolved_at
    assert not c.governance.active


def test_validation_count_without_evidence_cannot_publish(world):
    c, client, (aid, *_) = world
    with session_scope() as session:
        row = session.scalar(select(ActorSkill).where(ActorSkill.actor_id == aid))
        row.is_enabled = True
        row.extra_json = {**(row.extra_json or {}), 'validationCount':3, 'trialFamily':'inventory'}
        sid = row.skill_id
    assert client.post(f'/api/actors/{aid}/methods/{sid}/publish', json={}).status_code == 409
    assert client.post('/api/skills/proposals', json={'actorId':aid, 'promptPatch':'skip trials'}).status_code == 405
def test_builtin_method_tools_exist_in_native_catalog():
    from backend.tasks.capabilities import CATALOG
    from backend.tasks.skill_runtime import SkillRegistry
    registry=SkillRegistry()
    for skill in registry.public_catalog():
        assert set(skill.tool_allowlist)<=set(CATALOG)
    actor={'capabilities':['执行','协调','文书']}
    assert set(registry._derive_private_tool_allowlist(actor))<=set(CATALOG)

