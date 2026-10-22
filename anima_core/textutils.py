"""Tag normalization in the style used by SD WebUI prompts.

    kunikida_hanamaru         ->  "kunikida hanamaru"
    fern_(sousou_no_frieren)  ->  "fern \\(sousou no frieren\\)"

Parentheses are escaped because in Stable Diffusion they mean "emphasis".
"""
from __future__ import annotations

import re
from typing import Iterable

KAOMOJI = [
    "0_0", "(o)_(o)", "+_+", "+_-", "._.", "<o>_<o>", "<|>_<|>", "=_=", ">_<",
    "3_3", "6_9", ">_o", "@_@", "^_^", "o_o", "u_u", "x_x", "|_|", "||_||",
]


def normalize_tag(
    tag: str,
    *,
    underscores: bool = False,
    escape_parens: bool = True,
) -> str:
    """Convert a Danbooru tag into the form used in prompts."""
    if not tag:
        return ""
    t = str(tag).strip().strip(",")
    if not t:
        return ""
    if t in KAOMOJI:
        return t

    if underscores:
        t = t.replace(" ", "_")
        # Danbooru uses underscores inside parentheses too: "fern_(series)"
        t = re.sub(r"\s*_\s*", "_", t)
    else:
        t = t.replace("_", " ")

    # unescape first, so already-escaped tags are not escaped twice
    t = t.replace(r"\(", "(").replace(r"\)", ")")
    if escape_parens:
        t = t.replace("(", r"\(").replace(")", r"\)")
    return t


def normalize_tags(tags: Iterable[str], **kw) -> list[str]:
    out: list[str] = []
    for t in tags:
        n = normalize_tag(t, **kw)
        if n and n not in out:
            out.append(n)
    return out


def split_tags(text: str) -> list[str]:
    """Split a comma-separated tag string, respecting parentheses."""
    if not text:
        return []
    parts: list[str] = []
    depth = 0
    buf: list[str] = []
    for ch in text:
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth = max(0, depth - 1)
        if ch == "," and depth == 0:
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    if buf:
        parts.append("".join(buf))
    return [p.strip() for p in parts if p.strip()]


def dedupe_ci(tags: Iterable[str]) -> list[str]:
    """Drop duplicates ignoring case, spaces, underscores and parentheses."""
    seen: set[str] = set()
    out: list[str] = []
    for t in tags:
        key = re.sub(r"[\\_\s()\-]+", "", t.lower())
        if key and key not in seen:
            seen.add(key)
            out.append(t)
    return out
