from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from socket import create_connection
from typing import Any
from urllib.parse import urlsplit

try:  # pragma: no cover - optional dependency wiring
    import chromadb
except Exception:  # pragma: no cover - optional dependency wiring
    chromadb = None


@dataclass
class MemoryHit:
    text: str
    metadata: dict[str, Any]


class MemoryStore:
    def __init__(self, chroma_url: str, collection_name: str, path, enabled: bool = True):
        self._enabled = enabled
        self._recent = deque(maxlen=512)
        self._collection = None
        self._collection_name = collection_name
        self._chroma_url = chroma_url
        self._path = path

    def start(self) -> None:
        if not self._enabled or chromadb is None or not self._chroma_url:
            return
        try:
            if self._chroma_url.startswith("http"):
                parsed = urlsplit(self._chroma_url)
                host = parsed.hostname or "127.0.0.1"
                port = parsed.port or (443 if parsed.scheme == "https" else 80)
                if not self._http_endpoint_reachable(host, port):
                    return
                client = chromadb.HttpClient(host=host, port=port)
            else:
                self._path.mkdir(parents=True, exist_ok=True)
                client = chromadb.PersistentClient(path=str(self._path))
            self._collection = client.get_or_create_collection(name=self._collection_name)
        except Exception:
            self._collection = None

    def _http_endpoint_reachable(self, host: str, port: int, timeout: float = 0.2) -> bool:
        try:
            with create_connection((host, port), timeout=timeout):
                return True
        except OSError:
            return False

    def add(self, memory_id: str, text: str, metadata: dict[str, Any]) -> None:
        text = str(text or "").strip()
        if not text:
            return
        payload = {**metadata}
        self._recent.appendleft(MemoryHit(text=text, metadata=payload))
        if self._collection is None:
            return
        try:
            self._collection.add(ids=[memory_id], documents=[text], metadatas=[payload])
        except Exception:
            pass

    def query(self, text: str, actor_id: str | None = None, conversation_id: str | None = None, limit: int = 4) -> list[MemoryHit]:
        if self._collection is not None:
            where: dict[str, Any] = {}
            if actor_id:
                where["actor_id"] = actor_id
            if conversation_id:
                where["conversation_id"] = conversation_id
            try:
                result = self._collection.query(query_texts=[text], n_results=limit, where=where or None)
                docs = result.get("documents", [[]])[0]
                metas = result.get("metadatas", [[]])[0]
                return [MemoryHit(text=doc, metadata=meta or {}) for doc, meta in zip(docs, metas)]
            except Exception:
                pass

        hits: list[MemoryHit] = []
        lowered = str(text or "").lower()
        for item in self._recent:
            if actor_id and item.metadata.get("actor_id") != actor_id:
                continue
            if conversation_id and item.metadata.get("conversation_id") != conversation_id:
                continue
            if lowered and lowered not in item.text.lower():
                continue
            hits.append(item)
            if len(hits) >= limit:
                break
        if hits:
            return hits
        fallback = []
        for item in self._recent:
            if actor_id and item.metadata.get("actor_id") != actor_id:
                continue
            if conversation_id and item.metadata.get("conversation_id") != conversation_id:
                continue
            fallback.append(item)
            if len(fallback) >= limit:
                break
        return fallback

    def clear(self) -> None:
        self._recent.clear()
        if self._collection is None:
            return
        try:
            ids = self._collection.get().get("ids") or []
            if ids:
                self._collection.delete(ids=ids)
        except Exception:
            pass
