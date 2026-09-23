import asyncio
import json
import time
from datetime import datetime
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from backend.database import session_scope
from backend.cognition_models import Experience, MindState
from backend.mind_models import MindDecision, MindCall, LifeActivity, LifeRecord, PersonalGoal
from backend.mind_contracts import StateDelta
from backend.mind_runtime import MindInterrupted
from test_cognition import world, observe


@pytest.fixture
def mind(world, monkeypatch):
    c,client,ids=world
    r=c.cognition.runtime
    r.enabled=True
    r.life.recover()
    monkeypatch.setattr(r.life,'tick',AsyncMock())
    old=c.settings_loader
    monkeypatch.setattr(c,'settings_loader',lambda:{**old(),'llmApiKey':'synthetic-test-only'})
    stages=[]
    async def generate(messages,cfg):
        stage=cfg['_mindStage'];stages.append(stage)
        data=json.loads(messages[-1]['content'])
        if stage=='profile':return {'values':['不辜负约定'],'interests':['阅读'],'exceptions':['紧急时先行动']}
        if stage=='understand':return {'interpretation':'先理解当前问题。','sourceIds':[data['current']['id']],
            'emotion':{'name':'关切','target':'当前问题','cause':'希望做好','intensity':.4}}
        if stage=='decide':return {'action':data['allowedActions'][0],'purpose':'先处理关键问题。',
            'sourceIds':data['frame']['sourceIds']}
        if stage=='reflect':return {'beliefs':[]}
        return {'segments':['我在。'],'sourceIds':[data['sourceId']]}
    r.generate=generate
    return r,c,client,ids,stages


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


def test_public_memory_excludes_private_events(mind):
    r,c,_,ids,_=mind
    observe(c,ids[0],'私密口令：檀木',conversationId='private-room')
    observe(c,ids[0],'公开讨论天气',conversationId='public-room')
    snap=r.snapshot(ids[0],'天气','current','public-room',True)
    assert '檀木' not in json.dumps(snap,ensure_ascii=False)
    assert '公开讨论天气' in json.dumps(snap,ensure_ascii=False)
    assert '檀木' not in json.dumps(r.snapshot(ids[1],'天气','other'),ensure_ascii=False)


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
    with session_scope() as s:
        row=s.get(LifeActivity,aid);row.data={**row.data,'onlineSeconds':200}
    with pytest.raises(ValueError):r.life.transition(aid,ids[0],'finish','只等时间，未发生交流')
    with pytest.raises(ValueError):r.life.start(ids[1],{'action':'read','purpose':'阅读'},'conflict')


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
    from backend.mind_models import MindProfile
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
    from types import SimpleNamespace
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
    monkeypatch.setattr('backend.mind_provider.httpx.AsyncClient',Provider)
    response=client.post('/api/internal/background-model/trial-token/chat/completions',json={'messages':[]})
    assert response.status_code==200 and len(requests)==1
    assert r.control()['callsLastHour']==1 and r.control()['tokensLastHour']==12
    r.control({'hourlyCalls':1})
    assert client.post('/api/internal/background-model/trial-token/chat/completions',json={}).status_code==503
    assert len(requests)==1
    c.trial_runners.clear()


def test_new_default_and_legacy_saved_background_budget(mind):
    from backend.mind_models import MindControl
    from backend.social_models import SocialTopic
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


def test_task_intent_reaches_hermes_without_authority_expansion(mind):
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
    assert set(captured['settings']['_allowedActions'])=={'list_dir','deliver'}
    assert any('stateVersion' in m['content'] for m in captured['history'])
