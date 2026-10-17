"""Thirty fixed turns through the real assembly/cognitive chain, synthetic provider.

Measures submitted input and calls, not actual billing or role-play quality.
"""
import asyncio
import json
import statistics
from pathlib import Path
from uuid import uuid4

from backend.context.service import estimate
from backend.database import session_scope
from backend.models import Actor


def test_thirty_turn_three_preset_replay(mind):
    runtime, c, client, ids, stages = mind
    client.get('/api/bootstrap')
    with session_scope() as s:
        actor = s.get(Actor, ids[0])
        actor_data = {'id': actor.id, 'name': actor.name, 'sourceCharacter': actor.source_character}
    cid = 'dm-' + ids[0]
    c.expression.enabled = True
    # speak() is invoked directly by this replay, without Coordinator.launch's
    # foreground task lease. Keep background indexing yielded just as production does.
    c.context.foreground_busy = lambda: True
    previous_settings = c.settings_loader
    original_generate = runtime.generate
    captured, mode, replies = [], ['balanced'], []
    c.settings_loader = lambda: {**previous_settings(), 'dialoguePreset': mode[0]}
    async def cognitive(messages, cfg):
        if cfg['_mindStage'] != 'profile':
            captured.append({'stage': cfg['_mindStage'], 'estimatedInputTokens': estimate(messages)})
        if cfg['_mindStage'] == 'compact':
            data = json.loads(messages[-1]['content'])
            source = data['snapshot']['current']['id']
            return {'frame': {'interpretation': '回应用户正在聊的兴趣', 'sourceIds': [source]},
                'intent': {'action': 'speak', 'purpose': '自然回应', 'sourceIds': [source]}, 'needsFullChain': False}
        return await original_generate(messages, cfg)
    async def expression(messages, cfg):
        captured.append({'stage': 'express', 'estimatedInputTokens': estimate(messages)})
        return {'segments': ['我在，接着说吧。']}
    async def publish(*args):
        pass
    runtime.generate, c.expression.generate, c.message_callback = cognitive, expression, publish
    history = [{'speakerId': ids[0] if n % 2 else 'commander',
        'content': ('一起读书时，我喜欢讨论书信里表达的关心，也会留意作者写海风和港口的方式。' * 8) + str(n)}
        for n in range(36)]
    prompts = [f'今天想听听你对书信交流的感受，第{n + 1}个小话题。' for n in range(30)]
    report = {'kind': 'offline-production-assembly', 'roundsPerPreset': 30,
        'providerUsage': None, 'blindRoleplayReview': 'requires real-model outputs and human review', 'presets': {}}
    async def replay():
        await runtime.compile_profile(ids[0], False, runtime.revision)
        for preset in ('compact', 'balanced', 'rich'):
            mode[0] = preset
            c.cognition.configure(ids[0], reset=True)
            rows = []
            for prompt in prompts:
                captured.clear()
                run, _ = c.store.create({'actorId': ids[0], 'actors': [actor_data], 'mode': 'chat',
                    'collaborative': False, 'conversationId': cid, 'prompt': prompt, 'history': history}, uuid4().hex)
                reply = await c.expression.speak(run, actor_data, 'chat', prompt)
                assert reply
                rows.append({'input': prompt, 'calls': len(captured), 'stages': list(captured),
                    'estimatedInputTokens': sum(v['estimatedInputTokens'] for v in captured),
                    'retries': max(0, len(captured) - (2 if preset == 'compact' else 3)), 'actualUsage': None})
            report['presets'][preset] = {'medianEstimatedInputTokens': statistics.median(
                r['estimatedInputTokens'] for r in rows), 'calls': sum(r['calls'] for r in rows), 'rounds': rows}
    asyncio.run(replay())
    compact, balanced = report['presets']['compact'], report['presets']['balanced']
    report['estimatedMedianReduction'] = 1 - compact['medianEstimatedInputTokens'] / balanced['medianEstimatedInputTokens']
    output = Path('validation/context-budget-replay.json')
    output.parent.mkdir(exist_ok=True)
    output.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding='utf-8')
    assert compact['calls'] == 60 and balanced['calls'] == 90
    assert report['estimatedMedianReduction'] >= .30, report
