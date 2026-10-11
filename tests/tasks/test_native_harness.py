"""The installed official runtime executes a native plugin against a local synthetic model."""
import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from backend.tasks.deepseek_bridge import DeepSeekHarnessBridge


@pytest.fixture
def waiting_model():
    """A local model whose first inference stays open during steering/cancel."""
    received, release = threading.Event(), threading.Event()
    requests = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            requests.append(body)
            received.set()
            if len(requests) == 1:
                release.wait(8)
            try:
                self.send_response(200)
                self.send_header('Content-Type', 'text/event-stream')
                self.end_headers()
                text = 'Updated answer.' if any('additional instruction' in str(m['content'])
                    for m in body['messages']) else 'Original answer.'
                for delta, finish in [({'content': text}, None), ({}, 'stop')]:
                    self.wfile.write(('data: ' + json.dumps({'id': 'synthetic', 'object': 'chat.completion.chunk',
                        'choices': [{'index': 0, 'delta': delta, 'finish_reason': finish}]}) + '\n\n').encode())
                self.wfile.write(b'data: [DONE]\n\n')
            except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                pass

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f'http://127.0.0.1:{server.server_port}', received, release, requests
    finally:
        release.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def chat_bridge(tmp_path, endpoint, events):
    async def on_event(kind, payload):
        events.append((kind, payload))
    return DeepSeekHarnessBridge(tmp_path / 'home', {
        'llmBaseUrl': endpoint, 'llmApiKey': 'synthetic', 'llmModel': 'deepseek-chat',
        '_allowedActions': [], '_characterIdentity': 'Synthetic character.',
        '_workspace': str(tmp_path), 'chatTimeoutSeconds': 30,
    }, endpoint + '/tool', 'synthetic-token', on_event)


async def wait_received(received):
    async with asyncio.timeout(30):
        while not received.is_set():
            await asyncio.sleep(.01)


@pytest.mark.asyncio
async def test_official_runtime_receives_steering_during_inference(tmp_path, waiting_model):
    endpoint, received, release, requests = waiting_model
    events = []
    bridge = chat_bridge(tmp_path, endpoint, events)
    task = asyncio.create_task(bridge.prompt('Initial instruction.', str(tmp_path)))
    try:
        await wait_received(received)
        receipt = await bridge.steer('additional instruction', 'instruction-1')
        assert receipt['queued'] and receipt['messageId']
        release.set()
        assert await asyncio.wait_for(task, 30) == 'Updated answer.'
        assert len(requests) == 2
        assert [(kind, payload['instructionId']) for kind, payload in events if kind == 'runtime.steering'] == [
            ('runtime.steering', 'instruction-1')]
    finally:
        release.set()
        await bridge.close()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_official_runtime_cancellation_reaps_process_and_call(tmp_path, waiting_model):
    endpoint, received, release, _ = waiting_model
    events = []
    bridge = chat_bridge(tmp_path, endpoint, events)
    task = asyncio.create_task(bridge.prompt('Initial instruction.', str(tmp_path)))
    try:
        await wait_received(received)
        client, worker = bridge.harness.client, bridge._worker
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(task, 8)
        assert client._proc is None and worker.done()
        assert bridge.harness is None
        assert not any(kind == 'message.complete' for kind, _ in events)
    finally:
        release.set()
        await bridge.close()
        await asyncio.gather(task, return_exceptions=True)


@pytest.mark.asyncio
async def test_official_runtime_does_not_inject_an_assistant_persona(tmp_path, waiting_model):
    endpoint, received, release, requests = waiting_model
    release.set()
    bridge = chat_bridge(tmp_path, endpoint, [])
    bridge.settings['_characterPolicy'] = 'Only host-authored policy.'
    try:
        assert await asyncio.wait_for(bridge.prompt('Hello.', str(tmp_path)), 30) == 'Original answer.'
        systems = [m['content'] for m in requests[0]['messages'] if m['role'] == 'system']
        assert systems == ['Synthetic character.\nOnly host-authored policy.']
        assert requests[0]['messages'][-1]['content'] == 'Hello.'
        assert not requests[0].get('tools')
    finally:
        await bridge.close()


@pytest.mark.asyncio
async def test_official_runtime_calls_native_workspace_tool(tmp_path):
    requests, calls, events = [], [], []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
            if self.path == '/tool':
                assert self.headers['Authorization'] == 'Bearer synthetic-token'
                calls.append(body)
                self.send_response(200)
                self.end_headers()
                self.wfile.write(json.dumps({'callId': body['callId'], 'status': 'completed',
                                             'result': {'files': ['synthetic.txt']}}).encode())
                return
            requests.append(body)
            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream')
            self.end_headers()
            if any(message['role'] == 'tool' for message in body['messages']):
                delta, finish = {'content': 'Native workspace verified.'}, 'stop'
            else:
                tool = next(tool['function'] for tool in body['tools'] if tool['function']['name'].endswith('workspace'))
                assert tool['parameters']['properties']['action']['enum'] == ['list_dir']
                delta = {'tool_calls': [{'index': 0, 'id': 'native_call_test', 'type': 'function',
                                        'function': {'name': tool['name'], 'arguments': '{"action":"list_dir","args":{}}'}}]}
                finish = 'tool_calls'
            for choice in [{'index': 0, 'delta': delta, 'finish_reason': None},
                           {'index': 0, 'delta': {}, 'finish_reason': finish}]:
                self.wfile.write(('data: ' + json.dumps({'id': 'synthetic', 'object': 'chat.completion.chunk',
                                                       'choices': [choice]}) + '\n\n').encode())
            self.wfile.write(b'data: [DONE]\n\n')

    server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    endpoint = f'http://127.0.0.1:{server.server_port}'

    async def on_event(kind, payload):
        events.append((kind, payload))

    bridge = DeepSeekHarnessBridge(tmp_path / 'home', {
        'llmBaseUrl': endpoint, 'llmApiKey': 'synthetic', 'llmModel': 'deepseek-chat',
        '_allowedActions': ['list_dir'], '_characterIdentity': 'Synthetic character.',
        '_workspace': str(tmp_path), 'chatTimeoutSeconds': 30,
    }, endpoint + '/tool', 'synthetic-token', on_event)
    try:
        result = await asyncio.wait_for(bridge.prompt('List the directory.', str(tmp_path)), 45)
        assert result == 'Native workspace verified.'
        assert len(calls) == 1 and calls[0]['name'] == 'list_dir'
        assert calls[0]['callId'].isalnum()
        assert len(requests) == 2
        assert any(kind == 'tool.complete' for kind, _ in events)
        assert any(kind == 'runtime.native' and payload['type'] == 'tool/result' for kind, payload in events)
    finally:
        await bridge.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)
