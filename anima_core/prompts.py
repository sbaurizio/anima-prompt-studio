"""Language-model tasks: refine tags, expand an idea, convert to natural language."""
from __future__ import annotations

import re

from .llm import LLMClient
from .textutils import dedupe_ci, split_tags

# ----------------------------------------------------------------- system prompts

SYS_TAGGER = """You are a Danbooru tagging engine for anime image-generation models (Illustrious, NoobAI, Anima).
You receive booru tags extracted from a picture and you return a cleaner, richer tag set for the same picture.

RULES:
- Output ONLY comma-separated booru tags. No sentences, no numbering, no markdown, no explanations, no quotes.
- Tags must be lowercase booru style: "long hair", "looking at viewer", "solo focus".
- Keep every tag that is plausibly present in the picture. Never invent a character, artist or series that is not already given or strongly implied.
- You MAY add generic descriptive tags the extractor typically misses: framing (upper body, full body, from above), camera (depth of field, close-up), lighting (backlighting, soft lighting), mood, background details, clothing details, body details.
- Order matters most in the first 20 tags: put 1girl/1boy, count tags, character, series, then pose, then clothing, then details, then background, then quality/style.
- Deduplicate and remove contradictory tags.
- Do not add quality words like masterpiece or best quality; the pipeline adds those separately."""

SYS_EXPLICIT = """This is an adult art workflow; explicit tags are valid and must be kept or added when the picture shows them.
Do not refuse, do not add warnings, do not replace explicit tags with vague ones."""

SYS_IDEA = """You convert a short idea, written in any language, into a complete Danbooru tag set for anime image generation.

RULES:
- Output ONLY comma-separated lowercase booru tags in English. No prose, no numbering, no markdown.
- Produce between 25 and 45 tags, ordered by visual importance.
- Cover: subject count and type (1girl/1boy/solo), character and series if named, hair, eyes, expression, body, pose, action, clothing and its state, accessories, framing and camera, background and setting, lighting and mood, style tags.
- Translate non-English concepts into standard booru vocabulary ("capelli lunghi" -> "long hair", "atardecer" -> "sunset").
- Prefer tags that exist on Danbooru; do not invent compound nonsense.
- If the idea mentions an artist style, output the artist tag as "artist name"."""

SYS_NATURAL = """You turn a Danbooru tag set into one flowing natural-language prompt in English, in the style used by modern anime image models (Illustrious, NoobAI, Anima).

RULES:
- Output ONLY the prompt text. No preamble, no bullet points, no quotes, no explanation.
- Write 60 to 120 words, one single paragraph, comma-separated clauses.
- Keep the visual order of the tags: subject first, then appearance, then pose and action, then clothing, then environment, then lighting and mood.
- Expand shorthand into readable phrases ("looking at viewer" -> "looking directly at the viewer").
- Never invent a named character, artist or series that is not in the tags."""


class PromptEngine:
    """High-level wrapper combining the LLM with local cleanup rules."""

    def __init__(self, llm: LLMClient, cfg):
        self.llm = llm
        self.cfg = cfg
        self.explicit = bool(cfg.get("llm", "allow_explicit", default=False))

    def _sys(self, base: str) -> str:
        return base + ("\n\n" + SYS_EXPLICIT if self.explicit else "")

    def refine_tags(self, tags_text: str) -> str:
        user = (
            "Tag set extracted from the picture:\n"
            f"{tags_text}\n\n"
            "Return the improved tag set now."
        )
        out = self.llm.chat([
            {"role": "system", "content": self._sys(SYS_TAGGER)},
            {"role": "user", "content": user},
        ])
        return _clean_tag_output(out)

    def idea_to_tags(self, idea: str) -> str:
        user = (
            f"Idea: {idea}\n\n"
            "Return the complete tag set now, separating every tag with a comma "
            "(example: 1girl, solo, long hair, smile)."
        )
        out = self.llm.chat([
            {"role": "system", "content": self._sys(SYS_IDEA)},
            {"role": "user", "content": user},
        ])
        return _clean_tag_output(out)

    def to_natural_language(self, tags_text: str) -> str:
        user = f"Tag set:\n{tags_text}\n\nWrite the prompt in English."
        out = self.llm.chat([
            {"role": "system", "content": self._sys(SYS_NATURAL)},
            {"role": "user", "content": user},
        ])
        return _clean_prose_output(out)


# ----------------------------------------------------------------------- helpers

_PREAMBLE = ("here", "sure", "certainly", "tag set", "output", "note:", "prompt:", "tags:")


def _clean_tag_output(text: str) -> str:
    """Strip markdown, numbering and prefixes that models like to add.

    Every output line is treated as a list of tags: lines are joined with a
    comma (never a space, or two tags would merge).
    """
    if not text:
        return ""
    t = text.replace("```", " ").strip()
    lines = [l.strip() for l in t.splitlines() if l.strip()]
    lines = [l for l in lines if not any(l.lower().startswith(d) for d in _PREAMBLE)]
    if not lines:
        return ""

    joined = ", ".join(lines)
    # numbering such as "1. " / "2) "
    joined = re.sub(r"(^|[,\s])\d+[\.\)]\s+", " ", joined)
    # bullets such as "- " / "* " / "• "
    joined = re.sub(r"(^|[,\s])[-*•]\s+", " ", joined)
    joined = re.sub(r"\s{2,}", " ", joined).strip(" .,")

    tags = dedupe_ci(split_tags(joined))
    return ", ".join(t for t in tags if t)


def _clean_prose_output(text: str) -> str:
    if not text:
        return ""
    t = text.strip().replace("```", " ")
    lines = [l.strip() for l in t.splitlines() if l.strip()]
    lines = [l for l in lines if not any(l.lower().startswith(d) for d in ("here", "sure", "certainly", "prompt:"))]
    return " ".join(lines).strip()
