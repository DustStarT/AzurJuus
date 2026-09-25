"""Workspace, model, character, and reset routes."""
from __future__ import annotations
import time
from typing import Any
from fastapi import HTTPException, Request
from backend.database import session_scope
from backend.models import ModelConnection
from backend.platform.constants import DEFAULT_SETTINGS
from backend.social.routes import install_moment_routes


def install_app_routes(app, service, settings, publish, maintenance_lock, runtime_context):
    @app.get("/api/health")
    async def api_health():
        return {
            "status": "ok",
            "mode": "python-local-fastapi",
            "message": "AzurJuus local runtime is connected.",
            "database": "sqlite" if settings.database_url.startswith("sqlite") else "postgresql",
            "redis": settings.redis_url,
            "chroma": settings.chroma_url,
        }

    @app.get("/api/workspace/load")
    async def api_workspace_load(request: Request):
        with session_scope() as session:
            workspace = service.build_workspace_payload(session, actor_id=request.state.actor_id)
        return {"status": "ok", "workspace": workspace}

    @app.post("/api/workspace/save")
    async def api_workspace_save(request: Request, payload: dict[str, Any]):
        workspace_payload = payload.get("workspace") if isinstance(payload, dict) else None
        if not isinstance(workspace_payload, dict):
            raise HTTPException(status_code=400, detail="workspace payload is required")
        with session_scope() as session:
            workspace = service.save_workspace_payload(session, workspace_payload, actor_id=request.state.actor_id)
        return {"status": "ok", "workspace": workspace}

    @app.get('/api/model/connections')
    def api_model_connections():
        from backend.tasks.model_connections import remember, recent
        with session_scope() as session:
            if not recent(session):
                current=service.get_workspace(session)
                if (current.llm_api_key or current.llm_base_url!=DEFAULT_SETTINGS['llmBaseUrl']
                        or current.llm_model!=DEFAULT_SETTINGS['llmModel']):
                    remember(session,current)
            return {'connections':recent(session)}

    @app.post('/api/model/connections/activate')
    async def api_activate_model(payload:dict):
        from backend.tasks.model_connections import remember
        with session_scope() as session:
            row=session.get(ModelConnection,str(payload.get('id') or ''))
            if not row:raise HTTPException(404,'找不到保存的模型配置。')
            settings_row=service.get_workspace(session)
            remember(session,settings_row)
            settings_row.llm_base_url=row.base_url
            settings_row.llm_model=row.model
            settings_row.llm_api_key=row.api_key
            row.last_used_at=time.time()
            workspace=service.build_workspace_payload(session)
        await publish('workspace.changed',{'modelConnectionChanged':True})
        return {'workspace':workspace}

    @app.post('/api/model/connections/remove')
    def api_remove_model(payload:dict):
        from backend.tasks.model_connections import connection_id, normalize, recent
        with session_scope() as session:
            row=session.get(ModelConnection,str(payload.get('id') or ''))
            if not row:raise HTTPException(404,'找不到保存的模型配置。')
            current=service.get_workspace(session)
            try:active_id=connection_id(*normalize(current.llm_base_url,current.llm_model))
            except ValueError:active_id=''
            if row.id==active_id:raise HTTPException(409,'当前正在使用的配置不能从记录中删除。')
            session.delete(row)
            session.flush()
            return {'connections':recent(session)}

    @app.post('/api/model/check')
    async def api_check_model(payload:dict):
        from backend.credentials import reveal
        from backend.tasks.model_connections import connection_id, normalize, probe
        with session_scope() as session:
            current=service.get_workspace(session)
            try:base_url,model=normalize(payload.get('baseUrl') or current.llm_base_url,
                payload.get('model') or current.llm_model)
            except ValueError as exc:raise HTTPException(422,str(exc)) from exc
            api_key=str(payload.get('apiKey') or '') or reveal(current.llm_api_key)
        result=await probe(base_url,model,api_key)
        if result['available'] and payload.get('checkReasoning') is True:
            from backend.tasks.model_connections import probe_reasoning
            from backend.tasks.reasoning_policy import remember as remember_reasoning
            result['reasoning']=await probe_reasoning(base_url,model,api_key)
            remember_reasoning(base_url,model,result['reasoning'])
        with session_scope() as session:
            row=session.get(ModelConnection,connection_id(base_url,model))
            if row:
                row.last_checked_at=time.time()
                row.last_check='available' if result['available'] else 'unavailable'
        return result

    @app.get("/api/bootstrap")
    async def api_bootstrap(request: Request):
        with session_scope() as session:
            workspace = service.build_workspace_payload(session, actor_id=request.state.actor_id)
        return {"status": "ok", "snapshot": workspace["data"], "workspace": workspace}

    @app.post("/api/conversations/open")
    async def api_open_conversation(payload: dict[str, Any]):
        conversation_id = str(payload.get("conversationId") or "")
        with session_scope() as session:
            snapshot = service.open_conversation(session, conversation_id)
        await publish("conversation.message.updated", {"conversationId": conversation_id, "snapshot": snapshot})
        return {"status": "ok", "snapshot": snapshot}

    @app.post("/api/agents/favorite")
    async def api_favorite_agent(payload: dict[str, Any]):
        try:
            with session_scope() as session:
                snapshot = service.toggle_favorite_agent(session, str(payload.get("agentId") or ""))
        except ValueError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        await publish("conversation.message.updated", {"snapshot": snapshot})
        return {"status": "ok", "snapshot": snapshot}

    @app.post("/api/groups/roles")
    async def api_update_group_role(payload: dict[str, Any]):
        try:
            with session_scope() as session:
                snapshot = service.update_group_role(
                    session,
                    conversation_id=str(payload.get("conversationId") or ""),
                    agent_id=str(payload.get("agentId") or ""),
                    role=str(payload.get("role") or "member"),
                )
        except ValueError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        await publish("conversation.message.updated", {"snapshot": snapshot})
        return {"status": "ok", "snapshot": snapshot}

    install_moment_routes(app, service, publish)

    @app.post("/api/personas/compose")
    async def api_personas_compose(payload: dict[str, Any]):
        try:
            with session_scope() as session:
                result = service.compose_personas(
                    session,
                    names=[str(item) for item in payload.get("names") or []],
                    participant_count=int(payload.get("participantCount") or 0),
                )
        except ValueError as error:
            raise HTTPException(status_code=422, detail=str(error)) from error
        except FileNotFoundError as error:
            raise HTTPException(status_code=404, detail=str(error)) from error
        await publish("conversation.message.updated", {"snapshot": result["snapshot"]})
        return {
            "status": "ok",
            "snapshot": result["snapshot"],
            "personas": result["personas"],
            "count": len(result["personas"]),
        }

    @app.post("/api/system/reset")
    async def api_system_reset(request: Request, payload: dict | None = None):
        from backend.platform.personal_settings import require_idle
        require_idle(app.state.runs)
        async with maintenance_lock:
            require_idle(app.state.runs)
            with session_scope() as session:
                preserve=(payload or {}).get('preserveKeysAndProfile') is True
                retained=None
                if preserve:
                    settings_row=service.get_workspace(session)
                    user=service.get_or_create_user(session)
                    retained={'llm':settings_row.llm_api_key,'tool':settings_row.tool_api_key,
                        'model':settings_row.llm_model,'base':settings_row.llm_base_url,
                        'provider':settings_row.llm_provider,
                        'connections':[{'id':row.id,'base_url':row.base_url,'model':row.model,
                            'api_key':row.api_key,'last_used_at':row.last_used_at,
                            'last_checked_at':row.last_checked_at,'last_check':row.last_check}
                            for row in session.query(ModelConnection).all()],
                        'extras':{k:v for k,v in (settings_row.ui_session_json or {}).get('settingsExtras',{}).items() if k.lower().endswith('apikey')},
                        'profile':{k:getattr(user,k) for k in ('name','initials','avatar_url')}}
                workspace = service.reset_system(session, actor_id=request.state.actor_id)
                if retained:
                    settings_row=service.get_workspace(session)
                    settings_row.llm_api_key=retained['llm'];settings_row.tool_api_key=retained['tool']
                    settings_row.llm_model=retained['model'];settings_row.llm_base_url=retained['base']
                    settings_row.llm_provider=retained['provider']
                    for item in retained['connections']:session.add(ModelConnection(**item))
                    settings_row.ui_session_json={**(settings_row.ui_session_json or {}),'settingsExtras':retained['extras']}
                    user=service.get_or_create_user(session)
                    for key,value in retained['profile'].items():setattr(user,key,value)
                    session.flush()
                    workspace=service.build_workspace_payload(session,actor_id=request.state.actor_id)
                from backend.mind.cognition_models import MindCursor
                # Commit a source barrier with the reset: a crash before runtime
                # cleanup must not project old events into freshly seeded actors.
                session.add(MindCursor(id='runtime', seq=app.state.runs.store.state()['cursor']))
            with app.state.runs.store.connect() as db:
                for table in ('speeches', 'memory_fts', 'calls', 'runs', 'events'):
                    db.execute('DELETE FROM ' + table)
            app.state.runs.cognition.initialize()
        await publish("conversation.message.updated", {"snapshot": workspace["data"]})
        return {"status": "ok", "workspace": workspace, "snapshot": workspace["data"]}

    from backend.platform.personal_settings import install_personal_settings
    install_personal_settings(app, service, maintenance_lock, publish)

    @app.post("/api/window/preset")
    async def api_window_preset(payload: dict[str, Any]):
        preset = str(payload.get("preset") or "balanced")
        callback = (runtime_context or {}).get("set_window_preset")
        if callable(callback):
            callback(preset)
            return {"status": "ok", "preset": preset}
        return {"status": "unsupported", "preset": preset}

