import asyncio
import json
from datetime import UTC, datetime
from uuid import uuid4
import pytest
from sqlalchemy import select
from test_cognition import world
from backend.database import session_scope
from backend.models import Conversation, ConversationMember, Message
from backend.social_models import SocialAction


def setup(world):
    c,client,ids=world
    engine=c.social_engine
    room=engine.snapshot('port-hub')
    run,_=c.store.create({'actorId':ids[0],'actors':room['members'],'mode':'chat','collaborative':False,
        'conversationId':'port-hub','prompt':'聊聊最近的兴趣','history':[]},uuid4().hex)
    tid=engine.begin(run)
    return engine,run,engine.topic(tid),room['members'][0]


def test_committed_message_is_atomic_and_duplicate_safe(world):
    e,run,t,a=setup(world)
    value={'action':'speak','segments':['这件事我也想听听。'],'mentions':[]}
    assert e.commit(t,a,'social-test-message',value)
    assert not e.commit(t,a,'social-test-message',value)
    with session_scope() as s:
        assert s.get(Message,'social-test-message').metadata_json['topicId']==t['id']
        assert s.get(SocialAction,'social-test-message').status=='committed'
    e.flush(); e.flush()
    assert len([x for x in e.c.store.events() if x['type']=='social.message'])==1


def test_new_user_input_invalidates_pending_decision(world):
    e,run,t,a=setup(world)
    e.interrupt(run['conversationId'])
    assert not e.commit(t,a,'stale',{'action':'speak','segments':['过期回复']})
    with session_scope() as s: assert s.get(Message,'stale') is None


def test_silence_does_not_create_fake_chat_message(world):
    e,run,t,a=setup(world)
    assert e.commit(t,a,'quiet',{'action':'wait'})
    with session_scope() as s:
        assert s.get(Message,'quiet') is None
        assert s.get(SocialAction,'quiet').data['action']['action']=='wait'


def test_invitation_acceptance_changes_members_without_granting_tools(world):
    e,run,t,a=setup(world)
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
    e,run,t,a=setup(world)
    world[1].post('/api/conversations/port-hub/social',json={'muted':True}).raise_for_status()
    assert not e.commit(t,a,'muted',{'action':'speak','segments':['不应投递']})


def test_stale_invitation_cannot_add_member(world):
    e,run,t,a=setup(world)
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
    e,run,t,a=setup(world)
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
    e,run,t,a=setup(world)
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
    e,run,t,a=setup(world)
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
    e,run,t,a=setup(world)
    boundary=datetime.now(UTC).replace(microsecond=0)
    with session_scope() as s:
        room=s.get(Conversation,'port-hub')
        room.extra_json={'social':{'joinedAt':{a['id']:boundary.timestamp()},'joinBoundaryIds':{a['id']:['before']}}}
        for mid in ['before','after']:
            s.add(Message(id=mid,conversation_id='port-hub',speaker_id='commander',type='text',body=mid,created_at=boundary))
    visible={m['id'] for m in e.snapshot('port-hub',a['id'])['history']}
    assert 'before' not in visible and 'after' in visible


def test_invalid_invitation_response_preserves_membership(world):
    e,run,t,a=setup(world)
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
    e,run,t,a=setup(world)
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
    e,run,t,a=setup(world)
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
    e,run,t,a=setup(world)
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
    e,_,_,_=setup(world)
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
    e,_,_,_=setup(world)
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


def test_idle_activity_does_not_open_topic(world):
    e,_,_,_=setup(world)
    e.enabled=True
    e.activity.touch('port-hub','does-not-matter')
    e.activity.last_input-=901
    async def forbidden(*args): raise AssertionError('Idle user must not trigger inference')
    e.generate=forbidden
    asyncio.run(e.activity.tick())
    assert e.activity.inspect()['callsLastHour']==0


def test_user_interrupt_cancels_model_wait(world):
    from backend.idle_social import SocialPreempted
    e,_,t,_=setup(world)
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
    from backend.terminal_characters import worldbook
    e,_,t,a=setup(world)
    book=worldbook()
    assert len(book['identities'])==12
    assert any(r['from']=='埃佛森' and r['to']=='七省' for r in book['relationships'])
    assert any(r['from']=='七省' and r['to']=='埃佛森' for r in book['relationships'])
    roster=world[1].get('/api/social/actors').json()['actors']
    assert not any(a['availability']=='background' for a in roster)
    with pytest.raises(ValueError):
        e.commit(t,a,'background-invite',{'action':'invite','targetId':'background-七省','summary':'讨论','reason':'讨论'})


def test_topic_relevance_changes_candidate_order(world):
    e,_,t,_=setup(world)
    room=e.snapshot('port-hub')
    room['history']=[{'speakerId':'commander','text':'想聊聊植物和昆虫的观察。'}]
    assert e.candidates(room,t)[0]['sourceName']=='埃佛森'


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
