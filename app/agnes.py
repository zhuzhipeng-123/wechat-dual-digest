from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any

import httpx

from app.core import AGNES_BASE_URL, AGNES_MODEL


class AgnesError(RuntimeError):
    pass


class AgnesConfigurationError(AgnesError):
    pass


class AgnesAuthenticationError(AgnesError):
    pass


class AgnesRateLimitError(AgnesError):
    pass


class AgnesResponseError(AgnesError):
    pass


@dataclass(frozen=True)
class AgnesSettings:
    api_key: str = field(repr=False)
    connect_timeout: float = 10
    read_timeout: float = 90

    @classmethod
    def from_environment(cls) -> AgnesSettings:
        api_key = os.getenv("AGNES_API_KEY", "").strip()
        if not api_key:
            raise AgnesConfigurationError("AGNES_API_KEY 未配置")
        return cls(api_key=api_key)


@dataclass(frozen=True)
class AgnesReply:
    content: str
    model: str
    usage: dict[str, Any] | None


class AgnesClient:
    def __init__(
        self, settings: AgnesSettings, transport: httpx.BaseTransport | None = None
    ) -> None:
        self.settings = settings
        self.client = httpx.Client(
            base_url=AGNES_BASE_URL,
            headers={"Authorization": f"Bearer {settings.api_key}"},
            timeout=httpx.Timeout(settings.read_timeout, connect=settings.connect_timeout),
            transport=transport,
        )

    def complete_json(self, messages: list[dict[str, str]], max_tokens: int = 600) -> AgnesReply:
        if not messages or any(
            message.get("role") not in {"system", "user", "assistant"} for message in messages
        ):
            raise ValueError("messages 必须包含受支持角色的消息")
        try:
            response = self.client.post(
                "/chat/completions",
                json={
                    "model": AGNES_MODEL,
                    "messages": messages,
                    "temperature": 0,
                    "max_tokens": max_tokens,
                    "response_format": {"type": "json_object"},
                },
            )
        except httpx.TimeoutException as error:
            raise AgnesResponseError("模型响应超时") from error
        except httpx.HTTPError as error:
            raise AgnesResponseError(f"模型连接失败：{type(error).__name__}") from error
        if response.status_code in {401, 403, 404}:
            raise AgnesAuthenticationError(f"模型服务拒绝请求：HTTP {response.status_code}")
        if response.status_code == 429:
            raise AgnesRateLimitError("模型调用达到限额")
        if response.status_code >= 400:
            raise AgnesResponseError(f"模型服务返回 HTTP {response.status_code}")
        try:
            payload = response.json()
            choice = payload["choices"][0]
            content = choice["message"]["content"]
            model = str(payload["model"])
        except (ValueError, KeyError, IndexError, TypeError) as error:
            raise AgnesResponseError("模型返回结构不正确") from error
        if choice.get("finish_reason") == "length":
            raise AgnesResponseError("模型输出被截断")
        if model != AGNES_MODEL:
            raise AgnesResponseError(f"模型响应标识不匹配：{model}")
        if not isinstance(content, str) or not content.strip():
            raise AgnesResponseError("模型未返回正文")
        usage = payload.get("usage")
        return AgnesReply(
            content=content, model=model, usage=usage if isinstance(usage, dict) else None
        )
