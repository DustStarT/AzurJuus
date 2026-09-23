"""Saved model connections and a small, explicit compatibility probe."""
from __future__ import annotations

import hashlib
import time
from urllib.parse import urlsplit

import httpx
from sqlalchemy import select

from .credentials import protect
from .models import ModelConnection, WorkspaceSetting


def normalize(base_url: str, model: str) -> tuple[str, str]:
    base_url=str(base_url or '').strip().rstrip('/')
    model=str(model or '').strip()
    try:parsed=urlsplit(base_url)
    except ValueError as exc:raise ValueError('接口地址格式无效。') from exc
    if (parsed.scheme not in {'http','https'} or not parsed.hostname or parsed.username or parsed.password
            or parsed.query or parsed.fragment or not model or len(model)>255):
        raise ValueError('请输入有效的 HTTP(S) 接口地址和模型 ID。')
    try:
        _=parsed.port
    except ValueError as exc:
        raise ValueError('接口地址的端口无效。') from exc
    return base_url,model


def connection_id(base_url: str, model: str) -> str:
    return hashlib.sha256((base_url+'\0'+model).encode()).hexdigest()[:40]


def remember(session, workspace: WorkspaceSetting):
    try:base_url,model=normalize(workspace.llm_base_url,workspace.llm_model)
    except ValueError:return None
    cid=connection_id(base_url,model)
    row=session.get(ModelConnection,cid)
    if row is None:
        row=ModelConnection(id=cid,base_url=base_url,model=model,api_key=protect(workspace.llm_api_key or ''),
            last_used_at=time.time(),last_check='unchecked')
        session.add(row)
    else:
        row.api_key=protect(workspace.llm_api_key or '')
        row.last_used_at=time.time()
    session.flush()
    return row


def recent(session):
    return [{'id':row.id,'baseUrl':row.base_url,'model':row.model,
        'apiKeyConfigured':bool(row.api_key),'lastUsedAt':row.last_used_at,
        'lastCheckedAt':row.last_checked_at,'lastCheck':row.last_check}
        for row in session.scalars(select(ModelConnection).order_by(ModelConnection.last_used_at.desc()).limit(12))]


def check_message(status: int) -> str:
    return ({400:'接口拒绝请求格式或工具调用参数。',401:'API Key 无效或未授权。',
        402:'模型账户余额或额度不足（HTTP 402）。',403:'当前密钥没有访问权限。',
        404:'接口地址或模型 ID 不存在。',408:'模型请求超时。',
        429:'调用频率或额度受限。'}).get(status,
        '模型服务暂时不可用。' if status>=500 else f'模型服务返回 HTTP {status}。')


async def probe(base_url: str, model: str, api_key: str) -> dict:
    base_url,model=normalize(base_url,model)
    local=urlsplit(base_url).hostname in {'localhost','127.0.0.1','::1'}
    if not api_key and not local:
        return {'available':False,'toolCalling':'unconfirmed','message':'请先填写 API Key。'}
    headers={'Content-Type':'application/json','Authorization':'Bearer '+(api_key or 'local-no-auth')}
    messages=[{'role':'user','content':'请调用 azur_connection_probe 工具，value 填 ok。不要输出其他内容。'}]
    payload={'model':model,'messages':messages,'temperature':0,'max_tokens':48,
        'tools':[{'type':'function','function':{'name':'azur_connection_probe',
            'description':'Checks whether this model can call a tool.',
            'parameters':{'type':'object','properties':{'value':{'type':'string'}},'required':['value']}}}],
        'tool_choice':'auto'}
    start=time.monotonic()
    try:
        async with httpx.AsyncClient(timeout=20,follow_redirects=False) as client:
            response=await client.post(base_url+'/chat/completions',headers=headers,json=payload)
            tool_status='unconfirmed'
            if response.status_code in {400,422}:
                # A provider may reject the tool envelope while still serving chat.
                plain={'model':model,'messages':[{'role':'user','content':'请只回答 OK。'}],
                    'temperature':0,'max_tokens':16}
                response=await client.post(base_url+'/chat/completions',headers=headers,json=plain)
                tool_status='unsupported'
    except httpx.RequestError:
        return {'available':False,'toolCalling':'unconfirmed','message':'无法连接模型服务；请检查地址和网络。'}
    elapsed=round((time.monotonic()-start)*1000)
    if response.status_code!=200:
        return {'available':False,'toolCalling':tool_status,'httpStatus':response.status_code,
            'latencyMs':elapsed,'message':check_message(response.status_code)}
    try:
        message=response.json()['choices'][0]['message']
        if not isinstance(message,dict) or not (message.get('content') or message.get('tool_calls')):
            raise ValueError('empty choice')
    except (ValueError,KeyError,IndexError,TypeError):
        return {'available':False,'toolCalling':tool_status,'latencyMs':elapsed,
            'message':'接口返回成功，但没有可识别的回复。'}
    if message.get('tool_calls'):
        tool_status='confirmed'
    return {'available':True,'toolCalling':tool_status,'latencyMs':elapsed,
        'message':('模型可用，工具调用已确认。' if tool_status=='confirmed' else
            '模型对话可用，但工具调用未确认。' if tool_status=='unconfirmed' else
            '模型对话可用；接口未接受工具调用参数。')}
