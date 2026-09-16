"""Version-pinned Hermes JSON-RPC transport, isolated from application imports."""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import sys
from datetime import datetime, UTC
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class HermesUnavailable(RuntimeError):
    pass


class HermesBridge:
    def __init__(self, home: Path, settings: dict, endpoint: str, token: str, on_event):
        # Resolve before changing the child's cwd. A relative HERMES_HOME makes
        # Hermes load a different directory and silently miss our model/tools config.
        self.home, self.settings, self.endpoint, self.token = home.expanduser().resolve(), settings, endpoint, token
        self.on_event = on_event
        self.process = None
        self.pending = {}
        self.counter = 0
        self.ready = asyncio.Event()
        self.reader = self.stderr_reader = None
        self.diagnostics = []
        self.session_id = None
        self.completed = asyncio.Queue()
        self.budget_exhausted = False
        self._seen_events = set()

    def diagnostic(self, text):
        # Only transport diagnostics belong here: never log prompts or RPC bodies.
        text = str(text)
        for secret in (self.settings.get("llmApiKey"), self.token):
            if secret:
                text = text.replace(secret, "[redacted]")
        self.diagnostics = (self.diagnostics + [text])[-60:]
        self.home.mkdir(parents=True, exist_ok=True)
        path = self.home / "bridge.log"
        with contextlib.suppress(OSError):
            if path.exists() and path.stat().st_size > 512 * 1024:
                path.write_text("", encoding="utf-8")
            with path.open("a", encoding="utf-8") as output:
                output.write(datetime.now(UTC).isoformat() + " " + text + "\n")

    async def stage(self, name, label):
        self.diagnostic("stage=" + name)
        await self.on_event("runtime.stage", {"stage": name, "label": label})

    async def start(self):
        await self.stage("checking", "正在检查执行环境")
        source = Path(os.getenv("AZURJUUS_HERMES_SOURCE", str(ROOT / ".vendor" / "hermes-agent")))
        source = (source if source.is_absolute() else ROOT / source).resolve()
        if not (source / "tui_gateway" / "entry.py").exists():
            raise HermesUnavailable("Hermes 尚未安装。请运行 tools/setup_runtime.py。")
        revision = (ROOT / "hermes.lock.json").read_text(encoding="utf-8")
        expected = json.loads(revision)["revision"]
        check = await asyncio.create_subprocess_exec("git", "-C", str(source), "rev-parse", "HEAD", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, creationflags=0x08000000 if os.name == "nt" else 0)
        try:
            out, _ = await asyncio.wait_for(check.communicate(), 15)
        finally:
            if check.returncode is None:
                check.kill()
                await check.wait()
        if out.decode().strip() != expected:
            raise HermesUnavailable("Hermes 版本与 hermes.lock.json 不符，请运行安装工具恢复锁定版本。")
        self.home.mkdir(parents=True, exist_ok=True)
        config = {
            "model": {"default": self.settings["llmModel"], "provider": "custom", "base_url": self.settings["llmBaseUrl"], "api_key": "${OPENAI_API_KEY}"},
            "agent": {"max_turns": int(self.settings.get("maxTurns", 80))},
            "platform_toolsets": {"cli": ["mcp-azurjuus"]},
            "tools": {"tool_search": {"enabled": "off"}},
            "mcp_servers": {"azurjuus": {"command": sys.executable, "args": ["-X", "utf8", str(ROOT / "backend" / "mcp_host.py")], "env": {"AZURJUUS_TOOL_ENDPOINT": "${AZURJUUS_TOOL_ENDPOINT}", "AZURJUUS_TOOL_TOKEN": "${AZURJUUS_TOOL_TOKEN}", "AZURJUUS_TOOL_ACTIONS": "${AZURJUUS_TOOL_ACTIONS}"}, "timeout": 3600}},
            "terminal": {"backend": "local"},
            "memory": {"memory_enabled": False, "user_profile_enabled": False},
        }
        # JSON is valid YAML. No credentials are written to this file.
        (self.home / "config.yaml").write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
        env = dict(os.environ)
        env.update(HERMES_HOME=str(self.home), OPENAI_API_KEY=self.settings.get("llmApiKey", ""), OPENAI_BASE_URL=self.settings["llmBaseUrl"], PYTHONIOENCODING="utf-8", PYTHONUTF8="1", HERMES_SKIP_UPDATE_CHECK="1")
        # Other provider credentials must not override the explicitly selected endpoint.
        for key in list(env):
            if (key.endswith("_API_KEY") or key.endswith("_TOKEN")) and key not in {"OPENAI_API_KEY"}:
                env.pop(key, None)
        env.update(AZURJUUS_TOOL_ENDPOINT=self.endpoint, AZURJUUS_TOOL_TOKEN=self.token, HERMES_TUI_TOOLSETS="mcp-azurjuus")
        from .capabilities import CATALOG
        env["AZURJUUS_TOOL_ACTIONS"] = json.dumps(self.settings.get("_allowedActions", list(CATALOG)))
        await self.stage("starting", "正在启动执行核心")
        self.process = await asyncio.create_subprocess_exec(sys.executable, "-m", "tui_gateway.entry", cwd=source, env=env, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, limit=8 * 1024 * 1024, creationflags=0x08000000 if os.name == "nt" else 0)
        self.diagnostic("process.pid=" + str(self.process.pid))
        self.reader = asyncio.create_task(self._read())
        self.stderr_reader = asyncio.create_task(self._stderr())
        try:
            await asyncio.wait_for(self.ready.wait(), 90)
        except TimeoutError as exc:
            await self.close()
            raise HermesUnavailable("Hermes 启动超时：" + "\n".join(self.diagnostics[-5:])) from exc
        if self.process.returncode is not None:
            raise HermesUnavailable("Hermes 无法启动：" + "\n".join(self.diagnostics[-5:]))

    async def _stderr(self):
        while line := await self.process.stderr.readline():
            text = line.decode("utf-8", errors="replace").strip()
            self.diagnostic(text)

    async def _read(self):
        try:
            while line := await self.process.stdout.readline():
                try:
                    message = json.loads(line)
                except (ValueError, UnicodeError):
                    continue
                if "id" in message:
                    future = self.pending.pop(message["id"], None)
                    if future and not future.done():
                        if "error" in message:
                            future.set_exception(RuntimeError(str(message["error"].get("message", message["error"]))))
                        else:
                            future.set_result(message.get("result", {}))
                elif message.get("method") == "event":
                    event = message.get("params", {})
                    kind = event.get("type", "")
                    payload = event.get("payload") or {}
                    if kind not in self._seen_events:
                        self._seen_events.add(kind)
                        self.diagnostic("event=" + kind)
                    if kind == "notification.show":
                        self.diagnostic("notice=" + str(payload.get("text", "")))
                    if kind == "gateway.ready":
                        self.diagnostic("gateway.ready")
                        self.ready.set()
                    if kind == "message.start":
                        await self.stage("generating", "正在等待模型回复")
                    if kind in {"message.start", "message.delta", "message.complete", "tool.start", "tool.complete", "approval.request", "clarify.request", "session.error"}:
                        await self.on_event(kind, payload)
                    if kind == "message.complete":
                        # Persist/publish the last message before waking the caller,
                        # which may immediately close and cancel this reader.
                        await self.completed.put(payload)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self.diagnostic(str(exc))
        finally:
            error = HermesUnavailable("Hermes 进程已退出。" + "\n".join(self.diagnostics[-3:]))
            for future in self.pending.values():
                if not future.done():
                    future.set_exception(error)
            self.pending.clear()
            self.ready.set()
            await self.completed.put({"status": "error", "text": str(error)})

    async def rpc(self, method, params=None, timeout=90):
        self.counter += 1
        request_id = self.counter
        future = asyncio.get_running_loop().create_future()
        self.pending[request_id] = future
        try:
            self.process.stdin.write((json.dumps({"jsonrpc": "2.0", "id": request_id, "method": method, "params": params or {}}, ensure_ascii=False) + "\n").encode())
            await self.process.stdin.drain()
            return await asyncio.wait_for(future, timeout)
        finally:
            self.pending.pop(request_id, None)

    async def prompt(self, text, workspace, history=None):
        if self.session_id is None:
            await self.stage("session", "正在准备角色与工具")
            session = await self.rpc("session.create", {"cwd": workspace, "messages": history or [], "model": self.settings["llmModel"], "provider": "custom"})
            self.session_id = session["session_id"]
        while not self.completed.empty():
            self.completed.get_nowait()
        # prompt.submit acknowledges a queued turn before the agent is built.
        # message.start is the gateway evidence that generation actually began.
        await self.stage("initializing", "正在初始化角色执行")
        await self.rpc("prompt.submit", {"session_id": self.session_id, "text": text})
        result = await self.completed.get()
        self.budget_exhausted = int((result.get("usage") or {}).get("calls", 0)) >= int(self.settings.get("maxTurns", 80))
        if result.get("status") in {"error", "interrupted"}:
            raise RuntimeError(result.get("text") or "执行已中断。")
        return result.get("text", "")

    async def steer(self, text):
        return await self.rpc("session.steer", {"session_id": self.session_id, "text": text})

    async def close(self):
        self.diagnostic("transport.closing")
        if self.process and self.process.returncode is None:
            if self.session_id:
                with contextlib.suppress(Exception):
                    await self.rpc("session.interrupt", {"session_id": self.session_id}, timeout=3)
            if self.process.stdin:
                self.process.stdin.close()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self.process.wait(), 5)
            if self.process.returncode is None:
                if os.name == "nt":
                    killer = await asyncio.create_subprocess_exec("taskkill", "/PID", str(self.process.pid), "/T", "/F", stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL, creationflags=0x08000000)
                    await killer.wait()
                else:
                    self.process.kill()
                await self.process.wait()
        for task in (self.reader, self.stderr_reader):
            if task:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        self.diagnostic("transport.closed")
