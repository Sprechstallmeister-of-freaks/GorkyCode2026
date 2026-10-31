"""Small OpenAI-compatible DeepSeek client.

The client only exchanges chat/tool messages. Business operations remain behind
ToolRegistry and Planner; no SQL or local credentials are ever sent to the model.
"""
from __future__ import annotations

import httpx


class DeepSeekError(Exception):
    """A transport, HTTP or provider-protocol failure safe to fallback from."""


class DeepSeekClient:
    def __init__(self, *, api_key: str, base_url: str = "https://api.deepseek.com",
                 model: str = "deepseek-chat", timeout_seconds: float = 30.0,
                 transport: httpx.BaseTransport | None = None):
        self.model = model
        self.connected = False
        self._http = httpx.Client(
            base_url=base_url.rstrip("/") + "/",
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            timeout=httpx.Timeout(timeout_seconds), transport=transport,
        )

    def complete(self, messages: list[dict], tools: list[dict]) -> dict:
        payload: dict = {"model": self.model, "messages": messages}
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        try:
            response = self._http.post("chat/completions", json=payload)
        except httpx.HTTPError as exc:
            self.connected = False
            raise DeepSeekError(f"DeepSeek недоступен: {type(exc).__name__}") from exc
        if response.status_code != 200:
            self.connected = False
            raise DeepSeekError(f"DeepSeek вернул HTTP {response.status_code}")
        try:
            data = response.json()
            message = data["choices"][0]["message"]
            if not isinstance(message, dict):
                raise TypeError("message must be an object")
            content = message.get("content")
            tool_calls = message.get("tool_calls") or []
            if content is not None and not isinstance(content, str):
                raise TypeError("content must be a string or null")
            if not isinstance(tool_calls, list):
                raise TypeError("tool_calls must be an array")
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            self.connected = False
            raise DeepSeekError("DeepSeek вернул некорректную структуру ответа") from exc
        result = {"role": "assistant", "content": content}
        if tool_calls:
            result["tool_calls"] = tool_calls
        self.connected = True
        return result

    def close(self):
        self._http.close()
