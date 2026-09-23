import asyncio
import json
from unittest.mock import AsyncMock
from sqlalchemy import select
from backend.database import session_scope
from backend.models import Actor, Message
from backend.social_models import SocialTopic
from test_cognition import world
from test_expression import fixture_run
from test_social_engine import setup


def test_structured_sticker_choice_becomes_valid_standalone_bubble():
    from backend.expression import normalize_speech,validate
    from backend.sticker_catalog import catalog
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
    e,run,topic,actor=setup(world)
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
