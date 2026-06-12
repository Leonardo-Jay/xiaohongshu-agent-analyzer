from __future__ import annotations

import os
import asyncio
import ipaddress
import re
from dataclasses import dataclass, field
from typing import Any, AsyncIterator
from urllib.parse import urlparse
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

import httpx
import json
from loguru import logger

DEFAULT_MODEL = "ernie-4.5-21b-a3b"
DEFAULT_API_URL = "https://qianfan.baidubce.com/v2/chat/completions"

BUILTIN_LLM_PROVIDERS: dict[str, dict[str, Any]] = {
    "qianfan": {
        "api_type": "openai-compatible",
        "api_url": "https://qianfan.baidubce.com/v2/chat/completions",
        "api_key_env": "QIANFAN_BEARER_TOKEN",
        "models": [
            "deepseek-v4-pro",
            "deepseek-v4-flash",
            "deepseek-v3.2",
            "ernie-lite-pro-128k",
            "ernie-speed-pro-128k",
            "ernie-4.5-turbo-128k",
            "qwen3-coder-30b-a3b-instruct",
            "qwen3-30b-a3b",
            "qwen3-14b",
        ],
    },
    "longcat": {
        "api_type": "openai-compatible",
        "api_url": "https://api.longcat.chat/openai/v1/chat/completions",
        "api_key_env": "LONGCAT_API_KEY",
        "models": ["LongCat-2.0-Preview"],
    },
    "modelscope": {
        "api_type": "openai-compatible",
        "api_url": "https://api-inference.modelscope.cn/v1",
        "api_key_env": "MODELSCOPE_API_KEY",
        "models": [
            "moonshotai/Kimi-K2.6",
            "MiniMax/MiniMax-M2.7",
            "ZhipuAI/GLM-4.7-Flash",
            "deepseek-ai/DeepSeek-R1-Distill-Qwen-32B",
        ],
    },
}

_TRUE_VALUES = {"1", "true", "yes", "on"}
_SUPPORTED_CUSTOM_API_TYPES = {"openai-compatible", "anthropic-compatible"}

# 定义重试规则：重试3次，退避时间 2s -> 4s -> 8s
retry_llm = retry(
    stop=stop_after_attempt(3),
    wait=wait_exponential(multiplier=1, min=2, max=10),
    retry=retry_if_exception_type((httpx.HTTPStatusError, httpx.RequestError)),
    reraise=True
)


@dataclass
class ToolCall:
    id: str
    name: str
    arguments: dict[str, Any]


@dataclass
class LLMResponse:
    content: str
    tool_calls: list[ToolCall] | None = field(default=None)
    finish_reason: str = "stop"


class LLMConfigError(ValueError):
    """User-facing validation error for request-level LLM config."""


def _normalize_text(text: str) -> str:
    text = text.strip()
    if text.startswith("```json") and text.endswith("```"):
        return text[7:-3].strip()
    if text.startswith("```") and text.endswith("```"):
        return text[3:-3].strip()
    return text


def _normalize_openai_chat_url(api_url: str) -> str:
    value = str(api_url or "").strip().rstrip("/")
    if value.endswith("/chat/completions"):
        return value
    return f"{value}/chat/completions"


def _is_private_host(hostname: str) -> bool:
    host = (hostname or "").strip().lower()
    if host in {"localhost", "127.0.0.1", "::1"}:
        return True
    try:
        addr = ipaddress.ip_address(host)
        return bool(addr.is_private or addr.is_loopback or addr.is_link_local)
    except ValueError:
        return False


def _validate_custom_url(api_url: str) -> str:
    value = str(api_url or "").strip()
    if not value:
        raise LLMConfigError("自定义接口 URL 不能为空")
    parsed = urlparse(value)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise LLMConfigError("自定义接口 URL 必须是合法的 HTTP/HTTPS 地址")
    if parsed.username or parsed.password:
        raise LLMConfigError("自定义接口 URL 不能包含用户名或密码")
    if parsed.fragment:
        raise LLMConfigError("自定义接口 URL 不能包含 fragment")
    allow_private = os.getenv("ALLOW_PRIVATE_LLM_ENDPOINTS", "").strip().lower() in _TRUE_VALUES
    if parsed.scheme != "https" and not allow_private:
        raise LLMConfigError("自定义接口默认必须使用 HTTPS；本地私有接口需开启 ALLOW_PRIVATE_LLM_ENDPOINTS")
    if _is_private_host(parsed.hostname or "") and not allow_private:
        raise LLMConfigError("默认不允许请求本地或内网 LLM 地址；如确有需要请开启 ALLOW_PRIVATE_LLM_ENDPOINTS")
    return value.rstrip("/")


def sanitize_llm_error(error: BaseException | str, secrets: list[str] | None = None, limit: int = 1200) -> str:
    text = str(error)
    if isinstance(error, httpx.HTTPStatusError) and error.response is not None:
        try:
            body = error.response.text
        except Exception:
            body = ""
        text = f"HTTP {error.response.status_code} {error.request.url}: {body}"

    for secret in secrets or []:
        if secret:
            text = text.replace(secret, "[REDACTED]")

    patterns = [
        r"(?i)(authorization\s*[:=]\s*bearer\s+)[^\s,;'\"]+",
        r"(?i)(bearer\s+)[A-Za-z0-9._\-+/=]+",
        r"(?i)(x-api-key\s*[:=]\s*)[^\s,;'\"]+",
        r"(?i)(api[_-]?key\s*[:=]\s*)[^\s,;'\"]+",
    ]
    for pattern in patterns:
        text = re.sub(pattern, r"\1[REDACTED]", text)

    text = text.replace("\r", " ").replace("\n", " ").strip()
    if len(text) > limit:
        return text[:limit].rstrip() + "...[truncated]"
    return text


def llm_config_secret_values(llm_config: dict[str, Any] | None) -> list[str]:
    if not llm_config:
        return []
    api_key = str(llm_config.get("api_key") or "")
    return [api_key] if api_key else []


def public_llm_config_info(llm_config: dict[str, Any] | None) -> dict[str, Any]:
    if not llm_config:
        return {
            "has_llm_config": False,
            "llm_config_mode": "system",
        }
    return {
        "has_llm_config": True,
        "llm_config_mode": llm_config.get("mode", "custom"),
        "llm_provider": llm_config.get("provider", "custom"),
        "llm_model": llm_config.get("model", ""),
        "llm_api_type": llm_config.get("api_type", ""),
        "llm_api_key_source": llm_config.get("api_key_source", ""),
    }


def normalize_user_llm_config(raw_config: dict[str, Any] | None) -> dict[str, Any] | None:
    if not raw_config:
        return None
    mode = str(raw_config.get("mode") or "").strip().lower()
    if mode in {"", "system"}:
        return None

    if mode == "builtin":
        provider = str(raw_config.get("provider") or "").strip().lower()
        model = str(raw_config.get("model") or "").strip()
        provider_info = BUILTIN_LLM_PROVIDERS.get(provider)
        if not provider_info:
            raise LLMConfigError("不支持的内置 LLM 提供商")
        if model not in provider_info["models"]:
            raise LLMConfigError("该内置提供商不支持所选模型")
        api_key_env = provider_info["api_key_env"]
        api_key = (os.getenv(api_key_env) or "").strip()
        if not api_key:
            raise LLMConfigError(f"服务器未配置该提供商 API Key：{api_key_env}")
        return {
            "mode": "builtin",
            "api_type": provider_info["api_type"],
            "provider": provider,
            "api_url": provider_info["api_url"],
            "model": model,
            "api_key": api_key,
            "api_key_source": f"env:{api_key_env}",
        }

    if mode == "custom":
        api_type = str(raw_config.get("api_type") or "").strip().lower()
        if api_type not in _SUPPORTED_CUSTOM_API_TYPES:
            raise LLMConfigError("自定义接口类型必须是 openai-compatible 或 anthropic-compatible")
        api_url = _validate_custom_url(str(raw_config.get("api_url") or ""))
        model = str(raw_config.get("model") or "").strip()
        api_key = str(raw_config.get("api_key") or "").strip()
        if not model:
            raise LLMConfigError("模型调用名不能为空")
        if len(model) > 200:
            raise LLMConfigError("模型调用名过长")
        if not api_key:
            raise LLMConfigError("API Key 不能为空")
        if len(api_key) < 8 or len(api_key) > 4096:
            raise LLMConfigError("API Key 长度不合法")
        return {
            "mode": "custom",
            "api_type": api_type,
            "provider": "custom",
            "api_url": api_url,
            "model": model,
            "api_key": api_key,
            "api_key_source": "request",
        }

    raise LLMConfigError("不支持的 LLM 配置模式")


def _parse_tool_calls(message: dict, finish_reason: str) -> list[ToolCall] | None:
    """从 message 中解析 tool_calls，兼容 finish_reason 为 tool_calls 或 function_call。"""
    raw = message.get("tool_calls")
    if not raw:
        return None
    result = []
    for tc in raw:
        fn = tc.get("function", {})
        args_raw = fn.get("arguments", "{}")
        try:
            args = json.loads(args_raw) if isinstance(args_raw, str) else args_raw
        except Exception:
            logger.warning(f"[LLM] tool_call JSON 解析失败, name={fn.get('name')}, raw={str(args_raw)[:200]}")
            continue
        result.append(ToolCall(id=tc.get("id", ""), name=fn.get("name", ""), arguments=args))
    return result or None


class QianfanChatAdapter:
    def __init__(
        self,
        *,
        api_url: str,
        bearer_token: str,
        model: str,
        temperature: float = 0,
        timeout: float = 120.0,
        max_tokens: int = 4096,
    ) -> None:
        self.api_url = api_url
        self.bearer_token = bearer_token
        self.model = model
        self.temperature = temperature
        self.timeout = timeout
        self.max_tokens = max_tokens

    @retry_llm
    async def ainvoke(
        self,
        prompt: str | list[dict],
        tools: list[dict] | None = None,
    ) -> LLMResponse:
        if not self.bearer_token:
            raise RuntimeError("缺少 QIANFAN_BEARER_TOKEN，无法调用千帆模型")

        messages = [{"role": "user", "content": prompt}] if isinstance(prompt, str) else prompt
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
        }
        if tools:
            payload["tools"] = tools

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.bearer_token}",
        }

        async with httpx.AsyncClient(timeout=self.timeout, trust_env=False) as client:
            resp = await client.post(self.api_url, headers=headers, json=payload)
            resp.raise_for_status()
            data = resp.json()

        choice = (data.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        finish_reason = choice.get("finish_reason", "stop")
        if finish_reason == "length":
            logger.warning(f"[LLM] Qianfan finish_reason=length, 响应被截断, model={self.model}")
        tool_calls = _parse_tool_calls(message, finish_reason)
        content = message.get("content") or ""
        if isinstance(content, list):
            content = "".join(
                item if isinstance(item, str) else (item.get("text") or item.get("content") or "")
                for item in content
            )
        return LLMResponse(
            content=self._normalize_text(str(content)),
            tool_calls=tool_calls,
            finish_reason=finish_reason,
        )

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
                    "max_tokens": self.max_tokens,
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
        max_tokens: int = 4096,
    ) -> None:
        self.api_url = api_url
        self.api_key = api_key
        self.model = model
        self.temperature = temperature
        self.timeout = timeout
        self.max_tokens = max_tokens

    @retry_llm
    async def ainvoke(
        self,
        prompt: str | list[dict],
        tools: list[dict] | None = None,
    ) -> LLMResponse:
        if not self.api_key:
            raise RuntimeError("缺少 LONGCAT_API_KEY，无法调用 Longcat 模型")

        messages = [{"role": "user", "content": prompt}] if isinstance(prompt, str) else prompt
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
        }
        if tools:
            payload["tools"] = tools

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }

        async with httpx.AsyncClient(timeout=self.timeout, trust_env=False) as client:
            resp = await client.post(self.api_url, headers=headers, json=payload)
            resp.raise_for_status()
            data = resp.json()
            logger.debug(f"[LLM] Longcat ainvoke response: {json.dumps(data, ensure_ascii=False)[:300]}...")

        choice = (data.get("choices") or [{}])[0]
        message = choice.get("message", {})
        finish_reason = choice.get("finish_reason", "stop")
        if finish_reason == "length":
            logger.warning(f"[LLM] Longcat finish_reason=length, 响应被截断, model={self.model}")
        tool_calls = _parse_tool_calls(message, finish_reason)
        content = message.get("content") or ""
        return LLMResponse(
            content=self._normalize_text(content),
            tool_calls=tool_calls,
            finish_reason=finish_reason,
        )

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
                    "max_tokens": self.max_tokens,
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



class ModelScopeChatAdapter:
    """兼容 OpenAI 格式的 ModelScope/DeepSeek API 适配器"""
    def __init__(
        self,
        *,
        api_url: str,
        api_key: str,
        model: str,
        temperature: float = 0,
        timeout: float = 120.0,
        max_tokens: int = 4096,
    ) -> None:
        self.api_url = api_url if api_url.endswith("/chat/completions") else f"{api_url.rstrip('/')}/chat/completions"
        self.api_key = api_key
        self.model = model
        self.temperature = temperature
        self.timeout = timeout
        self.max_tokens = max_tokens

    @retry_llm
    async def ainvoke(
        self,
        prompt: str | list[dict],
        tools: list[dict] | None = None,
    ) -> LLMResponse:
        if not self.api_key:
            raise RuntimeError("缺少 MODELSCOPE_API_KEY，无法调用 ModelScope 模型")

        messages = [{"role": "user", "content": prompt}] if isinstance(prompt, str) else prompt
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
        }
        if tools:
            payload["tools"] = tools

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }

        async with httpx.AsyncClient(timeout=self.timeout, trust_env=False) as client:
            resp = await client.post(self.api_url, headers=headers, json=payload)
            resp.raise_for_status()
            data = resp.json()
            logger.debug(f"[LLM] ModelScope ainvoke response: {json.dumps(data, ensure_ascii=False)[:300]}...")

        choice = (data.get("choices") or [{}])[0]
        message = choice.get("message", {})
        finish_reason = choice.get("finish_reason", "stop")
        if finish_reason == "length":
            logger.warning(f"[LLM] ModelScope finish_reason=length, 响应被截断, model={self.model}")
        tool_calls = _parse_tool_calls(message, finish_reason)
        content = message.get("content") or ""
        return LLMResponse(
            content=self._normalize_text(content),
            tool_calls=tool_calls,
            finish_reason=finish_reason,
        )

    @retry_llm
    async def astream(self, prompt: str) -> AsyncIterator[str]:
        if not self.api_key:
            raise RuntimeError("缺少 MODELSCOPE_API_KEY，无法调用 ModelScope 模型")

        max_retries = 2
        for attempt in range(max_retries + 1):
            try:
                payload = {
                    "model": self.model,
                    "messages": [{"role": "user", "content": prompt}],
                    "temperature": self.temperature,
                    "stream": True,
                    "max_tokens": self.max_tokens,
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
                            data = line[5:].strip()
                            if data == "[DONE]":
                                break
                            if not data:
                                continue
                            try:
                                chunk = json.loads(data)
                                delta = chunk.get("choices", [{}])[0].get("delta", {})
                                # 对于深思模型，有 reasoning_content 和 content 
                                reasoning = delta.get("reasoning_content", "")
                                content = delta.get("content", "")
                                if reasoning:
                                    # 如果你想在前端流式看到思考过程，可以将 reasoning 当作文本返回给前端
                                    # 但通常对于 Agent 只需要最终结果，我们这里暂且将思维链屏蔽或选择忽略
                                    pass
                                if content:
                                    yield content
                            except Exception as e:
                                logger.error(f"[LLM] ModelScope 解析 SSE chunk 异常: {e}, 原始数据: {data}")
                                continue
                return

            except Exception as e:
                logger.warning(f"[LLM] ModelScope 流式请求尝试 {attempt+1} 失败: {str(e)}")
                if attempt < max_retries:
                    await asyncio.sleep(2)
                else:
                    logger.error(f"[LLM] ModelScope 流式重试 3 次均失败，终止。")
                    raise e

    @staticmethod
    def _normalize_text(text: str) -> str:
        return _normalize_text(text)


class OpenAICompatibleChatAdapter:
    """Generic OpenAI Chat Completions compatible adapter."""

    def __init__(
        self,
        *,
        api_url: str,
        api_key: str,
        model: str,
        provider: str = "custom",
        temperature: float = 0,
        timeout: float = 120.0,
        max_tokens: int = 4096,
    ) -> None:
        self.api_url = _normalize_openai_chat_url(api_url)
        self.api_key = api_key
        self.model = model
        self.provider = provider
        self.temperature = temperature
        self.timeout = timeout
        self.max_tokens = max_tokens

    @retry_llm
    async def ainvoke(
        self,
        prompt: str | list[dict],
        tools: list[dict] | None = None,
    ) -> LLMResponse:
        if not self.api_key:
            raise RuntimeError(f"缺少 {self.provider} API Key，无法调用模型")

        messages = [{"role": "user", "content": prompt}] if isinstance(prompt, str) else prompt
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
        }
        if tools:
            payload["tools"] = tools

        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }

        try:
            async with httpx.AsyncClient(timeout=self.timeout, trust_env=False) as client:
                resp = await client.post(self.api_url, headers=headers, json=payload)
                resp.raise_for_status()
                data = resp.json()
        except httpx.HTTPStatusError as exc:
            raise RuntimeError(sanitize_llm_error(exc, [self.api_key])) from exc

        choice = (data.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        finish_reason = choice.get("finish_reason", "stop")
        if finish_reason == "length":
            logger.warning("[LLM] {} finish_reason=length, model={}", self.provider, self.model)
        content = message.get("content") or ""
        if isinstance(content, list):
            content = "".join(
                item if isinstance(item, str) else (item.get("text") or item.get("content") or "")
                for item in content
            )
        return LLMResponse(
            content=self._normalize_text(str(content)),
            tool_calls=_parse_tool_calls(message, finish_reason),
            finish_reason=finish_reason,
        )

    @retry_llm
    async def astream(self, prompt: str) -> AsyncIterator[str]:
        if not self.api_key:
            raise RuntimeError(f"缺少 {self.provider} API Key，无法调用模型")

        payload = {
            "model": self.model,
            "messages": [{"role": "user", "content": prompt}],
            "temperature": self.temperature,
            "stream": True,
            "max_tokens": self.max_tokens,
        }
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
        }

        timeout = httpx.Timeout(40.0, connect=10.0, read=40.0)
        try:
            async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
                async with client.stream("POST", self.api_url, headers=headers, json=payload) as resp:
                    resp.raise_for_status()
                    async for line in resp.aiter_lines():
                        if not line.startswith("data:"):
                            continue
                        data = line[5:].strip()
                        if data == "[DONE]":
                            break
                        if not data:
                            continue
                        try:
                            chunk = json.loads(data)
                        except json.JSONDecodeError:
                            continue
                        delta = chunk.get("choices", [{}])[0].get("delta", {})
                        content = delta.get("content", "")
                        if content:
                            yield content
        except httpx.HTTPStatusError as exc:
            raise RuntimeError(sanitize_llm_error(exc, [self.api_key])) from exc

    @staticmethod
    def _normalize_text(text: str) -> str:
        return _normalize_text(text)


class AnthropicCompatibleChatAdapter:
    """Anthropic Messages API compatible adapter with the shared LLMResponse surface."""

    def __init__(
        self,
        *,
        api_url: str,
        api_key: str,
        model: str,
        provider: str = "custom",
        temperature: float = 0,
        timeout: float = 120.0,
        max_tokens: int = 4096,
    ) -> None:
        self.api_url = api_url.strip()
        self.api_key = api_key
        self.model = model
        self.provider = provider
        self.temperature = temperature
        self.timeout = timeout
        self.max_tokens = max_tokens

    def _convert_messages(self, prompt: str | list[dict]) -> tuple[list[dict[str, Any]], str | None]:
        if isinstance(prompt, str):
            return [{"role": "user", "content": prompt}], None

        messages: list[dict[str, Any]] = []
        system_chunks: list[str] = []
        for item in prompt:
            role = item.get("role", "user")
            content = item.get("content") or ""
            if role == "system":
                if content:
                    system_chunks.append(str(content))
                continue
            if role == "tool":
                messages.append({
                    "role": "user",
                    "content": [{
                        "type": "tool_result",
                        "tool_use_id": item.get("tool_call_id") or item.get("id") or "",
                        "content": str(content),
                    }],
                })
                continue
            if role == "assistant":
                blocks: list[dict[str, Any]] = []
                if content:
                    blocks.append({"type": "text", "text": str(content)})
                for tc in item.get("tool_calls") or []:
                    fn = tc.get("function", {})
                    args_raw = fn.get("arguments", "{}")
                    try:
                        args = json.loads(args_raw) if isinstance(args_raw, str) else args_raw
                    except Exception:
                        args = {}
                    blocks.append({
                        "type": "tool_use",
                        "id": tc.get("id") or "",
                        "name": fn.get("name") or "",
                        "input": args if isinstance(args, dict) else {},
                    })
                messages.append({"role": "assistant", "content": blocks or [{"type": "text", "text": ""}]})
                continue
            messages.append({"role": "user", "content": content})
        return messages, "\n\n".join(system_chunks) if system_chunks else None

    @staticmethod
    def _convert_tools(tools: list[dict] | None) -> list[dict] | None:
        if not tools:
            return None
        result = []
        for tool in tools:
            if "input_schema" in tool and "name" in tool:
                result.append(tool)
                continue
            fn = tool.get("function", {}) if tool.get("type") == "function" else tool
            name = fn.get("name")
            if not name:
                continue
            result.append({
                "name": name,
                "description": fn.get("description", ""),
                "input_schema": fn.get("parameters") or {"type": "object", "properties": {}},
            })
        return result or None

    @retry_llm
    async def ainvoke(
        self,
        prompt: str | list[dict],
        tools: list[dict] | None = None,
    ) -> LLMResponse:
        if not self.api_key:
            raise RuntimeError(f"缺少 {self.provider} API Key，无法调用 Anthropic 兼容模型")

        messages, system_prompt = self._convert_messages(prompt)
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
        }
        if system_prompt:
            payload["system"] = system_prompt
        converted_tools = self._convert_tools(tools)
        if converted_tools:
            payload["tools"] = converted_tools

        headers = {
            "Content-Type": "application/json",
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01",
        }

        try:
            async with httpx.AsyncClient(timeout=self.timeout, trust_env=False) as client:
                resp = await client.post(self.api_url, headers=headers, json=payload)
                resp.raise_for_status()
                data = resp.json()
        except httpx.HTTPStatusError as exc:
            raise RuntimeError(sanitize_llm_error(exc, [self.api_key])) from exc

        text_chunks: list[str] = []
        tool_calls: list[ToolCall] = []
        for block in data.get("content") or []:
            if block.get("type") == "text":
                text_chunks.append(block.get("text") or "")
            elif block.get("type") == "tool_use":
                tool_calls.append(ToolCall(
                    id=block.get("id") or "",
                    name=block.get("name") or "",
                    arguments=block.get("input") or {},
                ))
        stop_reason = data.get("stop_reason") or "stop"
        finish_reason = "tool_calls" if stop_reason == "tool_use" else stop_reason
        return LLMResponse(
            content=self._normalize_text("".join(text_chunks)),
            tool_calls=tool_calls or None,
            finish_reason=finish_reason,
        )

    @retry_llm
    async def astream(self, prompt: str) -> AsyncIterator[str]:
        if not self.api_key:
            raise RuntimeError(f"缺少 {self.provider} API Key，无法调用 Anthropic 兼容模型")

        messages, system_prompt = self._convert_messages(prompt)
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": self.temperature,
            "max_tokens": self.max_tokens,
            "stream": True,
        }
        if system_prompt:
            payload["system"] = system_prompt
        headers = {
            "Content-Type": "application/json",
            "x-api-key": self.api_key,
            "anthropic-version": "2023-06-01",
        }

        timeout = httpx.Timeout(40.0, connect=10.0, read=40.0)
        try:
            async with httpx.AsyncClient(timeout=timeout, trust_env=False) as client:
                async with client.stream("POST", self.api_url, headers=headers, json=payload) as resp:
                    resp.raise_for_status()
                    async for line in resp.aiter_lines():
                        if not line.startswith("data:"):
                            continue
                        data = line[5:].strip()
                        if not data or data == "[DONE]":
                            continue
                        try:
                            event = json.loads(data)
                        except json.JSONDecodeError:
                            continue
                        if event.get("type") == "content_block_delta":
                            delta = event.get("delta") or {}
                            if delta.get("type") == "text_delta" and delta.get("text"):
                                yield delta["text"]
                        elif event.get("type") == "content_block_start":
                            block = event.get("content_block") or {}
                            if block.get("type") == "text" and block.get("text"):
                                yield block["text"]
        except httpx.HTTPStatusError as exc:
            raise RuntimeError(sanitize_llm_error(exc, [self.api_key])) from exc

    @staticmethod
    def _normalize_text(text: str) -> str:
        return _normalize_text(text)


def create_llm(
    *,
    temperature: float = 0,
    model: str | None = None,
    max_tokens: int = 4096,
    llm_config: dict[str, Any] | None = None,
    **kwargs: Any,
) -> QianfanChatAdapter | LongcatChatAdapter | ModelScopeChatAdapter | OpenAICompatibleChatAdapter | AnthropicCompatibleChatAdapter:
    """创建聊天补全适配器（根据 LLM_PROVIDER 动态分发）。"""
    timeout = float(kwargs.pop("timeout", 120.0))
    if llm_config:
        api_type = llm_config.get("api_type")
        provider = llm_config.get("provider", "custom")
        model_name = model or str(llm_config.get("model") or "").strip()
        if api_type == "openai-compatible":
            return OpenAICompatibleChatAdapter(
                api_url=str(llm_config.get("api_url") or ""),
                api_key=str(llm_config.get("api_key") or ""),
                model=model_name,
                provider=provider,
                temperature=temperature,
                timeout=timeout,
                max_tokens=max_tokens,
            )
        if api_type == "anthropic-compatible":
            return AnthropicCompatibleChatAdapter(
                api_url=str(llm_config.get("api_url") or ""),
                api_key=str(llm_config.get("api_key") or ""),
                model=model_name,
                provider=provider,
                temperature=temperature,
                timeout=timeout,
                max_tokens=max_tokens,
            )
        raise LLMConfigError("不支持的请求级 LLM 接口类型")

    provider = os.getenv("LLM_PROVIDER", "qianfan").strip().lower()

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
            max_tokens=max_tokens,
        )
    elif provider == "modelscope":
        api_url = (os.getenv("MODELSCOPE_BASE_URL") or "https://api-inference.modelscope.cn/v1").strip()
        api_key = (os.getenv("MODELSCOPE_API_KEY") or "").strip()
        model_name = model or (os.getenv("MODELSCOPE_MODEL") or "deepseek-ai/DeepSeek-R1-Distill-Qwen-32B").strip()
        return ModelScopeChatAdapter(
            api_url=api_url,
            api_key=api_key,
            model=model_name,
            temperature=temperature,
            timeout=timeout,
            max_tokens=max_tokens,
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
            max_tokens=max_tokens,
        )
