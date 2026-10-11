from __future__ import annotations
from backend.characters.personality import expression_rules

import asyncio
import json
import math
from typing import Any

import httpx
from backend.tasks.model_output import completion_message, text_content, function_calls

class AgentRuntime:
    def __init__(
        self,
        timeout_seconds: float = 45.0,
        *,
        max_concurrent_requests: int = 2,
        max_retry_attempts: int = 2,
    ):
        self._timeout = timeout_seconds
        self._request_gate = asyncio.Semaphore(max(1, max_concurrent_requests))
        self._max_retry_attempts = max(0, max_retry_attempts)

    async def generate_social_post(
        self,
        *,
        settings: dict[str, Any],
        agent: dict[str, Any],
        prompt: str,
        memory_snippets: list[str],
        skill_context: str = "",
    ) -> str:
        api_key = str(settings.get("llmApiKey") or "").strip()
        base_url = str(settings.get("llmBaseUrl") or "https://api.openai.com/v1").rstrip("/")
        model = str(settings.get("llmModel") or "gpt-4.1-mini")
        if not api_key:
            raise RuntimeError("未配置模型 API Key。")

        system_prompt = str(agent.get("systemPrompt") or agent.get("promptSeed") or agent.get("persona") or "")
        merged_system = (
            f"{system_prompt}\n你正在发布一条 JUUS 动态，请保持角色口吻，简洁自然。"
            if system_prompt
            else "你正在发布一条 JUUS 动态，请保持角色口吻，简洁自然。"
        )

        text, _tools, _meta = await self._complete_chat(
            api_key=api_key,
            base_url=base_url,
            model=model,
            messages=[
                {"role": "system", "content": merged_system + expression_rules("chat")},
                *([{"role": "system", "content": skill_context[:1000]}] if skill_context else []),
                {"role": "user", "content": prompt},
            ],
            temperature=0.95,
        )
        return text

    async def generate_social_comment(
        self,
        *,
        settings: dict[str, Any],
        agent: dict[str, Any],
        prompt: str,
        relationship_hint: str,
        skill_context: str = "",
    ) -> str:
        api_key = str(settings.get("llmApiKey") or "").strip()
        base_url = str(settings.get("llmBaseUrl") or "https://api.openai.com/v1").rstrip("/")
        model = str(settings.get("llmModel") or "gpt-4.1-mini")
        if not api_key:
            raise RuntimeError("未配置模型 API Key。")

        system_prompt = str(agent.get("systemPrompt") or agent.get("promptSeed") or agent.get("persona") or "")
        merged_system = (
            f"{system_prompt}\n你正在为其他角色的 JUUS 动态发表评论，请保持角色口吻。"
            if system_prompt
            else "你正在为其他角色的 JUUS 动态发表评论，请保持角色口吻。"
        )

        text, _tools, _meta = await self._complete_chat(
            api_key=api_key,
            base_url=base_url,
            model=model,
            messages=[
                {"role": "system", "content": merged_system + expression_rules("chat")},
                {"role": "system", "content": f"关系提示：{relationship_hint or '普通同伴'}。"},
                *([{"role": "system", "content": skill_context[:1000]}] if skill_context else []),
                {"role": "user", "content": prompt},
            ],
            temperature=0.9,
        )
        return text

    async def _complete_chat(
        self,
        *,
        api_key: str,
        base_url: str,
        model: str,
        messages: list[dict[str, Any]],
        temperature: float,
        tools: list[dict[str, Any]] | None = None,
    ) -> tuple[str, list[dict[str, Any]], dict[str, Any]]:
        trimmed_messages = self._trim_messages(messages)
        payload: dict[str, Any] = {
            "model": model,
            "messages": trimmed_messages,
            "temperature": temperature,
        }
        if tools:
            payload["tools"] = tools

        response: httpx.Response | None = None
        for attempt in range(self._max_retry_attempts + 1):
            try:
                async with self._request_gate:
                    async with httpx.AsyncClient(timeout=self._timeout) as client:
                        response = await client.post(
                            f"{base_url}/chat/completions",
                            headers={
                                "Authorization": f"Bearer {api_key}",
                                "Content-Type": "application/json",
                            },
                            content=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                        )
                if response.status_code == 429:
                    retry_after = self._retry_after_seconds(response, attempt)
                    if attempt < self._max_retry_attempts:
                        await asyncio.sleep(retry_after)
                        continue
                    raise RuntimeError("模型请求受限（HTTP 429），请稍后重试。")
                if response.status_code >= 500 and attempt < self._max_retry_attempts:
                    await asyncio.sleep(self._retry_after_seconds(response, attempt))
                    continue
                if response.status_code != 200:
                    print(f"[AgentRuntime] LLM API Error: HTTP {response.status_code}")
                response.raise_for_status()
                data = response.json()
                break
            except (httpx.ConnectError, httpx.ReadTimeout, httpx.RemoteProtocolError) as error:
                if attempt < self._max_retry_attempts:
                    await asyncio.sleep(1.5 * (attempt + 1))
                    continue
                print(f"[AgentRuntime] Chat completion failed: {str(error)}")
                raise RuntimeError("模型连接失败，请检查网络和配置。") from error
            except Exception as error:
                raise RuntimeError(str(error).replace(api_key, "[redacted]") if api_key else str(error)) from error
        else:
            raise RuntimeError("模型请求重试次数耗尽。")

        message = completion_message(data)
        content = text_content(message.get('content'))
        tool_calls = message.get("tool_calls") or []

        parsed_tools = function_calls(tool_calls)

        if not content and not tool_calls:
            raise RuntimeError("模型返回了空响应。")
        return content, parsed_tools, {
            "status": "ok",
            "httpStatus": response.status_code if response is not None else 200,
            "raw_tool_calls": tool_calls,
        }

    def _trim_messages(self, messages: list[dict[str, Any]], char_budget: int = 9000) -> list[dict[str, Any]]:
        # Tool requests and their results form an indivisible protocol unit.
        # Keeping a tool result after dropping its empty assistant call is invalid.
        if not messages:
            return []
        system = [m for m in messages if m.get("role") == "system"]
        groups: list[list[dict[str, Any]]] = []
        for message in messages:
            if message.get("role") == "system":
                continue
            if message.get("role") == "tool":
                ids = {c.get("id") for c in groups[-1][0].get("tool_calls", [])} if groups else set()
                if message.get("tool_call_id") in ids:
                    groups[-1].append(message)
            else:
                groups.append([message])
        used = sum(len(json.dumps(m, ensure_ascii=False)) for m in system)
        kept = []
        for group in reversed(groups):
            size = len(json.dumps(group, ensure_ascii=False))
            if kept and used + size > char_budget:
                break
            kept.insert(0, group)
            used += size
        return [*system, *(m for group in kept for m in group)]


    def _normalize_message_content(self, content: Any) -> str:
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return self._extract_message_content({"choices": [{"message": {"content": content}}]})
        return str(content or "")

    def _retry_after_seconds(self, response: httpx.Response | None, attempt: int) -> float:
        header = ""
        if response is not None:
            header = str(response.headers.get("retry-after") or "").strip()
        if header:
            try:
                seconds = float(header)
                if math.isfinite(seconds):
                    return min(30.0, max(1.0, seconds))
            except ValueError:
                pass
        return min(12.0, 2.0 * (attempt + 1))

    def _extract_message_content(self, payload: dict[str, Any]) -> str:
        return text_content(completion_message(payload).get('content'))
