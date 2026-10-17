import asyncio
import json
import sqlite3
import time
from datetime import datetime
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from backend.database import session_scope
from backend.mind.cognition_models import Experience, MindState
from backend.mind.mind_models import MindDecision, MindCall, LifeActivity, LifeRecord, MindControl
from backend.mind.mind_contracts import StateDelta
from backend.mind.mind_runtime import MindInterrupted
from conftest import observe


def test_cycle_is_structured_and_idempotent(mind):
    r,c,_,ids,stages=mind
    first=asyncio.run(r.think(ids[0],'request-1','你好'))
    second=asyncio.run(r.think(ids[0],'request-1','你好'))
    assert first==second
    assert stages==['profile','understand','decide']
    assert first['intent']['action']=='speak'
    assert c.cognition.inspect(ids[0])['data']['emotion']['cause']=='希望做好'
    with session_scope() as s:
        assert len(s.scalars(select(MindDecision)).all())==1


@pytest.mark.parametrize('repair_succeeds',[True,False])
def test_task_expression_budget_repair_and_result_intent(mind,repair_succeeds):
    from conftest import fixture_run
    r,c,_,ids,_=mind
    run,actor=fixture_run(c,ids[0])
    run=c.store.update(run['id'],mode='task',status='completed')
    attempts=[]
    async def generate(messages,settings):
        attempts.append(settings)
        assert settings['_structuredOutputTokens']==4096
        assert settings['_structuredTimeout']==40
        if len(attempts)==1 or not repair_succeeds:return {'segments':['已移动99份文件。']}
        assert '数字' in messages[-1]['content']
        return {'segments':['3份资料仍在原位。']}
    c.expression.generate=generate
    c.message_callback=AsyncMock()
    answer=asyncio.run(c.expression.speak(run,actor,'result','说明结果',facts={'count':3}))
    assert len(attempts)==2  # One repair owner, no nested four-attempt loop.
    assert r.traces(ids[0])[0]['intent']['action']=='speak'
    assert bool(answer)==repair_succeeds
    assert c.message_callback.await_count==int(repair_succeeds)
    with session_scope() as s:
        calls=s.scalars(select(MindCall).where(MindCall.kind=='express')).all()
        assert len(calls)==2
        assert sum(v.data['status']=='failed' for v in calls)==(1 if repair_succeeds else 2)


def test_stale_frame_cannot_overwrite_new_observation(mind):
    r,c,_,ids,_=mind
    original=r.generate
    async def generate(messages,cfg):
        value=await original(messages,cfg)
        if cfg['_mindStage']=='decide':observe(c,ids[0],'刚收到新的请求')
        return value
    r.generate=generate
    with pytest.raises(MindInterrupted):asyncio.run(r.think(ids[0],'stale','原问题'))
    assert 'emotion' not in c.cognition.inspect(ids[0])['data']
    assert r.traces(ids[0])[0]['status']=='interrupted'


def test_foreground_decision_retries_after_one_new_observation(mind):
    r,c,_,ids,stages=mind
    original=r.generate;injected=[]
    async def generate(messages,cfg):
        value=await original(messages,cfg)
        if cfg['_mindStage']=='decide' and not injected:
            injected.append(True)
            observe(c,ids[0],'推理期间收到更新')
        return value
    r.generate=generate
    decision=asyncio.run(r.think(ids[0],'refresh-once','继续回应'))
    assert decision['intent']['action']=='speak'
    assert stages.count('understand')==2 and stages.count('decide')==2
    assert r.traces(ids[0])[0]['status']=='committed'


def test_public_memory_excludes_private_events(mind):
    r,c,_,ids,_=mind
    observe(c,ids[0],'私密口令：檀木',conversationId='private-room')
    observe(c,ids[0],'公开讨论天气',conversationId='port-hub')
    snap=r.snapshot(ids[0],'天气','current','port-hub',True)
    assert '檀木' not in json.dumps(snap,ensure_ascii=False)
    assert '公开讨论天气' in json.dumps(snap,ensure_ascii=False)
    assert '檀木' not in json.dumps(r.snapshot(ids[1],'天气','other'),ensure_ascii=False)
    other_room=r.snapshot(ids[0],'天气','another','different-room',True)
    assert '公开讨论天气' not in json.dumps(other_room,ensure_ascii=False)


def test_actor_decisions_overlap_only_across_distinct_people(mind):
    r,_,_,ids,_=mind
    original=r.generate
    active=0
    peak=0
    async def generate(messages,cfg):
        nonlocal active,peak
        if cfg['_mindStage']=='understand':
            active+=1
            peak=max(peak,active)
            await asyncio.sleep(.04)
            active-=1
        return await original(messages,cfg)
    r.generate=generate
    async def pair(first,second,prefix):
        return await asyncio.gather(r.think(first,prefix+'-a','hello'),
            r.think(second,prefix+'-b','hello'))
    asyncio.run(pair(ids[0],ids[1],'parallel'))
    assert peak==2
    peak=0
    asyncio.run(pair(ids[0],ids[0],'serial'))
    assert peak==1


def test_online_life_dispatches_two_slots(mind,monkeypatch):
    from backend.mind.life_runtime import LifeRuntime
    r,c,_,_,_=mind
    monkeypatch.setattr(r.life,'pulse',lambda:None)
    monkeypatch.setattr(r,'decay',lambda:None)
    monkeypatch.setattr(c.relationship_research,'has_pending',lambda:False)
    active=0
    peak=0
    async def slot():
        nonlocal active,peak
        active+=1
        peak=max(peak,active)
        await asyncio.sleep(.04)
        active-=1
    monkeypatch.setattr(r.life,'_tick_once',slot)
    asyncio.run(LifeRuntime.tick(r.life))
    assert peak==2


def test_empty_model_body_is_counted_without_saving_reasoning(mind):
    r,_,_,ids,_=mind
    async def empty(messages,cfg):
        raise ValueError('模型未返回结构化正文')
    r.generate=empty
    with pytest.raises(ValueError):
        asyncio.run(r.call(ids[0],'express',{}))
    usage=r.control()
    assert usage['emptyResponsesLastHour']==3
    assert usage['stagesLastHour'][0]['failures']==3


@pytest.mark.parametrize('first_body,empty_count',[(' '*77,1),('not a JSON object',0)])
def test_unstructured_response_retries_without_provider_json_mode(mind, monkeypatch, first_body, empty_count):
    import httpx
    r,c,_,ids,_=mind
    requests=[]
    class Client:
        def __init__(self, **kwargs):pass
        async def __aenter__(self):return self
        async def __aexit__(self, *args):pass
        async def post(self, url, **kwargs):
            requests.append(kwargs['json'])
            content=first_body if len(requests)==1 else '{"segments":["好了。"]}'
            return httpx.Response(200,json={'choices':[{'message':{'content':content},'finish_reason':'stop'}],
                'usage':{'completion_tokens':77,'total_tokens':120}})
    monkeypatch.setattr('backend.chat.expression.httpx.AsyncClient',Client)
    cfg={'llmBaseUrl':'https://example.test/v1','llmModel':'generic-json','llmApiKey':'synthetic'}
    value=asyncio.run(r.call(ids[0],'express',{},generator=c.expression.complete,settings=cfg))
    assert value['segments']==['好了。']
    assert len(requests)==2
    assert requests[0]['response_format']=={'type':'json_object'}
    assert 'response_format' not in requests[1]
    assert requests[1]['temperature']==.2
    assert 'thinking' not in requests[1]
    assert r.control()['emptyResponsesLastHour']==empty_count


def test_decision_style_ablation_keeps_identity_and_sources(mind,monkeypatch):
    r,_,_,ids,_=mind
    asyncio.run(r.compile_profile(ids[0]))
    regular=r.snapshot(ids[0],'hello','regular','room-a',True)
    assert 'attentionPriorities' in regular['profile']
    monkeypatch.setenv('AZURJUUS_MIND_DECISION_STYLE','generic')
    generic=r.snapshot(ids[0],'hello','generic','room-a',True)
    assert 'attentionPriorities' not in generic['profile']
    assert generic['actorId']==regular['actorId']
    assert generic['background']==regular['background']


@pytest.mark.parametrize('failure',[RuntimeError('HTTP 429'),TimeoutError('model timeout')])
def test_background_model_failure_defers_without_consuming_plan(mind,failure):
    from backend.mind.life_runtime import LifeRuntime
    r,c,_,ids,_=mind
    for aid in ids[2:]:c.cognition.configure(aid,enabled=False)
    c.relationship_research.has_pending=lambda:False
    active=0
    peak=0
    async def unavailable(messages,cfg):
        nonlocal active,peak
        active+=1
        peak=max(peak,active)
        await asyncio.sleep(.04)
        active-=1
        raise failure
    r.generate=unavailable
    asyncio.run(LifeRuntime.tick(r.life))
    with session_scope() as s:
        queue=s.get(MindControl,'queue').data
        for aid in ids[:2]:
            assert queue['wake'][aid]['reason']=='failed'
            assert s.get(MindControl,'life-work:'+aid).data['source']
    assert r.control()['backgroundFailuresLastHour']>=2
    assert peak==2


def test_invisible_sources_and_tool_actions_rejected(mind):
    r,_,_,ids,_=mind
    original=r.generate
    async def generate(messages,cfg):
        value=await original(messages,cfg)
        if cfg['_mindStage']=='decide':value['sourceIds']=['secret-other-actor']
        return value
    r.generate=generate
    with pytest.raises(ValueError):asyncio.run(r.think(ids[0],'invalid','你好'))
    with pytest.raises(ValueError):r.life.start(ids[0],{'action':'execute'},'forged')


def test_semantic_validation_repairs_without_relaxing_permissions(mind):
    r,_,_,ids,_=mind
    original=r.generate
    attempts=[]
    async def generate(messages,cfg):
        value=await original(messages,cfg)
        if cfg['_mindStage']=='decide':
            payload=json.loads(messages[-1]['content']);attempts.append(payload)
            if len(attempts)==1:value['action']='execute'
        return value
    r.generate=generate
    result=asyncio.run(r.think(ids[0],'repair-action','阅读片刻',mode='life',background=True,actions=['read','wait']))
    assert result['intent']['action']=='read'
    assert len(attempts)==2 and 'allowedActions' in attempts[1]['validationCorrection']
    with session_scope() as s:
        calls=s.scalars(select(MindCall).where(MindCall.kind=='decide')).all()
        assert sorted(v.data['status'] for v in calls)==['completed','failed']
        assert all(v.data['background'] for v in calls)


def test_goals_limit_and_status_reason(mind):
    r,_,client,ids,_=mind
    payload={'title':'阅读','motivation':'兴趣','nextStep':'整理读书计划'}
    goals=[client.post(f'/api/actors/{ids[0]}/goals',json={**payload,'title':str(i)}).json() for i in range(3)]
    assert client.post(f'/api/actors/{ids[0]}/goals',json=payload).status_code==422
    assert client.post(f"/api/actors/{ids[0]}/goals/{goals[0]['id']}",json={**payload,'status':'paused'}).status_code==422
    response=client.post(f"/api/actors/{ids[0]}/goals/{goals[0]['id']}",json={**payload,'status':'paused','reason':'先完成工作'})
    assert response.status_code==200
    assert client.post(f'/api/actors/{ids[0]}/goals',json=payload).status_code==200


def test_joint_activity_needs_independent_acceptance(mind):
    r,_,_,ids,_=mind
    aid=r.life.start(ids[0],{'action':'invite','purpose':'聊聊阅读','targetId':ids[1]},'joint')
    assert r.life.activities(ids[0])[0]['status']=='invited'
    assert r.life.events(ids[1])==[]
    with pytest.raises(ValueError):r.life.respond(aid,ids[0],True,'替她同意')
    with pytest.raises(ValueError):r.life.transition(aid,ids[0],'finish','完成了')
    r.life.respond(aid,ids[1],True,'愿意一起聊')
    assert r.life.activities(ids[0])[0]['status']=='active'
    assert len(r.life.events(ids[1]))==1
    assert r.life.events(ids[2])==[]
    asyncio.run(r.life.converse(r.life.activities(ids[0])[0],ids[0],{'intent':{'action':'speak'}}))
    shared=next(event for event in r.life.events(ids[1]) if event['kind']=='dialogue')
    assert shared['actorId']==ids[0] and shared['participants']==ids[:2]
    with session_scope() as s:
        row=s.get(LifeActivity,aid);row.data={**row.data,'onlineSeconds':200}
    with pytest.raises(ValueError):r.life.transition(aid,ids[0],'finish','只等时间，未发生交流')
    with pytest.raises(ValueError):r.life.start(ids[1],{'action':'read','purpose':'阅读'},'conflict')


def test_group_contact_belongs_only_to_speaker(mind,monkeypatch):
    r,c,client,ids,_=mind
    response=client.post('/api/conversations/groups',json={'title':'生活测试群','memberIds':ids[:3]})
    response.raise_for_status()
    cid=response.json()['conversationId']
    monkeypatch.setattr(r.life,'can_contact',lambda *_:True)
    monkeypatch.setattr(c.social_engine,'exchange',AsyncMock())
    captured=[]
    original_call=r.call
    async def capture(actor_id,kind,payload,**kwargs):
        if kind=='express':
            captured.append(payload)
            return {'segments':['我想听听你们对这个想法的看法。'],'sourceIds':[payload['sourceId']]}
        return await original_call(actor_id,kind,payload,**kwargs)
    monkeypatch.setattr(r,'call',capture)
    asyncio.run(r.life.contact(ids[0],{'id':'contact-decision',
        'intent':{'targetId':cid,'purpose':'和同伴讨论自己的想法'}}))
    assert captured[0]['audience']['kind']=='group'
    assert {p['id'] for p in captured[0]['audience']['peers']}==set(ids[1:3])
    assert 'decisionPurpose' not in captured[0]
    assert '等待用户' in captured[0]['intent']
    own=client.get(f'/api/actors/{ids[0]}/life').json()['events']
    assert len(own)==1 and own[0]['kind']=='contact' and own[0]['actorId']==ids[0]
    assert own[0]['participants']==[ids[0]]
    assert client.get(f'/api/actors/{ids[1]}/life').json()['events']==[]
    assert not any(row['data'].get('eventId')==own[0]['id'] for row in c.cognition.experiences(ids[1]))
    with session_scope() as s:
        from backend.models import Message
        assert s.scalar(select(Message).where(Message.conversation_id==cid,Message.speaker_id==ids[0]))
        from backend.social.social_models import SocialTopic
        assert any(t.data.get('openingMessageId') for t in s.scalars(select(SocialTopic)).all())


def test_autonomous_group_opening_cannot_wait_for_user(mind,monkeypatch):
    r,c,client,ids,_=mind
    response=client.post('/api/conversations/groups',json={'title':'同伴聊天','memberIds':ids[:2]})
    response.raise_for_status()
    cid=response.json()['conversationId']
    monkeypatch.setattr(r.life,'can_contact',lambda *_:True)
    exchange=AsyncMock()
    monkeypatch.setattr(c.social_engine,'exchange',exchange)
    async def waiting(actor_id,kind,payload,**kwargs):
        assert kind=='express'
        return {'segments':['等指挥官回来我们再聊吧。'],'sourceIds':[payload['sourceId']]}
    monkeypatch.setattr(r,'call',waiting)
    asyncio.run(r.life.contact(ids[0],{'id':'unsolicited-wait','intent':{'targetId':cid}}))
    with session_scope() as s:
        from backend.models import Message
        from backend.social.social_models import SocialTopic
        assert not s.scalars(select(Message).where(Message.conversation_id==cid)).all()
        assert not s.scalars(select(SocialTopic).where(SocialTopic.conversation_id==cid)).all()
    exchange.assert_not_awaited()


def test_contact_repair_revokes_non_speaker_memory_and_is_idempotent(mind):
    from pathlib import Path
    r,c,_,ids,_=mind
    a,b=ids[:2]
    with session_scope() as s:
        event=LifeRecord(id='legacy-contact',data={'id':'legacy-contact','kind':'contact',
            'actorId':a,'participants':[a,b],'text':'在频道发出文字：测试',
            'at':time.time(),'sourceKind':'simulation','activityId':''})
        s.add(event);s.flush()
        for aid in (a,b):
            s.add(Experience(id='legacy-'+aid,actor_id=aid,source_seq=-event.seq,
                kind='simulation',text='在频道发出文字：测试',at=time.time(),
                data={'eventId':event.id,'mindReflection':'pending'}))
        s.add(Experience(id='dependent-memory',actor_id=b,source_seq=0,
            kind='social',text='基于错误经历的判断',at=time.time(),
            data={'dependencies':['legacy-'+b],'reflection':'done'}))
        state=c.cognition.state(s,b)
        state.data={**state.data,'focus':[{'sourceId':'legacy-'+b,'text':'错误关注'}],
            'commitments':[{'sourceId':'dependent-memory','text':'错误承诺'}],
            'emotion':{'sourceIds':['legacy-'+b],'at':time.time(),'intensity':.8},
            'mindMood':{'text':'错误心境','intensity':.8}}
        s.add(MindDecision(id='legacy-decision',actor_id=b,version=0,status='committed',
            at=time.time(),data={'sourceIds':['legacy-'+b]}))
        s.add(MindDecision(id='dependent-decision',actor_id=b,version=0,status='committed',
            at=time.time(),data={'sourceIds':['dependent-memory']}))
        s.add(LifeActivity(id='legacy-activity',actor_id=b,status='active',updated=time.time(),
            data={'participants':[b],'decisionId':'legacy-decision','goalId':'',
                'kind':'rest','onlineSeconds':0}))
    assert r.life.events(b)==[]  # API attribution guard works before migration.
    c.cognition.initialize()  # The startup path performs the repair before normal recovery.
    with session_scope() as s:
        assert s.scalar(select(LifeRecord).where(LifeRecord.id=='legacy-contact')).data['participants']==[a]
        assert not s.get(Experience,'legacy-'+a).forgotten
        assert s.get(Experience,'legacy-'+b).forgotten
        assert s.get(MindDecision,'legacy-decision').status=='invalidated'
        assert s.get(MindDecision,'dependent-decision').status=='invalidated'
        assert s.get(Experience,'dependent-memory').data['reflection']=='invalidated'
        assert s.get(Experience,'dependent-memory').forgotten
        assert s.get(LifeActivity,'legacy-activity').status=='suspended'
        state=s.get(MindState,b)
        assert 'emotion' not in state.data and not state.data['focus'] and not state.data['commitments']
        marker=s.get(MindControl,'contact-attribution-v1').data
        assert marker['correctedEvents']==1 and marker['invalidatedExperiences']==1
        assert Path(marker['backup']).is_file()
        version=state.version
    assert '在频道发出文字：测试' not in c.cognition.context(b)
    assert '基于错误经历的判断' not in c.cognition.context(b)
    with sqlite3.connect(marker['backup']) as backup:
        previous=json.loads(backup.execute("SELECT data FROM life_events WHERE id='legacy-contact'").fetchone()[0])
        assert previous['participants']==[a,b]
        assert backup.execute("SELECT forgotten FROM mind_experiences WHERE id=?",('legacy-'+b,)).fetchone()[0]==0
    c.cognition.initialize()
    with session_scope() as s:
        assert s.get(MindState,b).version==version
        assert s.get(MindControl,'contact-attribution-v1').data==marker


def test_contact_repair_rolls_back_after_interruption(mind,monkeypatch):
    r,c,_,ids,_=mind
    a,b=ids[:2]
    with session_scope() as s:
        event=LifeRecord(id='interrupted-contact',data={'id':'interrupted-contact','kind':'contact',
            'actorId':a,'participants':[a,b],'text':'测试','at':time.time(),
            'sourceKind':'simulation','activityId':''})
        s.add(event);s.flush()
        s.add(Experience(id='interrupted-memory',actor_id=b,source_seq=-event.seq,
            kind='simulation',text='测试',at=time.time(),data={'eventId':event.id}))
    original=r.invalidate_in_session
    def interrupt(*_):raise RuntimeError('synthetic interruption')
    monkeypatch.setattr(r,'invalidate_in_session',interrupt)
    with pytest.raises(RuntimeError,match='synthetic interruption'):
        r.life.repair_contact_attribution()
    with session_scope() as s:
        assert s.scalar(select(LifeRecord).where(LifeRecord.id=='interrupted-contact')).data['participants']==[a,b]
        assert not s.get(Experience,'interrupted-memory').forgotten
    monkeypatch.setattr(r,'invalidate_in_session',original)
    r.life.repair_contact_attribution()
    with session_scope() as s:
        assert s.scalar(select(LifeRecord).where(LifeRecord.id=='interrupted-contact')).data['participants']==[a]
        assert s.get(Experience,'interrupted-memory').forgotten


def test_goal_progress_requires_completed_activity_evidence(mind):
    r,_,_,ids,_=mind
    goal=r.save_goal(ids[0],{'title':'阅读','motivation':'兴趣','nextStep':'阅读已有资料','sourceIds':['user-setting']})
    original=r.generate
    evidence=['invented']
    async def generate(messages,cfg):
        value=await original(messages,cfg)
        if cfg['_mindStage']=='decide':value['goalUpdate']={'goalId':goal['id'],'status':'completed','nextStep':'休息','reason':'已完成这一轮资料阅读','evidenceIds':evidence}
        return value
    r.generate=generate
    with pytest.raises(ValueError):asyncio.run(r.think(ids[0],'no-evidence','完成目标了吗？'))
    aid=r.life.start(ids[0],{'action':'read','purpose':'阅读','goalId':goal['id']},'progress')
    with session_scope() as s:
        row=s.get(LifeActivity,aid);row.data={**row.data,'onlineSeconds':120}
    r.life.transition(aid,ids[0],'finish','阅读结束')
    evidence[:]=r.goals(ids[0])[0]['evidence']
    asyncio.run(r.think(ids[0],'with-evidence','可以整理下一步了'))
    assert r.goals(ids[0])[0]['status']=='completed'


def test_public_snapshot_does_not_leak_emotion_cause(mind):
    r,c,_,ids,_=mind
    with session_scope() as s:
        state=c.cognition.state(s,ids[0]);state.data={**state.data,'emotion':{'cause':'私密口令','sourceIds':['private-event']}}
    assert '私密口令' not in json.dumps(r.snapshot(ids[0],'你好','public',public=True),ensure_ascii=False)


def test_reset_participant_cancels_joint_activity_and_social_controls_match(mind):
    r,_,client,ids,_=mind
    r.life.start(ids[0],{'action':'invite','purpose':'聊天','targetId':ids[1]},'reset-joint')
    r.invalidate(ids[1],reset=True)
    assert r.life.activities(ids[0])[0]['status']=='cancelled'
    assert client.post('/api/social/settings',json={'paused':True,'hourlyCalls':42}).status_code==200
    assert r.control()['settings']['paused'] and r.control()['settings']['hourlyCalls']==42
    r.control({'hourlyCalls':60})
    assert client.get('/api/social/state').json()['settings']['hourlyCalls']==60


def test_profile_fields_edit_without_model_call(mind):
    r,_,client,ids,stages=mind
    response=client.post(f'/api/actors/{ids[0]}/mind-profile',json={'interpretation':{'selfImage':'谨慎但愿意尝试','values':['可靠'],'uncertainty':'可纠正的理解'}})
    assert response.status_code==200
    assert response.json()['origin']=='user' and not stages
    assert client.post(f'/api/actors/{ids[0]}/mind-profile',json={'interpretation':None}).json()['status']=='pending'


def test_pausing_goal_cannot_be_silently_resumed_by_activity(mind):
    r,_,_,ids,_=mind
    goal=r.save_goal(ids[0],{'title':'阅读','motivation':'兴趣','nextStep':'读资料','sourceIds':['user-setting']})
    aid=r.life.start(ids[0],{'action':'read','purpose':'阅读','goalId':goal['id']},'goal-pause')
    r.save_goal(ids[0],{},goal['id'],status='paused',reason='暂时不读了')
    assert r.life.activities(ids[0])[0]['status']=='suspended'
    with pytest.raises(ValueError):r.life.transition(aid,ids[0],'continue','自行恢复')


def test_life_events_are_once_only_and_not_tool_evidence(mind):
    r,_,_,ids,_=mind
    intent={'action':'read','purpose':'整理资料'}
    aid=r.life.start(ids[0],intent,'once')
    assert r.life.start(ids[0],intent,'once')==aid
    with session_scope() as s:
        row=s.get(LifeActivity,aid);row.data={**row.data,'onlineSeconds':120}
    r.life.transition(aid,ids[0],'finish','阅读结束')
    r.life.transition(aid,ids[0],'finish','再次投递')
    events=r.life.events(ids[0])
    assert len(events)==2
    assert all(e['sourceKind']=='simulation' for e in events)
    with session_scope() as s:
        assert len(s.scalars(select(Experience).where(Experience.kind=='simulation')).all())==2


def test_restart_preserves_progress_without_offline_completion(mind):
    r,_,_,ids,_=mind
    aid=r.life.start(ids[0],{'action':'rest','purpose':'休息'},'offline')
    with session_scope() as s:
        row=s.get(LifeActivity,aid);row.updated=time.time()-86400;row.data={**row.data,'onlineSeconds':20}
    before=len(r.life.events(ids[0]))
    r.life.recover()
    row=r.life.activities(ids[0])[0]
    assert row['status']=='suspended' and row['onlineSeconds']==20
    assert len(r.life.events(ids[0]))==before
    r.life.transition(aid,ids[0],'continue','现在继续休息')
    assert r.life.activities(ids[0])[0]['status']=='active'


def test_user_can_resume_suspended_life_without_offline_progress(mind):
    r,c,client,ids,_=mind
    aid=r.life.start(ids[0],{'action':'rest','purpose':'休息'},'user-resume')
    with session_scope() as s:
        row=s.get(LifeActivity,aid)
        row.data={**row.data,'onlineSeconds':24}
    r.life.recover()
    response=client.post(f'/api/actors/{ids[0]}/life/{aid}/resume',json={})
    assert response.status_code==200
    activity=response.json()['activities'][0]
    assert activity['status']=='active' and activity['onlineSeconds']==24
    assert client.post(f'/api/actors/{ids[0]}/life/{aid}/resume',json={}).status_code==409
    r.life.transition(aid,ids[0],'pause','先处理任务')
    c.store.create({'actorId':ids[0],'mode':'task','prompt':'工作','actors':[]},'resume-blocked')
    assert client.post(f'/api/actors/{ids[0]}/life/{aid}/resume',json={}).status_code==422


def test_resuming_joint_invitation_still_requires_peer_acceptance(mind):
    r,_,client,ids,_=mind
    aid=r.life.start(ids[0],{'action':'invite','purpose':'一起交流','targetId':ids[1]},'joint-resume')
    r.life.recover()
    response=client.post(f'/api/actors/{ids[0]}/life/{aid}/resume',json={})
    assert response.status_code==200
    activity=next(row for row in response.json()['activities'] if row['id']==aid)
    assert activity['status']=='invited' and activity['accepted']==[ids[0]]


def test_task_preempts_optional_life(mind):
    r,c,_,ids,_=mind
    r.life.start(ids[0],{'action':'practice','purpose':'兴趣练习'},'before-task')
    c.store.create({'actorId':ids[0],'mode':'task','prompt':'工作','actors':[]},'task')
    r.life.suspend_busy()
    activity=r.life.activities(ids[0])[0]
    assert activity['status']=='suspended'
    with pytest.raises(ValueError):r.life.transition(activity['id'],ids[0],'continue','继续玩')


def test_budget_counts_failed_calls_and_survives_restart(mind):
    r,_,_,ids,_=mind
    r.control({'hourlyCalls':1})
    async def fail(*_):raise RuntimeError('synthetic error')
    r.generate=fail
    with pytest.raises(RuntimeError):asyncio.run(r.call(ids[0],'express',{},background=True))
    with pytest.raises(MindInterrupted):asyncio.run(r.call(ids[0],'express',{},background=True))
    r.life.recover()
    assert r.control()['callsLastHour']==1
    assert r.control()['failuresLastHour']==1


def test_research_is_recorded_without_spending_or_obeying_life_quota(mind):
    r,_,_,ids,_=mind
    async def research(*_):return {'relationships':[]}
    r.control({'hourlyCalls':1})
    asyncio.run(r.call(ids[0],'reflect',{},background=True))
    assert not r.has_budget()
    asyncio.run(r.call(ids[0],'research',{},background=True,generator=research))
    r.control({'paused':True})
    asyncio.run(r.call(ids[0],'research',{},background=True,generator=research))
    with pytest.raises(MindInterrupted):
        asyncio.run(r.call(ids[0],'reflect',{},background=True))
    usage=r.control()
    assert usage['callsLastHour']==1
    assert usage['backgroundCallsLastHour']==3
    assert {stage['kind']:stage['calls'] for stage in usage['backgroundStages']}=={'reflect':1,'research':2}


def test_paused_autonomous_life_still_services_queued_research(mind,monkeypatch):
    from backend.mind.life_runtime import LifeRuntime
    r,c,_,_,_=mind
    r.control({'paused':True})
    monkeypatch.setattr(r.life,'pulse',lambda:None)
    monkeypatch.setattr(r,'decay',lambda:None)
    monkeypatch.setattr(c.relationship_research,'has_pending',lambda:True)
    research=AsyncMock()
    monkeypatch.setattr(c.relationship_research,'tick',research)
    asyncio.run(LifeRuntime.tick(r.life))
    research.assert_awaited_once()


def test_quiet_hours_and_pause(mind):
    r,_,_,ids,_=mind
    assert r.life.quiet(datetime(2026,9,22,23))
    assert r.life.quiet(datetime(2026,9,22,7))
    assert not r.life.quiet(datetime(2026,9,22,8))
    r.control({'paused':True})
    with pytest.raises(MindInterrupted):asyncio.run(r.call(ids[0],'express',{},background=True))
    assert not r.life.can_contact(ids[0],'dm')


def test_forgetting_invalidates_decisions_and_derived_beliefs(mind):
    r,c,client,ids,_=mind
    observe(c,ids[0],'与同伴合作',peers=[ids[1]])
    eid=c.cognition.experiences(ids[0])[0]['id']
    original=r.generate
    async def generate(messages,cfg):
        value=await original(messages,cfg)
        if cfg['_mindStage']=='understand':value['sourceIds']=[eid]
        return value
    r.generate=generate
    asyncio.run(r.think(ids[0],'memory-test','合作'))
    client.post(f'/api/actors/{ids[0]}/experiences/{eid}/forget',json={}).raise_for_status()
    assert r.traces(ids[0])[0]['status']=='invalidated'
    assert eid not in r.snapshot(ids[0],'合作','new')['allowedSources']


def test_background_model_wait_is_preempted(mind):
    r,_,_,ids,_=mind
    async def scenario():
        entered=asyncio.Event()
        async def slow(*_):
            entered.set()
            await asyncio.sleep(60)
        r.generate=slow
        task=asyncio.create_task(r.call(ids[0],'express',{},background=True,revision=r.revision))
        await entered.wait()
        r.interrupt()
        with pytest.raises(MindInterrupted):await task
    asyncio.run(scenario())


def test_habit_requires_independent_events_and_sessions(mind):
    r,c,_,ids,_=mind
    observe(c,ids[0],'检查附件',peers=[ids[1]],conversationId='one')
    eid=c.cognition.experiences(ids[0])[0]['id']
    async def generate(*_):return {'beliefs':[{'text':'总先检查附件','kind':'habit','sourceIds':[eid]}]}
    r.generate=generate
    asyncio.run(r.reflect())
    with session_scope() as s:
        assert s.get(Experience,eid).data['derived'][0]['stable'] is False
    assert not r.snapshot(ids[0],'附件','test')['memory']['semantic']


def test_three_independent_events_enable_habit(mind):
    r,c,_,ids,_=mind
    for n in range(3):observe(c,ids[0],'检查附件'+str(n),conversationId='session-'+str(n%2))
    sources=[e['id'] for e in c.cognition.experiences(ids[0])]
    async def generate(*_):return {'beliefs':[{'text':'检查附件','kind':'habit','sourceIds':sources,'confidence':.6}]}
    r.generate=generate
    asyncio.run(r.reflect())
    assert any(b.get('stable') for b in r.snapshot(ids[0],'检查附件','now')['memory']['semantic'])


def test_stale_reflection_is_not_applied(mind):
    r,c,_,ids,_=mind
    observe(c,ids[0],'检查附件')
    eid=c.cognition.experiences(ids[0])[0]['id']
    async def generate(*_):
        observe(c,ids[0],'新的要求')
        return {'beliefs':[{'text':'旧判断','sourceIds':[eid]}]}
    r.generate=generate
    with pytest.raises(MindInterrupted):asyncio.run(r.reflect())
    with session_scope() as s:assert not s.get(Experience,eid).data.get('derived')


def test_commitment_is_a_quote_not_invented_completion(mind):
    r,c,_,ids,_=mind
    observe(c,ids[0],'我会在提交前提醒检查附件。')
    eid=c.cognition.experiences(ids[0])[0]['id']
    async def generate(*_):return {'beliefs':[],'commitments':[
        {'text':'我会在提交前提醒检查附件。','sourceId':eid},
        {'text':'已经完成文件核验。','sourceId':eid}]}
    r.generate=generate
    asyncio.run(r.reflect())
    promises=c.cognition.inspect(ids[0])['data']['commitments']
    assert len(promises)==1 and promises[0]['text'].startswith('我会')
    c.cognition.revise(ids[0],eid,forget=True)
    assert not c.cognition.inspect(ids[0])['data']['commitments']


def test_profile_change_invalidates_inflight_reflection(mind):
    from backend.mind.mind_models import MindProfile
    r,c,_,ids,_=mind
    observe(c,ids[0],'阅读时先看目录')
    eid=c.cognition.experiences(ids[0])[0]['id']
    async def generate(*_):
        with session_scope() as s:
            profile=s.get(MindProfile,ids[0]);profile.version+=1
        return {'beliefs':[{'text':'总先看目录','sourceIds':[eid]}]}
    r.generate=generate
    with pytest.raises(MindInterrupted):asyncio.run(r.reflect())
    with session_scope() as s:assert not s.get(Experience,eid).data.get('derived')


def test_structured_repair_is_counted_against_budget(mind):
    r,_,_,ids,_=mind
    attempts=[]
    async def generate(*_):
        attempts.append(1)
        if len(attempts)==1:raise ValueError('invalid JSON')
        return {'beliefs':[]}
    r.generate=generate
    asyncio.run(r.call(ids[0],'reflect',{},StateDelta,background=True))
    assert r.control()['callsLastHour']==2 and r.control()['failuresLastHour']==1


def test_background_provider_requires_trial_token_and_counts_actual_requests(mind,monkeypatch):
    import httpx
    r,c,client,ids,_=mind
    assert client.post('/api/internal/background-model/unknown/chat/completions',json={}).status_code==403
    class Runner:
        tokens={'trial-token':('run',ids[0],'executor')}
    c.trial_runners.add(Runner())
    requests=[]
    class Provider:
        def __init__(self,**kwargs):pass
        async def __aenter__(self):return self
        async def __aexit__(self,*args):pass
        async def post(self,url,**kwargs):
            requests.append(url)
            return httpx.Response(200,json={'choices':[],'usage':{'total_tokens':12}})
    monkeypatch.setattr('backend.mind.mind_provider.httpx.AsyncClient',Provider)
    response=client.post('/api/internal/background-model/trial-token/chat/completions',json={'messages':[]})
    assert response.status_code==200 and len(requests)==1
    assert r.control()['callsLastHour']==1 and r.control()['tokensLastHour']==12
    r.control({'hourlyCalls':1})
    assert client.post('/api/internal/background-model/trial-token/chat/completions',json={}).status_code==503
    assert len(requests)==1
    c.trial_runners.clear()


def test_new_default_and_legacy_saved_background_budget(mind):
    from backend.mind.mind_models import MindControl
    from backend.social.social_models import SocialTopic
    r,_,_,_,_=mind
    with session_scope() as s:
        s.delete(s.get(MindControl,'life'))
        old=s.get(SocialTopic,'social-control')
        old.data={'settings':{'hourlyCalls':17,'paused':True}}
    assert r.control()['settings']['hourlyCalls']==17
    with session_scope() as s:
        s.delete(s.get(MindControl,'life'))
        s.get(SocialTopic,'social-control').data={'settings':{'hourlyCalls':30},'defaultsVersion':'life-v4'}
    assert r.control()['settings']['hourlyCalls']==60


def test_concurrent_projection_is_idempotent(mind):
    from concurrent.futures import ThreadPoolExecutor
    r,c,_,ids,_=mind
    for n in range(6):c.store.event(None,'mind.observation',{'key':'parallel-'+str(n),'actorIds':[ids[0]],'text':'并发事件','data':{}})
    with ThreadPoolExecutor(max_workers=4) as pool:list(pool.map(lambda _:c.cognition.pump(),range(4)))
    assert len(c.cognition.experiences(ids[0]))==6
    assert c.cognition.last_error is None


def test_task_intent_reaches_harness_without_authority_expansion(mind):
    r,c,_,ids,_=mind
    captured={}
    actor={'id':ids[0],'name':'合成人物'}
    run,_=c.store.create({'actorId':ids[0],'actors':[actor],'mode':'task','collaborative':False,
        'conversationId':'synthetic','workspace':'.','prompt':'只读核验','allowedActions':['list_dir','deliver']},'task-intent')
    class Bridge:
        def __init__(self,home,settings,endpoint,token,on_event):captured.update(settings=settings)
        async def start(self):pass
        async def prompt(self,prompt,workspace,history):captured['history']=history;return '没有宣称交付'
        async def close(self):pass
    c.bridge_factory=Bridge
    asyncio.run(c.execute_actor(run,actor,'executor','只读核验',c.settings_loader()))
    assert '当前人物已选择的工作意图' in captured['settings']['_characterPolicy']
    identity=json.loads(captured['settings']['_characterIdentity'])
    assert identity['personality']==r.profile(ids[0])['interpretation']
    assert 'machine-clock' in captured['settings']['_characterPolicy']
    assert set(captured['settings']['_allowedActions'])=={'list_dir','deliver'}
    assert any('stateVersion' in m['content'] for m in captured['history'])
