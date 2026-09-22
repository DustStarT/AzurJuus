from sqlalchemy import select
from test_cognition import world
from backend.database import session_scope
from backend.models import Actor, ConversationMember
from backend.terminal_characters import card, context, worldbook


def test_all_twelve_have_terminal_cards():
    for identity in worldbook()['identities']:
        value=card(identity['name'])
        assert value and len(value['examples'])==6
        assert value['faction']==identity['faction']
        assert '原创' in value['provenance']


def test_enable_is_additive_social_only_and_idempotent(world):
    c,client,original=world
    first=client.post('/api/social/characters/七省/enable',json={})
    first.raise_for_status()
    aid=first.json()['actorId']
    assert first.json()['socialOnly'] is True
    assert client.post('/api/social/characters/七省/enable',json={}).json()['actorId']==aid
    with session_scope() as s:
        actor=s.get(Actor,aid)
        assert not actor.tools and not actor.capabilities
        assert all(s.get(Actor,old).is_active for old in original)
        assert s.scalar(select(ConversationMember).where(ConversationMember.conversation_id=='port-hub',ConversationMember.actor_id==aid)) is None
        assert s.scalar(select(ConversationMember).where(ConversationMember.conversation_id=='dm-'+aid,ConversationMember.actor_id=='commander'))
        assert len(s.scalars(select(Actor).where(Actor.source_character=='七省')).all())==1
    assert card('七省')['style'] in context(aid)[0]
    assert '原创终端表达示例' not in context(aid)[0]
    response=client.post('/api/messages/send',json={'conversationId':'dm-'+aid,'mode':'task','content':'列出文件'})
    assert response.status_code==400 and '尚未开放' in response.json()['detail']


def test_reenable_keeps_user_edits(world):
    _,client,ids=world
    aid=ids[0]
    with session_scope() as s:
        actor=s.get(Actor,aid)
        name=actor.source_character
        actor.name='自定义称呼'
        actor.avatar_url='custom-avatar.png'
        actor.extra_json={**actor.extra_json,'terminalOverride':'保留这份人设','customState':{'value':7}}
    client.post(f'/api/social/characters/{name}/enable',json={}).raise_for_status()
    with session_scope() as s:
        actor=s.get(Actor,aid)
        assert actor.name=='自定义称呼' and actor.avatar_url=='custom-avatar.png'
        assert actor.extra_json['customState']=={'value':7}
        assert actor.extra_json['terminalOverride']=='保留这份人设'
    assert context(aid)[0].startswith('保留这份人设\n')
    assert '可选表情名' in context(aid)[0]


def test_reconnecting_roster_preserves_prompt_and_extra(world):
    c,_,ids=world
    with session_scope() as s:
        actor=s.get(Actor,ids[0]); name=actor.source_character
        actor.system_prompt='自定义系统提示'
        actor.avatar_url='my-avatar.png'
        actor.extra_json={'terminalOverride':'自定义覆盖','learned':True}
    with session_scope() as s:
        c.social_engine.service.apply_personas(s,[{'sourceName':name,'displayName':'不应覆盖','promptSeed':'不应覆盖'}],[name],1)
    with session_scope() as s:
        actor=s.get(Actor,ids[0])
        assert actor.system_prompt=='自定义系统提示'
        assert actor.avatar_url=='my-avatar.png'
        assert actor.extra_json=={'terminalOverride':'自定义覆盖','learned':True}


def test_unknown_character_cannot_trigger_remote_import(world):
    assert world[1].post('/api/social/characters/不存在的人物/enable',json={}).status_code==400


def test_legacy_roster_import_cannot_grant_new_social_actor_tools(world):
    c,_,_=world
    with session_scope() as s:
        c.social_engine.service.apply_personas(s,[{'sourceName':'七省','displayName':'七省',
            'tools':['shell'],'capabilities':['execute'],'promptSeed':'原始皮肤台词'}],['七省'],1)
    with session_scope() as s:
        actor=s.scalar(select(Actor).where(Actor.source_character=='七省'))
        assert actor.extra_json['socialOnly'] and not actor.tools and not actor.capabilities
        assert '原始皮肤台词' not in actor.system_prompt
