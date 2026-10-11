import asyncio
import json
import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import select

from backend.app import create_app
from backend.config import get_settings
from backend.credentials import reveal
from backend.database import session_scope
from backend.models import ModelConnection, WorkspaceSetting
from conftest import configure_test_env


@pytest.fixture
def client(monkeypatch,tmp_path):
    configure_test_env(monkeypatch,tmp_path)
    monkeypatch.setenv('AZURJUUS_MIND_ENABLED','0')
    monkeypatch.setenv('AZURJUUS_SOCIAL_ENGINE_ENABLED','0')
    get_settings.cache_clear()
    with TestClient(create_app()) as current:
        yield current


def save(client,base,model,key):
    response=client.post('/api/workspace/save',json={'workspace':{'settings':{
        'llmBaseUrl':base,'llmModel':model,'llmApiKey':key}}})
    response.raise_for_status()
    if key:assert key not in response.text


def test_saved_connections_switch_keys_and_survive_preserving_reset(client):
    assert client.get('/api/model/connections').json()['connections']==[]
    save(client,'https://model-one.example/v1','model-a','secret-for-model-a')
    save(client,'https://model-two.example/v1','model-b','secret-for-model-b')
    rows=client.get('/api/model/connections').json()['connections']
    assert {(row['model'],row['baseUrl']) for row in rows}>={
        ('model-a','https://model-one.example/v1'),('model-b','https://model-two.example/v1')}
    assert 'secret-for-model-a' not in str(rows) and 'secret-for-model-b' not in str(rows)
    first=next(row for row in rows if row['model']=='model-a')
    response=client.post('/api/model/connections/activate',json={'id':first['id']})
    response.raise_for_status()
    assert response.json()['workspace']['settings']['llmModel']=='model-a'
    assert 'secret-for-model-a' not in response.text
    with session_scope() as s:
        current=s.get(WorkspaceSetting,1)
        assert reveal(current.llm_api_key)=='secret-for-model-a'
        assert s.get(ModelConnection,first['id']).api_key!='secret-for-model-a'
    save(client,'https://model-two.example/v1','model-b','')
    with session_scope() as s:
        assert reveal(s.get(WorkspaceSetting,1).llm_api_key)=='secret-for-model-b'
    client.post('/api/model/connections/activate',json={'id':first['id']}).raise_for_status()
    reset=client.post('/api/system/reset',json={'preserveKeysAndProfile':True})
    reset.raise_for_status()
    assert reset.json()['workspace']['settings']['llmModel']=='model-a'
    assert reset.json()['workspace']['settings']['llmBaseUrl']=='https://model-one.example/v1'
    assert 'secret-for-model-a' not in reset.text
    restored=client.get('/api/model/connections').json()['connections']
    assert {row['model'] for row in restored}>={'model-a','model-b'}
    assert client.post('/api/model/connections/remove',json={'id':first['id']}).status_code==409
    second=next(row for row in restored if row['model']=='model-b')
    client.post('/api/model/connections/remove',json={'id':second['id']}).raise_for_status()
    client.post('/api/system/reset',json={}).raise_for_status()
    with session_scope() as s:
        assert not s.scalars(select(ModelConnection)).all()


def test_model_check_reports_tool_support_and_payment_error_without_echoing_key(client,monkeypatch):
    from backend.tasks import model_connections
    real_async_client=httpx.AsyncClient
    seen=[]
    status=200
    def handle(request):
        seen.append(request)
        if status==402:return httpx.Response(402,json={'error':{'message':'secret must not leak'}})
        if status==400:
            if 'tools' in json.loads(request.content):return httpx.Response(400,json={'error':'tools unsupported'})
            return httpx.Response(200,json={'choices':[{'message':{'content':'OK'}}]})
        return httpx.Response(200,json={'choices':[{'message':{'content':None,
            'tool_calls':[{'id':'call-1','type':'function','function':{'name':'azur_connection_probe','arguments':'{"value":"ok"}'}}]}}]})
    def factory(*args,**kwargs):
        return real_async_client(*args,transport=httpx.MockTransport(handle),**kwargs)
    monkeypatch.setattr(model_connections.httpx,'AsyncClient',factory)
    payload={'baseUrl':'https://probe.example/v1','model':'probe-model','apiKey':'synthetic-secret'}
    result=client.post('/api/model/check',json=payload)
    assert result.status_code==200
    assert result.json()['available'] and result.json()['toolCalling']=='confirmed'
    assert seen[-1].headers['Authorization']=='Bearer synthetic-secret'
    assert 'synthetic-secret' not in result.text
    status=402
    failed=client.post('/api/model/check',json=payload)
    assert failed.status_code==200 and failed.json()['available'] is False
    assert failed.json()['httpStatus']==402 and 'secret' not in failed.text
    status=400
    plain=client.post('/api/model/check',json=payload)
    assert plain.json()['available'] and plain.json()['toolCalling']=='unsupported'
    assert client.post('/api/model/check',json={**payload,'baseUrl':'file:///etc/passwd'}).status_code==422
    status=200
    local=client.post('/api/model/check',json={'baseUrl':'http://127.0.0.1:8765/v1',
        'model':'local-model','apiKey':''})
    assert local.json()['available']
    assert seen[-1].headers['Authorization']=='Bearer local-no-auth'


def test_reasoning_probe_is_optional_cached_and_never_returns_reasoning_text(client,monkeypatch):
    from backend.tasks import model_connections, reasoning_policy
    real_client=httpx.AsyncClient
    seen=[]
    def handle(request):
        body=json.loads(request.content)
        seen.append(body)
        if body.get('thinking'):
            return httpx.Response(400,json={'error':'unsupported secret reasoning payload'})
        return httpx.Response(200,json={'choices':[{'message':{'content':'private chain text'}}]})
    monkeypatch.setattr(model_connections.httpx,'AsyncClient',
        lambda *args,**kwargs:real_client(*args,transport=httpx.MockTransport(handle),**kwargs))
    payload={'baseUrl':'https://probe.example/v1','model':'reason-model',
        'apiKey':'synthetic-secret','checkReasoning':True}
    response=client.post('/api/model/check',json=payload)
    assert response.status_code==200
    assert response.json()['reasoning']=={'structuredOutput':'supported',
        'reasoningEffort':'supported','thinkingMode':'unsupported',
        'tokenLimitParameter':'max_tokens'}
    assert len(seen)==5  # Availability, accepted base envelope, three extensions.
    assert 'private chain text' not in response.text
    assert 'synthetic-secret' not in response.text
    configured={'llmBaseUrl':payload['baseUrl'],'llmModel':payload['model'],'_mindStage':'decide',
        '_mindComplex':True}
    result=reasoning_policy.apply({'temperature':0,'response_format':{'type':'json_object'}},configured)
    assert result['reasoning_effort']=='high' and 'thinking' not in result
    assert 'temperature' not in result
    client.post('/api/model/check',json={k:v for k,v in payload.items() if k!='checkReasoning'}).raise_for_status()
    assert len(seen)==6  # Ordinary availability check does not spend probe calls.
    deepseek={'llmBaseUrl':'https://api.deepseek.com/v1','llmModel':'deepseek-flash'}
    cognitive=reasoning_policy.apply({'temperature':0},
        {**deepseek,'_mindStage':'understand'})
    expression=reasoning_policy.apply({'temperature':0},
        {**deepseek,'_mindStage':'express'})
    assert cognitive['thinking']=={'type':'enabled'} and cognitive['reasoning_effort']=='low'
    assert 'temperature' not in cognitive
    assert expression['thinking']=={'type':'disabled'} and 'reasoning_effort' not in expression


def test_reasoning_only_endpoint_uses_accepted_output_limit(client,monkeypatch):
    from backend.tasks import model_connections, reasoning_policy
    real_client=httpx.AsyncClient
    def handle(request):
        body=json.loads(request.content)
        if 'max_tokens' in body:return httpx.Response(400)
        return httpx.Response(200,json={'choices':[{'message':{'content':'{"ok":true}'}}]})
    monkeypatch.setattr(model_connections.httpx,'AsyncClient',
        lambda *args,**kwargs:real_client(*args,transport=httpx.MockTransport(handle),**kwargs))
    base='https://reason-only.example/v1';model='reason-only'
    result=asyncio.run(model_connections.probe_reasoning(base,model,'synthetic-secret'))
    assert result['tokenLimitParameter']=='max_completion_tokens'
    reasoning_policy.remember(base,model,result)
    request=reasoning_policy.apply({'max_tokens':1024,'temperature':.4},
        {'llmBaseUrl':base,'llmModel':model,'_mindStage':'decide'})
    assert request['max_completion_tokens']==1024 and 'max_tokens' not in request
