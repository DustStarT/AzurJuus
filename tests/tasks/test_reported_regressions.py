import asyncio
import json
from unittest.mock import AsyncMock
from uuid import uuid4
import pytest
from backend.database import session_scope
from backend.models import Actor, Message
from backend.social.social_models import SocialTopic
from conftest import fixture_run
from conftest import setup_social_topic


def test_structured_sticker_choice_becomes_valid_standalone_bubble():
    from backend.chat.expression import normalize_speech,validate
    from backend.chat.sticker_catalog import catalog
    label=catalog()[0]['label']
    value=normalize_speech({'segments':'收到啦。','sticker':label,'sourceIds':['current']})
    assert validate(value,'current')==['收到啦。','[表情:'+label+']']


def test_result_can_deliver_many_paragraphs_without_losing_content(world):
    c,_,ids=world
    run,actor=fixture_run(c,ids[0])
    text='阅读完成，建议按主题分类。'
    async def generate(messages,settings):
        source=json.loads(messages[-1]['content'])['sourceId']
        assert '可用表情包标签' in messages[0]['content']
        return {'segments':[text]*15,'sourceIds':[source]}
    c.expression.generate=generate
    c.message_callback=AsyncMock()
    result=asyncio.run(c.expression.speak(run,actor,'result','说明建议',facts={'summary':text}))
    assert result.count(text)==15
    assert c.message_callback.await_count==1


def test_failed_role_expression_still_delivers_verified_task_result(world):
    c,_,ids=world
    run,actor=fixture_run(c,ids[0])
    run=c.store.update(run['id'],mode='task',conversationId='dm-'+ids[0],status='completed',
        result={'summary':'已读内容在交付清单中。'})
    c.expression.enabled=True
    async def invalid(messages,settings):return {'segments':[],'sourceIds':[json.loads(messages[-1]['content']).get('sourceId','')]}
    c.expression.generate=invalid
    asyncio.run(c.finish_callback(run['id'],'已读内容在交付清单中。'))
    with session_scope() as s:
        message=s.get(Message,run['id']+'-verified-result')
        assert message and message.metadata_json['expressionFallback']
        assert '已读内容在交付清单中。' in message.body
    assert c.store.get(run['id'])['status']=='completed'


def test_old_completed_task_with_missing_message_is_repaired_without_model(world):
    c,_,ids=world
    run,_=fixture_run(c,ids[0])
    run=c.store.update(run['id'],mode='task',conversationId='dm-'+ids[0],status='completed',
        resultMessageId='nonexistent-old-speech',result={'summary':'已有核验结论'})
    c.expression.generate=AsyncMock(side_effect=AssertionError('recovery must not call a model'))
    c.completed_recovery(run);c.completed_recovery(c.store.get(run['id']))
    with session_scope() as s:assert s.get(Message,c.store.get(run['id'])['resultMessageId']).body.endswith('已有核验结论')
    assert c.expression.generate.await_count==0


def test_new_task_excludes_prior_chat_and_chat_never_becomes_task(world,monkeypatch,tmp_path):
    c,client,ids=world
    monkeypatch.setattr(c,'launch',lambda *_:None)
    with session_scope() as s:
        c.social_engine.service.get_workspace(s).authorized_workspace_root=str(tmp_path)
        s.add(Message(id='unrelated-chat',conversation_id='dm-'+ids[0],speaker_id='commander',type='text',body='闲聊里的护理站设想'))
    response=client.post('/api/messages/send',json={'conversationId':'dm-'+ids[0],'content':'列出文件','mode':'task'})
    assert response.status_code==200,response.text
    task=response.json()
    assert c.store.get(task['runId'])['history']==[]
    chat=client.post('/api/messages/send',json={'conversationId':'port-hub','content':'你在干什么？','mode':'chat','collaborative':False}).json()
    assert c.store.get(chat['runId'])['mode']=='chat'
    assert not c.store.get(chat['runId'])['assignments']


def test_casual_reply_history_excludes_work_transcript(world,monkeypatch,tmp_path):
    c,client,ids=world
    monkeypatch.setattr(c,'launch',lambda *_:None)
    cid='dm-'+ids[0]
    with session_scope() as s:
        c.social_engine.service.get_workspace(s).authorized_workspace_root=str(tmp_path)
        s.add_all([
            Message(id='past-task-question',conversation_id=cid,speaker_id='commander',type='task',body='之前请分析工作文件'),
            Message(id='past-task-progress',conversation_id=cid,speaker_id=ids[0],type='task_progress',body='工作文件的旧结论',metadata_json={'expression':True}),
            Message(id='recent-casual',conversation_id=cid,speaker_id='commander',type='text',body='今天心情如何？'),
        ])
    response=client.post('/api/messages/send',json={'conversationId':cid,'content':'想聊点别的','mode':'chat'})
    assert response.status_code==200,response.text
    history=c.store.get(response.json()['runId'])['history']
    texts=[item['content'] for item in history]
    assert '今天心情如何？' in texts
    assert '之前请分析工作文件' not in texts and '工作文件的旧结论' not in texts


def test_task_outcome_requires_explicit_followup_in_casual_memory(mind):
    from uuid import uuid4
    r,c,_,ids,_=mind
    prior,_=c.store.create({'actorId':ids[0],'actors':[{'id':ids[0],'name':'人物'}],
        'mode':'task','collaborative':False,'conversationId':'port-hub',
        'prompt':'检查旧报告中的红色密码','history':[]},uuid4().hex)
    c.store.update(prior['id'],status='completed',result={'summary':'旧报告列出海洋生态结论'})
    c.cognition.pump()
    casual=r.snapshot(ids[0],'今天想聊什么','new-chat','port-hub',include_task_memory=False)
    explicit=r.snapshot(ids[0],'之前的工作结果是什么','followup','port-hub',include_task_memory='outcomes')
    assert '旧报告' not in json.dumps(casual,ensure_ascii=False)
    assert '海洋生态结论' in json.dumps(explicit,ensure_ascii=False)
    assert '红色密码' not in json.dumps(explicit,ensure_ascii=False)
    assert '旧报告' not in c.cognition.context(ids[0],'今天想聊什么',include_task_history=False)


def test_work_followup_detection_is_specific():
    from backend.chat.conversation_scope import asks_about_work
    assert asks_about_work('刚才的工作结果是什么？')
    assert asks_about_work('任务进度怎么样？')
    assert not asks_about_work('今天想聊什么？')
    assert not asks_about_work('工作区有本书，你喜欢读书吗？')


@pytest.mark.parametrize('collaborative',[False,True])
def test_task_updates_and_team_division_reach_conversation_offline(world,tmp_path,collaborative):
    c,_,ids=world
    c.cognition.runtime.enabled=False
    c.expression.enabled=True
    c.settings_loader=lambda:{'llmApiKey':'synthetic','userAddress':'指挥官'}
    work=tmp_path.parent/(tmp_path.name+'-work')
    (work/'data').mkdir(parents=True)
    (work/'data'/'source.txt').write_text('合成资料',encoding='utf-8')
    with session_scope() as s:
        actors=[{'id':aid,'name':s.get(Actor,aid).name} for aid in ids[:2 if collaborative else 1]]
    cid='port-hub' if collaborative else 'dm-'+ids[0]
    run,_=c.store.create({'actorId':ids[0],'actors':actors,'mode':'task',
        'collaborative':collaborative,'conversationId':cid,'workspace':str(work),
        'prompt':'查看工作目录并说明情况','history':[],'grants':[]},uuid4().hex)
    async def expression(messages,settings):
        source=json.loads(messages[-1]['content'])['sourceId']
        if 'plan-notice' in source:text='分工已定，我先核对目录，同伴再核对结果。'
        elif 'task-start' in source:text='我先看看目录里有什么，再决定怎么处理。'
        elif 'work-update' in source:text='目录已检查，接下来核对交付内容。'
        else:text='目录核对完成，可以交付。'
        return {'segments':[text]}
    c.expression.generate=expression
    class Bridge:
        def __init__(self,path,settings,endpoint,token,emit):
            self.phase=path.name;self.token=token;self.emit=emit
        async def start(self):pass
        async def close(self):pass
        async def prompt(self,prompt,workspace,history):
            phase=self.phase
            if phase=='planner':
                tasks=[{'id':'first','actorId':actors[0]['id'],'brief':'核对目录','dependsOn':[],
                    'acceptance':['列出目录']}]
                tasks.append({'id':'second','actorId':actors[1]['id'],'brief':'再次核对目录','dependsOn':['first'],
                    'acceptance':['确认目录']})
                assert (await c.tool(self.token,{'callId':'delegate1','name':'delegate','args':{'tasks':tasks}}))['status']=='completed'
            else:
                scan=phase.replace('-','')+'scan'
                result=await c.tool(self.token,{'callId':scan,'name':'list_dir','args':{'path':'data'}})
                assert result['status']=='completed',result
                result=await c.tool(self.token,{'callId':phase.replace('-','')+'deliver','name':'deliver','args':{
                    'summary':'已核对目录','artifacts':[],'checks':[{'callId':scan}],'unresolved':[]}})
                assert result['status']=='completed',result
            await self.emit('message.complete',{'text':'阶段已处理，核验记录在工具结果中。'})
            return '阶段结束'
    c.bridge_factory=Bridge
    asyncio.run(c.run(run['id']))
    finished=c.store.get(run['id'])
    assert finished['status']=='completed',finished.get('error')
    with session_scope() as s:
        rows=s.query(Message).filter(Message.conversation_id==cid).all()
        phases=[(m.metadata_json or {}).get('phase') for m in rows if (m.metadata_json or {}).get('runId')==run['id']]
    assert 'task_start' in phases and any(str(phase).startswith('work_update_') for phase in phases)
    assert ('plan_notice' in phases)==collaborative
    assert 'result' in phases and not finished.get('expressionError')


def test_group_casual_context_hides_finished_task_messages(world):
    c,_,ids=world
    with session_scope() as s:
        s.add_all([
            Message(id='group-task-result',conversation_id='port-hub',speaker_id=ids[0],type='task_progress',
                body='旧任务的文件结论',metadata_json={'expression':True}),
            Message(id='group-new-topic',conversation_id='port-hub',speaker_id='commander',type='text',body='今天聊点别的'),
        ])
    room=c.social_engine.snapshot('port-hub',ids[0])
    assert '旧任务的文件结论' not in [m['text'] for m in room['history']]


def test_research_is_not_starved_by_actor_cooldowns(world,monkeypatch):
    c,_,ids=world
    r=c.cognition.runtime;r.enabled=True;r.life.recover()
    for aid in ids:c.cognition.configure(aid,enabled=False)
    with session_scope() as s:
        actor=s.get(Actor,ids[0]);actor.extra_json={**actor.extra_json,'wikiResearch':{'status':'pending','queuedAt':1}}
    tick=AsyncMock();monkeypatch.setattr(c.relationship_research,'tick',tick)
    asyncio.run(r.life.tick())
    assert tick.await_count==1


def test_group_reply_can_close_topic_and_paraphrase_repetition_is_blocked(world):
    e,run,topic,actor=setup_social_topic(world)
    assert e.commit(topic,actor,'first-close',{'action':'speak','segments':['这里已经安排妥当，等你有新的要求再继续。'],'endAfterReply':True})
    assert e.topic(topic['id'])['status']=='ended'
    assert not e.commit(e.topic(topic['id']),actor,'after-close',{'action':'speak','segments':['继续待命。']})
    with session_scope() as s:s.get(SocialTopic,topic['id']).status='active'
    assert not e.commit(e.topic(topic['id']),actor,'repeat-close',{'action':'speak','segments':['这里已经安排妥当，等你有新的要求再继续！']})


def test_reset_preserves_only_keys_and_personal_profile(world):
    c,client,ids=world
    client.post('/api/profile',json={'name':'保留称呼','avatar':''}).raise_for_status()
    service=c.social_engine.service
    with session_scope() as s:
        cfg=service.get_workspace(s);cfg.llm_api_key='synthetic-protected-key';cfg.tool_api_key='synthetic-tool-key'
        cfg.ui_session_json={'settingsExtras':{'searchApiKey':'synthetic-search-key','maxTurns':123}}
        cfg.character_roster_text='changed'
        actor=s.get(Actor,'commander');actor.extra_json={**actor.extra_json,'terminalSettings':{'worldOverrides':{'x':'reset this'}}}
    result=client.post('/api/system/reset',json={'preserveKeysAndProfile':True})
    assert result.status_code==200
    assert 'synthetic-protected-key' not in result.text
    with session_scope() as s:
        cfg=service.get_workspace(s)
        assert cfg.llm_api_key=='synthetic-protected-key' and cfg.tool_api_key=='synthetic-tool-key'
        assert cfg.ui_session_json['settingsExtras']=={'searchApiKey':'synthetic-search-key'}
        assert cfg.character_roster_text!='changed'
        actor=s.get(Actor,'commander');assert actor.name=='保留称呼'
        assert not (actor.extra_json or {}).get('terminalSettings')
