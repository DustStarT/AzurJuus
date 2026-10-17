import asyncio

import pytest
from sqlalchemy import select

from conftest import setup_social_topic
from backend.context.service import topic_ledger
from backend.database import session_scope
from backend.models import Message
from backend.social.social_models import SocialTopic
from backend.social.conversation_policy import closing_context, repeats_own_speech


FIRST = '指挥官晚安～不过我这儿的杂务清单还剩两格，收完尾就睡，保证不熬夜！\n\n同伴也早点休息，明天的早餐交给我来摆盘就好。'
REPLAY = '指挥官晚安～不过我这边杂务清单还剩两格，收完尾就睡，保证不熬夜！\n\n同伴也早点休息，明天的早餐摆盘交给我就好。'


@pytest.mark.parametrize('text,expected', [
    ('各位，晚上好，已经十一点半了，该睡觉了，晚安', True),
    ('大家晚安。', True), ('今天先聊到这，拜拜。', True),
    ('先别睡，继续聊。', False), ('“晚安”是什么意思', False),
    ('翻译一下晚安这个词', False), ('大家晚上好', False),
])
def test_closing_is_a_speech_act_not_a_keyword(text, expected):
    assert closing_context([], {'prompt': text})['closing'] is expected
    assert not closing_context([], {'prompt': text, 'proactive': True})['closing']


def test_same_actor_near_replay_is_not_a_new_answer():
    assert repeats_own_speech(REPLAY, [FIRST], '明天我可以来帮忙吗？')
    assert not repeats_own_speech(REPLAY, [FIRST], '可以把刚才的原话再说一遍吗？')
    assert repeats_own_speech(REPLAY, [FIRST], '不用再说一遍。')
    assert not repeats_own_speech('可以。', ['可以。'], '明天可以来帮忙吗？')


def test_replay_with_deleted_quoted_clause_is_still_rejected():
    first = '德文郡这句“早安来得有点迟”说得可太对了，都快中午啦！我这边杂务清单也快清完了，正好赶上饭点～'
    edited = '德文郡这话说得可太对了，都快中午啦！我这边杂务清单也快清完了，正好赶上饭点～'
    assert repeats_own_speech(edited, [first], edited)


@pytest.mark.parametrize('body,previous', [
    ('清单还剩一格，核对完就结束。', '清单还剩两格，核对完就结束。'),
    ('我不同意这个方案，先确认目录再读取资料。', '我同意这个方案，先确认目录再读取资料。'),
    ('核对完目录以后可以直接读取。更正，应该先确认权限。', '核对完目录以后可以直接读取。'),
    ('核对完目录以后可以直接读取，没问题。', '核对完目录以后可以直接读取。'),
])
def test_corrections_quantities_and_new_answers_remain_eligible(body, previous):
    assert not repeats_own_speech(body, [previous])


def test_peer_mention_is_not_automatically_an_unanswered_request():
    ledger = topic_ledger([
        {'id': 'u', 'speakerId': 'commander', 'text': '各位晚安'},
        {'id': 'a', 'speakerId': 'a', 'text': '同伴也早点休息。', 'mentions': ['b']},
    ])
    assert ledger['openQuestions'] == []
    ledger = topic_ledger([
        {'id': 'q', 'speakerId': 'a', 'text': '明天可以来帮忙吗？', 'mentions': ['b']},
    ])
    assert ledger['openQuestions'][0]['targets'] == ['b']


def test_user_chat_does_not_reenter_actor_after_peer_mention(world):
    engine, _, topic, _ = setup_social_topic(world)
    members = engine.snapshot('port-hub')['members']
    first, second = members[:2]
    room = engine.snapshot('port-hub')
    room['history'] = [
        {'id': 'u', 'speakerId': 'commander', 'text': '早上好，各位', 'mentions': []},
        {'id': 'a', 'speakerId': first['id'], 'text': '早上好。', 'mentions': []},
        {'id': 'b', 'speakerId': second['id'], 'text': f'{first["name"]}说得对。', 'mentions': [first['id']]},
    ]
    topic = {**topic, 'routeKind': 'chat', 'recent': [first['id'], second['id']]}
    assert first['id'] not in {actor['id'] for actor in engine.candidates(room, topic)}


@pytest.mark.parametrize('text', ['晚安之前请读一下这份文件', '晚安，提醒我明早看结果', '晚安之前再说说你的想法', '晚安。\n请看一下这个问题'])
def test_a_request_does_not_disappear_inside_a_farewell(text):
    assert closing_context([], {'prompt': text})['hasUserRequest']


def set_request(engine, topic, text, mentions=()):
    with session_scope() as session:
        row = session.get(SocialTopic, topic['id'])
        row.data = {**row.data, 'prompt': text}
        session.add(Message(id='closing-user', conversation_id='port-hub', speaker_id='commander',
            type='text', body=text, metadata_json={'mentions': list(mentions)}))
    return engine.topic(topic['id'])


def test_genuine_farewell_closes_after_acknowledgement_without_round_robin(world, monkeypatch):
    engine, _, topic, _ = setup_social_topic(world)
    topic = set_request(engine, topic, '各位，已经十一点半了，该睡觉了，晚安')
    calls = []
    async def reply(actor, room, current, source):
        calls.append(actor['id'])
        return {'action': 'speak', 'segments': ['指挥官晚安，安心休息吧。'], 'replyTo': 'closing-user'}
    monkeypatch.setattr(engine, 'decide', reply)
    asyncio.run(engine._exchange(topic['id']))
    assert len(calls) == 1
    assert engine.topic(topic['id'])['status'] == 'ended'


def test_everyone_is_allowed_to_reply_when_user_explicitly_requests_it(world):
    engine, _, topic, actor = setup_social_topic(world)
    members = engine.snapshot('port-hub')['members']
    topic = set_request(engine, topic, '请每个人都说一句晚安。')
    for i, member in enumerate(members):
        assert engine.commit(engine.topic(topic['id']), member, 'goodnight-' + member['id'],
            {'action': 'speak', 'segments': ['晚安。'], 'replyTo': 'closing-user', 'endAfterReply': True})
        assert engine.topic(topic['id'])['status'] == ('ended' if i == len(members)-1 else 'active')


def test_farewell_never_silences_a_concrete_user_question(world):
    engine, _, topic, actor = setup_social_topic(world)
    topic = set_request(engine, topic, '晚安前请回答：这个错误为什么出现？', [actor['id']])
    assert engine.commit(topic, actor, 'answer-before-bed', {'action': 'speak',
        'segments': ['先确认你指的是哪一条错误记录？'], 'mentions': [], 'replyTo': 'closing-user'})
    assert engine.topic(topic['id'])['status'] == 'active'


def test_replayed_own_answer_is_suppressed_even_when_mentioned(world):
    engine, _, topic, actor = setup_social_topic(world)
    assert engine.commit(topic, actor, 'first-real-answer', {'action': 'speak', 'segments': [FIRST]})
    peer = next(a for a in engine.snapshot('port-hub')['members'] if a['id'] != actor['id'])
    assert engine.commit(engine.topic(topic['id']), peer, 'peer-question', {'action': 'speak',
        'segments': ['明天的早餐摆盘，我可以早点过去帮忙吗？'], 'mentions': [actor['id']]})
    before = engine.topic(topic['id'])
    assert not engine.commit(before, actor, 'replayed-answer', {'action': 'speak',
        'segments': [REPLAY], 'replyTo': 'peer-question', 'mentions': [peer['id']]})
    assert engine.topic(topic['id'])['version'] == before['version']
    assert engine.topic(topic['id'])['status'] == 'active'
    assert engine.commit(before, actor, 'actual-new-answer', {'action': 'speak',
        'segments': ['可以，先休息，明早再确认。'], 'replyTo': 'peer-question'})
    with session_scope() as session:
        assert session.get(Message, 'replayed-answer') is None


def test_farewell_rejects_the_screenshot_continuation_without_consuming_version(world):
    engine, _, topic, actor = setup_social_topic(world)
    topic = set_request(engine, topic, '各位，晚上好，已经十一点半了，该睡觉了，晚安')
    for i, body in enumerate(['明天早上需要我准备什么吗？', FIRST, '需要帮忙的话随时说，我还没睡。']):
        assert not engine.commit(topic, actor, 'reopen-' + str(i), {'action': 'speak', 'segments': [body]})
        assert engine.topic(topic['id'])['version'] == topic['version']
    assert engine.commit(topic, actor, 'natural-farewell', {'action': 'speak', 'segments': ['晚安，祝你好梦。']})


def test_closing_guidance_reaches_cognition_and_expression_contracts(world):
    engine, _, topic, actor = setup_social_topic(world)
    topic = set_request(engine, topic, '各位，该睡觉了，晚安')
    room = engine.snapshot('port-hub', actor['id'])
    contract = engine.turn_contract(actor, room, topic)
    assert set(contract[3]) == {'speak', 'wait', 'end'}
    assert '正在道别' in contract[4]
    assert engine.conversation_focus(room, topic)['closing']['closing']


def test_an_old_topics_goodnight_does_not_block_a_new_farewell(world):
    engine, _, topic, actor = setup_social_topic(world)
    with session_scope() as session:
        session.add(Message(id='old-goodnight', conversation_id='port-hub', speaker_id=actor['id'],
            type='text', body='晚安。', metadata_json={'topicId': 'yesterday'}))
    topic = set_request(engine, topic, '大家晚安。')
    assert engine.commit(topic, actor, 'new-goodnight', {'action': 'speak', 'segments': ['晚安。']})


def test_duplicate_required_answer_is_regenerated_instead_of_replayed(world):
    engine, _, topic, actor = setup_social_topic(world)
    assert engine.commit(topic, actor, 'first-answer', {'action': 'speak', 'segments': [FIRST]})
    peer = next(a for a in engine.snapshot('port-hub')['members'] if a['id'] != actor['id'])
    assert engine.commit(engine.topic(topic['id']), peer, 'peer-question', {'action': 'speak',
        'segments': ['明天的早餐摆盘，我可以早点过去帮忙吗？'], 'mentions': [actor['id']]})
    outputs = iter([
        {'action': 'speak', 'segments': [REPLAY], 'replyTo': 'peer-question', 'sourceIds': ['answer-source']},
        {'action': 'speak', 'segments': ['可以，先休息，明早再确认。'], 'replyTo': 'peer-question', 'sourceIds': ['answer-source']},
    ])
    calls = []
    async def model(messages, settings):
        calls.append(messages.copy())
        return next(outputs)
    engine.generate = model
    result = asyncio.run(engine.decide(actor, engine.snapshot('port-hub', actor['id']),
        engine.topic(topic['id']), 'answer-source'))
    assert result['segments'] == ['可以，先休息，明早再确认。']
    assert len(calls) == 2 and '重复' in calls[1][-1]['content']


def test_direct_peer_reply_cannot_keep_pointing_to_an_old_greeting(world):
    engine, _, topic, actor = setup_social_topic(world)
    topic = set_request(engine, topic, '大家晚上好。')
    peer = next(a for a in engine.snapshot('port-hub')['members'] if a['id'] != actor['id'])
    assert engine.commit(topic, peer, 'peer-question', {'action': 'speak',
        'segments': ['你赞成这个方案吗？'], 'mentions': [actor['id']]})
    outputs = iter([
        {'action': 'speak', 'segments': ['我赞成。'], 'replyTo': 'closing-user', 'sourceIds': ['direct-source']},
        {'action': 'speak', 'segments': ['我赞成。'], 'replyTo': 'peer-question', 'sourceIds': ['direct-source']},
    ])
    async def model(messages, settings):
        return next(outputs)
    engine.generate = model
    result = asyncio.run(engine.decide(actor, engine.snapshot('port-hub', actor['id']),
        engine.topic(topic['id']), 'direct-source'))
    assert result['replyTo'] == 'peer-question'
