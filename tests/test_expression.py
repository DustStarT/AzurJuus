import asyncio
import json
from uuid import uuid4
import pytest
from test_cognition import world
from backend.expression import validate
from backend.terminal_characters import activate, inspect, lore


def fixture_run(c, aid):
    from backend.database import session_scope
    from backend.models import Actor
    with session_scope() as session:
        actor = session.get(Actor, aid)
        payload = {'id':aid, 'name':actor.name}
    run, _ = c.store.create({'actorId':aid, 'actors':[payload], 'mode':'chat',
        'collaborative':False, 'conversationId':'missing-test-conversation', 'prompt':'你好', 'history':[]},uuid4().hex)
    return run, payload


def test_validator_preserves_technical_parentheses():
    assert validate({'segments':['采用方法（A）。'], 'sourceIds':['x']}, 'x')
    for text in ['（低头）我看过了。','*叹气* 好吧。','你接过了茶。','长'*181]:
        with pytest.raises(ValueError):
            validate({'segments':[text], 'sourceIds':['x']}, 'x')
    with pytest.raises(ValueError):
        validate({'segments':['移动了9份。'], 'sourceIds':['x']}, 'x', facts={'count':3})


def test_peer_reply_uses_visible_question_and_peer_address(world):
    from backend.database import session_scope
    from backend.models import Actor
    c, _, (aid,bid,*_) = world
    with session_scope() as session:
        actors=[{'id':i,'name':session.get(Actor,i).name} for i in (aid,bid)]
    run,_=c.store.create({'actorId':aid,'actors':actors,'collaborative':True,'mode':'task',
        'prompt':'合成核对任务','history':[]},uuid4().hex)
    c.store.update(run['id'],assignments=[{'actorId':aid},{'actorId':bid}])
    requests=[]
    async def generate(messages, settings):
        payload=json.loads(messages[-1]['content'])
        requests.append(payload)
        return {'segments':['这个附件也要核对吗？' if len(requests)==1 else '要，先看看有没有漏页。'],
            'sourceIds':[payload['sourceId']]}
    async def publish(*args): pass
    c.expression.enabled=True
    c.expression.generate=generate
    c.message_callback=publish
    async def check():
        await c.dialogue.send(run['id'],aid,{'actorId':bid,'text':'执行原稿：请判断附件是否也需要核对，暂未检查。'})
        await c.dialogue.drain(run['id'])
    asyncio.run(check())
    assert [r['address'] for r in requests] == [actors[1]['name'],actors[0]['name']]
    assert requests[1]['facts']['question']=='这个附件也要核对吗？'
    assert '执行原稿' in requests[1]['facts']['originalPoint']
    discussion=c.store.get(run['id'])['discussions'][0]
    assert discussion['status']=='completed' and discussion['spokenText']=='这个附件也要核对吗？'


def test_expression_repair_idempotence_and_no_tools(world):
    c, _, (aid,*_) = world
    c.expression.enabled = True
    run, actor = fixture_run(c, aid)
    requests, published = [], []
    async def generate(messages, settings):
        requests.append(messages)
        return {'segments':['（点头）好。' if len(requests)==1 else '嗯，我在。'], 'sourceIds':[run['id']+':chat']}
    async def publish(rid, payload): published.append(payload)
    c.expression.generate, c.message_callback = generate, publish
    async def check():
        assert await c.expression.speak(run, actor, 'chat', '你好') == '嗯，我在。'
        assert await c.expression.speak(run, actor, 'chat', '你好') == '嗯，我在。'
    asyncio.run(check())
    assert len(requests) == 2 and len(published) == 1
    assert published[0]['expression'] and published[0]['sourceIds']
    assert 'workspace 工具' not in json.dumps(requests,ensure_ascii=False)


def test_expression_failure_does_not_retract_work(world):
    c, _, (aid,*_) = world
    run, actor = fixture_run(c, aid)
    c.store.update(run['id'], status='completed')
    async def failure(*args): raise RuntimeError('synthetic outage')
    c.expression.generate = failure
    assert asyncio.run(c.expression.speak(run, actor, 'result', '告知结果', {'summary':'完成'})) == ''
    assert c.store.get(run['id'])['status'] == 'completed'
    assert any(e['type']=='expression.failed' for e in c.store.events())


def test_invalid_json_gets_one_repair(world):
    c, _, (aid,*_) = world
    run, actor = fixture_run(c, aid)
    attempts = []
    async def generate(*args):
        attempts.append(True)
        if len(attempts) == 1:
            raise json.JSONDecodeError('synthetic malformed result', '{', 1)
        return {'segments':['我在。'], 'sourceIds':[run['id']+':chat']}
    async def publish(*args): pass
    c.expression.generate, c.message_callback = generate, publish
    assert asyncio.run(c.expression.speak(run, actor, 'chat', '你好')) == '我在。'
    assert len(attempts) == 2


def test_card_override_and_prompt_preview(world):
    c, client, (aid,*_) = world
    original = inspect(aid)['text']
    assert inspect(aid)['version'] == 'legacy-database'
    client.post('/api/agents/persona', json={'agentId':aid,'systemPrompt':'用户保留的人设'}).raise_for_status()
    activate(aid, 'apply')
    assert inspect(aid)['text'] == '用户保留的人设'
    activate(aid, 'restore')
    assert '原创终端表达示例' in inspect(aid)['text']
    preview = client.get(f'/api/actors/{aid}/prompt-preview').json()
    assert 'llmApiKey' not in str(preview) and preview['version'].startswith('terminal-')
    assert len(lore('埃佛森 七省 实验', '埃佛森')) <= 6


def test_ready_outbox_recovers_without_regeneration(world):
    c, _, (aid,*_) = world
    run, actor = fixture_run(c, aid)
    c.store.update(run['id'], status='completed')
    source = run['id'] + ':result'
    import hashlib
    sid = 'speech-' + hashlib.sha256(source.encode()).hexdigest()[:24]
    c.expression.save(sid, run['id'], aid, 'ready', {'segments':['好了。'], 'intent':'结果', 'phase':'result','source':source})
    seen = []
    async def publish(rid,payload): seen.append(payload)
    async def unexpected(*args): raise AssertionError('must not regenerate')
    c.message_callback, c.expression.generate = publish, unexpected
    asyncio.run(c.expression.recover())
    asyncio.run(c.expression.recover())
    assert len(seen) == 1


def test_concurrent_delivery_and_failed_publish_recovery(world):
    c, _, (aid,*_) = world
    run, actor = fixture_run(c, aid)
    c.store.update(run['id'], status='completed')
    generated, delivered = [], []
    async def generate(*args):
        generated.append(True)
        await asyncio.sleep(.01)
        return {'segments':['整理好了。'], 'sourceIds':[run['id']+':result']}
    async def unavailable(*args): raise RuntimeError('publication interrupted')
    async def publish(rid, payload): delivered.append(payload)
    c.expression.generate, c.message_callback = generate, unavailable
    async def check():
        await c.expression.speak(run, actor, 'result', '告知结果')
        c.message_callback = publish
        await asyncio.gather(c.expression.recover(), c.expression.recover())
    asyncio.run(check())
    assert len(generated) == len(delivered) == 1
    assert c.store.get(run['id'])['status'] == 'completed'
