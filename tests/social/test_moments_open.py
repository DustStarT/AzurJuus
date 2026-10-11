"""Moments permissions, empty bootstrap, and online-only scheduling."""
import asyncio
import time
from unittest.mock import AsyncMock
import pytest

from sqlalchemy import select

from backend.database import session_scope
from backend.models import Actor, SocialComment, SocialPost, WorkspaceSetting
from backend.social.social_models import SocialPublication, SocialShareConsent
from backend.mind.cognition_models import Experience
from backend.mind.mind_models import LifeActivity
from backend.mind.mind_models import MindControl
from backend.social.moments_runtime import PostDraft
from conftest import open_client


def test_user_publication_audience_and_observer(monkeypatch,tmp_path):
    with open_client(monkeypatch,tmp_path) as client:
        service=client.app.state.service
        agents=client.get('/api/bootstrap').json()['snapshot']['agents']
        invited,other=agents[0]['id'],agents[1]['id']
        assert client.get('/api/posts').json()['posts']==[]
        assert client.post('/api/posts/publish',json={'authorId':invited,'body':'冒充人物'}).status_code==400
        response=client.post('/api/posts/publish',json={'body':'只和一位成员分享的完整内容。',
            'audience':{'kind':'selected','actorIds':[invited]}})
        response.raise_for_status()
        post=client.get('/api/posts').json()['posts'][0]
        assert post['body']=='只和一位成员分享的完整内容。'
        with session_scope() as session:
            row=session.get(SocialPost,post['id'])
            assert service.post_visible(session,row,invited)
            assert not service.post_visible(session,row,other)
            assert session.get(SocialPublication,row.id).data['audience']['actorIds']==[invited]
            observed=[r.actor_id for r in session.scalars(select(Experience)).all()
                if r.data.get('postId')==post['id']]
            assert invited in observed and other not in observed
            assert not any(r.data.get('shareable') for r in session.scalars(select(Experience)).all()
                if r.data.get('postId')==post['id'])
        client.post('/api/posts/comment',json={'postId':post['id'],'body':'我看到了。'}).raise_for_status()
        client.post('/api/posts/like',json={'postId':post['id']}).raise_for_status()
        post=client.get('/api/posts').json()['posts'][0]
        assert post['likedByUser'] and len(post['comments'])==1
        with session_scope() as session:
            author=session.get(Actor,invited)
            hidden=service._create_social_post(session,author=author,body='只给另一位成员看的动态。',
                audience={'kind':'selected','actorIds':[other]})
            hidden_id=hidden.id
        assert hidden_id not in {p['id'] for p in client.get('/api/posts').json()['posts']}
        assert hidden_id in {p['id'] for p in client.get('/api/posts?mode=observe').json()['posts']}
        assert client.post('/api/posts/like',json={'postId':hidden_id}).status_code==404
        public=client.post('/api/posts/publish',json={'body':'公开给港区的一条想法。'})
        public.raise_for_status()
        public_id=public.json()['snapshot']['posts'][0]['id']
        with session_scope() as session:
            visible=[r for r in session.scalars(select(Experience)).all() if r.data.get('postId')==public_id]
            assert {r.actor_id for r in visible}=={a['id'] for a in agents}
            assert all(r.data.get('shareable') for r in visible)


def test_old_post_is_observer_only_and_reset_clears_new_tables(monkeypatch,tmp_path):
    with open_client(monkeypatch,tmp_path) as client:
        with session_scope() as session:
            actor=session.scalar(select(Actor).where(Actor.kind=='agent'))
            session.add(SocialPost(id='legacy-only',author_id=actor.id,excerpt='截断摘要',
                likes=1,liked_by_user=False,art_palette=[],following=False))
        assert client.get('/api/posts').json()['posts']==[]
        archived=client.get('/api/posts?mode=observe').json()['posts'][0]
        assert archived['legacyArchived'] and archived['body']=='截断摘要'
        assert client.post('/api/posts/comment',json={'postId':'legacy-only','body':'不应允许'}).status_code==404
        client.post('/api/posts/publish',json={'body':'重置前的动态'}).raise_for_status()
        client.post('/api/system/reset',json={'preserveKeysAndProfile':False}).raise_for_status()
        assert client.get('/api/posts?mode=observe').json()['posts']==[]
        with session_scope() as session:
            assert session.scalars(select(SocialPublication)).all()==[]
            assert session.scalars(select(SocialComment)).all()==[]


def test_public_post_only_reaches_current_roster(monkeypatch,tmp_path):
    with open_client(monkeypatch,tmp_path) as client:
        agents=client.get('/api/bootstrap').json()['snapshot']['agents']
        hidden=agents[-1]
        with session_scope() as session:
            setting=session.get(WorkspaceSetting,1)
            setting.character_roster_text='\n'.join(a['sourceCharacter'] for a in agents[:-1])
        assert client.post('/api/posts/publish',json={'body':'只给当前港区名单。',
            'audience':{'kind':'selected','actorIds':[hidden['id']]}}).status_code==400
        client.post('/api/posts/publish',json={'body':'当前港区公开。'}).raise_for_status()
        with session_scope() as session:
            assert hidden['id'] not in {r.actor_id for r in session.scalars(select(Experience)).all()
                if r.data.get('sourceKind')=='social.post'}


def test_moments_idle_zero_calls_and_joint_share_needs_each_consent(mind,monkeypatch):
    runtime,coordinator,client,actors,stages=mind
    moments=coordinator.moments
    monkeypatch.setattr(runtime.life,'quiet',lambda:False)
    monkeypatch.setattr(runtime,'background_blocked',lambda *args,**kwargs:False)
    runtime.control({'quietStart':8,'quietEnd':8})
    runtime.generate=AsyncMock(side_effect=AssertionError('idle must not call model'))
    asyncio.run(moments.tick())
    assert runtime.generate.await_count==0

    decisions=[]
    async def think(actor_id,*args,**kwargs):
        decisions.append(actor_id)
        return {'intent':{'action':'decline'}}
    monkeypatch.setattr(runtime,'think',think)
    event={'id':'joint-event','participants':actors[:2],'text':'双方完成共同交流。'}
    assert not asyncio.run(moments._share_approved(event,actors[0]))
    assert decisions==[actors[1]]
    with session_scope() as session:
        rows=session.scalars(select(SocialShareConsent)).all()
        assert {row.actor_id:row.status for row in rows}=={actors[0]:'accepted',actors[1]:'declined'}
    assert not asyncio.run(moments._share_approved(event,actors[0]))
    assert decisions==[actors[1]]


def test_first_thought_is_due_within_session_and_not_repeated(mind,monkeypatch):
    runtime,coordinator,client,actors,_=mind
    moments=coordinator.moments
    monkeypatch.setattr(runtime.life,'quiet',lambda:False)
    monkeypatch.setattr(runtime,'background_blocked',lambda *args,**kwargs:False)
    post=AsyncMock(return_value='wait')
    monkeypatch.setattr(moments,'_post',post)
    moments._change(lambda data:data.update(startAt=time.time()-12*3600))
    assert client.get('/api/posts/status').json()['nextConsiderAt']<=time.time()
    asyncio.run(moments.tick())
    assert post.await_count==1
    first=post.await_args.args[0]
    assert first in actors
    with session_scope() as session:
        assert session.get(MindControl,'moments-control').data['thoughtAt'][first]>time.time()-5
    # The same actor is not retried; another overdue actor can take the next turn.
    asyncio.run(moments.tick())
    assert post.await_count==2
    assert post.await_args.args[0]!=first


def test_delayed_thought_does_not_block_other_due_actors(mind,monkeypatch):
    runtime,coordinator,_,actors,_=mind
    moments=coordinator.moments
    monkeypatch.setattr(runtime.life,'quiet',lambda:False)
    monkeypatch.setattr(runtime,'background_blocked',lambda *args,**kwargs:False)
    post=AsyncMock(return_value='wait')
    monkeypatch.setattr(moments,'_post',post)
    moments._change(lambda data:data.update(startAt=time.time()-12*3600,
        retry={'thought:'+actors[0]:time.time()+3600}))
    asyncio.run(moments.tick())
    assert post.await_count==1
    assert post.await_args.args[0]!=actors[0]


def test_restart_requires_fresh_online_thought_decision(mind,monkeypatch):
    runtime,coordinator,_,actors,_=mind
    moments=coordinator.moments
    monkeypatch.setattr(runtime.life,'quiet',lambda:False)
    monkeypatch.setattr(runtime,'background_blocked',lambda *args,**kwargs:False)
    post=AsyncMock(return_value='wait')
    monkeypatch.setattr(moments,'_post',post)
    moments._change(lambda data:data.update(startAt=time.time()-12*3600))
    moments.initialize(restart=True)
    asyncio.run(moments.tick())
    assert post.await_count==0
    moments._change(lambda data:data.update(resumeThoughtAt=time.time()-1))
    asyncio.run(moments.tick())
    assert post.await_count==1


def test_autonomous_new_activity_uses_life_state_machine_not_tools(mind,monkeypatch):
    runtime,coordinator,_,actors,_=mind
    actor_id=actors[0]
    monkeypatch.setattr(runtime,'background_blocked',lambda *args,**kwargs:False)
    monkeypatch.setattr(runtime,'profile',lambda aid:{'interpretation':{}})
    async def think(*args,**kwargs):
        return {'id':'synthetic-decision','intent':{'action':'publish','purpose':'练习阅读'}}
    async def call(*args,**kwargs):
        return PostDraft(publish=False,kind='new_activity',activityKind='read')
    monkeypatch.setattr(runtime,'think',think)
    monkeypatch.setattr(runtime,'call',call)
    assert asyncio.run(coordinator.moments._post(actor_id,'thought:'+actor_id,None))=='activity'
    with session_scope() as session:
        assert session.scalars(select(SocialPost)).all()==[]
        activities=session.scalars(select(LifeActivity)).all()
        assert len(activities)==1 and activities[0].data['kind']=='read'
    assert coordinator.store.list(all_rows=True)==[]


def test_daily_post_limit_does_not_call_model(mind,monkeypatch):
    runtime,coordinator,_,actors,_=mind
    actor_id=actors[0]
    runtime.control({'quietStart':8,'quietEnd':8,'momentsActorDailyPosts':0})
    monkeypatch.setattr(runtime.life,'quiet',lambda:False)
    monkeypatch.setattr(runtime,'background_blocked',lambda *args,**kwargs:False)
    runtime.think=AsyncMock(side_effect=AssertionError('daily cap must stop inference'))
    coordinator.moments._change(lambda data:data.update(pending=['thought:'+actor_id]))
    asyncio.run(coordinator.moments.tick())
    assert runtime.think.await_count==0
    with session_scope() as session:
        assert session.get(MindControl,'moments-control').data['pending']==[]


def test_post_and_daily_count_commit_together(mind,monkeypatch):
    runtime,coordinator,_,actors,_=mind
    actor_id=actors[0]
    monkeypatch.setattr(runtime,'background_blocked',lambda *args,**kwargs:False)
    monkeypatch.setattr(runtime,'profile',lambda aid:{'interpretation':{}})
    async def think(*args,**kwargs):
        return {'id':'synthetic-post-decision','intent':{'action':'publish','purpose':'想分享想法'}}
    async def call(*args,**kwargs):
        return PostDraft(publish=True,kind='thought',body='今天想记下一点心情。')
    monkeypatch.setattr(runtime,'think',think)
    monkeypatch.setattr(runtime,'call',call)
    assert asyncio.run(coordinator.moments._post(actor_id,'thought:'+actor_id,None))=='post'
    # Simulate a crash before _finish(): the committed post already spent quota.
    coordinator.moments.initialize(restart=True)
    with session_scope() as session:
        assert len(session.scalars(select(SocialPost)).all())==1
        control=session.get(MindControl,'moments-control').data
        assert control['daily']['posts'][actor_id]==1


@pytest.mark.parametrize('body',['今天在港口散步了。','我刚刚读完了一本书。','我看了test.pdf。'])
def test_unrecorded_activity_cannot_be_published_as_thought(mind,monkeypatch,body):
    runtime,coordinator,_,actors,_=mind
    monkeypatch.setattr(runtime,'background_blocked',lambda *args,**kwargs:False)
    monkeypatch.setattr(runtime,'profile',lambda aid:{'interpretation':{}})
    async def think(*args,**kwargs):
        return {'id':'no-event-decision','intent':{'action':'publish','purpose':'分享近况'}}
    async def call(*args,**kwargs):
        return PostDraft(publish=True,kind='thought',body=body)
    monkeypatch.setattr(runtime,'think',think)
    monkeypatch.setattr(runtime,'call',call)
    with pytest.raises(ValueError):
        asyncio.run(coordinator.moments._post(actors[0],'thought:'+actors[0],None))
    with session_scope() as session:
        assert session.scalars(select(SocialPost)).all()==[]


def test_restart_drops_unpublished_moments_plan(monkeypatch,tmp_path):
    with open_client(monkeypatch,tmp_path):
        with session_scope() as session:
            row=session.get(MindControl,'moments-control')
            row.data={**row.data,'pending':['thought:old','post:old']}
    with open_client(monkeypatch,tmp_path):
        with session_scope() as session:
            assert session.get(MindControl,'moments-control').data['pending']==[]
