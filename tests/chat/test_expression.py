import asyncio
import json
from uuid import uuid4
import pytest
from conftest import fixture_run
from backend.chat.expression import validate, decode_json_body
from backend.characters.dialogue_persona import public_dialogue_intent
from backend.characters.terminal_characters import activate, inspect, lore

def test_structured_deepseek_call_budget_and_empty_output(world,monkeypatch):
    import httpx
    c,_,_=world
    payloads=[]
    output={'choices':[{'message':{'content':'{"relationships":[]}'},'finish_reason':'stop'}]}
    class Client:
        def __init__(self,**kwargs):pass
        async def __aenter__(self):return self
        async def __aexit__(self,*args):pass
        async def post(self,url,**kwargs):
            payloads.append(kwargs['json'])
            return httpx.Response(200,json=output)
    monkeypatch.setattr('backend.chat.expression.httpx.AsyncClient',Client)
    cfg={'llmBaseUrl':'https://api.deepseek.com/v1','llmModel':'deepseek-flash','llmApiKey':'synthetic'}
    assert asyncio.run(c.expression.complete([],cfg))=={'relationships':[]}
    assert payloads[-1]['thinking']=={'type':'disabled'}
    asyncio.run(c.expression.complete([],{**cfg,'llmBaseUrl':'http://localhost:8000'}))
    assert 'thinking' not in payloads[-1]
    output['choices'][0]['message']['content']=''
    with pytest.raises(ValueError,match='未返回结构化正文'):
        asyncio.run(c.expression.complete([],cfg))
    output['choices'][0]['finish_reason']='length'
    with pytest.raises(ValueError,match='超出输出预算'):
        asyncio.run(c.expression.complete([],cfg))


def test_json_body_accepts_small_wrappers_but_not_extra_objects():
    assert decode_json_body('```JSON\n{"segments":["好"]}\n```')['segments']==['好']
    assert decode_json_body('回复如下：{"segments":["好"]}')['segments']==['好']
    with pytest.raises(json.JSONDecodeError):
        decode_json_body('{"segments":["好"]} {"extra":true}')


def test_deepseek_flash_cognitive_stage_controls_keep_json_mode(world, monkeypatch):
    import httpx
    c,_,_=world
    requests=[]
    class Client:
        def __init__(self, **kwargs):pass
        async def __aenter__(self):return self
        async def __aexit__(self, *args):pass
        async def post(self, url, **kwargs):
            requests.append(kwargs['json'])
            return httpx.Response(200,json={'choices':[{'message':{'content':'{"segments":["好"]}'},
                'finish_reason':'stop'}]})
    monkeypatch.setattr('backend.chat.expression.httpx.AsyncClient',Client)
    cfg={'llmBaseUrl':'https://api.deepseek.com/v1','llmModel':'deepseek-flash','llmApiKey':'synthetic'}
    assert asyncio.run(c.expression.complete([{'role':'user','content':'返回 JSON'}],cfg))['segments']==['好']
    assert requests[0]['response_format']=={'type':'json_object'}
    assert requests[0]['thinking']=={'type':'disabled'}
    assert asyncio.run(c.expression.complete([{'role':'user','content':'返回 JSON'}],
        {**cfg,'_mindStage':'understand'}))['segments']==['好']
    assert requests[1]['response_format']=={'type':'json_object'}
    assert requests[1]['thinking']=={'type':'enabled'}


def test_validator_preserves_technical_parentheses():
    assert validate({'segments':['采用方法（A）。'], 'sourceIds':['x']}, 'x')
    for text in ['（低头）我看过了。','*叹气* 好吧。','你接过了茶。','长'*181]:
        with pytest.raises(ValueError):
            validate({'segments':[text], 'sourceIds':['x']}, 'x')
    with pytest.raises(ValueError):
        validate({'segments':['移动了9份。'], 'sourceIds':['x']}, 'x', facts={'count':3})


def test_validator_normalizes_segment_whitespace_and_line_endings():
    value={'segments':['  第一行\r\n\r\n\r\n第二行  ', '\n  第二气泡\r\n'], 'sourceIds':['x']}
    assert validate(value, 'x') == ['第一行\n\n第二行', '第二气泡']


def test_public_dialogue_intent_excludes_private_cognition_fields():
    assert public_dialogue_intent({'action':'speak','targetId':'peer',
        'purpose':'PRIVATE_PURPOSE','method':'PRIVATE_METHOD','tradeoff':'PRIVATE_TRADEOFF'}) == {
            'action':'speak','targetId':'peer'}


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


def test_mistaken_greeting_does_not_reassign_speaker_or_actor(world):
    c,_,(aid,bid,*_)=world
    run,actor=fixture_run(c,aid)
    run['actors']=[actor,{'id':bid,'name':'乙'}]
    run['history']=[{'role':'user','speakerId':'commander','content':'乙，早上好'},
        {'role':'assistant','speakerId':bid,'content':'前面那件事我看到了'}]
    captured=[]
    async def generate(messages,settings):
        captured.extend(messages)
        return {'segments':['我是'+actor['name']+'，早上好。']}
    async def publish(*args):pass
    c.expression.generate=generate;c.message_callback=publish
    asyncio.run(c.expression.speak(run,actor,'chat','乙，早上好'))
    assert any('commander 始终是用户' in m['content'] for m in captured if m['role']=='system')
    assert any(m['content'].startswith('用户（commander）：乙，早上好') for m in captured)
    assert any(m['role']=='system' and 'speakerId='+bid in m['content'] and m['content'].endswith('前面那件事我看到了') for m in captured)
    assert json.loads(captured[-1]['content'])['respondingActorId']==aid


def test_expression_failure_does_not_retract_work(world):
    c, _, (aid,*_) = world
    run, actor = fixture_run(c, aid)
    c.store.update(run['id'], status='completed')
    async def failure(*args): raise RuntimeError('synthetic outage')
    c.expression.generate = failure
    assert asyncio.run(c.expression.speak(run, actor, 'result', '告知结果', {'summary':'完成'})) == ''
    assert c.store.get(run['id'])['status'] == 'completed'
    assert any(e['type']=='expression.failed' for e in c.store.events())


def test_expression_receives_selected_intent_without_private_appraisal(world, monkeypatch):
    c, _, (aid,*_) = world
    run, actor = fixture_run(c, aid)
    runtime = c.cognition.runtime
    monkeypatch.setattr(runtime, 'active', lambda actor_id: True)
    monkeypatch.setattr(runtime, 'profile', lambda actor_id: {
        'version': 7, 'interpretation': {'coreValues': ['helpful']}})

    async def think(*args, **kwargs):
        return {'profileVersion': 7,
            'frame': {'interpretation': 'PRIVATE_APPRAISAL'},
            'intent': {'action': 'speak', 'purpose': 'answer greeting', 'method': 'brief',
                'targetId': None, 'tradeoff': 'PRIVATE_TRADEOFF',
                'goalUpdate': {'title': 'PRIVATE_GOAL'}}}

    captured = []
    async def call(*args, **kwargs):
        captured.extend(kwargs['messages'])
        return {'segments': ['Hello.']}

    async def publish(*args):
        pass

    monkeypatch.setattr(runtime, 'think', think)
    monkeypatch.setattr(runtime, 'call', call)
    c.message_callback = publish
    assert asyncio.run(c.expression.speak(run, actor, 'chat', 'Hello')) == 'Hello.'
    prompt = json.dumps(captured, ensure_ascii=False)
    assert 'answer greeting' not in prompt
    assert 'PRIVATE_APPRAISAL' not in prompt
    assert 'PRIVATE_TRADEOFF' not in prompt
    assert 'PRIVATE_GOAL' not in prompt


def test_team_final_is_addressed_to_actual_participants(world):
    c, _, (aid,bid,*_) = world
    run, actor = fixture_run(c, aid)
    run=c.store.update(run['id'], mode='task', collaborative=True, status='completed',
        actors=[actor,{'id':bid,'name':'未参与者'}], assignments=[], discussions=[
            {'senderId':aid,'actorId':aid,'status':'completed','visibility':'team','text':'原始建议','spokenText':'公开建议','reply':'公开答复'},
            {'senderId':bid,'actorId':bid,'status':'completed','visibility':'private','text':'私人判断','reply':'私人答复'},
        ])
    requests=[]
    async def generate(messages,settings):
        requests.append(messages)
        return {'segments':['好了，这份清单可以用了。'], 'sourceIds':[run['id']+':result']}
    c.expression.enabled=True
    c.expression.generate=generate
    asyncio.run(c.finish_callback(run['id'],'清单已经核验。'))
    payload=json.loads(requests[0][-1]['content'])
    assert payload['address']=='当前协作群'
    audience=next(m['content'] for m in requests[0] if m['content'].startswith('当前发言场合'))
    assert actor['name'] in audience and '未参与者' not in audience
    prompt=json.dumps(requests[0],ensure_ascii=False)
    assert '公开建议' in prompt and '公开答复' in prompt
    assert '私人判断' not in prompt and '私人答复' not in prompt
    assert c.store.get(run['id'])['resultMessageId'].startswith('speech-')


def test_harness_execution_text_never_enters_chat_when_expression_enabled(world):
    c, _, (aid,*_) = world
    run,actor=fixture_run(c,aid)
    run=c.store.update(run['id'],mode='task',workspace=str(c.store.path.parent))
    published=[]
    class Bridge:
        def __init__(self,*args): self.emit=args[-1]
        async def start(self): pass
        async def prompt(self,*args,**kwargs):
            await self.emit('message.complete',{'text':'仅供工作记录的原始核验报告'})
            c.store.update(run['id'],reviewResult={'summary':'已核验'})
            return '已核验'
        async def close(self): pass
    async def publish(*args): published.append(args)
    c.expression.enabled=True
    c.bridge_factory=Bridge
    c.message_callback=publish
    asyncio.run(c.execute_actor(run,actor,'reviewer','检查结果',c.settings_loader()))
    assert not published
    events=c.store.events()
    assert any(e['type']=='execution.message.complete' for e in events)
    assert not any(e['type']=='message.complete' for e in events)


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


def test_group_chat_replies_are_distinct_attributed_and_idempotent(world, monkeypatch):
    c, client, _ = world
    c.social_engine.enabled = False  # Explicit rollback path; new social engine has separate tests.
    c.expression.enabled = True
    monkeypatch.setattr(c, 'launch', lambda rid: None)
    c.settings_loader = lambda: {'llmApiKey':'synthetic'}
    requests = []
    async def generate(messages, settings):
        requests.append(messages)
        value = json.loads(messages[-1]['content'])
        return {'segments':['我也在。'], 'sourceIds':[value['sourceId']]}
    c.expression.generate = generate
    client.post('/api/workspace/save',json={'workspace':{'settings':{'authorizedWorkspaceRoot':str(c.store.path.parent)}}}).raise_for_status()
    response = client.post('/api/messages/send', json={'conversationId':'port-hub','mode':'chat','content':'你好，各位','requestId':uuid4().hex})
    assert response.status_code == 200, response.text
    rid = response.json()['runId']
    asyncio.run(c.run(rid))
    run = c.store.get(rid)
    assert run['status'] == 'completed', run.get('error')
    assert len(requests) == len(run['actors']) > 1
    assert '当前群聊' in json.dumps(requests[0],ensure_ascii=False)
    assert any(m['role']=='system' and run['actors'][0]['name'] in m['content'] and m['content'].endswith('我也在。') for m in requests[1])
    events = [e for e in c.store.events() if e['type'] == 'message.complete']
    assert len({e['payload']['messageId'] for e in events}) == len(run['actors'])
    assert len({e['payload']['actorId'] for e in events}) == len(run['actors'])
    asyncio.run(c.group_chat(run))
    assert len(requests) == len(run['actors'])


def test_task_answer_can_preserve_multiple_book_descriptions(world):
    c, _, (aid,*_) = world
    run, actor = fixture_run(c, aid)
    run = c.store.update(run['id'],mode='task',prompt='这些书讲什么？')
    answer = '甲书介绍计算机系统，讨论处理器、内存和程序执行。乙书介绍统计方法，讨论抽样与误差。丙书介绍植物分类，讨论叶形和生境。' * 4
    requests = []
    async def generate(messages, settings):
        requests.append(messages)
        return {'segments':[answer],'sourceIds':[run['id']+':result']}
    c.expression.generate = generate
    assert asyncio.run(c.expression.speak(run,actor,'result','告知结果',
        {'request':run['prompt'],'summary':answer,'review':{'summary':'审查通过'}})) == answer
    assert '复核通过只是可信度背景' in requests[0][0]['content']


def test_task_expression_host_binds_source_and_preserves_sticker_at_limit(world):
    from backend.chat.sticker_catalog import catalog
    c,_,(aid,*_)=world
    run,actor=fixture_run(c,aid)
    parts=['资料按主题归档。']*12
    label=catalog()[0]['label']
    seen=[];requests=[]
    async def generate(messages,settings):
        requests.append(settings)
        # An opaque model-supplied ID is neither provenance nor a content claim.
        return {'segments':parts,'sticker':label,'sourceIds':['wrong-generated-id']}
    async def publish(rid,value):seen.append(value)
    c.expression.generate,c.message_callback=generate,publish
    answer=asyncio.run(c.expression.speak(run,actor,'result','告知结果',facts={'summary':parts[0]}))
    assert answer.count(parts[0])==12 and answer.endswith('[表情:'+label+']')
    assert len(requests)==1 and requests[0]['_structuredOutputTokens']==4096
    assert requests[0]['_structuredTimeout']==40
    assert seen[0]['sourceIds']==[run['id']+':result'] and len(seen[0]['segments'])==12
    assert not c.store.get(run['id']).get('expressionError')


def test_list_markers_do_not_bypass_factual_number_checks():
    value={'segments':['1. red=13\n2. blue=8'], 'sourceIds':['x']}
    assert validate(value,'x',True,{'red':13,'blue':8})
    for text in ['1. red=99','13.5 千克','1. blue=8\n2. 移动了99份文件']:
        with pytest.raises(ValueError,match='数字'):
            validate({'segments':[text],'sourceIds':['x']},'x',True,{'red':13,'blue':8})


def test_source_binding_does_not_accept_fabricated_task_facts(world):
    c,_,(aid,*_)=world
    run,actor=fixture_run(c,aid)
    published=[]
    async def generate(*_):return {'segments':['已移动99份文件。']}
    async def publish(*args):published.append(args)
    c.expression.generate,c.message_callback=generate,publish
    assert asyncio.run(c.expression.speak(run,actor,'result','告知结果',facts={'count':3}))==''
    assert not published
    events=[e for e in c.store.events() if e['type']=='expression.validation_failed']
    assert len(events)==2 and all(e['payload']['textChars'] for e in events)


def test_group_targeting_and_partial_failure_remain_visible(world):
    c, _, (aid,bid,*_) = world
    c.social_engine.enabled = False
    run, actor = fixture_run(c, aid)
    run = c.store.update(run['id'],actors=[actor,{'id':bid,'name':'测试同伴'}],prompt='大家好')
    spoken=[]
    async def speak(run,actor,*args,**kwargs):
        spoken.append(actor['id'])
        if actor['id']==aid:
            raise RuntimeError('synthetic timeout')
        # A later successful utterance used to clear the earlier failure.
        c.store.update(run['id'],expressionError=None)
        return '我在。'
    c.expression.speak=speak
    assert asyncio.run(c.group_chat(run))=='我在。'
    saved=c.store.get(run['id'])
    assert saved['expressionError'] and saved['groupChatErrors'][0]['actorId']==aid
    spoken.clear()
    run=c.store.update(run['id'],prompt='测试同伴，你觉得呢？')
    assert asyncio.run(c.group_chat(run))=='我在。'
    assert spoken==[bid]
    assert c.store.get(run['id'])['expressionError'] is None


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
