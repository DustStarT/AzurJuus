"""Local-user context management. Tool identities are bound by RunCoordinator."""
import asyncio
import time
from typing import Literal
from fastapi import HTTPException, Request
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from backend.database import session_scope
from backend.models import Conversation, ConversationMember, Message
from backend.context.models import ContextSource, ContextTrace, ContextGrant, ContextTombstone
from backend.context.contracts import ContextRequest
from backend.context.service import stable


class Configuration(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    provider: Literal['local', 'cloud', 'lexical'] = 'local'
    localModelPath: str = Field(default='', max_length=1000)
    baseUrl: str = Field(default='', max_length=1000)
    model: str = Field(default='', max_length=200)
    modelVersion: str = Field(default='', max_length=200)
    dimensions: int = Field(default=384, ge=1, le=4096)
    apiKey: str = Field(default='', max_length=4000)
    clearApiKey: bool = False
    contextLimit: int = Field(default=64000, ge=4096, le=2000000)
    chatTokenizerPath: str = Field(default='', max_length=1000)
    chatTokenizerModel: str = Field(default='', max_length=200)


def install_context_api(app, coordinator, service):
    context = coordinator.context

    def require_room(request, cid):
        with session_scope() as s:
            room = s.get(Conversation, cid)
            if not room or (room.extra_json or {}).get('archived'):
                raise HTTPException(404, '频道不存在或不可见')
            if not s.scalar(select(ConversationMember).where(ConversationMember.conversation_id == cid,
                    ConversationMember.actor_id == request.state.actor_id, ConversationMember.is_active.is_(True))):
                raise HTTPException(404, '频道不存在或不可见')

    @app.get('/api/context/settings')
    def settings():
        return context.public_settings()

    @app.post('/api/context/settings')
    def configure(payload: dict):
        from backend.api.input_contracts import checked
        values = checked(payload, Configuration)
        try:
            return context.configure(values)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc

    @app.get('/api/context/status')
    def status():
        return context.status()

    @app.post('/api/context/reindex')
    async def reindex():
        return await asyncio.to_thread(context.reindex)

    @app.post('/api/context/model/prepare')
    async def prepare():
        try:
            await asyncio.to_thread(context.embedding.prepare)
            await asyncio.to_thread(context.reindex)
            return context.status()
        except Exception as exc:
            raise HTTPException(409, '本机模型准备失败：' + type(exc).__name__ + '；仍可使用词法检索。') from exc

    @app.get('/api/context/traces/{trace_id}')
    def trace(trace_id: str, request: Request):
        with session_scope() as s:
            row = s.get(ContextTrace, trace_id)
            if not row:
                raise HTTPException(404, '记录不存在')
            cid, aid, data = row.conversation_id, row.actor_id, dict(row.data)
        if cid:
            require_room(request, cid)
        # Recheck live visibility; even diagnostics must not reveal revoked source IDs.
        allowed = context.visible(ContextRequest(aid, cid))
        data['selected'] = [v for v in data.get('selected', []) if v['sourceId'] in allowed]
        data['omitted'] = [v for v in data.get('omitted', []) if v['sourceId'] in allowed]
        return {'id': trace_id, **data}

    @app.get('/api/context/messages/{message_id}/sources')
    def message_sources(message_id: str, request: Request):
        with session_scope() as s:
            msg = s.get(Message, message_id)
            if not msg:
                raise HTTPException(404, '消息不存在')
            cid, actor_id = msg.conversation_id, msg.speaker_id
            traces = s.scalars(select(ContextTrace).where(ContextTrace.actor_id == actor_id,
                ContextTrace.conversation_id == cid).order_by(ContextTrace.at.desc())).all()
            source = (msg.metadata_json or {}).get('sourceId')
            trace_ids = [r.id for r in traces if source and r.data.get('sourceId') == source]
            if tid := (msg.metadata_json or {}).get('contextTraceId'):
                trace_ids = [tid] if any(r.id == tid for r in traces) else trace_ids
            if tid := (msg.metadata_json or {}).get('cognitionTraceId'):
                if any(r.id == tid for r in traces):
                    trace_ids.append(tid)
        require_room(request, cid)
        allowed = context.visible(ContextRequest(actor_id, cid))
        with session_scope() as s:
            ids = {v['sourceId'] for tid in trace_ids for v in s.get(ContextTrace, tid).data.get('selected', [])}
        return {'traces': trace_ids, 'sources': [v for k, v in allowed.items() if k in ids]}

    @app.get('/api/context/runs/{run_id}/sources')
    def run_sources(run_id: str, request: Request):
        try:
            run = coordinator.store.get(run_id)
        except KeyError:
            raise HTTPException(404, '任务不存在')
        require_room(request, run['conversationId'])
        context.sync()
        viewers = {run['actorId'], *(a['actorId'] for a in run.get('assignments', []) if a.get('actorId'))}
        permitted = {sid for actor_id in viewers for sid in context.visible(ContextRequest(actor_id,
            run['conversationId'], public=bool(run.get('collaborative'))))}
        with session_scope() as s:
            rows = [context.source_payload(r) for r in s.scalars(select(ContextSource).where(
                ContextSource.kind == 'document', ContextSource.active.is_(True))).all()
                if r.data.get('runId') == run_id and r.id in permitted and not s.get(ContextTombstone, r.id)]
        return {'sources': rows, 'scope': '实际返回片段；索引不代表通读全文'}

    @app.post('/api/context/sources/{source_id}/forget')
    def forget(source_id: str, request: Request):
        with session_scope() as s:
            row = s.get(ContextSource, source_id)
            grants = s.scalars(select(ContextGrant).where(ContextGrant.source_id == source_id)).all()
            cids = [g.conversation_id for g in grants if g.conversation_id]
        if not row or not cids:
            raise HTTPException(404, '来源不存在')
        require_room(request, cids[0])
        context.revoke(source_id)
        return {'status': 'forgotten'}

    class Sharing(BaseModel):
        model_config = ConfigDict(extra='forbid')
        conversationId: str = Field(max_length=100)
        revoke: bool = False

    @app.post('/api/context/sources/{source_id}/share')
    def share(source_id: str, payload: Sharing, request: Request):
        require_room(request, payload.conversationId)
        with session_scope() as s:
            origin = s.scalar(select(ContextGrant).where(ContextGrant.source_id == source_id,
                ContextGrant.active.is_(True)))
            if not origin:
                raise HTTPException(404, '来源不存在')
            origin_cid = origin.conversation_id
            origin_actor = origin.actor_id
        require_room(request, origin_cid)
        if source_id not in context.visible(ContextRequest(origin_actor, origin_cid)):
            raise HTTPException(409, '来源已被遗忘或撤销，需要重新读取后才能分享')
        with session_scope() as s:
            for member in s.scalars(select(ConversationMember).where(
                    ConversationMember.conversation_id == payload.conversationId, ConversationMember.is_active.is_(True))).all():
                if member.actor_id == request.state.actor_id:
                    continue
                gid = stable(source_id, member.actor_id, payload.conversationId)
                row = s.get(ContextGrant, gid)
                if row and payload.revoke and row.data.get('shared'):
                    row.active = False
                elif row and not payload.revoke and row.data.get('shared') and not s.get(ContextTombstone, gid):
                    row.active = True
                    row.data = {**row.data, 'sharedAt': time.time()}
                elif not row and not payload.revoke and not s.get(ContextTombstone, gid):
                    s.add(ContextGrant(id=gid, source_id=source_id, actor_id=member.actor_id,
                        conversation_id=payload.conversationId, active=True,
                        data={'reader': False, 'shared': True, 'sharedBy': request.state.actor_id,
                              'sharedAt': time.time()}))
        return {'status': 'revoked' if payload.revoke else 'shared'}
