"""LLM 调用层。

对外只暴露 4 个方法（json / text / stream / with_tools），业务节点不直接碰 SDK。
- 真实路径：任意 OpenAI 兼容接口（OpenAI / DeepSeek / Qwen / Ollama …）
- 离线路径：LLM_PROVIDER=mock 时走 app.llm_mock 的关键词桩，保证无 key 也能跑通全链路
"""

from __future__ import annotations

import json
import logging
import re
from collections.abc import AsyncIterator
from typing import Any

from openai import AsyncOpenAI

from app import llm_mock
from app.config import Settings, get_settings

logger = logging.getLogger(__name__)

#: 任务标签 → (用哪个模型, 用哪个温度)
_CLASSIFY_TASKS = frozenset({"intent", "tool_decision", "turn_eval"})

_JSON_FENCE_RE = re.compile(r"```(?:json)?\s*(.*?)```", re.S)


class LLMError(RuntimeError):
    """LLM 调用失败。节点层会降级处理，不让整通对话挂掉。"""


def _extract_json(raw: str) -> dict[str, Any]:
    """从模型输出里抠出 JSON。容忍 markdown 围栏和前后废话。"""
    text = raw.strip()

    fence = _JSON_FENCE_RE.search(text)
    if fence:
        text = fence.group(1).strip()

    try:
        parsed = json.loads(text)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass

    # 退一步：取第一个 { 到最后一个 } 之间的内容
    start, end = text.find("{"), text.rfind("}")
    if start != -1 and end > start:
        try:
            parsed = json.loads(text[start : end + 1])
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass

    raise LLMError(f"模型未返回合法 JSON：{raw[:200]}")


class LLMClient:
    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self._client: AsyncOpenAI | None = None

    # ------------------------------------------------------------ 内部

    @property
    def client(self) -> AsyncOpenAI:
        if self._client is None:
            if not self.settings.llm_api_key and not self.settings.is_mock_llm:
                raise LLMError("未配置 LLM_API_KEY")
            self._client = AsyncOpenAI(
                base_url=self.settings.llm_base_url,
                api_key=self.settings.llm_api_key or "not-needed",
                timeout=60.0,
                max_retries=2,
            )
        return self._client

    def _model_for(self, task: str) -> str:
        return self.settings.classifier_model if task in _CLASSIFY_TASKS else self.settings.llm_model

    def _temperature_for(self, task: str, override: float | None) -> float:
        if override is not None:
            return override
        return (
            self.settings.llm_temperature_classify
            if task in _CLASSIFY_TASKS
            else self.settings.llm_temperature_generate
        )

    @property
    def is_mock(self) -> bool:
        return self.settings.is_mock_llm

    # ------------------------------------------------------------ 结构化输出

    async def json(
        self,
        *,
        task: str,
        system: str,
        user: str,
        hints: dict[str, Any] | None = None,
        temperature: float | None = None,
        max_retries: int = 2,
    ) -> dict[str, Any]:
        """要求模型输出 JSON。失败会重试，最终失败抛 LLMError。"""
        if self.is_mock:
            return self._mock_json(task, hints or {})

        messages = [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ]
        last_error: Exception | None = None

        for attempt in range(max_retries + 1):
            try:
                resp = await self.client.chat.completions.create(
                    model=self._model_for(task),
                    messages=messages,
                    temperature=self._temperature_for(task, temperature),
                    response_format={"type": "json_object"},
                )
                content = resp.choices[0].message.content or ""
                return _extract_json(content)
            except LLMError as exc:
                last_error = exc
            except Exception as exc:  # noqa: BLE001 — 网络/限流/模型不支持 response_format 都在这
                last_error = exc
                logger.warning("LLM json 调用失败 task=%s attempt=%d: %s", task, attempt, exc)

            if attempt < max_retries:
                # 重试时把「只要 JSON」再强调一遍，并降级掉 response_format 依赖
                messages[0]["content"] = system + "\n\n注意：只输出一个 JSON 对象，不要任何其他文字。"
                continue

        raise LLMError(f"task={task} 连续 {max_retries + 1} 次未拿到合法 JSON：{last_error}")

    async def json_safe(
        self,
        *,
        task: str,
        system: str,
        user: str,
        fallback: dict[str, Any],
        hints: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """json() 的容错版：失败返回 fallback，绝不打断对话。"""
        try:
            return await self.json(task=task, system=system, user=user, hints=hints)
        except LLMError as exc:
            logger.error("LLM json_safe 降级 task=%s: %s", task, exc)
            return dict(fallback)

    # ------------------------------------------------------------ 纯文本

    async def text(
        self,
        *,
        task: str,
        system: str,
        user: str,
        hints: dict[str, Any] | None = None,
        temperature: float | None = None,
    ) -> str:
        if self.is_mock:
            return self._mock_text(task, user, hints or {})

        resp = await self.client.chat.completions.create(
            model=self._model_for(task),
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            temperature=self._temperature_for(task, temperature),
        )
        return (resp.choices[0].message.content or "").strip()

    # ------------------------------------------------------------ 流式

    async def stream(
        self,
        *,
        task: str,
        system: str,
        user: str,
        hints: dict[str, Any] | None = None,
        temperature: float | None = None,
    ) -> AsyncIterator[str]:
        """逐字产出。mock 模式下把整段话按标点切块模拟流式。"""
        if self.is_mock:
            full = self._mock_text(task, user, hints or {})
            for piece in _chunk_for_stream(full):
                yield piece
            return

        stream = await self.client.chat.completions.create(
            model=self._model_for(task),
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            temperature=self._temperature_for(task, temperature),
            stream=True,
        )
        async for chunk in stream:
            if not chunk.choices:
                continue
            delta = chunk.choices[0].delta
            if delta and delta.content:
                yield delta.content

    # ------------------------------------------------------------ 原生 function calling

    async def with_tools(
        self,
        *,
        system: str,
        user: str,
        tools: list[dict[str, Any]],
        hints: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """原生工具调用通道。

        返回 {"tool_calls": [{"name":..., "args": {...}}], "text": "..."}。
        模型不支持 tools 参数时自动降级到 JSON 决策通道。
        """
        if self.is_mock:
            return self._mock_tools(hints or {})

        try:
            resp = await self.client.chat.completions.create(
                model=self._model_for("tool_decision"),
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
                temperature=0.0,
                tools=tools,
                tool_choice="auto",
            )
        except Exception as exc:  # noqa: BLE001 — 有些兼容端点不认 tools
            logger.warning("模型不支持原生 function calling，降级到 JSON 决策通道：%s", exc)
            return await self._json_tool_fallback(system=system, user=user, hints=hints)

        msg = resp.choices[0].message
        calls: list[dict[str, Any]] = []
        for call in msg.tool_calls or []:
            try:
                args = json.loads(call.function.arguments or "{}")
            except json.JSONDecodeError:
                logger.warning("工具参数不是合法 JSON，已忽略：%s", call.function.arguments)
                continue
            calls.append({"name": call.function.name, "args": args})

        return {"tool_calls": calls, "text": (msg.content or "").strip(), "channel": "native"}

    async def _json_tool_fallback(
        self, *, system: str, user: str, hints: dict[str, Any] | None
    ) -> dict[str, Any]:
        from app.graph.prompts import TOOL_DECISION_SYSTEM

        decision = await self.json_safe(
            task="tool_decision",
            system=TOOL_DECISION_SYSTEM,
            user=user,
            fallback={"tool": None, "args": {}, "thought": "决策失败，跳过工具"},
            hints=hints,
        )
        name = decision.get("tool")
        if not name or name == "null":
            return {"tool_calls": [], "text": "", "channel": "json_fallback"}
        return {
            "tool_calls": [{"name": name, "args": decision.get("args") or {}}],
            "text": decision.get("thought", ""),
            "channel": "json_fallback",
        }

    # ------------------------------------------------------------ mock 分发

    def _mock_json(self, task: str, hints: dict[str, Any]) -> dict[str, Any]:
        question = hints.get("question", "")
        if task == "intent":
            return llm_mock.classify_intent(question, hints.get("history"))
        if task == "tool_decision":
            return llm_mock.decide_tool(
                hints.get("intent", "chitchat"), hints.get("slots") or {}, question
            )
        if task == "turn_eval":
            return llm_mock.evaluate_turn(question, hints.get("answer", ""))
        raise LLMError(f"mock 未实现的 JSON 任务：{task}")

    def _mock_text(self, task: str, user_prompt: str, hints: dict[str, Any]) -> str:
        if task == "respond":
            return llm_mock.compose_reply(user_prompt)
        if task == "escalation_notice":
            return llm_mock.escalation_reply()
        raise LLMError(f"mock 未实现的文本任务：{task}")

    def _mock_tools(self, hints: dict[str, Any]) -> dict[str, Any]:
        decision = llm_mock.decide_tool(
            hints.get("intent", "chitchat"), hints.get("slots") or {}, hints.get("question", "")
        )
        name = decision.get("tool")
        calls = [{"name": name, "args": decision.get("args") or {}}] if name else []
        return {"tool_calls": calls, "text": decision.get("thought", ""), "channel": "mock"}


def _chunk_for_stream(text: str, group: int = 2) -> list[str]:
    """把整段文本切成小块，模拟逐字输出。按标点断句后每 group 个字符一块。"""
    if not text:
        return []
    pieces: list[str] = []
    for segment in re.findall(r"[^，。！？；\n]*[，。！？；\n]?|\n", text):
        if not segment:
            continue
        for i in range(0, len(segment), group):
            pieces.append(segment[i : i + group])
    return pieces or [text]


_client: LLMClient | None = None


def get_llm() -> LLMClient:
    global _client
    if _client is None:
        _client = LLMClient()
    return _client
