"""Settings storage: reads/writes settings.json in the extension folder.

settings.json is created on first run and is ignored by git, so personal
values (server address, API keys, model paths) never end up in the repository.
"""
from __future__ import annotations

import copy
import json
import pathlib
from typing import Any

EXT_DIR = pathlib.Path(__file__).resolve().parent.parent
SETTINGS_PATH = EXT_DIR / "settings.json"
MODELS_DIR = EXT_DIR / "models"

# llama-server's own default address, used when Auto start/stop is on and
# the Server URL is left empty
DEFAULT_LOCAL_URL = "http://127.0.0.1:8080/v1"


def llm_base_url(cfg) -> str:
    """The address actually used for the language model server."""
    url = str(cfg.get("llm", "base_url", default="") or "").strip().rstrip("/")
    if not url and cfg.get("local_server", "enabled", default=False):
        url = DEFAULT_LOCAL_URL
    return url

DEFAULTS: dict[str, Any] = {
    "llm": {
        # any OpenAI-compatible server: llama.cpp, Jan, LM Studio, Ollama,
        # KoboldCpp, vLLM, or a remote provider
        "base_url": "",
        "api_key": "",
        "model": "",                 # empty = first model reported by the server
        "temperature": 0.75,
        "top_p": 0.9,
        "max_tokens": 1200,
        "timeout": 240,
        "disable_thinking": True,
        "allow_explicit": False,
    },
    "local_server": {
        # optional: let the extension start/stop a local llama-server process
        "enabled": False,
        "llama_server": "",          # path to llama-server(.exe)
        "model": "",                 # path to a .gguf file
        "ctx": 8192,
        "ngl": 99,
        "extra_args": "",
        "start_timeout": 120,
        "stop_on_generate": True,
    },
    "tagger": {
        "model_repo": "SmilingWolf/wd-eva02-large-tagger-v3",
        "model_dir": "",             # empty = models/<repo name> inside the extension
        "use_gpu": True,
        "device_id": 0,
        "unload_on_generate": True,  # free its VRAM before image generation
        "general_threshold": 0.35,
        "character_threshold": 0.85,
        "rating_threshold": 0.30,
    },
    "booru": {
        "danbooru_login": "",
        "danbooru_api_key": "",
    },
    "tag_browser": {
        # folder with noob-wiki/*.csv, thumbnails and tags.sqlite;
        # empty = data/ inside the extension
        "data_dir": "",
    },
    "privacy": {
        # delete Gradio/Forge temporary image copies when the WebUI starts
        "clean_temp_on_start": False,
    },
    "output": {
        "quality_prefix": "",
        "negative_prompt": "low quality, worst quality, watermark, score_1, score_2, score_3",
        "underscores": False,
        "escape_parens": True,
        "max_tags": 80,
        "dedupe": True,
        # artist tags of Danbooru posts (style); off = left out
        "keep_artist": False,
    },
}


def _deep_merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


class Config:
    """Nested-dict settings with save to disk."""

    def __init__(self, path: pathlib.Path | None = None):
        self.path = pathlib.Path(path or SETTINGS_PATH)
        self.data: dict[str, Any] = copy.deepcopy(DEFAULTS)
        self.load()

    def load(self) -> None:
        if self.path.exists():
            try:
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                self.data = _deep_merge(DEFAULTS, raw)
            except Exception:
                self.data = copy.deepcopy(DEFAULTS)
        else:
            self.save()

    def save(self) -> None:
        try:
            self.path.write_text(
                json.dumps(self.data, indent=2, ensure_ascii=False), encoding="utf-8"
            )
        except Exception:
            pass

    def get(self, *keys: str, default: Any = None) -> Any:
        cur: Any = self.data
        for k in keys:
            if not isinstance(cur, dict) or k not in cur:
                return default
            cur = cur[k]
        return cur

    def set(self, *keys_and_value: Any) -> None:
        """set('tagger', 'general_threshold', 0.4)"""
        *keys, value = keys_and_value
        cur = self.data
        for k in keys[:-1]:
            cur = cur.setdefault(k, {})
        cur[keys[-1]] = value

    # ---------------------------------------------------------------- helpers
    def tagger_dir(self) -> pathlib.Path:
        """Folder holding model.onnx + selected_tags.csv."""
        custom = str(self.get("tagger", "model_dir", default="") or "").strip()
        if custom:
            return pathlib.Path(custom)
        repo = str(self.get("tagger", "model_repo", default="") or DEFAULTS["tagger"]["model_repo"])
        return MODELS_DIR / repo.split("/")[-1]
