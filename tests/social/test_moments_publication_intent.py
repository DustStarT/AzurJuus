"""Publishing intent and reader permissions, with no live model calls."""
import asyncio
import json
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError
from sqlalchemy import select

from backend.database import session_scope
from backend.models import Actor, SocialPost, SocialComment
from backend.mind.mind_models import MindControl
from backend.social.moments_runtime import PostDraft, CommentDraft


def scripted_model(runtime, monkeypatch, *, action='publish', draft=None):
    original=runtime.generate
    requests=[]
    async def generate(messages, settings):
        stage=settings['_mindStage']
        data=json.loads(messages[-1]['content'])
        requests.append((stage,data))
        if stage=='decide':
            return {'action':action,'purpose':'现在分享一点想法。' if action=='publish' else '先继续聊天，等吃到再考虑分享。',
                'sourceIds':data['frame']['sourceIds']}
        if stage=='moments_post':return draft
        return await original(messages,settings)
    monkeypatch.setattr(runtime,'generate',generate)
    monkeypatch.setattr(runtime,'background_blocked',lambda *args,**kwargs:False)
    return requests


def assert_no_publication(actor_id):
    with session_scope() as session:
        assert session.scalars(select(SocialPost)).all()==[]
        assert session.get(MindControl,'moments-control').data.get('daily',{}).get('posts',{}).get(actor_id,0)==0


def test_wait_is_not_published_or_converted_to_a_chat_reply(mind,monkeypatch):
    runtime,coordinator,_,actors,_=mind
    requests=scripted_model(runtime,monkeypatch,action='wait')
    assert asyncio.run(coordinator.moments._post(actors[0],'thought:'+actors[0],None))=='wait'
    decision=next(payload for stage,payload in requests if stage=='decide')
    assert decision['allowedActions']==['publish','wait']
    assert all(stage!='moments_post' for stage,_ in requests)
    assert_no_publication(actors[0])


def test_generic_speak_is_never_a_publication(mind,monkeypatch):
    runtime,coordinator,_,actors,_=mind
    monkeypatch.setattr(runtime,'think',AsyncMock(return_value={'intent':{
        'action':'speak','purpose':'朋友圈先放一放，先确认午饭。'}}))
    call=AsyncMock(side_effect=AssertionError('chat intent must not become a draft'))
    monkeypatch.setattr(runtime,'call',call)
    assert asyncio.run(coordinator.moments._post(actors[0],'thought:'+actors[0],None))=='wait'
    assert call.await_count==0
    assert_no_publication(actors[0])


def test_later_opportunity_reconsiders_instead_of_reusing_wait(mind,monkeypatch):
    runtime,coordinator,client,actors,_=mind
    key='thought:'+actors[0]
    scripted_model(runtime,monkeypatch,action='wait')
    assert asyncio.run(coordinator.moments._post(actors[0],key,None))=='wait'
    requests=scripted_model(runtime,monkeypatch,draft={'publish':True,'body':'现在想分享一点心情。'})
    assert asyncio.run(coordinator.moments._post(actors[0],key,None))=='post'
    assert any(stage=='decide' for stage,_ in requests)
    assert len(client.get('/api/posts').json()['posts'])==1


def test_draft_can_withdraw_without_spending_post_quota(mind,monkeypatch):
    runtime,coordinator,_,actors,_=mind
    scripted_model(runtime,monkeypatch,draft={'publish':False,'kind':'thought',
        'body':'拉菲要焦糖布丁……朋友圈的事先放一放，等真的吃到了再想。',
        'audienceKind':'selected','selectedIds':[actors[1]]})
    asyncio.run(coordinator.moments._attempt('thought:'+actors[0],actors[0],
        lambda:coordinator.moments._post(actors[0],'thought:'+actors[0],None)))
    assert_no_publication(actors[0])
    with session_scope() as session:
        assert actors[0] in session.get(MindControl,'moments-control').data['thoughtAt']


def test_explicit_publication_has_actor_and_real_reader_choices(mind,monkeypatch):
    runtime,coordinator,client,actors,_=mind
    requests=scripted_model(runtime,monkeypatch,draft={'publish':True,'kind':'thought',
        'body':'忽然有点想尝尝焦糖布丁。','audienceKind':'port'})
    assert asyncio.run(coordinator.moments._post(actors[0],'thought:'+actors[0],None))=='post'
    payload=next(payload for stage,payload in requests if stage=='moments_post')
    assert payload['actorId']==actors[0]
    assert {reader['id'] for reader in payload['audienceOptions']}=={'commander',*actors}
    assert client.get('/api/posts').json()['posts'][0]['audience']['kind']=='port'


def test_intended_selected_readers_do_not_gain_commander_permission(mind,monkeypatch):
    runtime,coordinator,client,actors,_=mind
    scripted_model(runtime,monkeypatch,draft={'publish':True,'body':'只想和你分享这个想法。',
        'audienceKind':'selected','selectedIds':[actors[1]]})
    assert asyncio.run(coordinator.moments._post(actors[0],'thought:'+actors[0],None))=='post'
    assert client.get('/api/posts').json()['posts']==[]
    post=client.get('/api/posts?mode=observe').json()['posts'][0]
    assert post['audience']['actorIds']==[actors[1]]
    assert client.post('/api/posts/like',json={'postId':post['id']}).status_code==404
    assert client.post('/api/posts/comment',json={'postId':post['id'],'body':'越权评论'}).status_code==404


def test_missing_publication_choice_fails_closed(mind,monkeypatch):
    runtime,coordinator,_,actors,_=mind
    scripted_model(runtime,monkeypatch,draft={'kind':'thought','body':'不能默认发布。'})
    with pytest.raises(ValidationError):
        asyncio.run(coordinator.moments._post(actors[0],'thought:'+actors[0],None))
    assert_no_publication(actors[0])


def test_new_activity_cannot_also_be_a_publication(mind,monkeypatch):
    runtime,coordinator,_,actors,_=mind
    scripted_model(runtime,monkeypatch,draft={'publish':True,'kind':'new_activity','activityKind':'read'})
    with pytest.raises(ValueError,match='新生活活动'):
        asyncio.run(coordinator.moments._post(actors[0],'thought:'+actors[0],None))
    assert_no_publication(actors[0])


def test_author_can_reply_to_peer_comment_without_calling_peer_user(mind,monkeypatch):
    runtime,coordinator,_,actors,_=mind
    author_id,peer_id=actors[:2]
    with session_scope() as session:
        author=session.get(Actor,author_id)
        peer=session.get(Actor,peer_id)
        peer_name=peer.name
        post=coordinator.moments.service._create_social_post(session,author=author,body='想尝尝布丁。')
        post_id=post.id
        parent=coordinator.moments.service._create_social_comment(session,post=post,author_id=peer_id,body='焦糖的也不错。')
        parent_id=parent.id
        assert not coordinator.moments.service.actor_react(session,post_id,author_id,'like')
    monkeypatch.setattr(runtime,'background_blocked',lambda *args,**kwargs:False)
    monkeypatch.setattr(runtime,'profile',lambda actor:{'interpretation':{}})
    think=AsyncMock(return_value={'intent':{'action':'speak','purpose':'回应同伴'}})
    call=AsyncMock(return_value=CommentDraft(body='那就先试试焦糖的～'))
    monkeypatch.setattr(runtime,'think',think)
    monkeypatch.setattr(runtime,'call',call)
    assert asyncio.run(coordinator.moments._interact(author_id,post_id,'comment:'+parent_id,'焦糖的也不错。',peer_id,parent_id))=='interaction'
    assert '评论作者：'+peer_name in think.await_args.args[2]
    payload=call.await_args.args[2]
    assert payload['postAuthor']['id']==author_id and payload['commentAuthor']['id']==peer_id
    with session_scope() as session:
        comments=session.scalars(select(SocialComment).where(SocialComment.post_id==post_id)).all()
        assert {comment.author_id for comment in comments}=={author_id,peer_id}
        reply=next(comment for comment in comments if comment.author_id==author_id)
        assert reply.reply_to_id==parent_id
