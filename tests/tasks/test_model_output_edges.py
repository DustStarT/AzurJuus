import json

import httpx
import pytest

from backend.tasks.llm_runtime import AgentRuntime
from backend.tasks.model_output import completion_message, function_calls, text_content


INVALID = [None, [], 7, {}, {'choices': []}, {'choices': {}}, {'choices': [None]},
    {'choices': [{'message': []}]}, {'choices': [{'message': {'content': {'text': 'wrong'}}}]},
    {'choices': [{'message': {'content': [1]}}]}, {'choices': [{'message': {'tool_calls': 'wrong'}}]},
    {'choices': [{'message': {'tool_calls': [None]}}]},
    {'choices': [{'message': {'tool_calls': [{'id': 'x', 'type': 'function',
        'function': {'name': 'azur_connection_probe', 'arguments': '[]'}}]}}]},
    {'choices': [{'message': {'tool_calls': [{'id': 'x', 'type': 'function',
        'function': {'name': 'azur_connection_probe', 'arguments': '{"value":NaN}'}}]}}]},
]


@pytest.mark.parametrize('body', INVALID)
@pytest.mark.asyncio
async def test_bad_provider_body_is_a_clear_runtime_error(body, monkeypatch):
    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kwargs: original(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, content=json.dumps(body))), **kwargs))
    runtime = AgentRuntime(max_retry_attempts=0)
    with pytest.raises(RuntimeError) as error:
        await runtime._complete_chat(api_key='synthetic-secret', base_url='http://fixture/v1',
            model='fixture', messages=[{'role': 'user', 'content': 'synthetic'}], temperature=0)
    assert 'synthetic-secret' not in str(error.value)


@pytest.mark.parametrize('body', INVALID)
@pytest.mark.asyncio
async def test_connection_probe_never_confirms_malformed_success(body, monkeypatch):
    from backend.tasks.model_connections import probe
    original = httpx.AsyncClient
    monkeypatch.setattr(httpx, 'AsyncClient', lambda **kwargs: original(
        transport=httpx.MockTransport(lambda _: httpx.Response(200, content=json.dumps(body))), **kwargs))
    result = await probe('http://fixture/v1', 'fixture', 'synthetic-secret')
    assert not result['available'] and result['toolCalling'] != 'confirmed'


@pytest.mark.parametrize('header', ['NaN', 'Infinity', '-Infinity', '-1', '0', '1e200', 'invalid'])
def test_retry_after_cannot_create_unbounded_or_nonfinite_waits(header):
    runtime = AgentRuntime()
    response = httpx.Response(429, headers={'Retry-After': header})
    assert 1 <= runtime._retry_after_seconds(response, 0) <= 30


def test_valid_text_blocks_and_function_arguments_remain_supported():
    message = {'content': ['first', {'type': 'text', 'text': 'second'}],
        'tool_calls': [{'id': 'call-1', 'type': 'function',
                        'function': {'name': 'read_file', 'arguments': '{"path":"synthetic.txt"}'}}]}
    assert completion_message({'choices': [{'message': message}]}) == message
    assert text_content(message['content']) == 'first\nsecond'
    assert function_calls(message['tool_calls']) == [{'id': 'call-1', 'name': 'read_file', 'args': {'path': 'synthetic.txt'}}]
