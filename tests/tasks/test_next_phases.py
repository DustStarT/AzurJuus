import pytest
from sqlalchemy import select

from backend.database import session_scope
from backend.models import Actor, Conversation, ConversationMember, SkillProposal
from conftest import configure_test_env


def _actor_payload(actor):
    return {"id": actor.id, "name": actor.name, "capabilities": list(actor.capabilities or [])}


@pytest.mark.asyncio
async def test_public_skill_proposal_has_secretary_then_user_gate(world, monkeypatch):
    coordinator, client, (actor_id, *_) = world
    workspace = client.get('/api/workspace/load').json()['workspace']
    workspace['settings'].update(secretaryAgentId=actor_id, connectedAgentIds=[actor_id])
    client.post('/api/workspace/save', json={'workspace': workspace}).raise_for_status()
    async def review(**kwargs):
        return '{"approved":true,"reason":"证据范围清楚。"}', [], {}
    monkeypatch.setattr(coordinator.governance.service.bundle.runtime, '_complete_chat', review)
    from backend.models import ActorSkill
    with session_scope() as session:
        row = session.scalar(select(ActorSkill).where(ActorSkill.actor_id == actor_id))
        row.is_enabled = True
        row.extra_json = {**(row.extra_json or {}), 'validationCount': 3, 'trialFamily': 'inventory',
                          'trialSummary': {'stable': True},
                          'trialEvidence': [{'case': i, 'variant': 'candidate', 'passed': True} for i in range(3)]}
        skill_id = row.skill_id
    coordinator.growth.control(actor_id, skill_id, 'publish')
    proposal_id = client.get('/api/skills/proposals').json()['proposals'][0]['id']
    assert client.post(f'/api/skills/proposals/{proposal_id}/review', json={
        'reviewer': 'user', 'decision': 'approved',
    }).status_code == 409
    assert client.post(f'/api/skills/proposals/{proposal_id}/review', json={
        'reviewer': 'secretary', 'decision': 'approved', 'note': '证据范围清楚。',
    }).status_code == 403
    await coordinator.governance.review(proposal_id)
    assert client.post(f'/api/skills/proposals/{proposal_id}/review', json={
        'reviewer': 'user', 'decision': 'approved', 'note': '确认共享。',
    }).is_success
    with session_scope() as session:
        proposal = session.get(SkillProposal, proposal_id)
        assert proposal.status == 'approved'
        assert (proposal.extra_json or {})['secretaryReview']['status'] == 'approved'
        assert proposal.reviewer_actor_id == actor_id
        assert (proposal.extra_json or {})['userReview']['status'] == 'approved'
    assert client.post(f'/api/skills/proposals/{proposal_id}/rollback', json={'note': '发现需要重新试用。'}).is_success
    with session_scope() as session:
        assert session.get(SkillProposal, proposal_id).status == 'rolled_back'


def test_replan_preserves_completed_assignment_and_admits_external_helper(world):
    coordinator, client, actor_ids = world
    workspace = client.get('/api/workspace/load').json()['workspace']
    workspace['settings']['connectedAgentIds'] = actor_ids[:3]
    client.post('/api/workspace/save', json={'workspace': workspace}).raise_for_status()
    with session_scope() as session:
        actors = [_actor_payload(session.get(Actor, actor_id)) for actor_id in actor_ids[:3]]
        session.add(Conversation(id='replan-room', kind='group', title='Replan test'))
        session.flush()
        session.add_all([
            ConversationMember(conversation_id='replan-room', actor_id=actor_id, role='member')
            for actor_id in ['commander', actors[0]['id'], actors[1]['id']]
        ])
    run, _ = coordinator.store.create({
        'actorId': actors[0]['id'], 'actors': actors[:2], 'mode': 'task', 'collaborative': True,
        'teamConversationId': 'replan-room',
        'conversationId': 'replan-room', 'prompt': '生成有交接边界的报告', 'workspace': str(coordinator.store.path.parent),
        'assignments': [],
    }, 'replan-test')
    coordinator.store.update(run['id'], assignments=[
        {'id': 'source', 'actorId': actors[0]['id'], 'brief': '准备来源', 'dependsOn': [], 'acceptance': ['读取核验'],
         'status': 'completed', 'result': {'artifacts': [{'path': 'source.txt', 'sha256': 'abc'}]}},
        {'id': 'report', 'actorId': actors[1]['id'], 'brief': '汇总来源', 'dependsOn': ['source'], 'acceptance': ['交付报告'], 'status': 'pending'},
    ], status='running')
    request = coordinator.request_help(run['id'], requester_id=actors[1]['id'], assignment_id='report',
        brief='需要额外成员核对交接文件。', candidate_ids=[actors[2]['id']], reason='当前成员缺少核验时间。')
    workspace = client.get('/api/workspace/load').json()['workspace']
    workspace['settings']['connectedAgentIds'] = actor_ids[:2]
    client.post('/api/workspace/save', json={'workspace': workspace}).raise_for_status()
    endpoint = f"/api/workflows/help-request/{run['id']}/{request['id']}"
    approval = {'decision': 'approved', 'actorId': actors[2]['id']}
    assert client.post(endpoint, json=approval).status_code == 409
    workspace['settings']['connectedAgentIds'] = actor_ids[:3]
    client.post('/api/workspace/save', json={'workspace': workspace}).raise_for_status()
    coordinator.store.update(run['id'], maxTaskMembers=2)
    assert client.post(endpoint, json=approval).status_code == 409
    coordinator.store.update(run['id'], maxTaskMembers=3)
    response = client.post(endpoint, json=approval)
    assert response.is_success, response.text
    result = response.json()['result']
    assert result['assignment']['handoff'][0]['path'] == 'source.txt'
    with session_scope() as session:
        helper_member = session.scalar(select(ConversationMember).where(
            ConversationMember.conversation_id == 'replan-room',
            ConversationMember.actor_id == actors[2]['id']))
        assert helper_member is not None and helper_member.is_active
    assert coordinator.store.get(run['id'])['planHistory'][-1]['kind'] == 'external-helper'
    revised = coordinator.replan(run['id'], [
        {'id': 'source', 'actorId': actors[0]['id'], 'brief': '准备来源', 'dependsOn': [], 'acceptance': ['读取核验']},
        {'id': 'report', 'actorId': actors[1]['id'], 'brief': '汇总来源', 'dependsOn': ['source'], 'acceptance': ['交付报告']},
        {'id': 'final', 'actorId': actors[2]['id'], 'brief': '核对最终报告', 'dependsOn': ['report'], 'acceptance': ['复核']},
    ])
    assert revised['revision'] == 2
    saved = coordinator.store.get(run['id'])
    assert next(item for item in saved['assignments'] if item['id'] == 'source')['status'] == 'completed'
    assert next(item for item in saved['assignments'] if item['id'] == 'final')['status'] == 'pending'


def test_collaboration_admission_uses_only_connected_configured_members(world):
    _, client, actor_ids = world
    workspace = client.get('/api/workspace/load').json()['workspace']
    selected = actor_ids[:2]
    workspace['settings']['connectedAgentIds'] = selected
    workspace['settings']['secretaryAgentId'] = selected[0]
    workspace['settings']['authorizedWorkspaceRoot'] = workspace['settings'].get('authorizedWorkspaceRoot') or str(__import__('pathlib').Path.cwd())
    client.post('/api/workspace/save', json={'workspace': workspace}).raise_for_status()
    conversation = next(item for item in workspace['data']['conversations'] if item['kind'] == 'dm')
    response = client.post('/api/workflows/dispatch', json={
        'conversationId': conversation['id'], 'title': '合成协作', 'description': '只让配置成员协作',
    })
    assert response.is_success, response.text
    run = client.app.state.runs.store.get(response.json()['runId'])
    assert {item['id'] for item in run['actors']} == set(selected)
