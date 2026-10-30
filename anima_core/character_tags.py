"""Find the tags that describe *who* is in the picture, and what they wear.

Used by "Remove character tags": after tagging an image or reading a Danbooru
post, these tags are unchecked so another character, picked in the offline
browser, can take their place with their own look. Clothing, pose,
expression, background and style tags stay.

Three sources, from the most to the least precise:
  1. the tag category: character / copyright (Danbooru) or character (tagger);
  2. the appearance tags of the recognized characters, from the local database;
  3. body traits by pattern: hair, eyes, skin, ears, horns, tails, wings...

"Remove clothing" uses `clothing_tags`: clothing and accessories, recognized
by their last word (dress, boots, earrings...).
"""
from __future__ import annotations

import re
from typing import Callable, Iterable

IDENTITY_CATEGORIES = {"character", "copyright"}

# never removed: they count people, they don't describe a character
_COUNT = re.compile(r"^(\d\+?(girl|boy|other)s?|solo|solo focus|multiple (girls|boys|others))$")

_COLOR = (r"(?:(?:light|dark|pale|deep)[ -])?"
          r"(?:aqua|black|blonde|blue|brown|green|grey|gray|orange|pink|purple|red|"
          r"white|silver|yellow|violet|magenta|teal|beige|lavender|golden|cyan)")
_HAIR_DESC = (rf"(?:{_COLOR}|multicolored|two-tone|gradient|streaked|split-color|"
              r"long|short|medium|very long|very short|absurdly long|wavy|curly|"
              r"straight|messy|spiked|asymmetrical)")
_ANIMAL = (r"(?:animal|cat|fox|dog|wolf|rabbit|horse|bear|mouse|tiger|lion|raccoon|"
           r"cow|bat|deer|squirrel|dragon|demon|lizard|fish|monkey|sheep|goat)")

_TRAIT_PATTERNS = [
    rf"(?:{_HAIR_DESC} )+hair",
    r"(?:blunt |swept |parted |crossed |asymmetrical )?bangs",
    r"sidelocks|ahoge|antenna hair|hair intakes|hair flaps|hair between eyes|"
    r"hair over (?:one|both) eyes?|bob cut|hime cut|short hair with long locks|"
    r"(?:low |short )?twintails|(?:high |low |side )?ponytail|(?:single |twin |side )?braids?|"
    r"(?:single |double )?hair buns?|double bun|(?:twin )?drills|drill hair|"
    r"(?:one|two) side up|colored inner hair",
    rf"(?:{_COLOR}|multicolored) eyes|heterochromia|tsurime|tareme|"
    rf"(?:{_COLOR}|slit|symbol-shaped|heart-shaped|star-shaped) pupils",
    rf"(?:{_COLOR}|thick) eyebrows|(?:long|colored) eyelashes",
    rf"(?:{_COLOR}|dark|pale|colored) skin|dark-skinned (?:female|male)|tan",
    rf"(?:long )?pointy ears|{_ANIMAL} ears",
    rf"{_ANIMAL} tail|tail",
    rf"(?:{_ANIMAL}|single|curled) horns?|horns",
    rf"(?:{_ANIMAL}|angel|feathered|fairy|butterfly|insect|black|white) wings|wings|halo",
    r"fangs?|skin fang|mole|moles|mole under (?:eye|mouth)|freckles",
    r"flat chest|(?:small|medium|large|huge|gigantic) breasts",
]
_TRAIT = re.compile(r"^(?:" + "|".join(_TRAIT_PATTERNS) + r")$")

# clothing and accessories: a tag is one when its last word is one of these
# ("white dress", "black pantyhose", "brown footwear", "striped clothes")
_CLOTHING_WORDS = {
    "dress", "gown", "robe", "shirt", "t-shirt", "blouse", "skirt", "miniskirt",
    "shorts", "pants", "jeans", "trousers", "overalls", "pantyhose", "thighhighs",
    "kneehighs", "socks", "stockings", "leggings", "legwear", "boots", "shoes",
    "footwear", "sandals", "heels", "sneakers", "loafers", "slippers", "gloves",
    "mittens", "hat", "cap", "beret", "hood", "helmet", "headwear", "headband",
    "hairband", "headdress", "headpiece", "crown", "tiara", "veil", "ribbon",
    "bowtie", "necktie", "tie", "ascot", "scarf", "choker", "collar",
    "necklace", "pendant", "brooch", "earrings", "jewelry", "bracelet", "anklet",
    "belt", "sash", "obi", "suspenders", "capelet", "cape", "cloak",
    "coat", "jacket", "hoodie", "sweater", "cardigan", "vest", "uniform",
    "serafuku", "kimono", "yukata", "hakama", "armor", "apron", "leotard",
    "bodysuit", "swimsuit", "bikini", "bra", "panties", "underwear", "lingerie",
    "corset", "garter", "straps", "sleeves", "clothes", "clothing", "outfit",
    "costume", "glasses", "eyewear", "sunglasses", "goggles", "mask", "hairclip",
    "hairpin", "wristband", "armband", "epaulettes", "buttons",
    "frills", "lace", "tabard", "harness", "earmuffs", "neckerchief",
}
_CLOTHING_TAGS = {"sleeveless", "crop top", "tank top", "tube top", "hair flower",
                  "off shoulder", "garter belt", "garter straps", "thigh boots",
                  "ring", "wedding ring", "bow", "back bow"}
# "ring", "ornament" and "bow" alone at the end are often objects or places
# ("boxing ring", "christmas ornament", "gift bow"): only these endings are worn
_CLOTHING_ENDINGS = ("hair ornament", "hair bow", "hair ring", "ear ornament", "ribbon bow")


def is_clothing(tag: str) -> bool:
    k = key(tag)
    return (k in _CLOTHING_TAGS or k.endswith(_CLOTHING_ENDINGS)
            or (k.split(" ")[-1] if k else "") in _CLOTHING_WORDS)


def key(tag: str) -> str:
    """Compare tags ignoring case, underscores and escaped parentheses."""
    return (tag or "").replace(r"\(", "(").replace(r"\)", ")").replace("_", " ").strip().lower()


def character_tags(
    tags: Iterable[str],
    categories: dict[str, str],
    lookup: Callable[[str], dict | None] | None = None,
    keep: Iterable[str] = (),
) -> list[str]:
    """Tags among `tags` that identify the character(s) or their body traits.

    `categories` maps a tag to its category ("character", "copyright",
    "artist", "general"); `lookup` returns the database row of a character;
    tags in `keep` (e.g. the ones added from the browser) are never returned.
    Clothing is never returned, even when the database lists it.
    """
    tags = list(tags)
    keep_keys = {key(t) for t in keep}
    from_db: set[str] = set()
    for t in tags:
        if categories.get(t) == "character" and lookup is not None:
            try:
                row = lookup(t)
            except Exception:
                row = None
            if row:
                for field in ("core_tags", "trigger", "copyright"):
                    from_db.update(key(x) for x in (row.get(field) or "").split(",") if x.strip())

    out: list[str] = []
    for t in tags:
        k = key(t)
        if k in keep_keys or _COUNT.match(k):
            continue
        if categories.get(t) in IDENTITY_CATEGORIES:
            out.append(t)
        elif (k in from_db or _TRAIT.match(k)) and not is_clothing(k):
            out.append(t)
    return out


def clothing_tags(
    tags: Iterable[str],
    categories: dict[str, str],
    keep: Iterable[str] = (),
) -> list[str]:
    """Tags among `tags` that are clothing or accessories (`keep` excluded)."""
    keep_keys = {key(t) for t in keep}
    return [t for t in tags
            if key(t) not in keep_keys
            and categories.get(t) not in IDENTITY_CATEGORIES | {"artist"}
            and is_clothing(t)]
