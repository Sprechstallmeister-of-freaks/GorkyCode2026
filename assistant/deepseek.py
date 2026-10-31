"""Small OpenAI-compatible DeepSeek client with API-key failover.

The client only exchanges chat/tool messages. Business operations remain behind
ToolRegistry and Planner; no SQL or local credentials are ever sent to the model.
"""
from __future__ import annotations

import logging
import threading
import time

import httpx

logger = logging.getLogger(__name__)


class DeepSeekError(Exception):
    """A transport, HTTP or provider-protocol failure safe to fallback from."""


class DeepSeekClient:
    def __init__(self, *, api_key: str | None = None,
                 api_keys: list[str] | tuple[str, ...] | None = None,
                 base_url: str = "https://api.literouter.com/v1",
                 model: str = "deepseek-v4.1-flash-1017:free", timeout_seconds: float = 30.0,
                 transport: httpx.BaseTransport | None = None):
        candidates = list(api_keys or ())
        if api_key:
            candidates.insert(0, api_key)
        self._api_keys = tuple(dict.fromkeys(key.strip() for key in candidates if key and key.strip()))
        if not self._api_keys:
            raise ValueError("At least one DeepSeek API key is required")
        self.model = model
        self.connected = False
        self._key_index = 0
        self._exhausted: set[int] = set()
        self._cooldowns: dict[int, float] = {}
        self._key_lock = threading.Lock()
        self._http = httpx.Client(
            base_url=base_url.rstrip("/") + "/",
            headers={"Content-Type": "application/json"},
            timeout=httpx.Timeout(timeout_seconds), transport=transport,
        )

    @property
    def key_count(self) -> int:
        return len(self._api_keys)

    @property
    def active_key_index(self) -> int:
        """Zero-based key slot for diagnostics; never exposes the secret."""
        return self._key_index

    def complete(self, messages: list[dict], tools: list[dict]) -> dict:
        payload: dict = {"model": self.model, "messages": messages}
        if tools:
            payload["tools"] = tools
            payload["tool_choice"] = "auto"
        self.connected = False
        last_error: DeepSeekError | None = None

        # Retry only authentication/quota failures. Timeouts, 5xx and malformed
        # responses are not retried with another key, avoiding duplicate work.
        for _ in range(len(self._api_keys)):
            selected = self._acquire_key()
            if selected is None:
                break
            index, api_key = selected
            try:
                response = self._http.post(
                    "chat/completions", json=payload,
                    headers={"Authorization": f"Bearer {api_key}"},
                )
            except httpx.HTTPError as exc:
                raise DeepSeekError(f"DeepSeek недоступен: {type(exc).__name__}") from exc
            if response.status_code in {401, 402, 403, 429}:
                last_error = DeepSeekError(f"DeepSeek вернул HTTP {response.status_code}")
                self._reject_key(index, response.status_code, response.headers.get("Retry-After"))
                continue
            if response.status_code != 200:
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
                raise DeepSeekError("DeepSeek вернул некорректную структуру ответа") from exc
            with self._key_lock:
                self._key_index = index
            result = {"role": "assistant", "content": content}
            if tool_calls:
                result["tool_calls"] = tool_calls
            self.connected = True
            return result

        if last_error is not None:
            raise last_error
        raise DeepSeekError("Нет доступных DeepSeek API-ключей")

    def _acquire_key(self) -> tuple[int, str] | None:
        with self._key_lock:
            now = time.monotonic()
            for offset in range(len(self._api_keys)):
                index = (self._key_index + offset) % len(self._api_keys)
                if index in self._exhausted or self._cooldowns.get(index, 0) > now:
                    continue
                return index, self._api_keys[index]
        return None

    def _reject_key(self, index: int, status: int, retry_after: str | None):
        if status == 429:
            try:
                delay = float(retry_after) if retry_after else 60.0
            except ValueError:
                delay = 60.0
            delay = min(max(delay, 1.0), 86400.0)
            with self._key_lock:
                self._cooldowns[index] = max(self._cooldowns.get(index, 0), time.monotonic() + delay)
        else:
            with self._key_lock:
                self._exhausted.add(index)
        logger.info("DeepSeek key slot %d/%d rejected with HTTP %d; trying another slot",
                    index + 1, len(self._api_keys), status)

    def close(self):
        self._http.close()
