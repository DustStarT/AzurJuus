import asyncio
import json
import time
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import select
from backend.context.contracts import ContextRequest
from backend.context.models import ContextSource, ContextGrant, ContextTombstone, ContextReference
from backend.context.service import ContextService, topic_ledger
from backend.database import session_scope
from backend.models import Conversation, ConversationMember
from backend.mind.cognition_models import Experience


def room(client, actor):
    client.get('/api/bootstrap')
    with session_scope() as s:
        members = s.scalars(select(ConversationMember).where(ConversationMember.actor_id == actor,
            ConversationMember.is_active.is_(True))).all()
        return next(m.conversation_id for m in members if s.get(Conversation, m.conversation_id).kind == 'dm')


def read(c, actor, cid, name='阅读 笔记.pdf', text='船员以书信维持联系，每周一起讨论航海生活。', status='failed', page=3, file_hash='v1', total=20):
    run, _ = c.store.create({'actorId': actor, 'actors': [{'id': actor, 'name': 'reader'}],
        'mode': 'task', 'conversationId': cid, 'workspace': '.', 'collaborative': False,
        'prompt': '阅读文件', 'history': []}, uuid4().hex)
    call_id = uuid4().hex
    c.store.put_call(call_id, run['id'], 'read_document', {'path': name, 'offset': page - 1},
        'completed', {'sha256': file_hash, 'items': [{'page': page, 'text': text,
            'source': 'local-ocr', 'confidence': .87}], 'nextOffset': page if page < total else None, 'total': total}, phase='main')
    c.store.update(run['id'], status=status, result={'summary': text} if status == 'completed' else None)
    c.context.sync()
    return run


def test_partial_read_ambiguity_reference_restart_and_no_recent_100_gate(world):
    c, client, ids = world
    cid = room(client, ids[0])
    run = read(c, ids[0], cid)
    for n in range(120):
        with session_scope() as s:
            s.add(Experience(id='new-' + str(n), actor_id=ids[0], source_seq=n, kind='speech',
                text='今天吃了面包' + str(n), at=time.time() + n, data={'conversationId': cid}))
    request = ContextRequest(ids[0], cid, query='阅读 笔记.pdf 写得怎么样？', source_id='confirmed-message')
    bundle = c.context.collect(request)
    assert bundle.reference['runId'] == run['id']
    assert not bundle.reference['verified']  # successful pages do not complete a failed task
    assert bundle.documents and bundle.documents[0]['location']['page'] == 3
    assert bundle.documents[0]['location']['confidence'] == .87
    assert bundle.documents[0]['ownRead']
    read(c, ids[0], cid, name='另一篇.pdf')
    # Persisted conversation anchor wins over newer reads and survives service restart.
    restored = ContextService(c.store.path.parent, c.store, c.settings_loader)
    result = restored.collect(ContextRequest(ids[0], cid, query='这个 PDF 写得如何？'))
    assert result.reference['runId'] == run['id']
    assert result.reference['readSources'][0]['name'] == '阅读 笔记.pdf'
    unknown = restored.collect(ContextRequest(ids[0], cid, query='没有读过.pdf 写得怎样？'))
    assert unknown.reference['unresolved'] and not unknown.documents
    with session_scope() as s:
        for ref in s.scalars(select(ContextReference)).all():
            s.delete(ref)
    ambiguous = restored.collect(ContextRequest(ids[0], cid, query='这个pdf怎么样？'))
    assert ambiguous.reference['ambiguous']


def test_private_group_share_revoke_forget_and_rebuild(world):
    c, client, ids = world
    cid = room(client, ids[0])
    read(c, ids[0], cid, name='私人计划.pdf', text='机密口令是蓝色月亮。')
    own = c.context.collect(ContextRequest(ids[0], cid, query='私人计划.pdf'))
    sid = own.documents[0]['sourceId']
    for actor in ids[:2]:
        public = c.context.collect(ContextRequest(actor, 'port-hub', query='私人计划.pdf', public=True))
        assert '蓝色月亮' not in json.dumps(public.payload(), ensure_ascii=False)
        assert '私人计划.pdf' not in json.dumps(public.documents, ensure_ascii=False)
    assert client.post(f'/api/context/sources/{sid}/share', json={'conversationId': 'port-hub'}).status_code == 200
    shared = c.context.collect(ContextRequest(ids[1], 'port-hub', query='私人计划.pdf', public=True))
    assert shared.documents and not shared.documents[0]['ownRead']
    assert shared.documents[0]['readerId'] == ids[0]
    assert client.post(f'/api/context/sources/{sid}/share', json={'conversationId': 'port-hub', 'revoke': True}).status_code == 200
    assert not c.context.collect(ContextRequest(ids[1], 'port-hub', query='蓝色月亮', public=True)).documents
    assert client.post(f'/api/context/sources/{sid}/share', json={'conversationId': 'port-hub'}).status_code == 200
    assert c.context.collect(ContextRequest(ids[1], 'port-hub', query='私人计划.pdf', public=True)).documents
    c.context.revoke(sid)
    with pytest.raises(ValueError, match='权限或版本'):
        c.context.validate_trace(own.trace_id)
    c.context.reindex()
    assert not c.context.collect(ContextRequest(ids[0], cid, query='私人计划.pdf')).documents
    assert client.get(f'/api/context/traces/{own.trace_id}').json()['selected'] == []


def test_forgotten_outcome_blocks_successful_read_without_reindex(world):
    c, client, ids = world
    cid = room(client, ids[0])
    run = read(c, ids[0], cid, status='completed')
    c.cognition.pump()
    with session_scope() as s:
        exp = s.scalar(select(Experience).where(Experience.run_id == run['id'], Experience.kind == 'outcome'))
        assert exp
        exp.forgotten = True
    assert not c.context.collect(ContextRequest(ids[0], cid, query='阅读 笔记.pdf')).documents


def test_removed_member_cannot_retrieve_former_room(world):
    c, client, ids = world
    client.get('/api/bootstrap')
    read(c, ids[0], 'port-hub')
    with session_scope() as s:
        s.scalar(select(ConversationMember).where(ConversationMember.actor_id == ids[0],
            ConversationMember.conversation_id == 'port-hub')).is_active = False
    assert not c.context.collect(ContextRequest(ids[0], query='阅读 笔记.pdf')).documents


def test_version_change_and_wiki_are_not_current_execution(world):
    c, client, ids = world
    cid = room(client, ids[0])
    read(c, ids[0], cid, file_hash='old', text='旧版采用每周开会。')
    read(c, ids[0], cid, file_hash='new', text='新版采用按需交流。')
    current = c.context.collect(ContextRequest(ids[0], cid, query='阅读 笔记.pdf'))
    assert {d['fileHash'] for d in current.documents} == {'new'}
    c.context.register('wiki-source', 'wiki', '港区故事', '标枪在原作剧情里参加了航海训练。',
        {'url': 'https://example.org/story', 'provenance': 'original_background', 'stale': True})
    wiki = c.context.collect(ContextRequest(ids[0], cid, query='原作剧情航海训练'))
    assert any(v['kind'] == 'wiki' and not v['ownRead'] and v['stale'] for v in wiki.documents)


def test_full_return_evidence_does_not_turn_partial_sharing_into_full_read(world):
    c, client, ids = world
    cid = room(client, ids[0])
    read(c, ids[0], cid, name='one.pdf', total=1, page=1)
    one = c.context.collect(ContextRequest(ids[0], cid, query='one.pdf'))
    assert one.reference['coversAllReturnedUnits'] and not one.documents[0]['partial']
    read(c, ids[0], cid, name='two.pdf', total=2, page=1)
    first = c.context.collect(ContextRequest(ids[0], cid, query='two.pdf'))
    first_id = first.documents[0]['sourceId']
    read(c, ids[0], cid, name='two.pdf', total=2, page=2)
    all_pages = c.context.collect(ContextRequest(ids[0], cid, query='two.pdf'))
    assert all_pages.reference['coversAllReturnedUnits']
    assert client.post(f'/api/context/sources/{first_id}/share', json={'conversationId': 'port-hub'}).status_code == 200
    shared = c.context.collect(ContextRequest(ids[1], 'port-hub', query='two.pdf', public=True))
    assert not shared.reference['coversAllReturnedUnits'] and not shared.documents[0]['ownRead']


def test_settings_do_not_expose_or_reuse_chat_credentials(world):
    c, client, ids = world
    assert not client.get('/api/context/settings').json()['apiKeyConfigured']
    rejected = client.post('/api/context/settings', json={'apiKey': 'test-secret-' * 1000})
    assert rejected.status_code == 422 and 'test-secret-' not in rejected.text
    assert client.post('/api/context/settings', json={'provider': 'cloud', 'baseUrl': 'https://example.org/v1',
        'model': 'test', 'dimensions': 384}).status_code == 422
    cfg = client.post('/api/context/settings', json={'provider': 'cloud', 'baseUrl': 'https://example.org/v1',
        'model': 'test', 'dimensions': 384, 'apiKey': 'embedding-test-only'}).json()
    assert cfg['apiKeyConfigured'] and 'apiKey' not in cfg
    assert 'embedding-test-only' not in json.dumps(client.get('/api/context/settings').json())
    client.post('/api/context/settings', json={'provider': 'lexical'})
    assert client.get('/api/context/status').json()['provider'] == 'lexical'


def test_repetition_does_not_consume_topic_version_and_correction_survives(world):
    from conftest import setup_social_topic
    e, run, topic, actor = setup_social_topic(world)
    assert e.commit(topic, actor, 'novel-one', {'action': 'speak', 'segments': ['甜甜圈确实很好吃。']})
    topic = e.topic(topic['id'])
    other = next(a for a in e.snapshot('port-hub')['members'] if a['id'] != actor['id'])
    assert not e.commit(topic, other, 'assent-only', {'action': 'speak', 'segments': ['我也同意，甜甜圈确实很好吃。']})
    assert e.topic(topic['id'])['version'] == topic['version']
    assert e.topic(topic['id'])['status'] == 'active'
    assert e.commit(topic, other, 'correction', {'action': 'speak', 'segments': ['不过，甜甜圈并不是每个人都喜欢。']})
    latest = e.topic(topic['id'])
    assert e.commit(latest, actor, 'new-detail', {'action': 'speak',
        'segments': ['我也同意，甜甜圈确实很好吃，我今天还买了两盒。']})


def test_topic_ledger_tracks_each_mention_and_deduplicates_questions():
    history = [{'id': 'q', 'speakerId': 'commander', 'text': '你们怎么看？', 'mentions': ['a', 'b']},
        {'id': 'a-reply', 'speakerId': 'a', 'text': '我赞成。'},
        {'id': 'same-q', 'speakerId': 'c', 'text': '你们怎么看？'}]
    pending = topic_ledger(history)['openQuestions']
    assert len(pending) == 1
    assert pending[0]['targets'] == ['b']


def test_compact_merges_only_safe_chat_and_falls_back_for_effects(mind):
    r, c, client, ids, stages = mind
    previous = c.settings_loader
    c.settings_loader = lambda: {**previous(), 'dialoguePreset': 'compact'}
    original = r.generate
    async def generate(messages, cfg):
        if cfg['_mindStage'] == 'compact':
            stages.append('compact')
            snap = json.loads(messages[-1]['content'])['snapshot']
            return {'frame': {'interpretation': '回应问候', 'sourceIds': [snap['current']['id']]},
                'intent': {'action': 'speak', 'purpose': '自然回应', 'sourceIds': [snap['current']['id']]},
                'needsFullChain': False}
        return await original(messages, cfg)
    r.generate = generate
    asyncio.run(r.think(ids[0], 'compact-chat', '你好'))
    assert stages.count('compact') == 1 and 'decide' not in stages
    asyncio.run(r.think(ids[0], 'action-chat', '请你提醒我阅读'))
    assert 'understand' in stages and 'decide' in stages


def test_soft_budget_expands_but_hard_context_limit_fails(world):
    c, client, ids = world
    cid = room(client, ids[0])
    bundle = c.context.collect(ContextRequest(ids[0], cid, query='你好', preset='compact'))
    messages = [{'role': 'system', 'content': '必要规则' * 2000}]
    assert c.context.pack_messages(bundle, messages, 1000) == messages
    c.context.configure({'contextLimit': 4096})
    with pytest.raises(ValueError, match='上限'):
        c.context.pack_messages(bundle, messages, 1000)


def test_revocation_during_generation_blocks_publication_and_retry_uses_fresh_context(world):
    c, client, ids = world
    cid = room(client, ids[0])
    read(c, ids[0], cid, name='private.pdf', text='机密口令是蓝色月亮。')
    bundle = c.context.collect(ContextRequest(ids[0], cid, query='private.pdf'))
    sid = bundle.documents[0]['sourceId']
    actor = {'id': ids[0], 'name': 'reader'}
    run, _ = c.store.create({'actorId': ids[0], 'actors': [actor], 'mode': 'chat',
        'collaborative': False, 'conversationId': cid, 'prompt': 'private.pdf 写得怎么样？', 'history': []}, uuid4().hex)
    published, requests = [], []
    c.expression.enabled = True
    async def generate(messages, cfg):
        requests.append(messages)
        if len(requests) == 1:
            assert '蓝色月亮' in json.dumps(messages, ensure_ascii=False)
            c.context.revoke(sid)
            return {'segments': ['口令是蓝色月亮。']}
        assert '蓝色月亮' not in json.dumps(messages, ensure_ascii=False)
        return {'segments': ['这份材料当前不可引用，请重新确认来源。']}
    async def publish(rid, payload):
        published.append(payload['text'])
    c.expression.generate, c.message_callback = generate, publish
    async def scenario():
        with pytest.raises(RuntimeError):
            await c.expression.speak(run, actor, 'chat', run['prompt'])
        assert not published
        await c.expression.speak(run, actor, 'chat', run['prompt'])
    asyncio.run(scenario())
    assert published == ['这份材料当前不可引用，请重新确认来源。']
