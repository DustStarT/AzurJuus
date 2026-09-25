from fastapi import HTTPException
from pydantic import BaseModel
from backend.characters import terminal_characters as cards

class CardAction(BaseModel):
    action: str

def install_terminal_api(app, coordinator):
    @app.get('/api/runs/{run_id}/transcripts')
    async def transcripts(run_id: str):
        import json
        with coordinator.store.connect() as db:
            rows = db.execute("SELECT seq,payload FROM events WHERE run_id=? AND type='execution.message.complete' ORDER BY seq DESC LIMIT 50",(run_id,)).fetchall()
        return {'records':[{'seq':r['seq'],**json.loads(r['payload'])} for r in reversed(rows)]}

    @app.get('/api/actors/{actor_id}/character-card')
    async def inspect(actor_id: str):
        try:
            return cards.inspect(actor_id)
        except ValueError as exc:
            raise HTTPException(404, str(exc))

    @app.post('/api/actors/{actor_id}/character-card')
    async def activate(actor_id: str, payload: CardAction):
        if payload.action not in {'apply', 'restore'}:
            raise HTTPException(422, '请选择应用更新或恢复基础卡。')
        try:
            result = cards.activate(actor_id, payload.action)
            coordinator.store.event(None, 'workspace.changed', {})
            return result
        except ValueError as exc:
            raise HTTPException(404, str(exc))

    @app.get('/api/worldbook')
    async def worldbook():
        return {'entries':cards.world_entries(), 'version':cards.worldbook()['version']}

    @app.get('/api/actors/{actor_id}/prompt-preview')
    async def preview(actor_id: str):
        try:
            value = cards.inspect(actor_id)
            return {'version':value['version'], 'terminal':cards.TERMINAL, 'character':value['text'],
                'world':cards.lore('', (value.get('latest') or {}).get('name', '')),
                'note':'预览不包含密钥或其他人物私人状态；实际请求还包含本次可见经历和对话。'}
        except ValueError as exc:
            raise HTTPException(404, str(exc))
