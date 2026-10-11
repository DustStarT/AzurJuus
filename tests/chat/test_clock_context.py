import asyncio
import json
from datetime import UTC, datetime

import pytest

from backend.database import session_scope
from backend.models import Message
from backend.platform import local_clock
from conftest import fixture_run, setup_social_topic


def fixed_clock(local):
    value = datetime.fromisoformat(local)
    return {'unixSeconds': value.timestamp(), 'local': local,
        'date': value.date().isoformat(), 'hour': value.hour,
        'weekday': value.isoweekday(), 'timezone': 'Asia/Hong_Kong',
        'utcOffsetSeconds': int(value.utcoffset().total_seconds()), 'source': 'machine-clock'}


def clocks(messages):
    return [json.JSONDecoder().raw_decode(m['content'][len(local_clock.TIME_CONTEXT_PREFIX):])[0]
        for m in messages if m['role']=='system' and
        isinstance(m['content'],str) and m['content'].startswith(local_clock.TIME_CONTEXT_PREFIX)]


@pytest.mark.parametrize('cognition_enabled', [False, True])
@pytest.mark.parametrize('question', ['今天吃什么', '今晚的晚饭吃什么'])
def test_group_expression_gets_noon_clock_and_keeps_explicit_future_plans(mind, world, monkeypatch, cognition_enabled, question):
    runtime, _, _, _, _ = mind
    monkeypatch.setattr(runtime, 'enabled', cognition_enabled)
    noon = fixed_clock('2026-10-06T12:00:00+08:00')
    monkeypatch.setattr(local_clock, 'snapshot', lambda: dict(noon))
    engine, _, topic, actor = setup_social_topic(world)
    with session_scope() as session:
        session.add(Message(id='meal-question', conversation_id='port-hub', speaker_id='commander',
            type='text', body=question, created_at=datetime.fromisoformat(noon['local']).astimezone(UTC)))
    room = engine.snapshot('port-hub', actor['id'])
    topic = {**topic, 'prompt': question}
    requests = []
    answer = '晚饭想吃咖喱。' if '今晚' in question else '咖喱配炸猪排怎么样？'
    async def generate(messages, settings):
        requests.append(messages)
        return {'action':'speak', 'segments':[answer], 'sourceIds':['meal-turn']}
    engine.generate = generate
    reply = asyncio.run(engine.decide(actor, room, topic, 'meal-turn'))
    assert reply['segments'] == [answer]  # No keyword rewriting or meal prohibition.
    assert clocks(requests[-1]) == [noon]
    payload = json.loads(requests[-1][-1]['content'])
    assert payload['focus']['current']['text'] == question
    question_row = next(m for m in payload['room']['history'] if m['id']=='meal-question')
    assert datetime.fromisoformat(question_row['createdAt']).timestamp() == question_row['at'] == noon['unixSeconds']


def test_group_retry_refreshes_clock_across_midnight(world, monkeypatch):
    engine, _, topic, actor = setup_social_topic(world)
    monkeypatch.setattr(engine.c.cognition.runtime, 'enabled', False)
    current = [fixed_clock('2026-10-06T23:59:59+08:00')]
    monkeypatch.setattr(local_clock, 'snapshot', lambda: dict(current[0]))
    requests = []
    async def generate(messages, settings):
        requests.append(messages)
        if len(requests)==1:
            current[0] = fixed_clock('2026-10-07T00:00:01+08:00')
            return {'action':'speak', 'segments':[], 'sourceIds':['midnight-turn']}
        return {'action':'speak', 'segments':['咖喱怎么样？'], 'sourceIds':['midnight-turn']}
    engine.generate = generate
    room = engine.snapshot('port-hub', actor['id'])
    result = asyncio.run(engine.decide(actor, room, {**topic,'prompt':'聊聊吃什么'}, 'midnight-turn'))
    assert result['segments'] == ['咖喱怎么样？'] and len(requests)==2
    assert clocks(requests[0])[0]['date'] == '2026-10-06'
    assert clocks(requests[1]) == [current[0]]


@pytest.mark.parametrize('cognition_enabled', [False, True])
def test_private_expression_uses_the_same_clock_context(mind, monkeypatch, cognition_enabled):
    runtime, coordinator, _, ids, _ = mind
    monkeypatch.setattr(runtime, 'enabled', cognition_enabled)
    noon = fixed_clock('2026-10-06T12:00:00+08:00')
    monkeypatch.setattr(local_clock, 'snapshot', lambda: dict(noon))
    run, actor = fixture_run(coordinator, ids[0])
    requests = []
    async def generate(messages, settings):
        requests.append(messages)
        return {'segments':['咖喱怎么样？']}
    async def publish(*args): pass
    coordinator.expression.enabled = True
    coordinator.expression.generate = generate
    coordinator.message_callback = publish
    assert asyncio.run(coordinator.expression.speak(run, actor, 'chat', '今天吃什么')) == '咖喱怎么样？'
    assert clocks(requests[-1]) == [noon]


@pytest.mark.parametrize('kind', ['express', 'moments_post', 'moments_comment'])
def test_autonomous_expression_has_clock_but_profile_compilation_does_not(mind, monkeypatch, kind):
    runtime, _, _, ids, _ = mind
    noon = fixed_clock('2026-10-06T12:00:00+08:00')
    monkeypatch.setattr(local_clock, 'snapshot', lambda: dict(noon))
    requests = []
    async def generate(messages, settings):
        requests.append(messages)
        return {'segments':['咖喱怎么样？']}
    asyncio.run(runtime.call(ids[0], kind, {'sourceId':'time-test'}, generator=generate))
    assert clocks(requests[-1]) == [noon]
    assert json.loads(requests[-1][-1]['content'])['sourceId'] == 'time-test'
    asyncio.run(runtime.call(ids[0], 'profile', {'sources':{}}, generator=generate))
    assert not clocks(requests[-1])


def test_clock_refresh_preserves_message_order_and_original_inputs(monkeypatch):
    current = [fixed_clock('2026-10-06T23:59:59+08:00')]
    monkeypatch.setattr(local_clock, 'snapshot', lambda: dict(current[0]))
    messages = [{'role':'system','content':'角色资料'},
        {'role':'user','content':'今晚吃什么？'}, {'role':'assistant','content':'咖喱怎么样？'}]
    before = local_clock.with_time_context(messages)
    current[0] = fixed_clock('2026-10-07T00:00:01+08:00')
    after = local_clock.with_time_context(before)
    assert clocks(after) == [current[0]]
    assert clocks(before)[0]['date'] == '2026-10-06'
    assert after[0] == messages[0] and after[-2:] == messages[-2:]
    assert len(messages)==3 and messages[-1]['content']=='咖喱怎么样？'
