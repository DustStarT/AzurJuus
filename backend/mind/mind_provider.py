"""Meter isolated skill trials through the same durable background budget."""
import json
import httpx
from fastapi import HTTPException, Request, Response
from backend.database import session_scope
from backend.mind.mind_models import MindCall


def install_background_provider(app, coordinator):
    @app.post('/api/internal/background-model/{token}/chat/completions')
    async def complete(token:str, request:Request):
        runner=next((r for r in coordinator.trial_runners if token in r.tokens),None)
        if runner is None:raise HTTPException(403,'后台试用凭据无效。')
        runtime=coordinator.cognition.runtime
        if not runtime or not runtime.enabled:raise HTTPException(409,'新后台调度未启用。')
        _,actor_id,_=runner.tokens[token]
        payload=await request.json()
        settings=coordinator.settings_loader()
        async def forward(messages,cfg):
            async with httpx.AsyncClient(timeout=38) as client:
                result=await client.post(settings['llmBaseUrl'].rstrip('/')+'/chat/completions',
                    headers={'Authorization':'Bearer '+settings['llmApiKey']},json=payload)
                # Both ordinary and streamed providers can report usage; never infer it.
                usage=None
                if result.status_code==200:
                    try:
                        if 'text/event-stream' in result.headers.get('content-type',''):
                            for line in result.text.splitlines():
                                if line.startswith('data:') and line[5:].strip()!='[DONE]':
                                    usage=json.loads(line[5:]).get('usage') or usage
                        else:usage=result.json().get('usage')
                    except (ValueError,TypeError):pass
                    if usage:
                        with session_scope() as s:
                            row=s.get(MindCall,cfg['_mindCallId']);row.data={**row.data,'usage':usage}
                if result.status_code!=200:raise RuntimeError('后台模型 HTTP '+str(result.status_code))
                return {'body':result.content,'contentType':result.headers.get('content-type','application/json')}
        try:
            result=await runtime.call(actor_id,'skill-trial',{},background=True,revision=runtime.revision,generator=forward,settings=settings)
        except Exception as exc:
            raise HTTPException(503,str(exc)[:160]) from exc
        return Response(content=result['body'],media_type=result['contentType'])
