"""Assemble the final prompt in the format anime models expect."""
from __future__ import annotations

import re
from typing import Any, Iterable

from .textutils import KAOMOJI, dedupe_ci, normalize_tags

# Tags about text and marks drawn on the picture (watermarks, signatures,
# usernames, logos): they make the model draw them, so they are left out.
_SITES = r"(?:patreon|twitter|pixiv|fanbox|fantia|instagram|weibo|tumblr|deviantart|artstation|gumroad|ko-fi|youtube|tiktok|bluesky|skeb|subscribestar)"
UNWANTED = re.compile(
    r"^(?:(?:sample |text |artist )?watermark|watermarked|signature|artist signature|"
    r"artist name|character name|copyright name|group name|dated|username|"
    rf"{_SITES} (?:username|logo|reward|id)|{_SITES}|artist logo|company logo|"
    r"web address|url|copyright notice|commission(?:er name)?|"
    r"paid reward(?: available)?|reward available|logo|"
    # writing and speech balloons drawn on the picture
    r"(?:english|japanese|chinese|korean|russian|spanish|french|german|"
    r"translated|engrish|romaji) text|"
    r"speech bubble|thought bubble|spoken (?:heart|ellipsis|question mark|exclamation mark|musical note))$"
)


# censorship tags: censored, uncensored, censorship, mosaic censoring, bar censor…
CENSORSHIP = re.compile(r"\b(?:un|de)?censor")


def is_unwanted(tag: str) -> bool:
    t = (tag or "").replace("_", " ").strip().lower()
    return bool(UNWANTED.match(t) or CENSORSHIP.search(t))

# Danbooru rating -> readable label
RATING_LABEL = {
    "g": "general",
    "s": "sensitive",
    "q": "questionable",
    "e": "explicit",
    "general": "general",
    "sensitive": "sensitive",
    "questionable": "questionable",
    "explicit": "explicit",
}


def rating_label(code: str) -> str:
    return RATING_LABEL.get((code or "").lower().strip(), code or "unknown")


def build_positive(tags: Iterable[str], cfg) -> str:
    """Join the default prefix with the normalized tags."""
    opts = {
        "underscores": bool(cfg.get("output", "underscores", default=False)),
        "escape_parens": bool(cfg.get("output", "escape_parens", default=True)),
    }
    body = normalize_tags(tags, **opts)
    if cfg.get("output", "dedupe", default=True):
        body = dedupe_ci(body)

    limit = int(cfg.get("output", "max_tags", default=80) or 0)
    if limit and len(body) > limit:
        body = body[:limit]

    prefix = str(cfg.get("output", "quality_prefix", default="") or "")
    prefix_tags = normalize_tags(
        [p.strip() for p in prefix.split(",") if p.strip()], **opts
    )
    merged = dedupe_ci(prefix_tags + body) if prefix_tags else body
    return ", ".join(merged)


def build_negative(cfg) -> str:
    return str(cfg.get("output", "negative_prompt", default="") or "")


# Danbooru "meta" tags describe the post (commentary, translated, bad_id…),
# not the picture. Only these are kept, because people use them as quality tags.
KEEP_META = {"highres", "absurdres", "incredibly_absurdres"}


def from_booru(result, cfg) -> dict[str, Any]:
    """Build the prompt from a BooruResult."""
    meta = [t for t in result.meta if t in KEEP_META]
    artist = result.artist if cfg.get("output", "keep_artist", default=False) else []
    ordered: list[str] = []
    for group in (result.character, result.copyright, artist,
                  result.general, meta):
        for t in group:
            # spaces instead of underscores, like the local tagger shows them
            t = t if t in KAOMOJI else t.replace("_", " ")
            if t and t not in ordered and not is_unwanted(t):
                ordered.append(t)
    return {
        "positive": build_positive(ordered, cfg),
        "negative": build_negative(cfg),
        "tags": ordered,
        "rating_label": rating_label(result.rating),
    }


def from_tagger(pred: dict[str, Any], cfg) -> dict[str, Any]:
    """Build the prompt from a tagger prediction."""
    ordered: list[str] = []
    for t, _ in list(pred.get("character", [])) + list(pred.get("general", [])):
        if t and t not in ordered and not is_unwanted(t):
            ordered.append(t)
    return {
        "positive": build_positive(ordered, cfg),
        "negative": build_negative(cfg),
        "tags": ordered,
        "rating_label": rating_label(pred.get("rating", "")),
    }
