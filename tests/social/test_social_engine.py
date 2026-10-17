import asyncio
import json
from datetime import UTC, datetime
import pytest
from sqlalchemy import select
from conftest import setup_social_topic
from backend.database import session_scope
from backend.models import Conversation, ConversationMember, Message
from backend.social.social_models import SocialAction, SocialTopic


def test_group_prepares_two_people_together_but_publishes_one(world,monkeypatch):
    e,_,topic,_=setup_social_topic(world)
    e.c.cognition.runtime.enabled=True
    active=0
    peak=0
    prepared=[]
    async def prepare(actor,room,current,source):
        nonlocal active,peak
        prepared.append(actor['id'])
        active+=1
        peak=max(peak,active)
        await asyncio.sleep(.04)
        active-=1
        return {'intent':{'action':'speak'}}
    async def decide(actor,room,current,source,prepared=None):
        assert prepared is not None
        return {'action':'speak','segments':['现在换一个轻松话题。'],
            'sourceIds':[source],'endAfterReply':True}
    monkeypatch.setattr(e,'prepare',prepare)
    monkeypatch.setattr(e,'decide',decide)
    asyncio.run(e._exchange(topic['id']))
    assert len(prepared)==2 and peak==2
    with session_scope() as session:
        actions=session.scalars(select(SocialAction).where(SocialAction.topic_id==topic['id'])).all()
        assert len(actions)==1 and actions[0].data['action']['action']=='speak'


def test_prepared_group_decision_is_dropped_after_channel_revision(world,monkeypatch):
    e,_,topic,_=setup_social_topic(world)
    e.c.cognition.runtime.enabled=True
    prepared=False
    async def prepare(actor,room,current,source):
        nonlocal prepared
        if not prepared:
            prepared=True
            with session_scope() as session:
                session.get(SocialTopic,topic['id']).version+=1
        return {'intent':{'action':'speak'}}
    async def decide(*args,**kwargs):
        raise AssertionError('stale candidate must not reach expression')
    monkeypatch.setattr(e,'prepare',prepare)
    monkeypatch.setattr(e,'decide',decide)
    asyncio.run(e._exchange(topic['id']))
    with session_scope() as session:
        assert not session.scalars(select(SocialAction).where(SocialAction.topic_id==topic['id'])).all()


def test_group_prefetch_can_be_disabled_for_comparison(world,monkeypatch):
    e,_,topic,_=setup_social_topic(world)
    e.c.cognition.runtime.enabled=True
    monkeypatch.setenv('AZURJUUS_GROUP_PREFETCH','0')
    async def prepare(*args):
        raise AssertionError('prefetch must be disabled')
    async def decide(actor,room,current,source):
        return {'action':'speak','segments':['这一轮只说一句。'],
            'sourceIds':[source],'endAfterReply':True}
    monkeypatch.setattr(e,'prepare',prepare)
    monkeypatch.setattr(e,'decide',decide)
    asyncio.run(e._exchange(topic['id']))
    assert e.topic(topic['id'])['status']=='ended'


def test_committed_message_is_atomic_and_duplicate_safe(world):
    e,run,t,a=setup_social_topic(world)
    value={'action':'speak','segments':['这件事我也想听听。'],'mentions':[]}
    assert e.commit(t,a,'social-test-message',value)
    assert not e.commit(t,a,'social-test-message',value)
    with session_scope() as s:
        assert s.get(Message,'social-test-message').metadata_json['topicId']==t['id']
        assert s.get(SocialAction,'social-test-message').status=='committed'
    e.flush(); e.flush()
    assert len([x for x in e.c.store.events() if x['type']=='social.message'])==1
    e.c.cognition.pump()
    listener=next(x for x in e.snapshot('port-hub')['members'] if x['id']!=a['id'])
    heard=next(row for row in e.c.cognition.experiences(listener['id']) if row['text']=='这件事我也想听听。')
    assert heard['data']['speakerId']==a['id'] and heard['data']['ownSpeech'] is False
    assert heard['speakerName']==a['name']


def test_new_user_input_invalidates_pending_decision(world):
    e,run,t,a=setup_social_topic(world)
    e.interrupt(run['conversationId'])
    assert not e.commit(t,a,'stale',{'action':'speak','segments':['过期回复']})
    with session_scope() as s: assert s.get(Message,'stale') is None


def test_silence_does_not_create_fake_chat_message(world):
    e,run,t,a=setup_social_topic(world)
    assert e.commit(t,a,'quiet',{'action':'wait'})
    with session_scope() as s:
        assert s.get(Message,'quiet') is None
        assert s.get(SocialAction,'quiet').data['action']['action']=='wait'


def test_invitation_acceptance_changes_members_without_granting_tools(world):
    e,run,t,a=setup_social_topic(world)
    other=next(x for x in e.snapshot('port-hub')['members'] if x['id']!=a['id'])
    with session_scope() as s:
        m=s.scalar(select(ConversationMember).where(ConversationMember.conversation_id=='port-hub',ConversationMember.actor_id==other['id']))
        m.is_active=False
        s.add(Message(id='private-old',conversation_id='port-hub',speaker_id=a['id'],type='text',body='未分享的旧历史'))
    value={'action':'invite','targetId':other['id'],'reason':'想听听建议','summary':'正在讨论植物'}
    assert e.commit(t,a,'invite-test',value)
    async def accept(*args): return {'accept':True}
    e.generate=accept
    asyncio.run(e.invitation('invite-test'))
    room=e.snapshot('port-hub',other['id'])
    assert other['id'] in {x['id'] for x in room['members']}
    assert '未分享的旧历史' not in str(room['history'])
    assert room['invitationSummary']=='正在讨论植物'
    assert not e.c.store.get(run['id'])['assignments']
    assert not e.c.tokens


def test_muting_prevents_inflight_action(world):
    e,run,t,a=setup_social_topic(world)
    world[1].post('/api/conversations/port-hub/social',json={'muted':True}).raise_for_status()
    assert not e.commit(t,a,'muted',{'action':'speak','segments':['不应投递']})


def test_stale_invitation_cannot_add_member(world):
    e,run,t,a=setup_social_topic(world)
    other=next(x for x in e.snapshot('port-hub')['members'] if x['id']!=a['id'])
    with session_scope() as s:
        m=s.scalar(select(ConversationMember).where(ConversationMember.conversation_id=='port-hub',ConversationMember.actor_id==other['id']))
        m.is_active=False
    assert e.commit(t,a,'stale-invite',{'action':'invite','targetId':other['id'],'reason':'讨论','summary':'可分享摘要'})
    async def accept(*args):
        e.interrupt('port-hub')
        return {'accept':True}
    e.generate=accept
    asyncio.run(e.invitation('stale-invite'))
    assert other['id'] not in {x['id'] for x in e.snapshot('port-hub')['members']}


def test_model_can_choose_silence_without_forced_fallback(world):
    e,run,t,a=setup_social_topic(world)
    requests=[]
    async def silent(messages,settings):
        requests.append(messages)
        return {'action':'wait'}
    e.generate=silent
    asyncio.run(e.chat(run))
    assert 1<=len(requests)<=3
    with session_scope() as s:
        assert not s.scalars(select(Message).where(Message.id.like('social-%'))).all()


def test_invited_context_does_not_leak_old_topic_or_other_summaries(world):
    e,run,t,a=setup_social_topic(world)
    with session_scope() as s:
        room=s.get(Conversation,'port-hub')
        room.extra_json={'social':{'joinedAt':{a['id']:datetime.now(UTC).timestamp()},
            'summaries':{a['id']:'可分享的植物话题','another':'其他成员的私密摘要'}}}
    captured=[]
    async def decide(messages,settings):
        captured.extend(messages)
        return {'action':'wait'}
    e.generate=decide
    t['prompt']='入群前未分享的敏感原始话题'
    asyncio.run(e.decide(a,e.snapshot('port-hub',a['id']),t,'source'))
    serialized=json.dumps(captured,ensure_ascii=False)
    assert '入群前未分享的敏感原始话题' not in serialized
    assert '其他成员的私密摘要' not in serialized
    assert '可分享的植物话题' in serialized


def test_restart_expires_invitation_and_replays_only_saved_notifications(world):
    e,run,t,a=setup_social_topic(world)
    other=next(x for x in e.snapshot('port-hub')['members'] if x['id']!=a['id'])
    with session_scope() as s:
        s.scalar(select(ConversationMember).where(ConversationMember.conversation_id=='port-hub',ConversationMember.actor_id==other['id'])).is_active=False
    e.commit(t,a,'before-restart',{'action':'invite','targetId':other['id'],'reason':'讨论','summary':'可分享摘要'})
    e.recover(); e.recover()
    assert e.topic(t['id'])['status']=='interrupted'
    with session_scope() as s:
        assert s.get(SocialAction,'before-restart').status=='expired'
    assert other['id'] not in {x['id'] for x in e.snapshot('port-hub')['members']}
    assert len([x for x in e.c.store.events() if x['type']=='social.action'])==1


def test_same_second_new_messages_visible_but_old_ones_hidden(world):
    e,run,t,a=setup_social_topic(world)
    boundary=datetime.now(UTC).replace(microsecond=0)
    with session_scope() as s:
        room=s.get(Conversation,'port-hub')
        room.extra_json={'social':{'joinedAt':{a['id']:boundary.timestamp()},'joinBoundaryIds':{a['id']:['before']}}}
        for mid in ['before','after']:
            s.add(Message(id=mid,conversation_id='port-hub',speaker_id='commander',type='text',body=mid,created_at=boundary))
    visible={m['id'] for m in e.snapshot('port-hub',a['id'])['history']}
    assert 'before' not in visible and 'after' in visible


def test_invalid_invitation_response_preserves_membership(world):
    e,run,t,a=setup_social_topic(world)
    other=next(x for x in e.snapshot('port-hub')['members'] if x['id']!=a['id'])
    with session_scope() as s:
        s.scalar(select(ConversationMember).where(ConversationMember.conversation_id=='port-hub',ConversationMember.actor_id==other['id'])).is_active=False
    e.commit(t,a,'invalid-invite',{'action':'invite','targetId':other['id'],'reason':'讨论','summary':'可分享摘要'})
    async def invalid(*args): return []
    e.generate=invalid
    with pytest.raises(ValueError,match='邀请决定无效'):
        asyncio.run(e.invitation('invalid-invite'))
    assert other['id'] not in {x['id'] for x in e.snapshot('port-hub')['members']}


def test_structured_mention_has_priority_without_admitting_outsiders(world):
    e,run,t,a=setup_social_topic(world)
    room=e.snapshot('port-hub')
    target=room['members'][-1]
    room['history']=[{'speakerId':'commander','text':'你觉得呢？','mentions':[target['id'],'outsider']}]
    selected=e.candidates(room,t)
    assert selected[0]['id']==target['id']
    assert len(selected)<=3 and all(m in room['members'] for m in selected)


def test_send_rejects_unknown_structured_mention(world):
    _,client,_=world
    response=client.post('/api/messages/send',json={'conversationId':'port-hub','content':'你好','mode':'chat','mentions':['does-not-exist']})
    assert response.status_code==400


def test_notification_replay_does_not_give_old_experience_to_new_member(world):
    e,run,t,a=setup_social_topic(world)
    other=next(x for x in e.snapshot('port-hub')['members'] if x['id']!=a['id'])
    with session_scope() as s:
        s.scalar(select(ConversationMember).where(ConversationMember.conversation_id=='port-hub',ConversationMember.actor_id==other['id'])).is_active=False
    e.commit(t,a,'visible-then',{'action':'speak','segments':['只有当时在场的人知道的讨论。']})
    with session_scope() as s:
        s.scalar(select(ConversationMember).where(ConversationMember.conversation_id=='port-hub',ConversationMember.actor_id==other['id'])).is_active=True
    e.flush(); e.c.cognition.pump()
    assert '只有当时在场的人知道的讨论' in e.c.cognition.context(a['id'])
    assert '只有当时在场的人知道的讨论' not in e.c.cognition.context(other['id'])


def propose_group(world, aid):
    e,run,t,a=setup_social_topic(world)
    other=next(x for x in e.snapshot('port-hub')['members'] if x['id']!=a['id'])
    value={'action':'create_group','targetId':other['id'],'title':'植物交流','reason':'另开一个植物话题','summary':'分享种植经验，不涉及原群资料'}
    assert e.commit(t,a,aid,value)
    return e,run,t,a,other


def test_group_creation_requires_acceptance_and_shares_only_summary(world):
    e,run,t,a,other=propose_group(world,'new-group')
    async def accept(*args): return {'accept':True}
    e.generate=accept
    asyncio.run(e.invitation('new-group'))
    with session_scope() as s:
        record=s.get(SocialAction,'new-group')
        cid=record.data['createdConversationId']
        tid=record.data['nextTopicId']
        members=s.scalars(select(ConversationMember).where(ConversationMember.conversation_id==cid)).all()
        assert {m.actor_id for m in members}=={'commander',a['id'],other['id']}
        assert next(m for m in members if m.actor_id=='commander').role=='owner'
    room=e.snapshot(cid,other['id'])
    assert room['history']==[]
    assert e.topic(tid)['prompt']=='分享种植经验，不涉及原群资料'
    assert e.topic(tid)['openingActorId']==a['id']
    assert e.candidates(e.snapshot(cid),e.topic(tid))[0]['id']==a['id']
    decisions=iter([{'action':'wait'},
        {'action':'speak','segments':['这里聊植物吧。'],'sourceIds':['opening'],'replyTo':None,'mentions':[]}])
    async def opening(messages,settings):return next(decisions)
    e.generate=opening
    assert asyncio.run(e.decide(a,e.snapshot(cid,a['id']),e.topic(tid),'opening'))['action']=='speak'
    assert e.topic(t['id'])['status']=='moved'
    assert not e.c.store.get(run['id'])['assignments'] and not e.c.tokens
    # Deletion retains the tombstone and replay cannot recreate the channel.
    world[1].post(f'/api/conversations/{cid}/remove').raise_for_status()
    asyncio.run(e.invitation('new-group'))
    with pytest.raises(ValueError,match='归档'):
        e.snapshot(cid)


def test_rejected_group_invitation_creates_no_channel(world):
    e,run,t,a,other=propose_group(world,'declined-group')
    async def decline(*args): return {'accept':False}
    e.generate=decline
    asyncio.run(e.invitation('declined-group'))
    with session_scope() as s:
        row=s.get(SocialAction,'declined-group')
        assert row.status=='rejected' and 'createdConversationId' not in row.data
    assert e.topic(t['id'])['status']=='active'


def test_daily_group_limit_survives_engine_recovery(world):
    for index in range(3):
        e,run,t,a,other=propose_group(world,'daily-'+str(index))
        async def accept(*args): return {'accept':True}
        e.generate=accept
        asyncio.run(e.invitation('daily-'+str(index)))
        with session_scope() as s:
            row=s.get(SocialAction,'daily-'+str(index))
            assert row.status==('accepted' if index<2 else 'rejected')
            if index==2: assert '上限' in row.data['reason']
        e.recover()


def test_background_budget_persists_and_pause_does_not_spend(world):
    e,_,_,_=setup_social_topic(world)
    e.activity.change({'hourlyCalls':2})
    assert e.activity.reserve() and e.activity.reserve()
    assert not e.activity.reserve()
    e.recover()
    assert not e.activity.reserve()
    assert e.activity.inspect()['callsLastHour']==2
    e.activity.change({'paused':True,'hourlyCalls':3})
    assert not e.activity.reserve()
    assert e.activity.inspect()['callsLastHour']==2


def test_proactive_topic_requires_online_activity_and_does_not_replay(world):
    e,_,_,_=setup_social_topic(world)
    e.enabled=True
    calls=[]
    async def silent(*args): calls.append(True); return {'action':'wait'}
    e.generate=silent
    asyncio.run(e.activity.tick())
    assert not calls
    with session_scope() as s:
        s.add(Message(id='online-source',conversation_id='port-hub',speaker_id='commander',type='text',body='最近想了解植物。'))
    e.activity.touch('port-hub','online-source')
    e.activity.last_input-=61
    asyncio.run(e.activity.tick())
    assert 1<=len(calls)<=3
    count=len(calls)
    asyncio.run(e.activity.tick())
    assert len(calls)==count
    assert e.activity.inspect()['callsLastHour']==count
    assert not e.activity.reserve(topic=True)


def test_proactive_topic_budget_is_capped_even_for_legacy_rows(world):
    e, _, _, _ = setup_social_topic(world)
    assert e.topic_limit({'proactive': True, 'maxSpoken': 6}) == 6
    assert e.topic_limit({'proactive': False, 'maxSpoken': 6}) == 6
    assert e.topic_novelty('我也同意，甜甜圈确实很好吃。', ['甜甜圈确实很好吃。']) == 0.0


def test_idle_activity_does_not_open_topic(world):
    e,_,_,_=setup_social_topic(world)
    e.enabled=True
    e.activity.touch('port-hub','does-not-matter')
    e.activity.last_input-=901
    async def forbidden(*args): raise AssertionError('Idle user must not trigger inference')
    e.generate=forbidden
    asyncio.run(e.activity.tick())
    assert e.activity.inspect()['callsLastHour']==0


def test_user_interrupt_cancels_model_wait(world):
    from backend.social.idle_social import SocialPreempted
    e,_,t,_=setup_social_topic(world)
    async def scenario():
        started=asyncio.Event(); cancelled=asyncio.Event()
        async def slow(*args):
            started.set()
            try: await asyncio.sleep(10)
            finally: cancelled.set()
        e.generate=slow
        waiting=asyncio.create_task(e.call([],t))
        await started.wait()
        e.interrupt('port-hub')
        with pytest.raises(SocialPreempted): await asyncio.wait_for(waiting,1)
        assert cancelled.is_set()
    asyncio.run(scenario())


def test_social_settings_validate_ranges(world):
    client=world[1]
    assert client.post('/api/social/settings',json={'hourlyCalls':0}).status_code==400
    assert client.post('/api/social/settings',json={'paused':'yes'}).status_code==400
    assert client.post('/api/social/settings',json={'paused':True}).status_code==200
    assert client.get('/api/social/state').json()['settings']['paused'] is True


def test_worldbook_relationships_are_directional_and_background_is_not_invitable(world):
    from backend.characters.terminal_characters import worldbook
    e,_,t,a=setup_social_topic(world)
    book=worldbook()
    assert len(book['identities'])==12
    assert any(r['from']=='埃佛森' and r['to']=='七省' for r in book['relationships'])
    assert any(r['from']=='七省' and r['to']=='埃佛森' for r in book['relationships'])
    roster=world[1].get('/api/social/actors').json()['actors']
    assert not any(a['availability']=='background' for a in roster)
    with pytest.raises(ValueError):
        e.commit(t,a,'background-invite',{'action':'invite','targetId':'background-七省','summary':'讨论','reason':'讨论'})


def test_topic_relevance_changes_candidate_order(world):
    world[1].post('/api/personas/compose',json={'names':['标枪','绫波','Z23','拉菲','埃佛森'],
        'participantCount':5}).raise_for_status()
    e,_,t,_=setup_social_topic(world)
    room=e.snapshot('port-hub')
    room['history']=[{'speakerId':'commander','text':'想聊聊植物和昆虫的观察。'}]
    assert e.candidates(room,t)[0]['sourceName']=='埃佛森'


def test_group_focus_keeps_user_request_when_latest_speaker_digresses(world):
    e,_,t,_=setup_social_topic(world)
    room=e.snapshot('port-hub')
    room['history']=[
        {'id':'u1','speakerId':'commander','text':'标枪一行人包括谁？'},
        {'id':'a1','speakerId':room['members'][0]['id'],'text':'说起来我想喝茶。'},
    ]
    focus=e.conversation_focus(room,t)
    assert focus['latestUser']=={'id':'u1','text':'标枪一行人包括谁？'}
    assert focus['request']=='聊聊最近的兴趣'
    assert focus['openQuestion'] is None


def test_group_focus_moves_to_latest_speaker_after_first_reply(world):
    e,_,topic,_=setup_social_topic(world)
    room=e.snapshot('port-hub')
    room['history']=[
        {'id':'user','speakerId':'commander','text':'最初的问题？'},
        {'id':'peer','speakerId':room['members'][0]['id'],'text':'我看到的是另一种情况。'},
    ]
    focus=e.conversation_focus(room,{**topic,'spoken':1})
    assert focus['request'] is None
    assert focus['latestUser'] is None
    assert focus['current']['id']=='peer'


def test_autonomous_group_context_starts_at_its_own_opening(world):
    e,_,topic,actor=setup_social_topic(world)
    room=e.snapshot('port-hub',actor['id'])
    peer=next(a for a in room['members'] if a['id']!=actor['id'])
    room['history']=[
        {'id':'old-user','speakerId':'commander','text':'旧消息：等我回来再说。'},
        {'id':'opening','speakerId':peer['id'],'text':'我刚完成一次兴趣练习，想聊聊它。'},
        {'id':'peer-reply','speakerId':peer['id'],'text':'先聊练习时遇到的问题吧。'},
    ]
    topic={**topic,'proactive':True,'openingMessageId':'opening','spoken':2}
    captured=[]
    async def model(messages,settings):
        captured.extend(messages)
        return {'action':'wait'}
    e.generate=model
    assert asyncio.run(e.decide(actor,room,topic,'own-thread'))['action']=='wait'
    public=json.dumps(captured,ensure_ascii=False)
    assert '旧消息：等我回来再说' not in public
    assert '先聊练习时遇到的问题' in public
    assert '不是用户提问' in public


def test_autonomous_group_does_not_repeat_waiting_for_user(world):
    e,_,topic,actor=setup_social_topic(world)
    room=e.snapshot('port-hub',actor['id'])
    peer=next(a for a in room['members'] if a['id']!=actor['id'])
    room['history']=[
        {'id':'opening','speakerId':peer['id'],'text':'刚完成兴趣练习，想聊聊卡住的地方。'},
    ]
    topic={**topic,'proactive':True,'openingMessageId':'opening','spoken':1}
    attempts=[]
    async def model(messages,settings):
        attempts.append(messages)
        return ({'action':'speak','segments':['我也在等指挥官回复。'],'sourceIds':['peer-turn']}
            if len(attempts)==1 else {'action':'wait'})
    e.generate=model
    assert asyncio.run(e.decide(actor,room,topic,'peer-turn'))['action']=='wait'
    assert len(attempts)==2


def test_named_group_request_gets_a_response_without_repeating_old_topic(world):
    e,_,topic,actor=setup_social_topic(world)
    room=e.snapshot('port-hub',actor['id'])
    room['history']=[{'id':'u1','speakerId':'commander','text':'@'+actor['name']+'，请另建一个新群。','mentions':[actor['id']]}]
    replies=iter([{'action':'wait'},
        {'action':'speak','segments':['好，我先确认成员。'],'sourceIds':['directed'],'replyTo':'u1','mentions':[]}])
    calls=[]
    async def model(messages,settings):
        calls.append(messages)
        return next(replies)
    e.generate=model
    result=asyncio.run(e.decide(actor,room,topic,'directed'))
    assert result['action']=='speak'
    assert len(calls)==2
    assert 'create_group' in calls[0][0]['content']


def test_model_action_variants_require_unambiguous_fields():
    from backend.social.social_engine import normalized_social_action
    assert normalized_social_action({'type':'create_group','targetId':'a','title':'新群'})['action']=='create_group'
    assert normalized_social_action({'type':'json_object','targetId':'a','title':'新群',
        'reason':'讨论','summary':'仅分享当前话题'})['action']=='create_group'
    assert normalized_social_action({'segments':['一句话'],'sourceIds':['s']})['action']=='speak'
    assert normalized_social_action({'type':'json_object','title':'新群'})=={'type':'json_object','title':'新群'}


@pytest.mark.parametrize('request_text',['请另建一个植物交流群','请拉个群聊植物','请创建新群'])
def test_new_group_intent_and_expression_share_action_contract(world,monkeypatch,request_text):
    e,_,topic,actor=setup_social_topic(world)
    room=e.snapshot('port-hub',actor['id'])
    room['history']=[{'id':'u1','speakerId':'commander','text':'@'+actor['name']+request_text,
        'mentions':[actor['id']]}]
    peer=next(a for a in room['members'] if a['id']!=actor['id'])
    runtime=e.c.cognition.runtime
    monkeypatch.setattr(runtime,'active',lambda _:True)
    monkeypatch.setattr(runtime,'profile',lambda _:{'interpretation':{'tendencies':['认真回应']}})
    captured=[]
    async def think(*args,**kwargs):
        captured.append((json.loads(args[2]),kwargs['actions']))
        return {'intent':{'action':'create_group','targetId':peer['id']}}
    monkeypatch.setattr(runtime,'think',think)
    prepared=asyncio.run(e.prepare(actor,room,topic,'new-group-contract'))
    assert captured[0][1]==['speak','create_group']
    assert 'invite 只将群外人物加入当前群' in captured[0][0]['guidance']
    responses=iter([{'action':'invite','targetId':peer['id'],'reason':'聊植物','summary':'只聊植物'},
        {'action':'create_group','targetId':peer['id'],'reason':'聊植物','summary':'只聊植物','title':'植物交流'}])
    calls=[]
    async def generate(messages,settings):
        calls.append(messages)
        return next(responses)
    e.generate=generate
    result=asyncio.run(e.decide(actor,room,topic,'new-group-contract',prepared=prepared))
    assert result['action']=='create_group' and result['targetId']==peer['id']
    assert len(calls)==2
    assert '表达与已选择的行动不一致' in calls[-1][-1]['content']


def test_new_group_opening_allows_only_speech(world,monkeypatch):
    e,_,topic,actor=setup_social_topic(world)
    room={**e.snapshot('port-hub',actor['id']),'history':[]}
    assert e.turn_contract(actor,room,{**topic,'openingActorId':actor['id']})[3]==['speak']


def test_social_speech_does_not_invent_measurements_or_sent_files(world):
    e,_,topic,actor=setup_social_topic(world)
    room=e.snapshot('port-hub',actor['id'])
    for speech in ('我测得湿度是 85%。','记录发你了。'):
        outputs=iter([{'action':'speak','segments':[speech],'sourceIds':['fact-check']},{'action':'wait'}])
        async def model(messages,settings):return next(outputs)
        e.generate=model
        result=asyncio.run(e.decide(actor,room,topic,'fact-check'))
        assert result['action']=='wait'


def test_legacy_messages_with_same_second_follow_insert_order(world):
    e,_,_,a=setup_social_topic(world)
    when=datetime(2026,1,1,tzinfo=UTC)
    with session_scope() as s:
        s.add(Message(id='z-first',conversation_id='port-hub',speaker_id=a['id'],
            type='text',body='第一句',created_at=when))
        s.add(Message(id='a-second',conversation_id='port-hub',speaker_id=a['id'],
            type='text',body='第二句',created_at=when))
    ids=[m['id'] for m in e.snapshot('port-hub')['history']]
    assert ids.index('z-first')<ids.index('a-second')
    response=world[1].get('/api/workspace/load').json()['workspace']['data']['messages']['port-hub']
    ids=[m['id'] for m in response]
    assert ids.index('z-first')<ids.index('a-second')


def test_group_respects_auto_reply_budget_even_if_agents_keep_asking(world):
    e,_,t,_=setup_social_topic(world)
    calls=[]
    async def speak(actor, room, topic, source):
        calls.append((actor['id'],e.conversation_focus(room,topic)))
        last=room['history'][-1] if room['history'] else None
        return {'action':'speak','segments':[f'第{len(calls)}次接话？'],
            'sourceIds':[source],'replyTo':last['id'] if last else None,'mentions':[]}
    e.decide=speak
    asyncio.run(e._exchange(t['id']))
    assert len(calls)==e.topic_limit(t)
    assert calls[-1][1]['openQuestion']['text']==f'第{len(calls)-1}次接话？'
    assert e.topic(t['id'])['status']=='waiting'


def test_model_retries_inviting_an_existing_member_before_commit(world):
    e,_,t,a=setup_social_topic(world)
    peer=next(m for m in e.snapshot('port-hub')['members'] if m['id']!=a['id'])
    outputs=iter([
        {'action':'invite','targetId':peer['id'],'reason':'聊聊','summary':'已有的群聊'},
        {'action':'wait'},
    ])
    async def decide_model(messages,settings): return next(outputs)
    e.generate=decide_model
    value=asyncio.run(e.decide(a,e.snapshot('port-hub',a['id']),t,'new-action'))
    assert value['action']=='wait'


def test_direct_question_does_not_force_repetitive_answer_after_silence(world):
    e,_,t,_=setup_social_topic(world)
    decisions=[]
    async def model(messages,settings):
        request=json.loads(messages[-1]['content'])
        decisions.append(request)
        return {'action':'wait'}
    e.generate=model
    asyncio.run(e._exchange(t['id']))
    assert 1 <= len(decisions) <= 3
    assert all('instruction' not in decision for decision in decisions)
    with session_scope() as s:
        assert s.get(SocialTopic,t['id']).status=='ended'


def test_cancelling_exchange_expires_pending_invitation(world):
    e,_,t,a,_=propose_group(world,'cancel-pending')
    async def scenario():
        entered=asyncio.Event()
        async def wait_forever(tid):
            entered.set(); await asyncio.sleep(10)
        e._exchange=wait_forever
        task=asyncio.create_task(e.exchange(t['id']))
        await entered.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError): await task
    asyncio.run(scenario())
    with session_scope() as s:
        assert s.get(SocialAction,'cancel-pending').status=='expired'
    assert e.topic(t['id'])['status']=='interrupted'
