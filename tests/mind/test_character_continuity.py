"""Exercise actual stage inputs, rendering handoffs and durable relearning."""
import asyncio
import json
from unittest.mock import AsyncMock

import pytest

from backend.characters.dialogue_persona import imported_background, public_dialogue_intent
from backend.database import session_scope
from backend.mind.character_policy import POLICY_VERSION
from backend.mind.cognition_models import Experience
from backend.mind.mind_contracts import ProfileInterpretation
from backend.mind.mind_models import MindProfile
from backend.models import Actor
from conftest import fixture_run, observe, setup_social_topic


REPLY = {'focus': '窗外的雨声', 'stance': '还想安静听一会儿', 'tone': '熟悉的人面前，放松些'}


def test_character_perspective_and_voice_reach_every_mind_stage(mind):
    r, c, _, ids, _ = mind
    original = r.generate
    requests = {}

    async def generate(messages, cfg):
        kind = cfg['_mindStage']
        requests[kind] = messages
        value = await original(messages, cfg)
        if kind == 'profile':
            value.update(speechStyle=['话说一半会停一下'], emotionalHabits=['熟人面前会放松'])
        if kind == 'decide':
            value['reply'] = REPLY
        return value

    r.generate = generate
    decision = asyncio.run(r.think(ids[0], 'rain-turn', '今天雨真大'))
    assert decision['intent']['reply'] == REPLY
    observe(c, ids[0], '雨声听着倒是舒服。')
    asyncio.run(r.reflect())
    for kind in ('profile', 'understand', 'decide', 'reflect'):
        instruction = requests[kind][0]['content']
        assert '性格、在意的事、思考习惯' in instruction
        assert '固定应答模板' in instruction
    snapshot = json.loads(requests['understand'][-1]['content'])
    assert snapshot['profile']['speechStyle'] == ['话说一半会停一下']
    assert '人物自己的简短语言' in requests['decide'][0]['content']


def test_compact_mind_keeps_the_same_shareable_reply(mind, monkeypatch):
    r, c, _, ids, _ = mind
    original = r.generate
    old_settings = c.settings_loader
    monkeypatch.setattr(c, 'settings_loader', lambda: {**old_settings(), 'dialoguePreset': 'compact'})
    calls = []

    async def generate(messages, cfg):
        calls.append(cfg['_mindStage'])
        if cfg['_mindStage'] == 'compact':
            payload = json.loads(messages[-1]['content'])
            source = payload['snapshot']['current']['id']
            assert 'reply.stance' in messages[0]['content']
            return {'frame': {'interpretation': '这雨有点吵。', 'sourceIds': [source]},
                    'intent': {'action': 'speak', 'purpose': '聊会儿雨声', 'reply': REPLY, 'sourceIds': [source]}}
        return await original(messages, cfg)

    r.generate = generate
    result = asyncio.run(r.think(ids[0], 'compact-rain', '雨声真响'))
    assert result['intent']['reply'] == REPLY
    assert calls == ['profile', 'compact']


@pytest.mark.parametrize('group', [False, True])
def test_shareable_direction_survives_private_and_group_expression(mind, monkeypatch, group):
    r, c, _, ids, _ = mind
    original = r.generate

    async def generate(messages, cfg):
        value = await original(messages, cfg)
        if cfg['_mindStage'] == 'decide':
            value.update(reply=REPLY, purpose='PRIVATE_PURPOSE', tradeoff='PRIVATE_TRADEOFF')
        return value

    r.generate = generate
    captured = []

    async def expression(messages, settings):
        captured.extend(messages)
        return {'action': 'speak', 'segments': ['雨声听着倒是舒服。'],
                **({'sourceIds': ['group-rain']} if group else {})}

    if group:
        engine, _, topic, actor = setup_social_topic((c, None, ids))
        engine.generate = expression
        room = engine.snapshot('port-hub', actor['id'])
        result = asyncio.run(engine.decide(actor, room, topic, 'group-rain'))
        assert result['action'] == 'speak'
    else:
        run, actor = fixture_run(c, ids[0])
        c.expression.generate = expression
        c.message_callback = AsyncMock()
        assert asyncio.run(c.expression.speak(run, actor, 'chat', '今天下雨了'))
    prompt = json.dumps(captured, ensure_ascii=False)
    assert REPLY['stance'] in prompt and REPLY['tone'] in prompt
    assert 'PRIVATE_PURPOSE' not in prompt and 'PRIVATE_TRADEOFF' not in prompt


@pytest.mark.parametrize('kind', ['express', 'moments_post', 'moments_comment'])
def test_lightweight_expression_has_identity_even_without_a_compiled_profile(mind, kind):
    r, _, _, ids, _ = mind
    captured = []

    async def generate(messages, cfg):
        captured.extend(messages)
        return {}

    r.generate = generate
    with session_scope() as s:
        name = s.get(Actor, ids[0]).source_character
    asyncio.run(r.call(ids[0], kind, {'sourceId': 'background-turn'}))
    prompt = '\n'.join(m['content'] for m in captured if m['role'] == 'system')
    assert name in prompt
    assert '当前人物：' in prompt and '远程文字' in prompt
    assert '程序如何生成和校验内容' in prompt


def test_old_beliefs_are_archived_and_relearned_without_recursive_summaries(mind):
    r, c, client, ids, _ = mind
    aid = ids[0]
    observe(c, aid, '你说雨声吵，我倒想再听一会儿。')
    eid = c.cognition.experiences(aid)[0]['id']
    old_belief = {'text': 'OLD_SERVICE_SENTINEL', 'kind': 'belief', 'sourceIds': [eid], 'valid': True}
    current_belief = {**old_belief, 'text': 'CURRENT_SUMMARY_SENTINEL', 'policyVersion': POLICY_VERSION}
    with session_scope() as s:
        row = s.get(Experience, eid)
        row.data = {**row.data, 'mindReflection': 'done', 'derived': [old_belief, current_belief]}
    snap = r.snapshot(aid, '雨声', 'recall')
    assert [b['text'] for b in snap['memory']['semantic']] == ['CURRENT_SUMMARY_SENTINEL']
    visible = client.get(f'/api/actors/{aid}/beliefs').json()['beliefs']
    assert [b['text'] for b in visible] == ['CURRENT_SUMMARY_SENTINEL']
    original = r.generate
    reflected = []

    async def generate(messages, cfg):
        if cfg['_mindStage'] == 'reflect':
            snapshot = json.loads(messages[-1]['content'])
            assert snapshot['profile']  # Reflection compiles a pending profile first.
            assert not snapshot['memory']['semantic'] and not snapshot['memory']['procedural']
            assert 'OLD_SERVICE_SENTINEL' not in messages[-1]['content']
            reflected.append(True)
            return {'beliefs': [{'text': '我倒喜欢听雨声，不过她觉得吵。', 'sourceIds': [eid]}]}
        return await original(messages, cfg)

    r.generate = generate
    asyncio.run(r.reflect())
    asyncio.run(r.reflect())
    assert reflected == [True]
    with session_scope() as s:
        row = s.get(Experience, eid)
        assert row.text == '你说雨声吵，我倒想再听一会儿。' and not row.forgotten
        assert row.data['mindReflectionVersion'] == POLICY_VERSION
        assert row.data['derivedHistory'][0]['beliefs'][0] == old_belief
        assert row.data['derived'][0]['policyVersion'] == POLICY_VERSION


def test_policy_upgrade_recompiles_generated_profiles_but_preserves_user_edits(mind):
    r, _, _, ids, _ = mind
    aid = ids[0]
    asyncio.run(r.compile_profile(aid))
    with session_scope() as s:
        row = s.get(MindProfile, aid)
        row.data = {**row.data, 'anchorHash': 'old-policy-hash'}
    rebuilt = r.profile(aid)
    assert rebuilt['status'] == 'pending' and not rebuilt['interpretation']
    assert rebuilt['history'][-1]['interpretation']['values']
    custom = ProfileInterpretation(selfImage='我有自己的打算。', speechStyle=['话短些'],
                                   emotionalHabits=['熟悉了才会开玩笑'])
    r.edit_profile(aid, custom)
    with session_scope() as s:
        row = s.get(MindProfile, aid)
        row.data = {**row.data, 'anchorHash': 'another-old-policy-hash'}
    preserved = r.profile(aid)
    assert preserved['origin'] == 'user' and preserved['status'] == 'ready'
    assert preserved['interpretation'] == custom.model_dump()


def test_projection_keeps_custom_text_and_never_exports_private_intent_fields():
    from pathlib import Path
    root = Path(__file__).resolve().parents[2] / 'resources' / 'characters'
    seed = json.loads((root / 'Z23.json').read_text(encoding='utf-8'))['prompt_seed']
    projected = imported_background(seed, 'Z23')
    assert '你将扮演' not in projected and '角色定位：' in projected
    assert imported_background(seed + '\n我的自定义内容', 'Z23') == seed + '\n我的自定义内容'
    intent = {'action': 'speak', 'reply': {**REPLY, 'secret': 'PRIVATE'}, 'purpose': 'PRIVATE'}
    assert public_dialogue_intent(intent) == {'action': 'speak', 'reply': REPLY}
    assert public_dialogue_intent({**intent, 'action': 'wait'}) == {'action': 'wait'}
