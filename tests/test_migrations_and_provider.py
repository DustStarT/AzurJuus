import json
import sqlite3

import httpx
import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

from backend.app import create_app
from backend.database import get_engine
from backend.llm_runtime import AgentRuntime
from test_backend_flows import configure_test_env


def test_migration_backs_up_and_preserves_history(monkeypatch,tmp_path):
    configure_test_env(monkeypatch,tmp_path)
    with TestClient(create_app()) as client:
        original=client.get('/api/bootstrap').json()['snapshot']
    with get_engine().begin() as db:
        db.execute(text("UPDATE workspace_settings SET llm_api_key='migration-fixture-key',ui_session_json=:ui"),{'ui':json.dumps({'view':'circle','settingsExtras':{'searchApiKey':'migration-search-key'}})})
    monkeypatch.setenv('AZURJUUS_EXECUTION_BACKEND','hermes')
    with TestClient(create_app()) as client:
        boot=client.get('/api/bootstrap').json()
        assert boot['workspace']['settings']['llmApiKeyConfigured']
        assert boot['workspace']['uiSession']['view']=='circle'
        assert 'migration-fixture-key' not in json.dumps(boot)
        assert [a['id'] for a in boot['snapshot']['agents']]==[a['id'] for a in original['agents']]
        assert boot['snapshot']['messages']==original['messages']
        assert [p['id'] for p in boot['snapshot']['posts']]==[p['id'] for p in original['posts']]
        assert client.post('/api/workflows/tick',json={}).status_code==409
    backups=list((tmp_path/'migration-backups').glob('*-v0.db'))
    assert len(backups)==1
    with sqlite3.connect(backups[0]) as db:
        assert db.execute('SELECT llm_api_key FROM workspace_settings').fetchone()[0]=='migration-fixture-key'
    with sqlite3.connect(tmp_path/'azurjuus.db') as db:
        assert db.execute('SELECT llm_api_key FROM workspace_settings').fetchone()[0].startswith(('dpapi:','keyring:'))
    with TestClient(create_app()) as client:
        assert client.get('/api/bootstrap').status_code==200
    assert len(list((tmp_path/'migration-backups').glob('*-v0.db')))==1


@pytest.mark.asyncio
@pytest.mark.parametrize('status',[401,429,500])
async def test_provider_errors_never_turn_into_template_success(monkeypatch,status):
    monkeypatch.setenv('AZURJUUS_EXECUTION_BACKEND','hermes')
    client_type=httpx.AsyncClient
    transport=httpx.MockTransport(lambda request:httpx.Response(status,json={'error':'fixture failure'}))
    monkeypatch.setattr(httpx,'AsyncClient',lambda **kwargs:client_type(transport=transport,**kwargs))
    runtime=AgentRuntime(1)
    runtime._max_retry_attempts=0
    with pytest.raises(RuntimeError):
        await runtime._complete_chat(api_key='fixture-key',base_url='http://provider.test/v1',model='fixture',messages=[{'role':'user','content':'test'}],temperature=0,fallback='Pretend success')
