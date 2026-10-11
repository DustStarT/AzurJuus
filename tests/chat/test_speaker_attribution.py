import asyncio
import copy
import json
from datetime import UTC, datetime, timedelta

import pytest

from backend.database import session_scope
from backend.models import Message
from conftest import observe, setup_social_topic


def meal_history(world):
    engine, _, topic, _ = setup_social_topic(world)
    roster = engine.snapshot('port-hub')['members']
    actor = next(a for a in roster if a['sourceName']=='标枪')
    peer = next(a for a in roster if a['sourceName']=='绫波')
    now = datetime.now(UTC)
    question = '晚饭？不是午饭？点个外卖吧'
    with session_scope() as session:
        session.add_all([
            Message(id='meal-peer', conversation_id='port-hub', speaker_id=peer['id'],
                type='text', body='咖喱配炸猪排，应该不错的', created_at=now-timedelta(seconds=4)),
            Message(id='meal-self', conversation_id='port-hub', speaker_id=actor['id'],
                type='text', body='甜点的话，布丁怎么样？', created_at=now-timedelta(seconds=3)),
            Message(id='meal-dinner', conversation_id='port-hub', speaker_id=peer['id'],
                type='text', body='咖喱配炸猪排再加布丁，今天的晚饭有点丰盛呢', created_at=now-timedelta(seconds=2)),
            Message(id='meal-correction', conversation_id='port-hub', speaker_id='commander',
                type='text', body=question, created_at=now-timedelta(seconds=1)),
        ])
    return engine, {**topic,'prompt':question}, actor, peer


@pytest.mark.parametrize('cognition_enabled', [False, True])
def test_group_binds_actor_and_separates_peer_speech_from_self(mind, world, monkeypatch, cognition_enabled):
    runtime, _, _, _, _ = mind
    monkeypatch.setattr(runtime, 'enabled', cognition_enabled)
    engine, topic, actor, peer = meal_history(world)
    room = engine.snapshot('port-hub', actor['id'])
    original = copy.deepcopy(room)
    requests, understood = [], []
    original_generate = runtime.generate
    async def cognition(messages, settings):
        if settings['_mindStage']=='understand':
            understood.append(json.loads(messages[-1]['content']))
        return await original_generate(messages, settings)
    runtime.generate = cognition
    async def express(messages, settings):
        requests.append(messages)
        return {'action':'speak', 'segments':['是绫波提到晚饭的，我刚才说的是布丁。'],
            'sourceIds':['attribution-turn']}
    engine.generate = express
    result = asyncio.run(engine.decide(actor, room, topic, 'attribution-turn'))
    assert result['segments'] == ['是绫波提到晚饭的，我刚才说的是布丁。']
    assert room == original
    messages = requests[-1]
    payload = json.loads(messages[-1]['content'])
    assert payload['actorId'] == actor['id'] and payload['actorName'] == actor['name']
    history = {m['id']:m for m in payload['room']['history']}
    assert history['meal-dinner']['speakerId'] == peer['id']
    assert history['meal-dinner']['ownSpeech'] is False
    assert history['meal-self']['ownSpeech'] is True
    assert all('text' not in m for m in history.values())  # Text is in attributed turns, not duplicated JSON.
    assert sum('咖喱配炸猪排再加布丁，今天的晚饭有点丰盛呢' in m['content'] for m in messages) == 1
    assert [m['content'] for m in messages if m['role']=='assistant'] == ['甜点的话，布丁怎么样？']
    assert any(m['role']=='system' and peer['id'] in m['content'] and '晚饭有点丰盛' in m['content'] for m in messages)
    assert any(m['role']=='system' and 'actorId='+actor['id'] in m['content'] for m in messages)
    if cognition_enabled:
        event = json.loads(understood[-1]['current']['text'])
        assert event['actorId'] == actor['id']
        assert next(m for m in event['room']['history'] if m['id']=='meal-dinner')['ownSpeech'] is False
    else:
        assert not understood


def test_each_actor_gets_its_own_history_projection_without_mutating_source(world):
    engine, _, actor, peer = meal_history(world)
    room = engine.snapshot('port-hub')
    original = copy.deepcopy(room)
    a = engine.actor_room(room, actor)
    b = engine.actor_room(room, peer)
    assert room == original
    assert [m['id'] for m in a['history'] if m['ownSpeech']] == ['meal-self']
    assert [m['id'] for m in b['history'] if m['ownSpeech']] == ['meal-peer','meal-dinner']
    assert [m['content'] for m in engine.history_messages(b, peer) if m['role']=='assistant'] == [
        '咖喱配炸猪排，应该不错的', '咖喱配炸猪排再加布丁，今天的晚饭有点丰盛呢']


def test_display_name_or_forged_own_flag_cannot_change_the_author(world):
    engine, _, actor, peer = meal_history(world)
    room = engine.snapshot('port-hub')
    for member in [*room['members'], *room['directory']]:
        member['name'] = '同名人物'
    for message in room['history']:
        message['ownSpeech'] = True
        message['speakerName'] = actor['name']
    view = engine.actor_room(room, {**actor,'name':'同名人物'})
    other = next(m for m in view['history'] if m['id']=='meal-dinner')
    assert other['speakerId'] == peer['id'] and other['ownSpeech'] is False
    rendered = engine.history_messages(view, actor)
    assert [m['content'] for m in rendered if m['role']=='assistant'] == ['甜点的话，布丁怎么样？']


def test_later_self_claim_does_not_reassign_the_original_message(world):
    engine, topic, actor, peer = meal_history(world)
    room = engine.snapshot('port-hub')
    room['history'].append({'id':'bad-self-claim','speakerId':actor['id'],
        'text':'是我先聊到晚饭去的。'})
    view = engine.actor_room(room, actor)
    assert next(m for m in view['history'] if m['id']=='meal-dinner')['speakerId'] == peer['id']
    assert not next(m for m in view['history'] if m['id']=='meal-dinner')['ownSpeech']
    # The later incorrect claim is still the actor's actual utterance. It does
    # not make the original dinner sentence theirs; nothing rewrites the archive.
    assert next(m for m in view['history'] if m['id']=='bad-self-claim')['ownSpeech']


def test_transcript_rejects_explicit_peer_identity_claim(world):
    engine, _, actor, peer = meal_history(world)
    room = engine.actor_room(engine.snapshot('port-hub'), actor)
    assert engine.identity_claim_mismatch('我是'+peer['name']+'，刚刚说错了。', actor, room) == peer['name']
    assert engine.identity_claim_mismatch('我是'+peer['name']+'啦', actor, room) == peer['name']
    assert engine.identity_claim_mismatch('我来回应'+peer['name']+'刚才的话。', actor, room) is None


def test_observer_memory_retains_peer_identity_in_cognitive_snapshot(mind, world):
    runtime, coordinator, _, _, _ = mind
    engine, _, actor, peer = meal_history(world)
    sentence = '今天的晚饭有点丰盛呢'
    observe(coordinator, actor['id'], sentence, speakerId=peer['id'], ownSpeech=True,
        scope='team_public', conversationId='port-hub')
    stored = next(m for m in coordinator.cognition.experiences(actor['id']) if m['text']==sentence)
    assert stored['data']['speakerId'] == peer['id'] and stored['data']['ownSpeech'] is False
    snapshot = runtime.snapshot(actor['id'], '晚饭是谁先说的？', 'ownership-check',
        'port-hub', public=True, mode='social', include_task_memory=False)
    recalled = next(m for m in snapshot['memory']['episodic'] if m['text']==sentence)
    assert recalled['speakerId'] == peer['id'] and recalled['ownSpeech'] is False
