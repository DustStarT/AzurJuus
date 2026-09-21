import asyncio
from pathlib import Path
import pytest
from sqlalchemy import select
from test_cognition import world
from backend.database import session_scope
from backend.models import Actor,SocialPost
from backend.terminal_characters import lore
from backend.attachments import resolve
from backend.expression import validate


def test_world_override_persists_and_reaches_context(world):
    c,client,_=world
    original=client.get('/api/terminal/settings').json()
    entry=original['world'][0]
    response=client.post('/api/terminal/settings',json={'maxTaskMembers':18,'worldOverrides':{entry['id']:'用户设定的港区背景'}})
    response.raise_for_status()
    assert '用户设定的港区背景' in str(lore('港区','信浓'))
    assert client.get('/api/terminal/settings').json()['settings']['maxTaskMembers']==18
    assert client.post('/api/terminal/settings',json={'maxTaskMembers':25}).status_code==422
    assert client.post('/api/terminal/settings',json={'worldOverrides':{'unknown':'x'}}).status_code==400
    client.post('/api/terminal/settings',json={'worldOverrides':{}}).raise_for_status()
    assert '用户设定的港区背景' not in str(lore('港区','信浓'))

def test_moments_are_paused_and_not_returned(world):
    _,client,_=world
    assert client.get('/api/bootstrap').json()['snapshot']['posts']==[]
    for path in ('like','comment','publish'):
        response=client.post('/api/posts/'+path,json={})
        assert response.status_code==503 and response.json()['detail']=='动态功能完善中。'


def test_new_actor_relationships_and_task_permission(world):
    c,client,_=world
    aid=client.post('/api/social/characters/七省/enable',json={}).json()['actorId']
    relations=client.get(f'/api/actors/{aid}/relationships').json()['relationships']
    assert any(r['background'] for r in relations if r['name']=='埃佛森')
    client.post(f'/api/actors/{aid}/task-permission',json={'enabled':True}).raise_for_status()
    with session_scope() as s:
        assert s.get(Actor,aid).extra_json['socialOnly'] is False
    client.post(f'/api/actors/{aid}/task-permission',json={'enabled':False}).raise_for_status()
    client.post(f'/api/actors/{aid}/profile',json={'name':'七省的新称呼','faction':'自定义阵营'}).raise_for_status()
    relations=client.get(f'/api/actors/{aid}/relationships').json()['relationships']
    assert any(r['background'] for r in relations if r['name']=='埃佛森')


def test_custom_actor_and_directional_user_relationship(world):
    c,client,ids=world
    result=client.post('/api/terminal/characters',json={'name':'测试人物'}).json()
    aid=result['actorId']
    assert result['socialOnly'] and '未匹配' in result['backgroundStatus']
    client.post(f'/api/actors/{aid}/relationships/{ids[0]}',json={'description':'认识多年的棋友'}).raise_for_status()
    view=c.cognition.social_participation(aid,'port-hub',ids[0],set())
    assert view['userDefinedRelationship']['description']=='认识多年的棋友'
    assert 'userDefinedRelationship' not in c.cognition.social_participation(ids[0],'port-hub',aid,set())


def test_roster_no_longer_generates_biography_posts(world):
    c,_,ids=world
    with session_scope() as s:
        a=s.get(Actor,ids[0])
        before=len(s.scalars(select(SocialPost)).all())
        c.social_engine.service._ensure_greeting_post(s,a)
        assert len(s.scalars(select(SocialPost)).all())==before


def test_upload_roundtrip_and_boundaries(world,tmp_path):
    c,client,_=world
    client.post('/api/workspace/save',json={'workspace':{'settings':{'authorizedWorkspaceRoot':str(tmp_path)}}}).raise_for_status()
    response=client.post('/api/attachments?conversationId=port-hub&name=example.txt',content=b'hello attachment')
    response.raise_for_status()
    value=response.json()
    files=resolve([value['id']],'port-hub',c.settings_loader())
    assert Path(files[0]['path']).read_text()=='hello attachment'
    with pytest.raises(ValueError):resolve([value['id']],'other-room',c.settings_loader())
    with pytest.raises(ValueError):resolve(['../secret'],'port-hub',c.settings_loader())
    assert client.post('/api/attachments?conversationId=port-hub&name=bad.exe',content=b'x').status_code==400
    assert client.post('/api/attachments?conversationId=port-hub&name=bad.png',content=b'not an image').status_code==400
    assert client.post('/api/attachments?conversationId=missing&name=x.txt',content=b'x').status_code==404
    from io import BytesIO
    from PIL import Image
    from backend.attachments import image_parts
    image=BytesIO();Image.new('RGB',(8,8),'blue').save(image,format='PNG')
    uploaded=client.post('/api/attachments?conversationId=port-hub&name=picture.png',content=image.getvalue())
    uploaded.raise_for_status()
    images=resolve([uploaded.json()['id']],'port-hub',c.settings_loader())
    assert image_parts(images)[0]['image_url']['url'].startswith('data:image/png;base64,')
    assert client.get('/api/attachments/'+value['id']+'?conversationId=port-hub').content==b'hello attachment'


def test_stickers_remain_optional_and_bounded():
    assert validate({'sourceIds':['a'],'segments':['[表情:赞同]']},'a')
    with pytest.raises(ValueError):validate({'sourceIds':['a'],'segments':['[表情:赞同] [表情:开心]']},'a')
    with pytest.raises(ValueError):validate({'sourceIds':['a'],'segments':['[表情:unknown]']},'a')


def test_plan_member_limit_enforced(world):
    c,_,ids=world
    actors=[{'id':a,'name':a} for a in ids]
    run,_=c.store.create({'actorId':ids[0],'actors':actors,'mode':'task','collaborative':True,
        'conversationId':'port-hub','prompt':'test','maxTaskMembers':1},'limit-test')
    c.store.update(run['id'],maxTaskMembers=1)
    tasks=[{'id':str(i),'actorId':a,'brief':'实际任务','acceptance':['核验'],'dependsOn':[]} for i,a in enumerate(ids[:2])]
    with pytest.raises(ValueError,match='成员上限'):c.plan(run['id'],{'tasks':tasks})


def test_move_into_existing_directory_only_asks_for_actual_collision(tmp_path):
    from backend.capabilities import LocalCapabilities
    root=tmp_path/'work';root.mkdir()
    dest=root/'分类';dest.mkdir()
    (root/'book.txt').write_text('原文件',encoding='utf-8')
    tools=LocalCapabilities(tmp_path/'runtime')
    run={'workspace':str(root)}
    args={'src':'book.txt','dest':'分类'}
    assert tools.approval_reason(run,'move',args) is None
    result=tools.file_tool(run,'move',args)
    assert Path(result['path'])==dest/'book.txt'
    assert (dest/'book.txt').read_text(encoding='utf-8')=='原文件'
    tools.file_tool(run,'undo',{'snapshotId':result['snapshotId']})
    assert (root/'book.txt').exists() and not (dest/'book.txt').exists()
    (dest/'book.txt').write_text('另一文件',encoding='utf-8')
    assert tools.approval_reason(run,'move',args)
