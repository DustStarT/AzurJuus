import asyncio
import json
import time
from datetime import UTC, datetime, timedelta

import pytest
from conftest import fixture_run, setup_social_topic
from backend.chat.conversation_scope import dialogue_history, is_greeting, current_dialogue_text
from backend.characters.terminal_characters import background, inspect
from backend.database import session_scope
from backend.models import Actor, Message
from backend.mind.cognition_models import Experience
from backend.mind.mind_contracts import ProfileInterpretation


def test_new_greeting_does_not_reopen_an_old_chat_or_remove_its_archive(world):
    engine, run, topic, actor = setup_social_topic(world)
    now = datetime.now(UTC)
    with session_scope() as session:
        session.add_all([
            Message(id='old-progress', conversation_id='port-hub', speaker_id=actor['id'],
                type='text', body='下午三点来核对备考和委托的进度。', created_at=now-timedelta(hours=34)),
            Message(id='fresh-greeting', conversation_id='port-hub', speaker_id='commander',
                type='text', body='晚上好', created_at=now),
        ])
    topic = {**topic, 'prompt': '晚上好'}
    room = engine.snapshot('port-hub', actor['id'])
    assert len(room['history']) == 2 and all(m['at'] for m in room['history'])
    visible = engine.topic_room(room, topic)
    assert [m['id'] for m in visible['history']] == ['fresh-greeting']
    captured = []
    async def model(messages, settings):
        captured.extend(messages)
        return {'action': 'speak', 'segments': ['晚上好。'], 'sourceIds': ['greeting-turn']}
    engine.generate = model
    assert asyncio.run(engine.decide(actor, room, topic, 'greeting-turn'))['action'] == 'speak'
    sent = json.loads(captured[-1]['content'])
    assert len(sent['room']['history']) == 1
    assert not any('下午三点' in m['content'] for m in captured)
    assert not any('已核验工作经历' in m['content'] for m in captured)
    assert len(engine.snapshot('port-hub')['history']) == 2


@pytest.mark.parametrize('greeting', ['晚上好', '早安！', '大家晚上好呀', 'hello'])
def test_greeting_detection_keeps_concrete_requests(greeting):
    assert is_greeting(greeting)
    assert not is_greeting(greeting + '，帮我核对进度')


def test_time_gap_scopes_the_session_but_explicit_followup_keeps_context():
    history = [{'id': 'old', 'at': 1, 'text': '刚才读的是甲.pdf'},
               {'id': 'new', 'at': 36000, 'speakerId': 'commander', 'text': '今天下雨了'}]
    assert dialogue_history(history, '今天下雨了') == history[1:]
    assert dialogue_history(history, '这本书怎么样', preserve_context=True) == history
    payload = {'room': {'history': history}, 'focus': {'current': history[-1]}}
    assert current_dialogue_text(json.dumps(payload, ensure_ascii=False)) == '今天下雨了'
    assert current_dialogue_text('{"focus":"hello","room":null}') == '{"focus":"hello","room":null}'


def test_stock_persona_projection_keeps_user_edits_and_original_source(world):
    _, client, _ = world
    with session_scope() as session:
        actor = session.query(Actor).filter(Actor.source_character == 'Z23').first()
        aid, original = actor.id, actor.system_prompt
    assert '任务提醒' in original
    assert '任务提醒' not in background(aid)[0]
    assert inspect(aid)['text'] == original
    user_text = '任务提醒只是我的自定义标签。请保留我的特别说话风格。'
    client.post('/api/agents/persona', json={'agentId': aid, 'systemPrompt': user_text}).raise_for_status()
    assert user_text in background(aid)[0]


def test_greeting_memory_does_not_revive_stale_progress_or_goals(mind):
    runtime, coordinator, _, ids, _ = mind
    aid = ids[0]
    with session_scope() as session:
        session.add(Experience(id='stale-promise', actor_id=aid, source_seq=0, run_id=None,
            kind='speech', text='我会核对备考和委托的进度。', at=time.time()-34*3600,
            data={'conversationId': 'port-hub', 'ownSpeech': True, 'speakerId': aid}))
        state = coordinator.cognition.state(session, aid)
        state.data = {**state.data, 'commitments': [{'sourceId': 'stale-promise', 'text': '核对进度'}],
            'emotion': {'name': '担心进度', 'at': time.time()-34*3600},
            'mindMood': {'text': '备考压力', 'at': time.time()-34*3600, 'intensity': .5}}
    prompt = json.dumps({'room': {'history': [{'text': '核对备考进度'}, {'text': '晚上好'}]},
                         'focus': {'current': {'text': '晚上好'}}}, ensure_ascii=False)
    snap = runtime.snapshot(aid, prompt, 'new-turn', 'port-hub', mode='social', include_task_memory=False)
    assert not snap['memory']['episodic'] and not snap['memory']['prospective']
    assert not snap['goals'] and snap['emotion'] is None and not snap['mood']
    # An explicit recollection can still retrieve the old event.
    recalled = runtime.snapshot(aid, '你还记得那次备考吗', 'recollection', 'port-hub', mode='social', include_task_memory=False)
    assert any(m['id'] == 'stale-promise' for m in recalled['memory']['episodic'])
    with session_scope() as session:
        assert not session.get(Experience, 'stale-promise').forgotten


def test_private_dialogue_keeps_personality_but_not_execution_strategies(mind):
    runtime, coordinator, _, ids, _ = mind
    aid = ids[0]
    runtime.edit_profile(aid, ProfileInterpretation(tendencies=['有点怕生，熟悉后会开玩笑'],
        methods=['机械方法哨兵'], attentionPriorities=['机械注意哨兵']))
    run, actor = fixture_run(coordinator, aid)
    coordinator.expression.enabled = True
    captured = []
    async def model(messages, settings):
        captured.extend(messages)
        return {'segments': ['嗨。']}
    async def publish(*args): pass
    coordinator.expression.generate = model
    coordinator.message_callback = publish
    assert asyncio.run(coordinator.expression.speak(run, actor, 'chat', '你好')) == '嗨。'
    character = next(m['content'] for m in captured if m['content'].startswith('当前角色：'))
    assert '怕生' in character and '机械方法哨兵' not in character and '机械注意哨兵' not in character
    assert not any('事实包中的执行摘要' in m['content'] for m in captured)
    assert sum('可用表情包标签：' in m['content'] for m in captured) == 1
    assert any('[表情:标签]' in m['content'] for m in captured)
    assert runtime.profile(aid)['interpretation']['methods'] == ['机械方法哨兵']
