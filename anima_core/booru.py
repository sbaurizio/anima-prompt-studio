"""Fetch tags of a Danbooru post through the public API.

Danbooru sits behind Cloudflare and blocks User-Agents that pretend to be a
browser; an honest, descriptive User-Agent gets normal API responses.

Only the post id the user pasted is sent to Danbooru. Local images are never
uploaded or hashed for lookups.
"""
from __future__ import annotations

import re
from typing import Any
from urllib.parse import parse_qs, urlparse

import requests

from . import __version__

USER_AGENT = f"AnimaPromptStudio/{__version__} (Stable Diffusion WebUI extension)"

DANBOORU_HOSTS = {
    "danbooru.donmai.us", "betabooru.donmai.us", "sonohara.donmai.us",
    "testbooru.donmai.us", "hijiribe.donmai.us",
}


def parse_post_id(url_or_id: str) -> tuple[str, str] | None:
    """Extract (host, post_id) from a Danbooru URL. A bare id is accepted too."""
    s = (url_or_id or "").strip()
    if not s:
        return None
    if s.isdigit():
        return "danbooru.donmai.us", s
    try:
        u = urlparse(s)
    except Exception:
        return None
    host = (u.netloc or "").lower()
    if host not in DANBOORU_HOSTS:
        return None
    m = re.search(r"/posts?/(\d+)", u.path)
    if m:
        return host, m.group(1)
    ids = parse_qs(u.query or "").get("id") or []
    if ids and ids[0].isdigit():
        return host, ids[0]
    return None


class BooruResult:
    """Structured tags of a post."""

    def __init__(self, source: str = ""):
        self.source = source
        self.post_id: str = ""
        self.post_url: str = ""
        self.rating: str = ""         # g/s/q/e
        self.artist: list[str] = []
        self.copyright: list[str] = []
        self.character: list[str] = []
        self.general: list[str] = []
        self.meta: list[str] = []
        self.preview_url: str = ""    # image shown in the panel
        self.raw: dict[str, Any] = {}

    @property
    def all_tags(self) -> list[str]:
        out: list[str] = []
        for group in (self.character, self.copyright, self.artist, self.general):
            for t in group:
                if t and t not in out:
                    out.append(t)
        return out


def _t(tag_string: str | None) -> list[str]:
    return [x for x in (tag_string or "").split(" ") if x]


class BooruClient:
    def __init__(self, cfg):
        login = str(cfg.get("booru", "danbooru_login", default="") or "").strip()
        key = str(cfg.get("booru", "danbooru_api_key", default="") or "").strip()
        # HTTP basic auth keeps the key out of the URL
        self.auth = (login, key) if login and key else None

    def by_post_id(self, post_id: str, host: str = "danbooru.donmai.us") -> BooruResult:
        try:
            r = requests.get(
                f"https://{host}/posts/{post_id}.json",
                headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
                auth=self.auth,
                timeout=30,
            )
            r.raise_for_status()
            data = r.json()
        except Exception as exc:
            raise RuntimeError(f"request failed: {exc}") from exc
        if isinstance(data, list):
            data = data[0] if data else {}
        if not data:
            raise RuntimeError(f"post #{post_id} not found on {host}")

        res = BooruResult("danbooru")
        res.post_id = str(data.get("id", ""))
        res.post_url = f"https://{host}/posts/{res.post_id}"
        res.rating = data.get("rating") or ""
        res.artist = _t(data.get("tag_string_artist"))
        res.copyright = _t(data.get("tag_string_copyright"))
        res.character = _t(data.get("tag_string_character"))
        res.general = _t(data.get("tag_string_general"))
        res.meta = _t(data.get("tag_string_meta"))
        # the resized "sample" is enough for a preview; videos/animations only
        # have a still image in preview_file_url
        if str(data.get("file_ext") or "").lower() in ("jpg", "jpeg", "png", "webp", "gif"):
            res.preview_url = data.get("large_file_url") or data.get("file_url") or ""
        if not res.preview_url:
            res.preview_url = data.get("preview_file_url") or ""
        res.raw = data
        return res

    def fetch_image(self, url: str):
        """Download a post image (for the preview) and return it as a PIL image."""
        from io import BytesIO

        from PIL import Image

        r = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=30)
        r.raise_for_status()
        img = Image.open(BytesIO(r.content))
        img.load()
        return img
