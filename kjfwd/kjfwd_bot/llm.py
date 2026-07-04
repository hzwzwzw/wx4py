from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Callable, Protocol

from .config import LLMConfig


class ChatModel(Protocol):
    def complete(self, system_prompt: str, user_prompt: str) -> str:
        ...


class OpenAIChatClient:
    def __init__(
        self,
        config: LLMConfig,
        *,
        urlopen: Callable = urllib.request.urlopen,
        sleep: Callable[[float], None] = time.sleep,
    ):
        self.config = config
        self._urlopen = urlopen
        self._sleep = sleep
        self.url = self._endpoint(config.base_url)

    def complete(self, system_prompt: str, user_prompt: str) -> str:
        payload = {
            "model": self.config.model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            "temperature": self.config.temperature,
            "max_tokens": self.config.max_tokens,
        }
        request = urllib.request.Request(
            self.url,
            data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            headers={
                "Authorization": f"Bearer {self.config.api_key}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
            method="POST",
        )
        attempts = self.config.retries + 1
        for attempt in range(attempts):
            try:
                with self._urlopen(request, timeout=self.config.timeout_seconds) as response:
                    data = json.loads(response.read().decode("utf-8"))
                text = data.get("choices", [{}])[0].get("message", {}).get("content", "")
                text = str(text or "").strip()
                if not text:
                    raise RuntimeError("LLM 返回了空回复")
                return text
            except urllib.error.HTTPError as exc:
                body = exc.read().decode("utf-8", errors="replace")[:1000]
                retryable = exc.code == 429 or 500 <= exc.code < 600
                if not retryable or attempt + 1 >= attempts:
                    raise RuntimeError(f"LLM HTTP {exc.code}: {body}") from exc
            except urllib.error.URLError as exc:
                if attempt + 1 >= attempts:
                    raise RuntimeError(f"LLM 网络请求失败: {exc.reason}") from exc
            self._sleep(min(2 ** attempt, 4))
        raise RuntimeError("LLM 请求失败")

    @staticmethod
    def _endpoint(base_url: str) -> str:
        value = base_url.strip().rstrip("/")
        if not value.lower().startswith(("http://", "https://")):
            value = "https://" + value
        if value.endswith("/chat/completions"):
            return value
        if value.endswith("/v1"):
            return value + "/chat/completions"
        return value + "/v1/chat/completions"
