"""User-owned terminal configuration, separate from canonical materials."""
from sqlalchemy import select
from fastapi import HTTPException
from pydantic import BaseModel, Field
from .database import session_scope
from .models import Actor


def read():
    with session_scope() as session:
        user=session.get(Actor,'commander')
        return dict((user.extra_json or {}).get('terminalSettings',{})) if user else {}


class Configuration(BaseModel):
    maxTaskMembers: int = Field(default=12,ge=1,le=24)
    secretaryAgentId: str | None = None
    worldOverrides: dict[str,str] = Field(default_factory=dict)


def install(app, service, coordinator):
    from .relationship_research import install as install_research
    install_research(app,coordinator,service)
    from .attachments import install as install_attachments
    install_attachments(app,coordinator)
    @app.get('/api/terminal/settings')
    def inspect():
        from .terminal_characters import world_entries
        from .character_identity import roster_actors
        with session_scope() as session:
            actors=roster_actors(session)
            members=[service.serialize_actor(a) for a in actors]
            secretary=service.get_workspace(session).secretary_agent_id
        world=world_entries()
        config=Configuration(**{**read(),'secretaryAgentId':secretary}).model_dump()
        config['worldOverrides']={k:v for k,v in config['worldOverrides'].items() if k in {e['id'] for e in world}}
        return {'settings':config,'members':members,'world':[e for e in world if e['id']=='port-terminal' or e['id'].startswith('world-')]}

    @app.post('/api/terminal/settings')
    def save(payload:Configuration):
        from .terminal_characters import world_entries
        allowed={e['id'] for e in world_entries()}
        if not set(payload.worldOverrides)<=allowed or any(len(v)>2000 for v in payload.worldOverrides.values()):
            raise HTTPException(400,'世界条目不存在或超过2000字。')
        with session_scope() as session:
            if payload.secretaryAgentId:
                actor=session.get(Actor,payload.secretaryAgentId)
                if not actor or actor.kind!='agent' or not actor.is_active or (actor.extra_json or {}).get('socialOnly'):
                    raise HTTPException(400,'秘书必须是已连接且允许执行任务的角色。')
                service.get_workspace(session).secretary_agent_id=actor.id
            user=service.get_or_create_user(session)
            previous=(user.extra_json or {}).get('terminalSettings',{}).get('worldOverrides',{})
            saved=payload.model_dump()
            saved['worldOverrides']={**{k:v for k,v in previous.items() if k not in allowed},**payload.worldOverrides}
            user.extra_json={**(user.extra_json or {}),'terminalSettings':saved}
        coordinator.store.event(None,'workspace.changed',{})
        return {'saved':True}

    class TaskPermission(BaseModel):
        enabled: bool

    class Profile(BaseModel):
        name: str = Field(min_length=1,max_length=80)
        faction: str = Field(min_length=1,max_length=80)
        avatarUrl: str = Field(default='',max_length=700000)

    @app.post('/api/actors/{actor_id}/profile')
    def profile(actor_id:str,payload:Profile):
        if not payload.name.strip() or not payload.faction.strip(): raise HTTPException(400,'称呼和阵营不能为空。')
        if payload.avatarUrl and not payload.avatarUrl.startswith(('https://','http://','data:image/png;base64,','data:image/jpeg;base64,','data:image/webp;base64,','/api/')):
            raise HTTPException(400,'头像需要图片地址或 PNG/JPEG/WebP 数据。')
        with session_scope() as session:
            actor=session.get(Actor,actor_id)
            if not actor or actor.kind!='agent':raise HTTPException(404,'人物不存在。')
            actor.name=payload.name.strip();actor.faction=payload.faction.strip();actor.avatar_url=payload.avatarUrl or None
            # Canonical identity is retained for source matching after renaming.
        coordinator.store.event(None,'workspace.changed',{})
        return {'saved':True}

    class NewCharacter(BaseModel):
        name: str = Field(min_length=1,max_length=80)

    @app.post('/api/terminal/characters')
    def create_character(payload:NewCharacter):
        from .terminal_characters import card,TERMINAL
        from .social_roster import enable_character
        from uuid import uuid4
        name=payload.name.strip()
        if not name: raise HTTPException(400,'请输入人物名称。')
        if card(name):
            result=enable_character(service,name)
        else:
            with session_scope() as session:
                if session.scalar(select(Actor).where(Actor.name==name,Actor.kind=='agent')):
                    raise HTTPException(409,'该人物已存在，请编辑已有档案。')
                aid='custom-'+uuid4().hex
                actor=Actor(id=aid,kind='agent',name=name,handle='@'+aid,faction='用户设定',initials=name[:2],
                    system_prompt=TERMINAL+'当前姓名：'+name+'。背景尚未核实，不编造原作关系。',
                    extra_json={'socialOnly':True},tools=[],capabilities=[],is_active=True)
                session.add(actor);session.flush()
                ws=service.get_workspace(session)
                ws.connected_agent_ids=[*(ws.connected_agent_ids or []),aid]
                ws.character_roster_text='\n'.join([*ws.character_roster_text.splitlines(),name])
                ws.max_connected_agents=max(ws.max_connected_agents,len(ws.connected_agent_ids))
                service._ensure_dm_conversation(session,service.get_or_create_user(session),actor)
                service._sync_relationships(session,session.scalars(select(Actor).where(Actor.kind=='agent')).all())
                result={'actorId':aid,'name':name,'socialOnly':True,'backgroundStatus':'未匹配已核实原作材料，可编辑自定义设定'}
        coordinator.store.event(None,'workspace.changed',{})
        return result

    @app.post('/api/actors/{actor_id}/task-permission')
    def permission(actor_id:str,payload:TaskPermission):
        with session_scope() as session:
            actor=session.get(Actor,actor_id)
            if not actor or actor.kind!='agent': raise HTTPException(404,'人物不存在')
            actor.extra_json={**(actor.extra_json or {}),'socialOnly':not payload.enabled}
        coordinator.store.event(None,'workspace.changed',{})
        return {'enabled':payload.enabled}
