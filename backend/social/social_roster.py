"""Explicit, additive activation of curated social characters."""
from pathlib import Path
from sqlalchemy import select, update, or_, and_
from backend.database import session_scope
from backend.models import Actor, WorkspaceSetting
from backend.characters.terminal_characters import card, render, TERMINAL


def enable_character(service, name):
    base = card(name)
    if not base:
        raise ValueError('尚无经过整理的终端角色卡')
    from tools.blhx_character_import import load_profile, build_runtime_persona
    path = Path(__file__).resolve().parents[2]/'resources/characters'/(name+'.json')
    spec={}
    if path.is_file():
        profile=load_profile(path)
        from backend.characters.character_identity import source_names
        if profile.name not in source_names(name):raise ValueError('本地资料身份不匹配，请重新调查该人物资料。')
        spec=build_runtime_persona(profile)
    # The curated card itself is sufficient. Missing optional downloaded images
    # must not block activation or substitute a different character's profile.
    with session_scope() as session:
        session.execute(update(WorkspaceSetting).where(WorkspaceSetting.id==1).values(updated_at=WorkspaceSetting.updated_at))
        actor = session.scalar(select(Actor).where(or_(Actor.source_character==name,
            and_(Actor.source_character.is_(None),Actor.name==name)),Actor.kind=='agent'))
        if actor is None:
            if session.get(Actor,'juus-'+name): raise ValueError('人物编号已被其他账号占用')
            actor = Actor(id='juus-'+name, kind='agent', name=name, source_character=name,
                handle=spec.get('handle') or '@'+name+'.juus', faction=base['faction'], initials=name[:2],
                persona=base['style'], tone=base['style'], summary=base['style'],
                system_prompt=TERMINAL+render(base), tools=[], capabilities=[],
                avatar_url=spec.get('avatarUrl'), illustration_url=spec.get('illustrationUrl'),
                palette=spec.get('palette',[]), accent=spec.get('accent'), character_url=base['source'],
                extra_json={'socialOnly':True, 'aliases':spec.get('aliases',[]),
                    'terminalCard':{'version':base['version'],'text':render(base)}})
            session.add(actor); session.flush()
        actor.is_active = True
        workspace = service.get_workspace(session)
        ids = list(workspace.connected_agent_ids or [])
        if actor.id not in ids: ids.append(actor.id)
        workspace.connected_agent_ids = ids
        workspace.max_connected_agents = max(workspace.max_connected_agents,len(ids))
        names = workspace.character_roster_text.splitlines()
        if name not in names: workspace.character_roster_text = '\n'.join([*names,name])
        # Existing conversations/personas stay unchanged. No automatic hub join,
        # task assignment, public post, greeting, or personal skill is created.
        service._ensure_dm_conversation(session,service.get_or_create_user(session),actor)
        service._sync_relationships(session,session.scalars(select(Actor).where(Actor.kind=='agent')).all())
        return {'actorId':actor.id,'name':actor.name,'socialOnly':bool((actor.extra_json or {}).get('socialOnly'))}


def install_roster_api(app, engine):
    from fastapi import HTTPException
    from backend.characters.terminal_settings import install
    install(app,engine.service,engine.c)

    @app.get('/api/social/characters/{name}')
    def preview(name:str):
        value=card(name)
        if not value: raise HTTPException(404,'暂无终端角色卡')
        return value

    @app.post('/api/social/characters/{name}/enable')
    def enable(name:str):
        try: result=enable_character(engine.service,name)
        except ValueError as exc: raise HTTPException(400,str(exc))
        engine.c.store.event(None,'workspace.changed',{})
        return result
