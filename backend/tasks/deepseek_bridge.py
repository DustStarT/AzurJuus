"""Official DeepSeek Harness SDK bridge for the task coordinator."""
from __future__ import annotations

import asyncio
import base64
import json
import mimetypes
from pathlib import Path

from deepseek_harness import DeepSeekHarness, DeepSeekHarnessConfig

from backend.tasks.capabilities import CATALOG

ROOT = Path(__file__).resolve().parents[2]


class DeepSeekHarnessUnavailable(RuntimeError):
    pass


class DeepSeekHarnessBridge:
    """Adapt the blocking official SDK to the coordinator's async bridge API."""

    def __init__(self, home: Path, settings: dict, endpoint: str, token: str, on_event):
        self.home = home.expanduser().resolve()
        self.settings, self.endpoint, self.token, self.on_event = settings, endpoint, token, on_event
        self.harness: DeepSeekHarness | None = None
        self.session_id: str | None = None
        self.closed = False
        self._first_prompt = True
        self._tool_names: dict[str, str] = {}
        self._start_task = None
        self._close_task = None
        self._worker = None
        self._prompt_lock = asyncio.Lock()
        self._steer_workers = set()
        self._steering_messages: dict[str, str] = {}
        self._received_messages: set[str] = set()

    async def _event(self, kind, payload):
        if self.on_event:
            await self.on_event(kind, payload)

    async def stage(self, name, label):
        await self._event("runtime.stage", {"stage": name, "label": label})

    def _patch(self):
        self.home.mkdir(parents=True, exist_ok=True)
        patch = self.home / "azurjuus.cordis.patch.yml"
        patch.write_text("""- id: system-prompt
  config:
    includeHarnessIdentity: false
    includeRuntimeContext: false
    personaPrefix: !!js process.env.DSH_SYSTEM_PROMPT ?? ''
    personaSuffix: ''
- id: terminal-bash
  disabled: true
- id: terminal-pwsh
  disabled: true
- id: persistent-bash
  disabled: true
- id: persistent-pwsh
  disabled: true
- id: pty
  disabled: true
- id: subprocess
  disabled: true
- insert:
    - id: attachment-local
      name: '@deepseek-ai/dsh-attachment-local'
    - id: azurjuus-workspace
      name: NATIVE_PLUGIN_PATH
      config:
        endpoint: !!js process.env.AZURJUUS_TOOL_ENDPOINT
        token: !!js process.env.AZURJUUS_TOOL_TOKEN
        catalog: !!js JSON.parse(process.env.AZURJUUS_TOOL_CATALOG ?? '{}')
""".replace('NATIVE_PLUGIN_PATH', json.dumps((ROOT / 'kernel' / 'native' / 'workspace.mjs').as_uri())), encoding="utf-8")
        return patch

    def _environment(self):
        actions = [n for n in self.settings.get("_allowedActions", CATALOG) if n in CATALOG]
        return {
            "DEEPSEEK_API_KEY": str(self.settings.get("llmApiKey", "")),
            "DEEPSEEK_BASE_URL": str(self.settings.get("llmBaseUrl", "")).rstrip("/"),
            "DSH_SYSTEM_PROMPT": "\n".join(filter(None, [str(self.settings.get("_characterIdentity", "")), str(self.settings.get("_characterPolicy", ""))])),
            "AZURJUUS_TOOL_ENDPOINT": self.endpoint,
            "AZURJUUS_TOOL_TOKEN": self.token,
            "AZURJUUS_TOOL_CATALOG": json.dumps({name: CATALOG[name] for name in actions}),
            "DSH_MAX_TOKENS_AS_SUCCESS": "false",
        }

    async def start(self):
        if self.closed:
            raise DeepSeekHarnessUnavailable("DeepSeek Harness 会话已关闭。")
        if self._start_task is None:
            self._start_task = asyncio.create_task(self._start())
        try:
            await asyncio.shield(self._start_task)
        except BaseException:
            await self.close()
            raise
        if self.closed:
            raise DeepSeekHarnessUnavailable("DeepSeek Harness 会话已关闭。")

    async def _start(self):
        await self.stage("checking", "正在检查 DeepSeek Harness 运行环境")
        if not self.settings.get("llmBaseUrl"):
            raise DeepSeekHarnessUnavailable("未配置模型接口地址。")
        config = DeepSeekHarnessConfig(
            provider="deepseek-official", model=str(self.settings["llmModel"]),
            cwd=str(self.settings.get("_workspace", ROOT)), runtime_cwd=str(ROOT), profile="sdk-minimal",
            patches=(str(self._patch()),), dsh_home=str(self.home), env=self._environment(),
            api_key=str(self.settings.get("llmApiKey", "")), base_url=str(self.settings.get("llmBaseUrl", "")).rstrip("/"),
            initialize_timeout_seconds=90,
            request_timeout_seconds=float(self.settings.get("chatTimeoutSeconds", 120)),
        )
        self.harness = DeepSeekHarness(config)
        await self.stage("starting", "正在启动 DeepSeek Harness 内核")
        await asyncio.to_thread(self.harness.start)

    def _notify(self, notification):
        if notification.method != "session.event" or notification.payload.get("sessionId") != self.session_id:
            return
        event = (notification.payload or {}).get("event") or {}
        data = event.get("data") or {}
        if event.get("type") == "tool/call":
            call_id = str(data.get("callId", ""))
            self._tool_names[call_id] = str(data.get("name", ""))
            return "tool.start", {"name": self._tool_names[call_id], "callId": call_id}
        elif event.get("type") == "tool/result":
            message = data.get("message") or {}
            call_id = str(message.get("callId") or data.get("callId", ""))
            return "tool.complete", {"name": self._tool_names.pop(call_id, ""), "callId": call_id, "status": "failed" if message.get("isError") or data.get("isError") else "completed"}
        elif event.get("type") == "assistant/message":
            blocks = ((data.get("message") or {}).get("content") or [])
            text = "".join(str(b.get("text", "")) for b in blocks if isinstance(b, dict) and b.get("type") == "text")
            if text:
                return "message.delta", {"text": text}

    def _prompt_text(self, text, history):
        pieces = []
        if self._first_prompt:
            for item in (history or [])[1:]:
                pieces.append(("角色" if item.get("role") == "assistant" else "用户") + "：" + str(item.get("content", "")))
        pieces.append(text)
        return "\n\n".join(p for p in pieces if p)

    async def prompt(self, text, workspace, history=None):
        async with self._prompt_lock:
            return await self._prompt(text, workspace, history)

    async def _prompt(self, text, workspace, history):
        if self.closed:
            raise DeepSeekHarnessUnavailable("DeepSeek Harness 会话已关闭。")
        await self.start()
        if self.session_id is None:
            self.session_id = "azurjuus-" + self.home.parent.name + "-" + self.home.name
        await self.stage("generating", "正在等待模型回复")
        await self._event("message.start", {"sessionId": self.session_id})
        if self.closed:
            raise DeepSeekHarnessUnavailable("DeepSeek Harness 会话已关闭。")
        prompt_text = self._prompt_text(text, history)
        content = [{"type": "text", "text": prompt_text}]
        if self._first_prompt and self.settings.get("visionEnabled"):
            for filename in self.settings.get("_inputImages", []):
                path = Path(filename)
                content.append({"type": "image", "data": base64.b64encode(path.read_bytes()).decode("ascii"),
                    "mimeType": mimetypes.guess_type(path.name)[0] or "image/png"})
        loop = asyncio.get_running_loop()
        events = asyncio.Queue()
        # Session.run does not lazily restart the runtime. A queued SDK call
        # must fail after shutdown, rather than resurrecting a closed process.
        session = self.harness.start_session(self.session_id)
        self._received_messages.clear()

        def enqueue(event):
            if not self.closed and not loop.is_closed():
                loop.call_soon_threadsafe(events.put_nowait, event)

        def notify(notification):
            if self.closed or notification.method != 'session.event' or notification.payload.get('sessionId') != self.session_id:
                return
            native = (notification.payload or {}).get('event') or {}
            if native.get('type') == 'agent/inbox/spliced':
                enqueue(('_inbox', native.get('data') or {}))
            if native.get('type') in {'tool/call', 'tool/result', 'turn/start', 'turn/end'}:
                data = native.get('data') or {}
                message = data.get('message') or {}
                envelope = {'type': native['type'], 'seq': native.get('seq'), 'sessionId': self.session_id,
                            'callId': data.get('callId') or message.get('callId'),
                            'name': data.get('name'), 'isError': data.get('isError') or message.get('isError'),
                            'reason': data.get('reason')}
                enqueue(('runtime.native', envelope))
            event = self._notify(notification)
            if event is not None:
                enqueue(event)

        def run():
            try:
                return session.run(content, on_notification=notify)
            finally:
                if not loop.is_closed():
                    loop.call_soon_threadsafe(events.put_nowait, None)

        worker = asyncio.create_task(asyncio.to_thread(run))
        self._worker = worker
        try:
            while (event := await events.get()) is not None:
                if self.closed:
                    continue
                if event[0] == '_inbox':
                    self._received_messages.update(str(item['id']) for item in event[1].get('inserted', [])
                        if isinstance(item, dict) and item.get('id'))
                    await self._emit_steering_receipts()
                else:
                    await self._event(*event)
            result = await asyncio.shield(worker)
        except BaseException:
            # Cancelling to_thread only cancels its awaiter. Shutdown the SDK
            # and join the real call before the coordinator releases its token.
            await self.close()
            raise
        finally:
            if self._worker is worker:
                self._worker = None
        if self.closed:
            raise DeepSeekHarnessUnavailable("DeepSeek Harness 会话已关闭。")
        self._first_prompt = False
        reply = str(result.final_response or "").strip()
        if not reply:
            raise RuntimeError("模型返回了空响应（" + str(result.finish_reason or "unknown") + "）。")
        await self._event("message.complete", {"text": reply, "usage": {}, "status": "completed"})
        return reply

    async def _emit_steering_receipts(self):
        for message_id in self._received_messages & self._steering_messages.keys():
            instruction_id = self._steering_messages.pop(message_id)
            await self._event('runtime.steering', {'instructionId': instruction_id, 'messageId': message_id})

    async def steer(self, text, instruction_id):
        if self.closed or self._worker is None or self._worker.done():
            return {"queued": False}
        worker = asyncio.create_task(asyncio.to_thread(self.harness.client.session_prompt,
            self.session_id, [{"type": "text", "text": "用户补充要求：" + str(text)}]))
        self._steer_workers.add(worker)
        try:
            message_id = await asyncio.shield(worker)
            if self.closed:
                return {"queued": False}
            self._steering_messages[message_id] = instruction_id
            await self._emit_steering_receipts()
            return {"queued": True, "messageId": message_id}
        finally:
            # The shutdown owner also joins cancelled awaiters' SDK calls.
            if worker.done():
                self._steer_workers.discard(worker)

    async def close(self):
        self.closed = True
        if self._close_task is None:
            self._close_task = asyncio.create_task(self._close())
        await asyncio.shield(self._close_task)

    async def _close(self):
        if self._start_task:
            await asyncio.gather(self._start_task, return_exceptions=True)
        if self.harness:
            try:
                await asyncio.to_thread(self.harness.close)
            finally:
                pending = [*self._steer_workers]
                if self._worker:
                    pending.append(self._worker)
                if pending:
                    await asyncio.gather(*pending, return_exceptions=True)
                self.harness = None
        self._steer_workers.clear()
        self._steering_messages.clear()
        self._received_messages.clear()
        self._tool_names.clear()
        await self._event("runtime.stage", {"stage": "closed", "label": "DeepSeek Harness 会话已关闭"})
