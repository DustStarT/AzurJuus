from pathlib import Path
from sqlalchemy import select, func
from backend.database import session_scope
from backend.models import Message
from backend.cognition_models import Experience, MindState
from test_cognition import world, observe


def test_profile_and_validation(world):
    c, client, _ = world
    assert client.post('/api/profile', json={'name':'小港', 'avatar':''}).status_code == 200
    assert client.get('/api/workspace/load').json()['workspace']['data']['user']['name'] == '小港'
    assert c.settings_loader()['userAddress'] == '小港'
    assert client.post('/api/profile', json={'name':' ', 'avatar':''}).status_code == 422
    assert client.post('/api/profile', json={'name':'测试', 'avatar':'https://example.org/a.svg'}).status_code == 422


def test_clear_memory_skips_pending_events_and_records_are_separate(world):
    c, client, (a, b, *_) = world
    observe(c, a, '我会记住合成约定', peers=[b])
    # More than one consumption batch must not resurrect after clearing.
    for n in range(120):
        c.store.event(None, 'mind.observation', {'key':f'pending-{n}', 'actorIds':[a],
            'kind':'speech', 'text':'排队中的旧记忆', 'data':{}})
    assert client.post('/api/system/clear/memory').status_code == 200
    c.cognition.pump()
    assert not c.cognition.experiences(a)
    assert '初识' in next(r for r in c.cognition.relationships(a) if r['peerId'] == b)['summary']
    with session_scope() as s:
        assert s.scalar(select(func.count()).select_from(Message)) > 0
    observe(c, a, '清理之后的新约定')
    assert client.post('/api/system/clear/records').status_code == 200
    with session_scope() as s:
        assert s.scalar(select(func.count()).select_from(Message)) == 0
    assert '新约定' in c.cognition.context(a)


def test_reset_clears_cognition_and_runtime_but_preserves_workspace_files(world, tmp_path):
    c, client, (a, *_) = world
    artifact = tmp_path / 'keep.txt'
    artifact.write_text('actual deliverable', encoding='utf-8')
    observe(c, a)
    run, _ = c.store.create({'actorId':a, 'prompt':'synthetic', 'mode':'chat', 'collaborative':False}, 'reset-test')
    # Paused/pending execution may carry unknown side effects. Never erase it.
    assert client.post('/api/system/reset').status_code == 409
    c.store.update(run['id'], status='cancelled')
    assert client.post('/api/system/reset').status_code == 200
    assert c.store.list() == []
    with session_scope() as s:
        assert s.scalar(select(func.count()).select_from(Experience)) == 0
        assert s.scalar(select(func.count()).select_from(MindState)) == 0
    assert artifact.read_text(encoding='utf-8') == 'actual deliverable'
    observe(c, a, '初始化之后的经历')
    assert len(c.cognition.experiences(a)) == 1


def test_relationship_changes_only_from_visible_shared_experience(world):
    c, _, (a, b, *_) = world
    before = next(r for r in c.cognition.relationships(a) if r['peerId'] == b)
    assert '初识' in before['familiarity']
    observe(c, a, '我来请你看一下草稿。', peers=[b])
    after = next(r for r in c.cognition.relationships(a) if r['peerId'] == b)
    assert '已经有过交流' in after['familiarity'] and after['sharedSources']
    reverse = next(r for r in c.cognition.relationships(b) if r['peerId'] == a)
    assert '初识' in reverse['familiarity']
    c.cognition.revise(a, after['sharedSources'][0], forget=True)
    assert '初识' in next(r for r in c.cognition.relationships(a) if r['peerId'] == b)['familiarity']


def test_user_defined_relationship_is_directional_and_resettable(world):
    c, client, (a, b, *_) = world
    path = f'/api/actors/{a}/relationships/{b}'
    assert client.post(path, json={'description':'旧识，允许直接指出疏漏'}).status_code == 200
    assert '旧识，允许直接指出疏漏' in c.cognition.context(a, peers={b})
    assert '旧识，允许直接指出疏漏' not in c.cognition.context(b, peers={a})
    assert c.cognition.experiences(a) == []
    assert client.post(f'/api/actors/{a}/relationships/{a}', json={'description':'self'}).status_code == 404
    c.cognition.configure(a, reset=True)
    assert '旧识，允许直接指出疏漏' not in c.cognition.context(a, peers={b})
