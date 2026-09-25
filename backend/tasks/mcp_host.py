"""Minimal MCP stdio relay. The application owns execution and authorization."""
from __future__ import annotations

import json
import os
import sys
import urllib.request
import urllib.error
from uuid import uuid4

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parents[2]))
from backend.tasks.capabilities import CATALOG


def handle(request):
    method, params = request.get("method"), request.get("params", {})
    if method == "initialize":
        return {"protocolVersion": params.get("protocolVersion", "2024-11-05"), "capabilities": {"tools": {}}, "serverInfo": {"name": "azurjuus", "version": "1.0.0"}}
    if method == "ping":
        return {}
    if method == "tools/list":
        actions = [name for name in json.loads(os.getenv("AZURJUUS_TOOL_ACTIONS", json.dumps(list(CATALOG)))) if name in CATALOG]
        if not actions:
            return {"tools": []}
        return {"tools": [{"name": "workspace", "description": "Execute authorized local work. All operations are logged. Evidence uses the OUTER callId returned by this tool, never nested IDs from execution_history.\n" + "\n".join(k + ": " + CATALOG[k] for k in actions), "inputSchema": {"type": "object", "properties": {"action": {"type": "string", "enum": actions}, "args": {"type": "object"}}, "required": ["action", "args"]}}]}
    if method == "tools/call":
        arguments = params.get("arguments", {})
        body = {"callId": uuid4().hex, "name": arguments.get("action"), "args": arguments.get("args", {})}
        req = urllib.request.Request(os.environ["AZURJUUS_TOOL_ENDPOINT"], json.dumps(body).encode(), {"Content-Type": "application/json", "Authorization": "Bearer " + os.environ["AZURJUUS_TOOL_TOKEN"]})
        try:
            with urllib.request.urlopen(req, timeout=3600) as response:
                result = json.load(response)
            payload = result.get("result", result)
            if isinstance(payload, dict) and "image" in payload:
                metadata = {k:v for k,v in payload.items() if k != "image"}
                return {"content": [{"type": "text", "text": json.dumps({"callId":body["callId"], **metadata}, ensure_ascii=False)}, {"type": "image", "data": payload["image"], "mimeType": payload["mimeType"]}]}
            return {"content": [{"type": "text", "text": json.dumps(result, ensure_ascii=False, default=str)}], "isError": result.get("status") == "failed"}
        except urllib.error.HTTPError as exc:
            try:
                error = json.load(exc)
                detail = error.get("detail") or error.get("error") or str(exc)
            except (ValueError, OSError):
                detail = str(exc)
            return {"content": [{"type": "text", "text": json.dumps({"callId": body["callId"], "status": "failed", "httpStatus": exc.code, "error": detail}, ensure_ascii=False)}], "isError": True}
        except Exception as exc:
            return {"content": [{"type": "text", "text": str(exc)}], "isError": True}
    if method and method.startswith("notifications/"):
        return None
    raise ValueError("Unsupported MCP method")


def main():
    for line in sys.stdin:
        request = {}
        try:
            request = json.loads(line)
            result = handle(request)
            if "id" not in request:
                continue
            response = {"jsonrpc": "2.0", "id": request["id"], "result": result}
        except Exception as exc:
            response = {"jsonrpc": "2.0", "id": request.get("id"), "error": {"code": -32603, "message": str(exc)}}
        print(json.dumps(response, ensure_ascii=False), flush=True)


if __name__ == "__main__":
    main()
