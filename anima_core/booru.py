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

# a preview is a resized sample (a few hundred KB): never download more than this
PREVIEW_MAX_BYTES = 15 * 1024 * 1024

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
    return [x for x in str(tag_string or "").split(" ") if x]


class BlockedError(RuntimeError):
    """Cloudflare answered with its anti-bot check instead of the data."""


def result_from_json(data: dict, host: str = "danbooru.donmai.us") -> BooruResult:
    """A post as returned by the API (or copied from the browser) -> BooruResult."""
    res = BooruResult("danbooru")
    res.post_id = str(data.get("id", ""))
    res.post_url = f"https://{host}/posts/{res.post_id}"
    res.rating = str(data.get("rating") or "")
    res.artist = _t(data.get("tag_string_artist"))
    res.copyright = _t(data.get("tag_string_copyright"))
    res.character = _t(data.get("tag_string_character"))
    res.general = _t(data.get("tag_string_general"))
    res.meta = _t(data.get("tag_string_meta"))
    # the resized "sample" is enough for a preview, else the small thumbnail
    # (also the only still image of videos). The original file (file_url) is
    # never downloaded: it can be tens of megabytes.
    if str(data.get("file_ext") or "").lower() in ("jpg", "jpeg", "png", "webp", "gif"):
        res.preview_url = str(data.get("large_file_url") or "")
    if not res.preview_url:
        res.preview_url = str(data.get("preview_file_url") or "")
    res.raw = data
    return res


def parse_pasted(text: str) -> BooruResult:
    """Post data the user copied from the browser (the post's .json page).

    The text is only read as JSON data, never run. A list (posts.json) is
    accepted too: its first post is used.
    """
    import json

    s = (text or "").strip()
    if not s.startswith(("{", "[")) and "tag_string_general" not in s:
        # the normal post page (Ctrl+A, Ctrl+C): read the tag list in the sidebar
        return parse_page_text(s)
    # browsers sometimes add text around the JSON when copying the whole page
    start = min([i for i in (s.find("{"), s.find("[")) if i >= 0], default=-1)
    if start < 0:
        raise ValueError("no post data found: copy the whole .json page of the post")
    try:
        data, _ = json.JSONDecoder().raw_decode(s[start:])
    except ValueError as exc:
        raise ValueError("the pasted text is not complete post data: "
                         "open the .json page again and copy everything (Ctrl+A, Ctrl+C)") from exc
    if isinstance(data, list):
        data = data[0] if data else {}
    if not isinstance(data, dict) or "tag_string_general" not in data:
        raise ValueError("this is not a Danbooru post: open https://danbooru.donmai.us/posts/<id>.json")
    return result_from_json(data)


# headings of the tag list in the sidebar of a post page
_SECTIONS = {
    "artist": "artist", "artists": "artist",
    "copyright": "copyright", "copyrights": "copyright",
    "character": "character", "characters": "character",
    "general": "general", "meta": "meta",
}
# what comes after the tag list: stop reading there
_STOP = {"information", "options", "history", "pools", "favorites", "related posts",
         "artist's commentary", "comments", "recommended", "similar"}
# "? long hair 1.2M" -> "long hair". A plain number ("? persona 5 812") is a
# count only on a "? tag count" line: otherwise it belongs to the name
# ("persona 5", with its count on the next line).
_COUNT_SUFFIX = re.compile(r"\s+\d+(?:[.,]\d+)?\s*[kKmM]$")
_COUNT_PLAIN = re.compile(r"\s+\d+(?:[.,]\d+)?$")


def parse_page_text(text: str) -> BooruResult:
    """The text of a normal post page copied from the browser -> BooruResult."""
    groups: dict[str, list[str]] = {k: [] for k in set(_SECTIONS.values())}
    current = None
    post_id = rating = ""
    lines = [raw.strip() for raw in text.splitlines()]
    for i, line in enumerate(lines):
        low = line.lower()
        if low in _SECTIONS:
            # a real tag-list heading is followed by tag lines, which start with
            # the "?" wiki link; the same words in the site menu ("Artists",
            # followed by "Tags") are not
            nxt = next((n for n in lines[i + 1:] if n), "")
            current = _SECTIONS[low] if nxt.startswith("?") else None
            continue
        if low in _STOP:
            current = None
        m = re.match(r"^id:\s*(\d+)", low)
        if m:
            post_id = m.group(1)
        m = re.match(r"^rating:\s*(\w+)", low)
        if m:
            rating = m.group(1)
        if current is None or not line:
            continue
        tag = _COUNT_SUFFIX.sub("", line.lstrip("?").strip()).strip()
        if line.startswith("?") and tag == line.lstrip("?").strip():
            tag = _COUNT_PLAIN.sub("", tag).strip()
        if not tag or tag == "?" or re.fullmatch(r"\d+(?:[.,]\d+)?\s*[kKmM]?", tag):
            continue  # a count on its own line
        tag = tag.replace(" ", "_")
        if tag not in groups[current]:
            groups[current].append(tag)
    if not groups["general"]:
        raise ValueError("no tags found: open the post page (or its .json page) in your "
                         "browser, press Ctrl+A, Ctrl+C and paste everything here")
    res = BooruResult("danbooru")
    res.post_id = post_id
    res.post_url = f"https://danbooru.donmai.us/posts/{post_id}" if post_id else ""
    res.rating = rating
    for key, tags in groups.items():
        setattr(res, key, tags)
    return res


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
        except Exception as exc:
            raise RuntimeError(f"request failed: {exc}") from exc
        if r.headers.get("cf-mitigated") == "challenge":
            # Cloudflare's anti-bot page: only a real browser can pass it
            raise BlockedError(
                "Danbooru's protection (Cloudflare) is blocking requests from your internet "
                "connection right now. Try again later or from another network (e.g. restart "
                "the router to get a new IP), or save the picture and use Tag image.")
        if r.status_code in (401, 403):
            hint = ("check the username and API key in Settings → Danbooru" if self.auth else
                    "the post may be restricted: an account (Settings → Danbooru) may be needed")
            raise RuntimeError(f"Danbooru answered {r.status_code} ({r.reason}): {hint}")
        try:
            r.raise_for_status()
            data = r.json()
        except Exception as exc:
            raise RuntimeError(f"request failed: {exc}") from exc
        if isinstance(data, list):
            data = data[0] if data else {}
        if not data:
            raise RuntimeError(f"post #{post_id} not found on {host}")
        return result_from_json(data, host)

    def fetch_image(self, url: str):
        """Download a post image (for the preview) and return it as a PIL image."""
        from io import BytesIO

        from PIL import Image

        # the URL comes from the API answer: only Danbooru's own servers are contacted
        u = urlparse(url or "")
        host = (u.hostname or "").lower()
        if u.scheme != "https" or not (host == "donmai.us" or host.endswith(".donmai.us")):
            raise RuntimeError(f"preview not on Danbooru's servers: {host or url!r}")
        with requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=30, stream=True) as r:
            r.raise_for_status()
            data = bytearray()
            for chunk in r.iter_content(chunk_size=256 * 1024):
                data += chunk
                if len(data) > PREVIEW_MAX_BYTES:
                    raise RuntimeError("preview image too large")
        img = Image.open(BytesIO(bytes(data)))
        img.load()
        return img
