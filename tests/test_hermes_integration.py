"""Exercises the pinned upstream process against a local scripted provider, not a mocked bridge."""
import asyncio
import json
import os
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from backend.hermes_bridge import HermesBridge, ROOT


@pytest.mark.asyncio
async def test_real_hermes_gateway_calls_only_host_mcp(tmp_path, monkeypatch):
    if not (ROOT / ".vendor/hermes-agent/tui_gateway/entry.py").exists():
        pytest.skip("Run tools/setup_runtime.py to install the pinned Hermes source")
    seen = []
    calls = []
    authorization = []
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def do_GET(self):
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"object": "list", "data": [{"id": "azur-test", "object": "model", "context_length": 131072}]}).encode())

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            expected = "host-test-token" if self.path == "/tool" else "local-test-only"
            authenticated = self.headers.get("Authorization") == "Bearer " + expected
            authorization.append(authenticated)
            if not authenticated:
                self.send_response(401)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b'{"error":{"message":"Wrong synthetic test credential"}}')
                return
            if self.path == "/tool":
                calls.append(body)
                result = {"status": "completed", "callId": body["callId"], "result": {"entries": [{"name": "evidence.txt", "directory": False}]}}
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(json.dumps(result).encode())
                return
            seen.append(body)
            tools = body.get("tools", [])
            tool_name = next((t["function"]["name"] for t in tools if "workspace" in t["function"]["name"]), None)
            if tool_name and sum(m.get("role") == "tool" for m in body["messages"]) < 7:
                message = {"role": "assistant", "content": None, "tool_calls": [{"id": "call_probe", "type": "function", "function": {"name": tool_name, "arguments": json.dumps({"action": "list_dir", "args": {"path": "."}})}}]}
            else:
                message = {"role": "assistant", "content": "LOCAL TOOL OK"}
            self.send_response(200)
            stream = body.get("stream")
            self.send_header("Content-Type", "text/event-stream" if stream else "application/json")
            self.end_headers()
            finish = "tool_calls" if message.get("tool_calls") else "stop"
            if stream:
                delta = {k:v for k,v in message.items() if v is not None}
                if "tool_calls" in delta:
                    delta["tool_calls"] = [{"index": 0, **c} for c in delta["tool_calls"]]
                chunks = [{"id":"chatcmpl-test","object":"chat.completion.chunk","created":1,"model":"azur-test","choices":[{"index":0,"delta":delta,"finish_reason":None}]}, {"id":"chatcmpl-test","object":"chat.completion.chunk","created":1,"model":"azur-test","choices":[{"index":0,"delta":{},"finish_reason":finish}]}]
                for chunk in chunks:
                    self.wfile.write(("data: " + json.dumps(chunk) + "\n\n").encode())
                self.wfile.write(b"data: [DONE]\n\n")
            else:
                self.wfile.write(json.dumps({"id":"chatcmpl-test","object":"chat.completion","created":1,"model":"azur-test","choices":[{"index":0,"message":message,"finish_reason":finish}],"usage":{"prompt_tokens":10,"completion_tokens":10,"total_tokens":20}}).encode())
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{server.server_port}"
    events = []
    async def event(kind, payload):
        events.append((kind,payload))
    monkeypatch.setenv("AZURJUUS_HERMES_SOURCE", str(ROOT / ".vendor/hermes-agent"))
    # Exercise a relative home even when Windows temp and checkout use different drives.
    monkeypatch.chdir(tmp_path.parent)
    if os.name == 'nt':
        # Reproduce a windowless desktop parent; protocol children still need python.exe.
        monkeypatch.setattr(sys, 'executable', str(Path(sys.executable).with_name('pythonw.exe')))
    relative_home = Path(os.path.relpath(tmp_path / "hermes", Path.cwd()))
    assert not relative_home.is_absolute()
    bridge = HermesBridge(relative_home, {"llmModel":"azur-test", "llmBaseUrl":base + "/v1", "llmApiKey":"local-test-only"}, base + "/tool", "host-test-token", event)
    assert bridge.home == (tmp_path / "hermes").resolve()
    try:
        await bridge.start()
        generated_config = (tmp_path / "hermes" / "config.yaml").read_text(encoding="utf-8")
        assert json.loads(generated_config)["model"]["api_key"] == "${OPENAI_API_KEY}"
        assert "local-test-only" not in generated_config
        assert "host-test-token" not in generated_config
        reply = await asyncio.wait_for(bridge.prompt("Call workspace list_dir then report its result.", str(tmp_path)), 120)
        assert reply == "LOCAL TOOL OK", (reply, bridge.diagnostics)
        assert calls and calls[0]["name"] == "list_dir", "\n".join(bridge.diagnostics) + "\n" + json.dumps(seen)
        assert len(calls) == 7
        assert all(authorization)
        names = {t["function"]["name"] for body in seen for t in body.get("tools", [])}
        assert names == {"mcp__azurjuus__workspace"}, names
        assert any(k == "message.complete" for k, _ in events)
    finally:
        await bridge.close()
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()
