"""Local user profile and explicit record/memory management."""
import base64
import binascii
from fastapi import HTTPException
from pydantic import BaseModel, Field
from sqlalchemy import delete, update, select
from backend.database import session_scope
from backend.models import Actor, Message, Conversation, SkillRun, MemoryChunk


class Profile(BaseModel):
    name: str = Field(min_length=1, max_length=40)
    avatar: str = Field(default='', max_length=750000)


def require_idle(runs):
    if runs.tasks or any(r['status'] not in {'completed', 'failed', 'cancelled'} for r in runs.store.list(all_rows=True)):
        raise HTTPException(409, '请先停止正在执行、暂停或等待审批的任务，再清理数据。')


def install_personal_settings(app, service, lock, publish):
    @app.post('/api/profile')
    async def profile(payload: Profile):
        name = payload.name.strip()
        if not name:
            raise HTTPException(422, '称呼不能为空。')
        avatar = payload.avatar
        if avatar:
            try:
                header, encoded = avatar.split(',', 1)
                raw = base64.b64decode(encoded, validate=True)
                valid = (header == 'data:image/png;base64' and raw.startswith(b'\x89PNG\r\n\x1a\n')) or (header == 'data:image/jpeg;base64' and raw.startswith(b'\xff\xd8\xff')) or (header == 'data:image/webp;base64' and raw.startswith(b'RIFF') and raw[8:12] == b'WEBP')
                if not valid:
                    raise ValueError('unsupported image')
            except (ValueError, binascii.Error):
                raise HTTPException(422, '请选择 PNG、JPEG 或 WebP 头像。')
        with session_scope() as session:
            user = service.get_or_create_user(session)
            user.name, user.initials, user.avatar_url = name, name[:2], avatar or None
        await publish('workspace.changed', {})
        return {'status': 'ok'}

    @app.post('/api/system/clear/{scope}')
    async def clear(scope: str):
        if scope not in {'records', 'memory'}:
            raise HTTPException(422, '清理范围无效。')
        runs = app.state.runs
        require_idle(runs)
        async with lock:
            require_idle(runs)
            if scope == 'memory':
                from backend.mind.cognition_models import MindCursor
                with session_scope() as session:
                    ids = list(session.scalars(select(Actor.id).where(Actor.kind == 'agent')))
                    session.execute(delete(MemoryChunk))
                    cursor = session.get(MindCursor, 'runtime')
                    if cursor:
                        cursor.seq = runs.store.state()['cursor']
                for aid in ids:
                    runs.cognition.configure(aid, reset=True)
                with runs.store.connect() as db:
                    db.execute('DELETE FROM memory_fts')
                service.bundle.memory.clear()
            else:
                with session_scope() as session:
                    session.execute(update(SkillRun).values(message_id=None))
                    session.execute(delete(Message))
                    session.execute(update(Conversation).values(preview='', unread_count=0))
                for run in runs.store.list(all_rows=True):
                    runs.store.remove(run['id'])
        await publish('workspace.changed', {})
        return {'status': 'ok', 'scope': scope}
