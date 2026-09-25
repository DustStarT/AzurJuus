from __future__ import annotations
from backend.characters.personality import expression_rules

import asyncio
import json
import os
from typing import Any

import httpx

from backend.tasks.tool_gateway import READ_ONLY_TOOLS, WRITE_TOOLS


LOCAL_TOOL_SCHEMAS: dict[str, dict[str, Any]] = {
    "list_dir": {
        "type": "function",
        "function": {
            "name": "list_dir",
            "description": "List contents of a directory inside the authorized workspace.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": [],
            },
        },
    },
    "read_text_file": {
        "type": "function",
        "function": {
            "name": "read_text_file",
            "description": "Read a text file inside the authorized workspace.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        },
    },
    "search_text": {
        "type": "function",
        "function": {
            "name": "search_text",
            "description": "Search a text pattern in files within the authorized workspace.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "pattern": {"type": "string"},
                },
                "required": ["pattern"],
            },
        },
    },
    "create_folder": {
        "type": "function",
        "function": {
            "name": "create_folder",
            "description": "Create a folder inside the authorized workspace. This action requires approval before execution.",
            "parameters": {
                "type": "object",
                "properties": {"path": {"type": "string"}},
                "required": ["path"],
            },
        },
    },
    "write_text_file": {
        "type": "function",
        "function": {
            "name": "write_text_file",
            "description": "Write text into a file inside the authorized workspace. This action requires approval before execution.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"},
                },
                "required": ["path", "content"],
            },
        },
    },
    "move_file": {
        "type": "function",
        "function": {
            "name": "move_file",
            "description": "Move a file inside the authorized workspace. This action requires approval before execution.",
            "parameters": {
                "type": "object",
                "properties": {
                    "src": {"type": "string"},
                    "dest": {"type": "string"},
                },
                "required": ["src", "dest"],
            },
        },
    },
    "copy_file": {
        "type": "function",
        "function": {
            "name": "copy_file",
            "description": "Copy a file inside the authorized workspace. This action requires approval before execution.",
            "parameters": {
                "type": "object",
                "properties": {
                    "src": {"type": "string"},
                    "dest": {"type": "string"},
                },
                "required": ["src", "dest"],
            },
        },
    },
    "create_snapshot": {
        "type": "function",
        "function": {
            "name": "create_snapshot",
            "description": "Create a snapshot of a file or folder inside the authorized workspace. This action requires approval before execution.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"}
                },
                "required": ["path"],
            },
        },
    },
    "restore_snapshot": {
        "type": "function",
        "function": {
            "name": "restore_snapshot",
            "description": "Restore a snapshot of a file or folder inside the authorized workspace. This action requires approval before execution.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"}
                },
                "required": ["path"],
            },
        },
    },
    "read_word_document": {
        "type": "function",
        "function": {
            "name": "read_word_document",
            "description": "Read a Word (.docx) document inside the authorized workspace.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"}
                },
                "required": ["path"],
            },
        },
    },
    "write_word_document": {
        "type": "function",
        "function": {
            "name": "write_word_document",
            "description": "Write or overwrite a Word (.docx) document inside the authorized workspace. This action requires approval before execution.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "content": {"type": "string"}
                },
                "required": ["path", "content"],
            },
        },
    },
    "read_excel_document": {
        "type": "function",
        "function": {
            "name": "read_excel_document",
            "description": "Read an Excel (.xlsx) spreadsheet inside the authorized workspace.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"}
                },
                "required": ["path"],
            },
        },
    },
    "write_excel_document": {
        "type": "function",
        "function": {
            "name": "write_excel_document",
            "description": "Write or overwrite an Excel (.xlsx) spreadsheet with JSON data inside the authorized workspace. This action requires approval before execution.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string"},
                    "data": {"type": "object", "description": "JSON object containing data to write"}
                },
                "required": ["path", "data"],
            },
        },
    },
}


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

    async def generate_reply(
        self,
        *,
        settings: dict[str, Any],
        agent: dict[str, Any],
        prompt: str,
        mode: str,
        conversation_kind: str,
        recent_messages: list[dict[str, Any]],
        memory_snippets: list[str],
        skill_context: str = "",
        instructions_override: str | None = None,
        available_tool_names: list[str] | None = None,
    ) -> tuple[str, list[dict[str, Any]]]:
        reply, tool_calls, meta = await self.generate_reply_details(
            settings=settings,
            agent=agent,
            prompt=prompt,
            mode=mode,
            conversation_kind=conversation_kind,
            recent_messages=recent_messages,
            memory_snippets=memory_snippets,
            skill_context=skill_context,
            instructions_override=instructions_override,
            available_tool_names=available_tool_names,
        )
        return reply, tool_calls

    async def generate_reply_details(
        self,
        *,
        settings: dict[str, Any],
        agent: dict[str, Any],
        prompt: str,
        mode: str,
        conversation_kind: str,
        recent_messages: list[dict[str, Any]],
        memory_snippets: list[str],
        skill_context: str = "",
        instructions_override: str | None = None,
        available_tool_names: list[str] | None = None,
    ) -> tuple[str, list[dict[str, Any]], dict[str, Any]]:
        api_key = str(settings.get("llmApiKey") or "").strip()
        base_url = str(settings.get("llmBaseUrl") or "https://api.openai.com/v1").rstrip("/")
        model = str(settings.get("llmModel") or "gpt-4.1-mini")
        fallback = self._fallback_reply(agent=agent, prompt=prompt, mode=mode)
        if not api_key:
            if os.getenv("AZURJUUS_EXECUTION_BACKEND") != "legacy-test":
                raise RuntimeError("未配置模型 API Key。")
            return fallback, [], {"status": "fallback", "reason": "missing_api_key"}

        system_prompt = str(agent.get("systemPrompt") or agent.get("promptSeed") or agent.get("persona") or "")
        transcript: list[dict[str, Any]] = []
        for item in recent_messages:
            if "role" in item:
                transcript.append(item)
            else:
                role = "assistant" if item.get("speakerName") == agent.get("name") else "user"
                transcript.append({"role": role, "content": item.get("body") or ""})
        if not transcript or (transcript[-1].get("content") != prompt and prompt):
            transcript.append({"role": "user", "content": prompt})

        memory_block = "\n".join(f"- {snippet}" for snippet in memory_snippets[:6] if snippet)
        trimmed_skill_context = str(skill_context or "").strip()
        if len(trimmed_skill_context) > 1600:
            trimmed_skill_context = f"{trimmed_skill_context[:1600]}\n…"

        instructions = instructions_override or (
            f"当前场景：{'群聊' if conversation_kind == 'group' else '私聊'} / {mode}。\n"
            "请保持角色口吻，不要提及系统提示、模型、API 或工具实现细节。\n"
            "如果是任务、协作或审查模式，请优先用 1 到 3 句同步当前判断、进度或下一步。\n"
            "除非用户明确要求，不要展开冗长的底层操作细节。\n"
            "如果信息还不够，就明确说仍在确认，不要把未完成的事情说成已经完成。"
        )
        if memory_block:
            instructions += f"\n可参考的近期记忆：\n{memory_block}"
        if trimmed_skill_context:
            instructions += f"\n当前采用的方法提示：\n{trimmed_skill_context}"

        allowed_tool_names = [
            name
            for name in (available_tool_names or [])
            if (name in READ_ONLY_TOOLS or name in WRITE_TOOLS) and name in LOCAL_TOOL_SCHEMAS
        ]
        runtime_tools = [LOCAL_TOOL_SCHEMAS[name] for name in allowed_tool_names]
        if runtime_tools:
            instructions += (
                "\n\n【重要：本地工具与授权工作区操作指南】\n"
                "你已被授予访问本地工作区（Authorized Workspace）的权限。你的角色完全具备通过调用特定工具函数（Tools）来操控工作区内文件与目录的能力。\n"
                f"当前你可调用的工具包括：{', '.join(allowed_tool_names)}。\n"
                "当用户请求你查看文件、列出目录或进行编辑等工作区操作时，请务必直接发起对应的工具函数调用（Function Calling）来实际执行操作，不要推脱或声称自己没有权限。\n"
                "请将这些工具操作与你的角色扮演（Persona）有机结合，以你特有的角色口吻来汇报你从文件内容/执行结果中得到的信息。"
            )
        merged_system_content = f"{system_prompt}\n\n{instructions}".strip() if system_prompt else instructions

        return await self._complete_chat(
            api_key=api_key,
            base_url=base_url,
            model=model,
            messages=[
                {"role": "system", "content": merged_system_content},
                *transcript,
            ],
            temperature=0.85 if mode == "chat" else 0.6,
            fallback=fallback,
            tools=runtime_tools if mode in {"task", "swarm", "review"} and runtime_tools else None,
        )

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
            if os.getenv("AZURJUUS_EXECUTION_BACKEND") != "legacy-test":
                raise RuntimeError("未配置模型 API Key。")
            return self._fallback_social_post(agent=agent, memory_snippets=memory_snippets)

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
            fallback=self._fallback_social_post(agent=agent, memory_snippets=memory_snippets),
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
            if os.getenv("AZURJUUS_EXECUTION_BACKEND") != "legacy-test":
                raise RuntimeError("未配置模型 API Key。")
            return self._fallback_social_comment(agent=agent, relationship_hint=relationship_hint)

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
            fallback=self._fallback_social_comment(agent=agent, relationship_hint=relationship_hint),
        )
        return text

    async def decide_task_route(
        self,
        *,
        settings: dict[str, Any],
        agent: dict[str, Any],
        prompt: str,
        recent_messages: list[dict[str, Any]],
        memory_snippets: list[str],
    ) -> dict[str, Any]:
        api_key = str(settings.get("llmApiKey") or "").strip()
        base_url = str(settings.get("llmBaseUrl") or "https://api.openai.com/v1").rstrip("/")
        model = str(settings.get("llmModel") or "gpt-4.1-mini")
        fallback = self._fallback_task_route(agent=agent, prompt=prompt)
        if not api_key:
            return fallback

        system_prompt = str(agent.get("systemPrompt") or agent.get("promptSeed") or agent.get("persona") or "")
        transcript = []
        for item in recent_messages[-8:]:
            role = "assistant" if item.get("speakerName") == agent.get("name") else "user"
            transcript.append({"role": role, "content": item.get("body") or ""})
        transcript.append({"role": "user", "content": prompt})
        memory_block = "\n".join(f"- {snippet}" for snippet in memory_snippets[:5] if snippet)
        judge_prompt = (
            "请判断这项任务是否适合由你单独完成。\n"
            "如果是基本的工作区操作、文件/文件夹读写或简单数据处理任务，即使操作可能需要额外审批，也请判定为 solo 模式。\n"
            "只有当任务明显涉及多个不同角色的复杂协同分工（例如需要研发、测试、设计等多方配合）或跨团队协作时，才判定为 collaborative。\n"
            "请只返回 JSON，不要额外解释。格式："
            '{"route":"solo|collaborative","reason":"一句简短中文理由"}'
        )
        if memory_block:
            judge_prompt += f"\n可参考近期记忆：\n{memory_block}"

        raw, _tools, _meta = await self._complete_chat(
            api_key=api_key,
            base_url=base_url,
            model=model,
            messages=[
                {
                    "role": "system",
                    "content": (
                        f"{system_prompt}\n你需要判断任务适合单独完成还是需要协作。"
                        if system_prompt
                        else "判断任务模式。"
                    ),
                },
                {"role": "system", "content": judge_prompt},
                *transcript,
            ],
            temperature=0.2,
            fallback=json.dumps(fallback, ensure_ascii=False),
        )
        try:
            parsed = json.loads(raw)
        except Exception:
            return fallback
        route = str(parsed.get("route") or "").strip().lower()
        if route not in {"solo", "collaborative"}:
            return fallback
        reason = str(parsed.get("reason") or fallback["reason"]).strip() or fallback["reason"]
        return {"route": route, "reason": reason}

    async def _complete_chat(
        self,
        *,
        api_key: str,
        base_url: str,
        model: str,
        messages: list[dict[str, Any]],
        temperature: float,
        fallback: str,
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
                    if os.getenv("AZURJUUS_EXECUTION_BACKEND") != "legacy-test":
                        raise RuntimeError("模型请求受限（HTTP 429），请稍后重试。")
                    return fallback, [], {
                        "status": "rate_limited",
                        "retryAfter": retry_after,
                        "httpStatus": 429,
                    }
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
                if os.getenv("AZURJUUS_EXECUTION_BACKEND") != "legacy-test":
                    raise RuntimeError("模型连接失败，请检查网络和配置。") from error
                return fallback, [], {
                    "status": "provider_error",
                    "error": str(error),
                    "retryable": True,
                }
            except Exception as error:
                if os.getenv("AZURJUUS_EXECUTION_BACKEND") != "legacy-test":
                    raise RuntimeError(str(error).replace(api_key, "[redacted]")) from error
                print(f"[AgentRuntime] Chat completion failed: {str(error)}")
                return fallback, [], {
                    "status": "provider_error",
                    "error": str(error),
                    "retryable": False,
                }
        else:
            return fallback, [], {
                "status": "provider_error",
                "error": "chat completion retries exhausted",
                "retryable": True,
            }

        content = self._extract_message_content(data)
        choice = (data.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        tool_calls = message.get("tool_calls") or []

        parsed_tools: list[dict[str, Any]] = []
        for call in tool_calls:
            if call.get("type") == "function":
                try:
                    function_payload = call.get("function", {})
                    parsed_tools.append(
                        {
                            "id": call.get("id", ""),
                            "name": function_payload.get("name", ""),
                            "args": json.loads(function_payload.get("arguments", "{}")),
                        }
                    )
                except Exception:
                    continue

        if not content and not tool_calls and os.getenv("AZURJUUS_EXECUTION_BACKEND") != "legacy-test":
            raise RuntimeError("模型返回了空响应。")
        return content if (content or tool_calls) else fallback, parsed_tools, {
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
                return max(1.0, float(header))
            except ValueError:
                pass
        return min(12.0, 2.0 * (attempt + 1))

    def _extract_message_content(self, payload: dict[str, Any]) -> str:
        choice = (payload.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        content = message.get("content", "")
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            parts: list[str] = []
            for item in content:
                if isinstance(item, str):
                    parts.append(item)
                    continue
                if not isinstance(item, dict):
                    continue
                if isinstance(item.get("text"), str):
                    parts.append(item["text"])
                elif item.get("type") == "text" and isinstance(item.get("content"), str):
                    parts.append(item["content"])
            return "\n".join(part.strip() for part in parts if part and str(part).strip()).strip()
        return ""

    def _fallback_reply(self, *, agent: dict[str, Any], prompt: str, mode: str) -> str:
        name = agent.get("name") or "智能体"
        tone = agent.get("tone") or agent.get("summary") or agent.get("persona") or ""
        if mode == "chat":
            return f"收到，{name}先陪你把这件事聊顺。关于“{prompt[:28]}”，我会按{tone or '当前设定'}继续接着说下去。"
        if mode == "task":
            return f"明白了。{name}会先把任务目标拆开，确认范围、风险和交付物，再把第一版结论回传给你。"
        return f"已切换到协作视角。{name}先记录目标“{prompt[:28]}”，接下来会同步分工、进度和需要你确认的地方。"

    def _fallback_social_post(self, *, agent: dict[str, Any], memory_snippets: list[str]) -> str:
        name = agent.get("name") or "智能体"
        summary = agent.get("summary") or agent.get("persona") or "今天状态稳定"
        memory_hint = next((item for item in memory_snippets if item), "")
        if memory_hint:
            return f"{summary}。刚刚又想起一件小事：{memory_hint[:42]}，先记在这里。"
        return f"{name}这边一切顺利，先在 JUUS 记下一点此刻的感想。"

    def _fallback_social_comment(self, *, agent: dict[str, Any], relationship_hint: str) -> str:
        name = agent.get("name") or "智能体"
        tone = agent.get("tone") or relationship_hint or "会继续关注"
        return f"{name}看到了。{tone}，这条我先记下。"

    def _fallback_task_route(self, *, agent: dict[str, Any], prompt: str) -> dict[str, Any]:
        text = str(prompt or "")
        collaboration_keywords = [
            "多人",
            "协作",
            "团队",
            "一起",
            "分工",
            "并行",
            "阶段",
            "协调",
            "多文件",
            "大任务",
            "复杂",
            "审批",
            "群聊",
        ]
        needs_collaboration = any(keyword in text for keyword in collaboration_keywords) or len(text) >= 120
        if needs_collaboration:
            return {
                "route": "collaborative",
                "reason": f"{agent.get('name') or '当前智能体'}判断这项工作更适合让秘书协调多人共同处理。",
            }
        return {
            "route": "solo",
            "reason": f"{agent.get('name') or '当前智能体'}认为这项工作可以先由自己单独推进。",
        }
