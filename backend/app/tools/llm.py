from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Any, AsyncIterator

import httpx
import json


DEFAULT_MODEL = "ernie-4.5-21b-a3b"
DEFAULT_API_URL = "https://qianfan.baidubce.com/v2/chat/completions"


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

    async def astream(self, prompt: str) -> AsyncIterator[str]:
        """流式调用，返回文本块（yield chunk）。"""
        if not self.bearer_token:
            raise RuntimeError("缺少 QIANFAN_BEARER_TOKEN，无法调用千帆模型")

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

        async with httpx.AsyncClient(timeout=self.timeout, trust_env=False) as client:
            async with client.stream("POST", self.api_url, headers=headers, json=payload) as resp:
                resp.raise_for_status()
                async for line in resp.aiter_lines():
                    if not line.startswith("data: "):
                        continue
                    data = line[6:]
                    if data.strip() in ("[DONE]", ""):
                        break
                    try:
                        chunk = json.loads(data)
                        delta = chunk.get("choices", [{}])[0].get("delta", {}).get("content", "")
                        if delta:
                            yield delta
                    except:
                        continue

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


def create_llm(*, temperature: float = 0, model: str = DEFAULT_MODEL, **kwargs: Any) -> QianfanChatAdapter:
    """创建百度千帆聊天补全适配器。"""
    api_url = (os.getenv("QIANFAN_BASE_URL") or DEFAULT_API_URL).strip()
    bearer_token = (os.getenv("QIANFAN_BEARER_TOKEN") or "").strip()
    model_name = (os.getenv("QIANFAN_MODEL") or model).strip() or DEFAULT_MODEL
    timeout = float(kwargs.pop("timeout", 120.0))

    return QianfanChatAdapter(
        api_url=api_url,
        bearer_token=bearer_token,
        model=model_name,
        temperature=temperature,
        timeout=timeout,
    )
