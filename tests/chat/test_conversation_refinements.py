import json
from datetime import UTC, datetime, timedelta, timezone

import pytest
from sqlalchemy import select

from backend.database import session_scope
from backend.chat.expression import validate
from backend.models import Actor, Conversation, Message
from backend.services import isoformat
from backend.characters.terminal_characters import context


def test_sqlite_utc_timestamp_is_not_shifted_by_local_timezone():
    # SQLite returns timezone-aware columns as naive datetimes. The stored
    # clock time is already UTC, even on machines set to UTC+8.
    assert isoformat(datetime(2026, 9, 24, 1, 40)) == '2026-09-24T01:40:00Z'
    assert isoformat(datetime(2026, 9, 24, 9, 40, tzinfo=timezone(timedelta(hours=8)))) == '2026-09-24T01:40:00Z'


def test_graph_excludes_learned_and_default_relationships(world, monkeypatch):
    c, client, ids = world
    a, b = ids[:2]
    relation = {'peerId':b, 'background':[], 'userDefined':'New friendship',
        'sharedSources':['experience'], 'defaultRelationship':{'familiarity':'familiar'},
        'summary':'New friendship after working together'}
    monkeypatch.setattr(c.cognition, 'relationships', lambda aid: [relation] if aid == a else [])
    assert client.get('/api/relationships/graph').json()['edges'] == []
    relation['background'] = [{'text':'Original relationship', 'source':'https://example.com/story'}]
    edge = client.get('/api/relationships/graph').json()['edges'][0]
    assert 'Original relationship' in edge['summary']
    assert 'New friendship' not in json.dumps(edge)


@pytest.mark.parametrize('status,mode', [('paused','task'), ('running','chat'), ('waiting_approval','task')])
def test_team_messages_steer_same_run_and_retry_once(world, tmp_path, status, mode):
    c, client, ids = world
    cid = 'test-current-team'
    with session_scope() as session:
        session.add(Conversation(id=cid, kind='group', title='Team'))
    run, _ = c.store.create({'conversationId':cid, 'teamConversationId':cid, 'collaborative':True,
        'actorId':ids[0], 'actors':[], 'prompt':'Original task', 'mode':'task', 'workspace':str(tmp_path)}, 'team-task')
    c.store.update(run['id'], status=status)
    delivered = []
    class ActiveBridge:
        session_id = 'active-session'
        async def steer(self, text, instruction_id):
            delivered.append(text)
            return {'queued': True}
        async def close(self):
            pass
    if status != 'paused':
        c.bridges[run['id']+':member'] = ActiveBridge()
    payload = {'conversationId':cid, 'content':'Use the revised requirements', 'mode':mode,
        'collaborative':True, 'requestId':'guidance-1'}
    for _ in range(2):
        response = client.post('/api/messages/send', json=payload)
        assert response.status_code == 200, response.text
        assert response.json()['runId'] == run['id']
        assert response.json()['guidance']
    current = c.store.get(run['id'])
    assert current['status'] == status
    assert delivered == ([] if status == 'paused' else [payload['content']])
    assert len(current['steering']) == 1
    assert current['prompt'] == 'Original task'
    assert len(c.store.list()) == 1
    with session_scope() as session:
        rows = session.scalars(select(Message).where(Message.conversation_id == cid)).all()
        assert len(rows) == 1 and rows[0].body == payload['content']
    assert client.post('/api/messages/send', json={**payload, 'content':'Conflicting retry'}).status_code == 409
    c.store.update(run['id'], status='completed')
    assert client.post('/api/messages/send', json=payload).json()['runId'] == run['id']


@pytest.mark.parametrize('text', ['[表情:missing', '[表情:]', '[表情:unknown]', '[表情:开心]\n[表情:疑惑]', 'hello [表情:开心]'])
def test_invalid_sticker_markup_is_rejected(text):
    with pytest.raises(ValueError):
        validate({'segments':[text], 'sourceIds':['source']}, 'source')


def test_sticker_segment_and_custom_persona_context(world):
    _, client, ids = world
    assert validate({'segments':['好的', '[表情:开心]'], 'sourceIds':['s']}, 's') == ['好的', '[表情:开心]']
    client.post('/api/agents/persona', json={'agentId':ids[0], 'systemPrompt':'A custom character'}).raise_for_status()
    prompt, _ = context(ids[0])
    assert 'A custom character' in prompt
    assert '可选表情名' not in prompt  # Supplied once by the expression catalog.
    assert '人物背景' in prompt and '留在心里' in prompt


def test_new_conversation_opens_with_greeting_not_biography(world):
    _, client, ids = world
    messages = client.get('/api/bootstrap').json()['snapshot']['messages']
    with session_scope() as session:
        for aid in ids:
            actor = session.get(Actor, aid)
            opening = session.scalar(select(Message).where(Message.conversation_id == 'dm-'+aid))
            assert opening is not None
            assert opening.body != actor.summary
            assert len(opening.body) < 40
            assert messages['dm-'+aid][0]['createdAt'] == opening.created_at.replace(tzinfo=UTC).isoformat().replace('+00:00', 'Z')
