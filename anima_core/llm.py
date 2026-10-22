"""Client for any OpenAI-compatible chat server.

Works with llama.cpp's llama-server, Jan, LM Studio, Ollama, KoboldCpp, vLLM
or a remote provider. Only the address configured in Settings is contacted.
"""
from __future__ import annotations

from typing import Any

import requests

from .config import llm_base_url


class LLMError(RuntimeError):
    pass


class LLMClient:
    def __init__(self, cfg):
        self.cfg = cfg
        self.base_url: str = llm_base_url(cfg)
        self.api_key: str = str(cfg.get("llm", "api_key", default="") or "").strip()
        self.model: str = str(cfg.get("llm", "model", default="") or "").strip()

    def _headers(self) -> dict[str, str]:
        h = {"Content-Type": "application/json"}
        if self.api_key:
            h["Authorization"] = f"Bearer {self.api_key}"
        return h

    # ------------------------------------------------------------- discovery
    def list_models(self, timeout: float = 3.0) -> list[str]:
        """Model ids reported by the server; empty list if unreachable."""
        if not self.base_url:
            return []
        try:
            r = requests.get(f"{self.base_url}/models", headers=self._headers(), timeout=timeout)
            r.raise_for_status()
            data = r.json()
            # plain llama-server answers {"models": [...]}, OpenAI style {"data": [...]}
            if isinstance(data, dict):
                items = data.get("data") or data.get("models") or []
            else:
                items = data
            out: list[str] = []
            for it in items or []:
                if isinstance(it, dict):
                    mid = it.get("id") or it.get("name") or it.get("model")
                    if mid:
                        out.append(str(mid))
                elif isinstance(it, str):
                    out.append(it)
            return sorted(set(out))
        except Exception:
            return []

    def is_up(self, timeout: float = 1.5) -> bool:
        if not self.base_url:
            return False
        try:
            r = requests.get(f"{self.base_url}/models", headers=self._headers(), timeout=timeout)
            return r.status_code == 200
        except Exception:
            return False

    # ------------------------------------------------------------------ chat
    def chat(
        self,
        messages: list[dict[str, str]],
        *,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> str:
        if not self.base_url:
            raise LLMError("No server address set: fill it in Settings → Inference server.")
        if not self.model:
            models = self.list_models()
            if not models:
                raise LLMError(f"No model available on {self.base_url}.")
            self.model = models[0]

        payload: dict[str, Any] = {
            "model": self.model,
            "messages": messages,
            "temperature": float(
                temperature if temperature is not None
                else self.cfg.get("llm", "temperature", default=0.75)
            ),
            "top_p": float(self.cfg.get("llm", "top_p", default=0.9)),
            "max_tokens": int(
                max_tokens if max_tokens is not None
                else self.cfg.get("llm", "max_tokens", default=1200)
            ),
            "stream": False,
        }
        # Reasoning models (Qwen3, Gemma 4…) may think for hundreds of tokens
        # before answering. Each server has its own switch, so send both:
        #   chat_template_kwargs -> llama.cpp / Jan (template variable)
        #   reasoning_effort     -> LM Studio and OpenAI-style servers
        thinking_keys = ("chat_template_kwargs", "reasoning_effort")
        if bool(self.cfg.get("llm", "disable_thinking", default=True)):
            payload["chat_template_kwargs"] = {"enable_thinking": False}
            payload["reasoning_effort"] = "none"

        timeout = int(self.cfg.get("llm", "timeout", default=240))
        url = f"{self.base_url}/chat/completions"

        try:
            r = requests.post(url, json=payload, headers=self._headers(), timeout=timeout)
            # some servers reject keys or values they don't know: retry without them
            if r.status_code in (400, 422) and any(k in payload for k in thinking_keys):
                for k in thinking_keys:
                    payload.pop(k, None)
                r = requests.post(url, json=payload, headers=self._headers(), timeout=timeout)
            if r.status_code >= 400:
                raise LLMError(f"HTTP {r.status_code}: {r.text[:300]}")
            data = r.json()
            choice = data["choices"][0]
            msg = choice.get("message") or {}
            content = (msg.get("content") or "").strip()
            if not content:
                reason = msg.get("reasoning_content") or msg.get("reasoning") or ""
                if reason:
                    raise LLMError(
                        "The model spent all its tokens on internal reasoning "
                        f"(finish_reason={choice.get('finish_reason')}). "
                        "Enable 'Disable thinking' or raise 'Max tokens' in Settings."
                    )
            return content
        except requests.exceptions.Timeout:
            raise LLMError(
                f"The model did not answer within {timeout}s. Raise the timeout in Settings."
            )
        except requests.exceptions.ConnectionError:
            raise LLMError(f"Cannot reach {url}. Is the server running?")
