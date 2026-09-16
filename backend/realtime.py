from __future__ import annotations

import asyncio
import contextlib
import json
from socket import create_connection
from collections import deque
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

from fastapi import WebSocket

try:  # pragma: no cover - optional dependency wiring
    from redis.asyncio import Redis
except Exception:  # pragma: no cover - optional dependency wiring
    Redis = None


def now_iso() -> str:
    return datetime.now(UTC).isoformat().replace("+00:00", "Z")


class RealtimeHub:
    def __init__(self, redis_url: str | None = None, channel: str = "azurjuus.events", history_limit: int = 256):
        self._connections: set[WebSocket] = set()
        self._redis_url = redis_url
        self._channel = channel
        self._redis: Redis | None = None
        self._listener_task: asyncio.Task | None = None
        self._instance_id = f"hub-{id(self)}"
        self._history: deque[dict[str, Any]] = deque(maxlen=max(32, history_limit))
        self._cursor = 0
        self._lock = asyncio.Lock()

    @property
    def last_cursor(self) -> int:
        return self._cursor

    async def start(self) -> None:
        if not self._redis_url or Redis is None:
            return
        if not self._redis_endpoint_reachable():
            return
        try:
            self._redis = Redis.from_url(self._redis_url, decode_responses=True)
            await self._redis.ping()
            self._listener_task = asyncio.create_task(self._listen())
        except Exception:
            self._redis = None
            self._listener_task = None

    def _redis_endpoint_reachable(self, timeout: float = 0.2) -> bool:
        parsed = urlsplit(self._redis_url or "")
        if parsed.scheme not in {"redis", "rediss"}:
            return True
        if parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
            return True
        try:
            with create_connection((parsed.hostname, parsed.port or 6379), timeout=timeout):
                return True
        except OSError:
            return False

    async def stop(self) -> None:
        if self._listener_task:
            self._listener_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._listener_task
        if self._redis is not None:
            await self._redis.close()
            self._redis = None

    async def connect(self, websocket: WebSocket) -> None:
        await websocket.accept()
        self._connections.add(websocket)

    async def disconnect(self, websocket: WebSocket) -> None:
        self._connections.discard(websocket)

    async def replay(self, websocket: WebSocket, after_cursor: int | None = None) -> None:
        if after_cursor is None or after_cursor < 0:
            return
        async with self._lock:
            backlog = [dict(item) for item in self._history if int(item.get("cursor") or 0) > after_cursor]
        for envelope in backlog:
            await websocket.send_json(envelope)

    async def publish(self, event_type: str, payload: dict[str, Any]) -> dict[str, Any]:
        envelope = await self._record_envelope(
            {
                "event": event_type,
                "payload": payload,
                "source": self._instance_id,
            }
        )
        await self._broadcast(envelope)
        if self._redis is not None:
            try:
                await self._redis.publish(self._channel, json.dumps(envelope, ensure_ascii=False))
            except Exception:
                pass
        return envelope

    async def _record_envelope(self, envelope: dict[str, Any]) -> dict[str, Any]:
        async with self._lock:
            self._cursor += 1
            stamped = {
                **envelope,
                "cursor": self._cursor,
                "emittedAt": now_iso(),
            }
            self._history.append(stamped)
        return stamped

    async def _broadcast(self, envelope: dict[str, Any]) -> None:
        dead: list[WebSocket] = []
        for websocket in list(self._connections):
            try:
                await websocket.send_json(envelope)
            except Exception:
                dead.append(websocket)
        for websocket in dead:
            self._connections.discard(websocket)

    async def _listen(self) -> None:
        if self._redis is None:
            return
        pubsub = self._redis.pubsub()
        await pubsub.subscribe(self._channel)
        try:
            async for message in pubsub.listen():
                if message.get("type") != "message":
                    continue
                raw = message.get("data")
                if not raw:
                    continue
                try:
                    envelope = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if envelope.get("source") == self._instance_id:
                    continue
                stamped = await self._record_envelope(
                    {
                        "event": envelope.get("event") or "runtime.external",
                        "payload": envelope.get("payload") or {},
                        "source": envelope.get("source") or "external",
                    }
                )
                await self._broadcast(stamped)
        finally:
            await pubsub.close()
