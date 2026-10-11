import json
import time
from datetime import datetime

from backend.database import session_scope
from backend.mind.mind_models import MindControl
from conftest import observe


def test_cached_filename_cannot_impersonate_another_character(tmp_path,monkeypatch):
    from tools import blhx_character_import as importer
    monkeypatch.setattr(importer,'OUTPUT_DIR',tmp_path)
    from dataclasses import asdict
    wrong=importer.CharacterProfile(name='甲',url=importer.normalize_url('甲'))
    (tmp_path/'乙.json').write_text(json.dumps(asdict(wrong)),encoding='utf-8')
    assert importer.find_cached_profile('乙') is None
    right=importer.CharacterProfile(name='乙',url=importer.normalize_url('乙'),aliases=['甲'])
    importer.save_profile(right)
    assert not (tmp_path/'甲.json').exists()
    assert importer.find_cached_profile('乙').name=='乙'


def test_cached_internal_name_must_match_its_page_identity(tmp_path,monkeypatch):
    from dataclasses import asdict
    from tools import blhx_character_import as importer
    monkeypatch.setattr(importer,'OUTPUT_DIR',tmp_path)
    corrupt=importer.CharacterProfile(name='乙',url=importer.normalize_url('甲'))
    (tmp_path/'乙.json').write_text(json.dumps(asdict(corrupt)),encoding='utf-8')
    assert importer.find_cached_profile('乙') is None
    assert importer.find_cached_profile('甲') is None


def test_ambiguous_alias_is_not_first_file_wins(tmp_path,monkeypatch):
    import pytest
    from tools import blhx_character_import as importer
    monkeypatch.setattr(importer,'OUTPUT_DIR',tmp_path)
    for name in ('甲','乙'):
        importer.save_profile(importer.CharacterProfile(name=name,url=importer.normalize_url(name),aliases=['共同别名']))
    with pytest.raises(ValueError,match='别名'):importer.find_cached_profile('共同别名')


def test_other_characters_navigation_portrait_is_not_imported():
    from bs4 import BeautifulSoup
    from tools.blhx_character_import import collect_images
    soup=BeautifulSoup('<img alt="乙头像" src="https://example.com/b.png"><img alt="甲头像" src="https://example.com/a.png">','html.parser')
    assert collect_images(soup,'甲')==('https://example.com/a.png',['https://example.com/a.png'])


def test_unrelated_tables_cannot_overwrite_personality():
    from tools.blhx_character_import import merge_role_info
    assert merge_role_info({'性格':'谨慎'},{'相关角色':[[['性格','冲动']]],
        '角色信息':[[['性格','含糊'],['身份','秘书']]]})=={'性格':'谨慎','身份':'秘书'}


def test_idle_wait_backoff_ignores_decay_but_wakes_on_new_event(mind):
    r,c,_,ids,_=mind
    with session_scope() as s:s.add(MindControl(id='queue',data={}))
    r.life.defer(ids[0],'wait')
    with session_scope() as s:wake=s.get(MindControl,'queue').data['wake'][ids[0]]
    assert not r.life.ready(ids[0],wake,time.time()+121)
    r.decay(time.time()+180)
    assert not r.life.ready(ids[0],wake,time.time()+181)
    observe(c,ids[0],'有新的阅读建议')
    assert r.life.ready(ids[0],wake,time.time()+182)
    r.life.defer(ids[0],'wait')
    with session_scope() as s:next_wake=s.get(MindControl,'queue').data['wake'][ids[0]]
    assert next_wake['at']>time.time()+590


def test_sleeping_actor_ticks_do_not_call_model(mind):
    import asyncio
    from backend.mind.life_runtime import LifeRuntime
    r,c,_,ids,stages=mind
    for aid in ids[1:]:c.cognition.configure(aid,enabled=False)
    with session_scope() as s:s.add(MindControl(id='queue',data={'attempts':{ids[0]:time.time()-180},'turn':0}))
    r.life.defer(ids[0],'wait')
    for _ in range(4):asyncio.run(LifeRuntime.tick(r.life))
    assert not stages and r.control()['callsLastHour']==0


def test_due_goal_wakes_without_fabricated_progress(mind):
    r,_,_,ids,stages=mind
    goal=r.save_goal(ids[0],{'title':'阅读','motivation':'兴趣','nextStep':'阅读资料','dueAt':time.time()-1})
    r.life.pulse()
    saved=next(g for g in r.goals(ids[0]) if g['id']==goal['id'])
    assert saved['overdue'] and saved['status']=='active' and not saved['evidence']
    assert not stages


def test_finishing_goal_cannot_start_new_activity_under_same_goal(mind):
    import asyncio
    from backend.mind.mind_models import LifeActivity
    r,_,_,ids,_=mind
    goal=r.save_goal(ids[0],{'title':'阅读一轮','motivation':'兴趣','nextStep':'阅读'})
    activity=r.life.start(ids[0],{'action':'read','purpose':'阅读','goalId':goal['id']},'read-once')
    with session_scope() as s:
        row=s.get(LifeActivity,activity);row.data={**row.data,'onlineSeconds':120}
    r.life.transition(activity,ids[0],'finish','阅读结束')
    evidence=r.goals(ids[0])[0]['evidence']
    original=r.generate;attempts=[]
    async def generate(messages,settings):
        value=await original(messages,settings)
        if settings['_mindStage']=='decide':
            attempts.append(1)
            value.update(action='reflect',goalId=goal['id'] if len(attempts)==1 else '',
                goalUpdate={'goalId':goal['id'],'status':'completed','nextStep':'休息','reason':'已读完','evidenceIds':evidence})
        return value
    r.generate=generate
    decision=asyncio.run(r.think(ids[0],'finish-and-reflect','结束阅读，独立整理心情',mode='life',actions=['reflect','wait']))
    assert len(attempts)==2 and decision['intent']['goalId']==''
    r.life.start(ids[0],decision['intent'],decision['id'])
    assert r.life.activities(ids[0])[0]['status']=='active'
    assert r.goals(ids[0])[0]['status']=='completed'


def test_unfinished_activity_does_not_need_another_model_call(mind):
    r,_,_,ids,_=mind
    r.life.start(ids[0],{'action':'read','purpose':'阅读'},'new-reading')
    assert not r.life.ready(ids[0],{},time.time()+121)


def test_task_memory_does_not_revive_old_casual_requests(mind):
    r,c,_,ids,_=mind
    observe(c,ids[0],'之前闲聊要求：请发贴纸',conversationId='same-room')
    assert '请发贴纸' in json.dumps(r.snapshot(ids[0],'任务结果','now','same-room'),ensure_ascii=False)
    snap=r.snapshot(ids[0],'任务结果','run-current:result','same-room',task_run_id='run-current')
    assert '请发贴纸' not in json.dumps(snap,ensure_ascii=False)


def test_checkpoint_uses_actual_task_id_when_retrieving_memory(mind,monkeypatch):
    import asyncio
    from backend.mind.cognition_models import Experience
    from sqlalchemy import select
    r,c,_,ids,_=mind
    observe(c,ids[0],'当前任务关键要求',conversationId='same-room')
    with session_scope() as s:
        experience=s.scalar(select(Experience).where(Experience.actor_id==ids[0],Experience.text=='当前任务关键要求'))
        experience.run_id='run-current'
    observed=[]
    original=r.snapshot
    def capture(*args,**kwargs):
        result=original(*args,**kwargs)
        observed.append((kwargs.get('task_run_id'),json.dumps(result,ensure_ascii=False)))
        return result
    monkeypatch.setattr(r,'snapshot',capture)
    asyncio.run(r.think(ids[0],'call-xyz:checkpoint','工具失败，请重评',mode='task',
        conversation_id='same-room',actions=['execute'],task_run_id='run-current'))
    assert observed[0][0]=='run-current'
    assert '当前任务关键要求' in observed[0][1]


def test_mistaken_name_in_user_request_remains_attributed_to_user(mind):
    from uuid import uuid4
    r,c,_,ids,_=mind
    run,_=c.store.create({'actorId':ids[0],'actors':[{'id':ids[0],'name':'当前人物'}],
        'mode':'chat','collaborative':False,'conversationId':'identity-room',
        'prompt':'乙，早上好','history':[]},uuid4().hex)
    c.cognition.pump()
    snapshot=r.snapshot(ids[0],'早上好',run['id']+':chat','identity-room')
    assert any(row['text']=='乙，早上好' and row['speakerId']=='commander'
        for row in snapshot['memory']['working'])
    legacy_context=c.cognition.context(ids[0],'早上好')
    assert '"speakerId": "commander"' in legacy_context


def test_background_usage_is_separated_by_stage(mind):
    from backend.mind.mind_models import MindCall
    r,_,_,ids,_=mind
    now=time.time()
    with session_scope() as s:
        s.add_all([
            MindCall(id='usage-understand',actor_id=ids[0],kind='understand',at=now,
                data={'background':True,'status':'completed','usage':{'total_tokens':300}}),
            MindCall(id='usage-research',actor_id=ids[0],kind='research',at=now,
                data={'background':True,'status':'failed','usage':{'total_tokens':80}}),
            MindCall(id='usage-front',actor_id=ids[0],kind='express',at=now,
                data={'background':False,'status':'completed','usage':{'total_tokens':100}}),
        ])
    stats=r.control()
    assert stats['callsLastHour']==1 and stats['backgroundCallsLastHour']==2
    assert stats['backgroundFailuresLastHour']==1
    assert stats['backgroundTokensLastHour']==380
    assert {s['kind']:s for s in stats['backgroundStages']}=={
        'understand':{'kind':'understand','calls':1,'failures':0,'tokens':300},
        'research':{'kind':'research','calls':1,'failures':1,'tokens':80}}


def test_machine_clock_is_available_without_model_and_in_snapshot(mind):
    r,_,client,ids,stages=mind
    result=client.get('/api/time').json()
    assert abs(result['unixSeconds']-time.time())<2
    local=datetime.fromtimestamp(result['unixSeconds']).astimezone()
    assert result['hour']==local.hour and result['date']==local.date().isoformat()
    assert datetime.fromisoformat(result['local']).utcoffset()==local.utcoffset()
    assert r.snapshot(ids[0],'现在几点','time-question')['time']['source']=='machine-clock'
    assert not stages


def test_group_equal_candidates_vary_by_topic_but_retry_is_stable(world,monkeypatch):
    c,_,ids=world
    engine=c.social_engine
    monkeypatch.setattr('backend.social.social_engine.worldbook',lambda:{'identities':[],'relationships':[]})
    monkeypatch.setattr(engine,'participation',lambda *_:{'familiarity':0,'commitments':[]})
    room={'members':[{'id':aid,'name':'member-'+str(i)} for i,aid in enumerate(ids)],
        'history':[{'id':'user','speakerId':'commander','text':'随便聊聊'}]}
    first=[]
    for i in range(20):
        topic={'id':str(i),'prompt':'随便聊聊','spoken':0}
        result=engine.candidates(room,topic)
        assert result==engine.candidates(room,topic)
        first.append(result[0]['id'])
    assert len(set(first))>1
    room['history'][0]['mentions']=[ids[0]]
    assert all(engine.candidates(room,{'id':str(i),'prompt':'随便聊聊','spoken':0})[0]['id']==ids[0] for i in range(10))
