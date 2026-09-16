import io
import json
import urllib.error

from backend.capabilities import phase_actions
from backend.mcp_host import handle


def test_mcp_schema_exposes_only_phase_authorized_actions(monkeypatch):
    for phase in ("chat", "planner", "reviewer", "worker"):
        actions = phase_actions(phase)
        monkeypatch.setenv("AZURJUUS_TOOL_ACTIONS", json.dumps(actions))
        tools = handle({"method": "tools/list"})["tools"]
        if phase == "chat":
            assert tools == []
        else:
            assert tools[0]["inputSchema"]["properties"]["action"]["enum"] == actions
            assert ("command:" in tools[0]["description"]) == (phase == "worker")
            assert ("delegate:" in tools[0]["description"]) == (phase == "planner")


def test_http_permission_detail_reaches_executor(monkeypatch):
    monkeypatch.setenv("AZURJUUS_TOOL_ENDPOINT", "http://127.0.0.1/tool")
    monkeypatch.setenv("AZURJUUS_TOOL_TOKEN", "synthetic-token")
    def denied(*args, **kwargs):
        raise urllib.error.HTTPError("http://127.0.0.1/tool", 403, "Forbidden", {}, io.BytesIO(b'{"detail":"Read-only reviewer cannot run commands"}'))
    monkeypatch.setattr("urllib.request.urlopen", denied)
    reply = handle({"method": "tools/call", "params": {"arguments": {"action": "command", "args": {}}}})
    result = json.loads(reply["content"][0]["text"])
    assert reply["isError"]
    assert result["httpStatus"] == 403
    assert "Read-only reviewer" in result["error"]
    assert result["callId"]
