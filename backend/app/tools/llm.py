from __future__ import annotations

import os
import asyncio
from dataclasses import dataclass
from typing import Any, AsyncIterator
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

import httpx
import json
from loguru import logger

DEFAULT_MODEL = "ernie-4.5-21b-a3b"
DEFAULT_API_URL = "https://qianfan.baidubce.com/v2/chat/completions"

# 定义重试规则：重试3次，退避时间 2s -> 4s -> 8s
retry_llm = retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=10),
    retry=retry_if_exception_type((httpx.HTTPStatusError, httpx.RequestError)),
    reraise=True
)

@dataclass
class LLMResponse:
    content: str


class QianfanChatAdapter:
    def __init__(
        self,
        *,
        api_url: str,
        bearer_token: str,
        model: str,
        temperature: float = 0,
        timeout: float = 120.0,
    ) -> None:
        self.api_url = api_url
        self.bearer_token = bearer_token
        self.model = model
        self.temperature = temperature
        self.timeout = timeout

    async def ainvoke(self, prompt: str) -> LLMResponse:
        if not self.bearer_token:
            raise RuntimeError("缺少 QIANFAN_BEARER_TOKEN，无法调用千帆模型")

        payload = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": self.temperature,
        }

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.bearer_token}",
        }

        async with httpx.AsyncClient(timeout=40.0, trust_env=False) as client:
            resp = await client.post(self.api_url, headers=headers, json=payload)
            resp.raise_for_status()
            data = resp.json()

        text = self._extract_content(data)
        return LLMResponse(content=self._normalize_text(text))

    @retry_llm
    async def astream(self, prompt: str) -> AsyncIterator[str]:
        """流式调用，内部植入重试逻辑，确保流式输出稳定。"""
        if not self.bearer_token:
            raise RuntimeError("缺少 QIANFAN_BEARER_TOKEN，无法调用千帆模型")

        max_retries = 2
        for attempt in range(max_retries + 1):
            try:
                payload = {
                    "model": self.model,
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": self.temperature,
                    "stream": True,
                }
                headers = {
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {self.bearer_token}",
                }

                # 显式使用较久的 read 超时
                timeout = httpx.Timeout(40.0, connect=10.0, read=40.0)
                async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
                    async with client.stream("POST", self.api_url, headers=headers, json=payload) as resp:
                        resp.raise_for_status()
                        async for line in resp.aiter_lines():
                            if not line.startswith("data: "):
                                continue
                            data = line[6:].strip()
                            if data == "[DONE]":
                                break
                            if not data:
                                continue
                            try:
                                chunk = json.loads(data)
                                delta = chunk.get("choices", [{}])[0].get("delta", {}).get("content", "")
                                if delta:
                                    yield delta
                            except:
                                continue
                return # 成功执行完毕，直接退出循环

            except Exception as e:
                logger.warning(f"[LLM] Qianfan 流式请求尝试 {attempt+1} 失败: {str(e)}")
                if attempt < max_retries:
                    await asyncio.sleep(2)
                else:
                    logger.error(f"[LLM] Qianfan 流式重试 3 次均失败，终止。")
                    raise e

    @staticmethod
    def _extract_content(data: dict[str, Any]) -> str:
        choices = data.get("choices") or []
        if not choices:
            raise RuntimeError(f"千帆返回缺少 choices: {data}")

        message = choices[0].get("message") or {}
        content = message.get("content", "")

        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts: list[str] = []
            for item in content:
                if isinstance(item, str):
                    parts.append(item)
                elif isinstance(item, dict):
                    text = item.get("text") or item.get("content") or ""
                    if isinstance(text, str):
                        parts.append(text)
            return "".join(parts)

        return str(content)

    @staticmethod
    def _normalize_text(text: str) -> str:
        text = text.strip()
        if text.startswith("```json") and text.endswith("```"):
            return text[7:-3].strip()
        if text.startswith("```") and text.endswith("```"):
            return text[3:-3].strip()
        return text


class LongcatChatAdapter:
    """兼容 OpenAI 格式的 Longcat/DeepSeek API 适配器"""
    def __init__(
        self,
        *,
        api_url: str,
        api_key: str,
        model: str,
        temperature: float = 0,
        timeout: float = 120.0,
    ) -> None:
        self.api_url = api_url
        self.api_key = api_key
        self.model = model
        self.temperature = temperature
        self.timeout = timeout

    async def ainvoke(self, prompt: str) -> LLMResponse:
        if not self.api_key:
            raise RuntimeError("缺少 LONGCAT_API_KEY，无法调用 Longcat 模型")

        payload = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": self.temperature,
            "max_tokens": 4096,
        }

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }

        async with httpx.AsyncClient(timeout=40.0, trust_env=False) as client:
            resp = await client.post(self.api_url, headers=headers, json=payload)
            resp.raise_for_status()
            data = resp.json()
            logger.debug(f"[LLM] Longcat ainvoke response: {json.dumps(data, ensure_ascii=False)[:300]}...")

        content = data.get("choices", [{}])[0].get("message", {}).get("content", "")
        return LLMResponse(content=self._normalize_text(content))

    @retry_llm
    async def astream(self, prompt: str) -> AsyncIterator[str]:
        if not self.api_key:
            raise RuntimeError("缺少 LONGCAT_API_KEY，无法调用 Longcat 模型")

        max_retries = 2
        for attempt in range(max_retries + 1):
            try:
                payload = {
                    "model": self.model,
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": self.temperature,
                    "stream": True,
                    "max_tokens": 4096,
                }
                headers = {
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {self.api_key}",
                }

                timeout = httpx.Timeout(40.0, connect=10.0, read=40.0)
                async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
                    async with client.stream("POST", self.api_url, headers=headers, json=payload) as resp:
                        resp.raise_for_status()
                        async for line in resp.aiter_lines():
                            if not line.startswith("data:"):
                                continue
                            # 适配 data: {"id"...} （缺少空格）以及 data: {"id"...}（包含空格）的情况
                            data = line[5:].strip()
                            if data.startswith(" "):
                                data = data.strip()
                            if data == "[DONE]":
                                break
                            if not data:
                                continue
                            try:
                                chunk = json.loads(data)
                                delta = chunk.get("choices", [{}])[0].get("delta", {})
                                content = delta.get("content", "")
                                if content:
                                    yield content
                            except Exception as e:
                                logger.error(f"[LLM] Longcat 解析 SSE chunk 异常: {e}, 原始数据: {data}")
                                continue
                return

            except Exception as e:
                logger.warning(f"[LLM] Longcat 流式请求尝试 {attempt+1} 失败: {str(e)}")
                if attempt < max_retries:
                    await asyncio.sleep(2)
                else:
                    logger.error(f"[LLM] Longcat 流式重试 3 次均失败，终止。")
                    raise e

    @staticmethod
    def _normalize_text(text: str) -> str:
        text = text.strip()
        if text.startswith("```json") and text.endswith("```"):
            return text[7:-3].strip()
        if text.startswith("```") and text.endswith("```"):
            return text[3:-3].strip()
        return text


def create_llm(*, temperature: float = 0, model: str | None = None, **kwargs: Any) -> QianfanChatAdapter | LongcatChatAdapter:
    """创建聊天补全适配器（根据 LLM_PROVIDER 动态分发）。"""
    provider = os.getenv("LLM_PROVIDER", "qianfan").strip().lower()
    timeout = float(kwargs.pop("timeout", 120.0))

    if provider == "longcat":
        api_url = (os.getenv("LONGCAT_BASE_URL") or "https://api.longcat.chat/openai/v1/chat/completions").strip()
        api_key = (os.getenv("LONGCAT_API_KEY") or "").strip()
        model_name = model or (os.getenv("LONGCAT_MODEL") or "deepseek-chat").strip()
        return LongcatChatAdapter(
            api_url=api_url,
            api_key=api_key,
            model=model_name,
            temperature=temperature,
            timeout=timeout,
        )
    else:
        api_url = (os.getenv("QIANFAN_BASE_URL") or DEFAULT_API_URL).strip()
        bearer_token = (os.getenv("QIANFAN_BEARER_TOKEN") or "").strip()
        model_name = model or (os.getenv("QIANFAN_MODEL") or DEFAULT_MODEL).strip()
        return QianfanChatAdapter(
            api_url=api_url,
            bearer_token=bearer_token,
            model=model_name,
            temperature=temperature,
            timeout=timeout,
        )
