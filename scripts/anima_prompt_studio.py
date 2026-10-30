"""Anima Prompt Studio for Stable Diffusion WebUI Forge (Neo).

An always-visible panel in txt2img and img2img:

    image -> WD tagger (local)  ┐
    Danbooru post URL -> API   ├-> tags -> (optional LLM) -> prompt -> Forge
    idea -> LLM                │
    character/artist -> local DB ┘

Safety rules:
  * the WebUI virtual environment is never touched: onnxruntime-directml lives
    in vendor/ inside this extension and is put on sys.path only here;
  * the bundled modules are loaded under the private name `anima_core`, so they
    cannot clash with anything in Forge;
  * any import error disables the extension instead of breaking the WebUI.
"""
from __future__ import annotations

import importlib.util
import pathlib
import sys
import threading
import time
import traceback

# --------------------------------------------------------------------------- #
# 0. Bootstrap
# --------------------------------------------------------------------------- #

EXT_DIR = pathlib.Path(__file__).resolve().parent.parent
VENDOR_DIR = EXT_DIR / "vendor"
CORE_DIR = EXT_DIR / "anima_core"
LOG_PATH = EXT_DIR / "anima.log"

AVAILABLE = False
BOOT_ERROR = ""

CFG = None
TAGGER_SERVICE = None
BOORU = None
LLM = None
ENGINE = None
SERVER = None
BROWSER = None

_TAGGER_LOCK = threading.Lock()


def _log(msg: str) -> None:
    """Append to anima.log next to the extension (local only, git-ignored)."""
    try:
        with open(LOG_PATH, "a", encoding="utf-8") as fh:
            fh.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}\n")
    except Exception:
        pass


try:
    # 1) onnxruntime-directml from vendor/ must come BEFORE site-packages,
    #    because anima_core.tagger imports onnxruntime at import time
    if (VENDOR_DIR / "onnxruntime").exists():
        sys.path.insert(0, str(VENDOR_DIR))

    # 2) bundled modules, loaded as the package `anima_core`
    _spec = importlib.util.spec_from_file_location(
        "anima_core", CORE_DIR / "__init__.py",
        submodule_search_locations=[str(CORE_DIR)],
    )
    _mod = importlib.util.module_from_spec(_spec)
    sys.modules["anima_core"] = _mod
    _spec.loader.exec_module(_mod)

    from anima_core import prompt_builder as pb                     # noqa: E402
    from anima_core.booru import (BlockedError, BooruClient,         # noqa: E402
                                  parse_pasted, parse_post_id)
    from anima_core import tag_browser as tb                        # noqa: E402
    from anima_core.character_tags import character_tags, clothing_tags  # noqa: E402
    from anima_core.config import DEFAULTS, MODELS_DIR, Config      # noqa: E402
    from anima_core.llm import LLMClient                            # noqa: E402
    from anima_core.prompts import PromptEngine                     # noqa: E402
    from anima_core.server_manager import ServerManager             # noqa: E402
    from anima_core.tagger import (KNOWN_TAGGERS, TaggerService,    # noqa: E402
                                   download_model, model_present)
    from anima_core.textutils import split_tags                     # noqa: E402
    from anima_core import tempclean                                # noqa: E402

    CFG = Config()
    SERVER = ServerManager(CFG, logger=_log)
    AVAILABLE = True
except Exception as _exc:  # pragma: no cover
    BOOT_ERROR = f"{type(_exc).__name__}: {_exc}"
    traceback.print_exc()

import gradio as gr                                        # noqa: E402
from modules import script_callbacks, scripts              # noqa: E402


def _rebuild_clients() -> None:
    """(Re)create the clients after settings change."""
    global BOORU, LLM, ENGINE, BROWSER
    BOORU = BooruClient(CFG)
    LLM = LLMClient(CFG)
    ENGINE = PromptEngine(LLM, CFG)
    BROWSER = tb.TagBrowser(CFG)


if AVAILABLE:
    _rebuild_clients()


# --------------------------------------------------------------------------- #
# Temporary image copies
# --------------------------------------------------------------------------- #
# To show an image in the browser, Gradio keeps a copy of it on disk: uploads
# go to its upload folder (%TEMP%\gradio by default), images we display go to
# Forge's temp_dir. The tagger only needs the picture in memory, so the copy
# used for tagging is deleted right away, and optionally every leftover copy
# is deleted when the WebUI starts.

_LAST_IMAGE: dict[str, object] = {"path": None, "image": None}


def _temp_dirs() -> list[pathlib.Path]:
    dirs: list[pathlib.Path] = []
    try:
        from gradio.utils import get_upload_folder
        dirs.append(pathlib.Path(get_upload_folder()).resolve())
    except Exception:
        pass
    try:
        from modules import shared
        if getattr(shared.opts, "temp_dir", ""):
            dirs.append(pathlib.Path(shared.opts.temp_dir).resolve())
    except Exception:
        pass
    return dirs


def _is_temp_copy(path: pathlib.Path) -> bool:
    try:
        p = path.resolve()
        return any(p.is_relative_to(d) for d in _temp_dirs())
    except Exception:
        return False


def _delete_temp_copy(path: pathlib.Path) -> None:
    """Delete a Gradio/Forge temporary copy; never touches other files."""
    if not _is_temp_copy(path):
        return
    for _ in range(3):
        try:
            path.unlink(missing_ok=True)
            return
        except PermissionError:
            # PIL may still hold the file open on Windows
            import gc
            gc.collect()
            time.sleep(0.2)
        except Exception:
            return


def _track_image_source(component) -> None:
    """Remember which file each image passed to the tagger came from.

    Gradio opens the temporary copy and hands us only the picture. Wrapping
    the component's preprocess lets us attach the path, so the copy can be
    deleted after tagging. If the browser sends the same (now deleted) file
    again, the picture kept in memory is reused.
    """
    original = component.preprocess

    def preprocess(payload):
        if payload is not None:
            src = str(getattr(payload, "path", "") or "")
            if src and not pathlib.Path(src).exists() and src == _LAST_IMAGE["path"]:
                return _LAST_IMAGE["image"]
        img = original(payload)
        if img is not None and payload is not None:
            try:
                img.info["anima_source"] = str(payload.path)
            except Exception:
                pass
        return img

    component.preprocess = preprocess


def _output_dirs() -> list[pathlib.Path]:
    """Folders where Forge saves images: never cleaned, whatever the settings."""
    out = [pathlib.Path("output").resolve(), pathlib.Path("outputs").resolve()]
    try:
        from modules import shared
        for key in ("outdir_samples", "outdir_txt2img_samples", "outdir_img2img_samples",
                    "outdir_extras_samples", "outdir_grids", "outdir_txt2img_grids",
                    "outdir_img2img_grids", "outdir_save", "outdir_init_images"):
            value = getattr(shared.opts, key, "")
            if value:
                out.append(pathlib.Path(value).resolve())
    except Exception:
        pass
    return out


def _setup_temp_cleanup() -> None:
    """Delete temporary images when the WebUI starts and when it closes.

    Runs once per WebUI process, not again on "Reload UI". The folders are
    resolved now, while Forge's settings are loaded; the setting itself is
    read again at exit, so turning it off takes effect without a restart.
    """
    import os
    if not AVAILABLE or os.environ.get("ANIMA_PS_TEMP_CLEANUP"):
        return
    os.environ["ANIMA_PS_TEMP_CLEANUP"] = "1"
    dirs, outputs = _temp_dirs(), _output_dirs()

    def clean(when: str) -> None:
        if CFG.get("privacy", "clean_temp_on_start", default=False):
            n = tempclean.clean_dirs(dirs, outputs, log=lambda m: _log(f"{when}: {m}"))
            _log(f"{when}: removed {n} temporary image(s)")

    clean("startup")
    tempclean.on_exit(lambda: clean("exit"))


_setup_temp_cleanup()


# --------------------------------------------------------------------------- #
# 1. Capture Forge's prompt fields
# --------------------------------------------------------------------------- #

PROMPT_COMPONENTS: dict[str, object] = {}

_WANTED = ("txt2img_prompt", "img2img_prompt", "txt2img_neg_prompt", "img2img_neg_prompt")

# CSS is injected with a <style> inside gr.HTML: a style.css file next to the
# extension is not reliably applied by Forge Neo.
_CSS = """
/* fixed-height tag list with internal scrolling, so 60+ tags never overlap
   the buttons below */
#anima_tags_txt2img, #anima_tags_img2img {
    height: 216px;
    min-height: 216px;
    overflow-y: auto;
    overflow-x: hidden;
    padding: 6px 8px;
    border: 1px solid var(--border-color-primary, #444);
    border-radius: 6px;
    background: var(--background-fill-secondary, transparent);
}
#anima_tags_txt2img .wrap, #anima_tags_img2img .wrap {
    max-height: 100%;
    overflow-y: auto;
}
#anima_tags_txt2img label, #anima_tags_img2img label {
    font-size: 0.83em;
    line-height: 1.22;
}
#anima_img_txt2img video, #anima_img_img2img video { object-fit: contain; }
/* results are already sorted by popularity; sorting by a header click would
   reorder the rows on screen only, and a click would then pick the wrong one */
#anima_br_txt2img thead, #anima_br_img2img thead { pointer-events: none; }
#anima_br_txt2img thead .sort-button, #anima_br_img2img thead .sort-button { display: none; }
"""

# Gradio 4's table draws only the rows in view: if it is scrolled down when
# new, shorter results arrive, it shows nothing. Scroll it back to the top
# (only the table of the tab being searched; TAB is replaced per tab).
_JS_TABLE_TOP = """
(...args) => {
    const root = document.querySelector('#anima_br_TAB');
    if (root) root.querySelectorAll('*').forEach(el => { if (el.scrollTop) el.scrollTop = 0; });
    return args;
}
"""


def _grab_component(component=None, **kwargs):
    """Keep a reference to Forge's prompt fields so we can write into them."""
    try:
        eid = kwargs.get("elem_id") or getattr(component, "elem_id", None)
        if eid in _WANTED:
            PROMPT_COMPONENTS[eid] = component
    except Exception:
        pass


# registered at module level: the prompt fields are built before the scripts UI
script_callbacks.on_after_component(_grab_component)


# --------------------------------------------------------------------------- #
# 2. Shared services (lazy: they do not slow down WebUI startup)
# --------------------------------------------------------------------------- #

def get_tagger():
    global TAGGER_SERVICE
    with _TAGGER_LOCK:
        if TAGGER_SERVICE is None:
            TAGGER_SERVICE = TaggerService(CFG)
            TAGGER_SERVICE.start()
        return TAGGER_SERVICE


def reset_tagger() -> None:
    """Unload the tagger so the next use picks up new settings."""
    global TAGGER_SERVICE
    with _TAGGER_LOCK:
        if TAGGER_SERVICE is not None:
            try:
                TAGGER_SERVICE.unload()
            except Exception:
                pass
        TAGGER_SERVICE = None


def _ensure_llm():
    """Make sure the language model server answers; start it if we manage it.

    Generator: yields a status line while a local server is starting.
    """
    if LLM.is_up():
        return
    if SERVER.enabled and SERVER.is_local:
        yield "⏳ starting the local llama-server (Settings → Auto start/stop is on)…"
        SERVER.start(wait=True)
        return
    raise RuntimeError(
        f"Cannot reach {LLM.base_url or '(no URL set)'}. Start your server, or check "
        "Settings → Inference server."
    )


# --------------------------------------------------------------------------- #
# 3. Button handlers
# --------------------------------------------------------------------------- #

def _split_tags(text: str) -> list[str]:
    return [t.strip() for t in (text or "").split(",") if t.strip()]


# The tag list is built from two parts, kept per tab in a gr.State:
#   base  - tags from the image, the Danbooru post or the idea (with categories)
#   added - tags added from the character/artist browser, one entry per type
# Tagging a new image replaces `base` but keeps what was added from the browser.

def _new_meta() -> dict:
    return {"base": [], "cats": {}, "added": {tb.CHARACTERS: [], tb.ARTISTS: []}}


def _choices(meta: dict) -> list[str]:
    added = meta["added"]
    return list(dict.fromkeys(added[tb.CHARACTERS] + added[tb.ARTISTS] + meta["base"]))


def _tags_out(meta: dict, checked) -> tuple:
    """CheckboxGroup update + positive prompt for the given checked tags."""
    choices = _choices(meta)
    checked = set(checked or [])
    value = [t for t in choices if t in checked]
    return gr.update(choices=choices, value=value), do_rebuild(value)


def _set_base(meta, tags: list[str], cats: dict[str, str]) -> tuple:
    """New tags from the image/post/idea; browser tags stay. Returns (tags update, positive, meta)."""
    meta = dict(meta or _new_meta())
    meta.update(base=list(tags), cats=dict(cats))
    update, positive = _tags_out(meta, _choices(meta))
    return update, positive, meta


def do_tag_image(img, gen_thr, char_thr, meta):
    """Image -> tags with the local tagger. Returns tags, positive, negative, status, meta."""
    if img is None:
        return gr.update(), gr.update(), gr.update(), "⚠️ load an image first", meta
    # keep the picture in memory and delete the temporary copy on disk
    src = img.info.get("anima_source") if hasattr(img, "info") else None
    if src:
        _LAST_IMAGE.update(path=src, image=img)
        _delete_temp_copy(pathlib.Path(src))
    try:
        svc = get_tagger()
        svc.ready.wait(timeout=180)
        if svc.error:
            err = svc.error
            reset_tagger()  # allow a retry after fixing settings / downloading
            return gr.update(), gr.update(), gr.update(), f"❌ tagger: {err}", meta
        pred = svc.predict(
            img,
            general_threshold=float(gen_thr),
            character_threshold=float(char_thr),
            rating_threshold=float(CFG.get("tagger", "rating_threshold", default=0.30)),
        )
        data = pb.from_tagger(pred, CFG)
        tags = data["tags"]
        cats = {t: "character" for t, _ in pred.get("character", [])}
        update, positive, meta = _set_base(meta, tags, cats)
        return (
            update,
            positive,
            data["negative"],
            f"✅ **{len(tags)} tags** · {pred.get('provider', '')} · "
            f"rating: **{data['rating_label']}**",
            meta,
        )
    except Exception as exc:
        _log(f"tag_image: {exc}\n{traceback.format_exc(limit=3)}")
        return gr.update(), gr.update(), gr.update(), f"❌ {type(exc).__name__}: {exc}", meta


def do_analyze_url(url, meta):
    """Danbooru URL -> exact tags from the API (no model involved) + preview.

    Returns tags, positive, negative, status, meta, image.
    """
    url = (url or "").strip()
    if not url:
        return gr.update(), gr.update(), gr.update(), "⚠️ paste a URL", meta, gr.update()
    try:
        parsed = parse_post_id(url)
        if not parsed:
            return gr.update(), gr.update(), gr.update(), "⚠️ not a valid Danbooru post link", meta, gr.update()
        host, post_id = parsed
        try:
            res = BOORU.by_post_id(post_id, host)
        except BlockedError as exc:
            # only a real browser passes Cloudflare's check: let it fetch the data
            link = f"https://{host}/posts/{post_id}.json"
            return (gr.update(), gr.update(), gr.update(),
                    f"⚠️ {exc}\n\n**Workaround:** open the post (or [{link}]({link})) in your browser, "
                    "press **Ctrl+A** then **Ctrl+C**, paste into *Paste Danbooru post data* "
                    "below and press **Read pasted data**.",
                    meta, gr.update())
        return _apply_booru(res, meta)
    except Exception as exc:
        _log(f"analyze_url: {exc}")
        return gr.update(), gr.update(), gr.update(), f"❌ {type(exc).__name__}: {exc}", meta, gr.update()


def do_paste_booru(text, meta):
    """Post data copied from the browser (the post's .json page) -> tags + preview."""
    if not (text or "").strip():
        return gr.update(), gr.update(), gr.update(), "⚠️ paste the post data first", meta, gr.update()
    try:
        return _apply_booru(parse_pasted(text), meta)
    except Exception as exc:
        _log(f"paste_booru: {exc}")
        return gr.update(), gr.update(), gr.update(), f"❌ {exc}", meta, gr.update()


def _apply_booru(res, meta):
    """BooruResult -> tags, positive, negative, status, meta, image."""
    data = pb.from_booru(res, CFG)
    tags = list(data["tags"])
    # same spelling as from_booru uses for the tag list
    cats = {t.replace("_", " "): cat
            for cat, group in (("character", res.character), ("copyright", res.copyright),
                               ("artist", res.artist))
            for t in group}
    update, positive, meta = _set_base(meta, tags, cats)
    status = (f"✅ **post #{res.post_id}** · {len(tags)} tags · "
              f"rating: **{data['rating_label']}**")
    image = gr.update()
    if res.preview_url:
        try:
            image = BOORU.fetch_image(res.preview_url)
        except Exception as exc:
            _log(f"preview: {exc}")
            status += " · preview not available (you can drop the saved image above)"
    elif res.raw:
        status += " · no preview (restricted post)"
    else:
        # read from the page text, which has no image address
        status += " · no preview from page text (you can drop the saved image above)"
    return update, positive, data["negative"], status, meta, image


def do_rebuild(selected):
    """Rebuild the positive prompt from the checked tags."""
    tags = list(selected or [])
    if not tags:
        return ""
    return pb.build_positive(tags, CFG)


def do_refine(selected, current_pos, natural_language):
    """LLM: clean up the tags, or turn them into natural language."""
    text = ", ".join(selected or []) or (current_pos or "")
    if not text.strip():
        yield current_pos, "⚠️ no tags to refine"
        return
    try:
        for msg in _ensure_llm():
            yield current_pos, msg
        if natural_language:
            out, done = ENGINE.to_natural_language(text), "✅ converted to natural language"
        else:
            # same normalization and default prefix as tags built locally
            out = pb.build_positive(split_tags(ENGINE.refine_tags(text)), CFG)
            done = "✅ refined"
        yield out, done
    except Exception as exc:
        _log(f"refine: {exc}")
        yield current_pos, f"❌ {type(exc).__name__}: {exc}"


def do_idea(idea, meta):
    """Idea (any language) -> full tag set."""
    if not (idea or "").strip():
        yield gr.update(), gr.update(), "⚠️ write an idea first", meta
        return
    try:
        for msg in _ensure_llm():
            yield gr.update(), gr.update(), msg, meta
        out = ENGINE.idea_to_tags(idea)
        tags = [t for t in _split_tags(out) if not pb.is_unwanted(t)]
        # the prompt is rebuilt from the filtered tags, never the raw answer
        update, positive, meta = _set_base(meta, tags, {})
        yield update, positive, f"✅ idea expanded into {len(tags)} tags", meta
    except Exception as exc:
        _log(f"idea: {exc}")
        yield gr.update(), gr.update(), f"❌ {type(exc).__name__}: {exc}", meta


def do_save_defaults(default_pos, default_neg, selected, current_pos):
    """Save the default prompts and apply them right away."""
    try:
        CFG.set("output", "quality_prefix", default_pos or "")
        CFG.set("output", "negative_prompt", default_neg or "")
        CFG.save()
        pos = do_rebuild(selected) or (current_pos or "")
        return pos, (default_neg or ""), "✅ default prompts saved"
    except Exception as exc:
        _log(f"save_defaults: {exc}")
        return current_pos, default_neg, f"❌ {type(exc).__name__}: {exc}"


# ------------------------------------------------ character / artist browser

def _copy_meta(meta) -> dict:
    meta = meta or _new_meta()
    return {"base": list(meta["base"]), "cats": dict(meta["cats"]),
            "added": {k: list(v) for k, v in meta["added"].items()}}


def _strip_characters(meta: dict, checked: list[str]) -> list[str]:
    """Checked tags that describe the image's character (browser tags excluded)."""
    keep = meta["added"][tb.CHARACTERS] + meta["added"][tb.ARTISTS]
    # one query for all the recognized characters
    rows = BROWSER.lookup_characters(t for t in checked if meta["cats"].get(t) == "character")
    return character_tags(checked, meta["cats"], rows.get, keep=keep)


def do_browse(mode, query, series, min_count, page):
    """Search the local database. Returns table rows, rows (state), status, selection."""
    try:
        rows = BROWSER.search(mode, query, series, min_count, page)
    except Exception as exc:
        _log(f"browse: {exc}")
        return [], [], f"❌ {type(exc).__name__}: {exc}", None
    if mode == tb.CHARACTERS:
        table = [[r["trigger"], r["count"], r["core_tags"]] for r in rows]
    else:
        table = [["@" + r["trigger"], r["count"], ""] for r in rows]
    status = f"{len(rows)} results (page {int(page or 1)})" if rows else "No results."
    return table, rows, status, None


def do_browse_click(mode, rows, with_tags, evt: gr.SelectData):
    """Row clicked: preview, info line, tags to add, selected row."""
    return (*do_browse_select(mode, rows, with_tags, evt.index[0]), evt.index[0])


def do_browse_select(mode, rows, with_tags, index):
    """Preview, info line and tags to add for the selected row."""
    if index is None or not rows or int(index) >= len(rows):
        return gr.update(), gr.update(), gr.update()
    row = rows[int(index)]
    info = (f"**{row['trigger']}** · {row['count']:,} posts"
            + (f" · series `{row['copyright']}`" if mode == tb.CHARACTERS else ""))
    image = None
    path = BROWSER.thumb(mode, row["trigger"])
    if path:
        try:
            from PIL import Image
            with Image.open(path) as im:
                image = im.copy()  # in memory: the file in data/ stays untouched
        except Exception as exc:
            _log(f"thumbnail: {exc}")
    return image, info, ", ".join(tb.entry_tags(mode, row, with_tags))


def do_browse_retag(mode, rows, with_tags, index):
    """'Include appearance tags' toggled: only the tags to add change."""
    if index is None or not rows or int(index) >= len(rows):
        return gr.update()
    return ", ".join(tb.entry_tags(mode, rows[int(index)], with_tags))


def do_browse_add(mode, text, checked, meta, replace, strip):
    """Add a character/artist to the tag list. Returns tags, positive, meta, status."""
    tags = split_tags(text)
    if not tags:
        return gr.update(), gr.update(), meta, "⚠️ click a row in the results first"
    meta = _copy_meta(meta)
    checked = list(checked or [])
    dropped: list[str] = []
    if replace:
        old = [t for t in meta["added"][mode] if t not in meta["base"]]
        checked = [t for t in checked if t not in old]
        meta["added"][mode] = []
    if strip and mode == tb.CHARACTERS:
        dropped = _strip_characters(meta, checked)
        checked = [t for t in checked if t not in dropped]
    meta["added"][mode] = list(dict.fromkeys(meta["added"][mode] + tags))
    update, positive = _tags_out(meta, checked + tags)
    status = f"✅ added {len(tags)} tag(s)"
    if dropped:
        status += f" · unchecked {len(dropped)} tag(s) of the image's character"
    return update, positive, meta, status


def do_browse_remove(mode, checked, meta):
    """Remove the character (or artist) added from the browser."""
    meta = _copy_meta(meta)
    old = [t for t in meta["added"][mode] if t not in meta["base"]]
    if not meta["added"][mode]:
        return gr.update(), gr.update(), meta, f"nothing added from {mode.lower()}"
    meta["added"][mode] = []
    update, positive = _tags_out(meta, [t for t in (checked or []) if t not in old])
    return update, positive, meta, f"✅ removed the {mode[:-1].lower()} added from the browser"


def _uncheck(meta, checked: list[str], dropped: list[str], what: str):
    """Tags, positive and status after unchecking `dropped`."""
    if not dropped:
        return gr.update(), gr.update(), f"no {what} tags found among the checked tags"
    update, positive = _tags_out(meta, [t for t in checked if t not in dropped])
    shown = ", ".join(dropped[:12]) + (" …" if len(dropped) > 12 else "")
    return update, positive, f"✅ unchecked {len(dropped)} {what} tag(s): {shown}"


def do_strip_character(checked, meta):
    """Uncheck the image's character tags (name, series, hair, eyes…)."""
    meta = meta or _new_meta()
    checked = list(checked or [])
    return _uncheck(meta, checked, _strip_characters(meta, checked), "character")


def do_strip_clothing(checked, meta):
    """Uncheck the clothing and accessories (browser tags excluded)."""
    meta = meta or _new_meta()
    checked = list(checked or [])
    keep = meta["added"][tb.CHARACTERS] + meta["added"][tb.ARTISTS]
    return _uncheck(meta, checked, clothing_tags(checked, meta["cats"], keep=keep), "clothing")


def do_download_data(data_dir):
    # the folder in the field, even if not saved yet
    browser = tb.TagBrowser(_TempCfg({("tag_browser", "data_dir"): data_dir or ""}))
    try:
        for line in tb.download_data(browser.data_dir):
            yield f"⏳ {line}"
        yield "⏳ building the database…"
        yield browser.build_db() + " Press **Save settings** if you changed the folder."
    except Exception as exc:
        _log(f"download_data: {exc}")
        yield f"❌ {type(exc).__name__}: {exc}"


# ------------------------------------------------------------ server buttons

def do_server_status():
    try:
        if not SERVER.enabled:
            # the chat client sends the API key, so it also works for remote servers
            if not LLM.base_url:
                return "⚪ no server URL set (Settings → Inference server)"
            return (f"🟢 running · {LLM.base_url}" if LLM.is_up()
                    else f"⚪ not reachable · {LLM.base_url}")
        return SERVER.status()
    except Exception as exc:
        return f"❓ unknown state: {exc}"


_NOT_MANAGED = (
    "ℹ️ Your server ({url}) is run by its own app (LM Studio, Ollama, Jan…): "
    "{action} it there. Start/Stop here only work with *Settings → Auto start/stop*."
)


def do_server_start():
    if not SERVER.enabled:
        state = "🟢 it is running" if LLM.is_up() else "⚪ it is not running"
        return _NOT_MANAGED.format(url=LLM.base_url or "no URL set", action="start") + f" Now: {state}."
    try:
        return f"🟢 {SERVER.start()}"
    except Exception as exc:
        _log(f"server start: {exc}")
        return f"❌ {exc}"


def do_server_stop():
    if not SERVER.enabled:
        return _NOT_MANAGED.format(url=LLM.base_url or "no URL set", action="stop")
    try:
        return f"⚪ {SERVER.stop()}"
    except Exception as exc:
        _log(f"server stop: {exc}")
        return f"❌ {exc}"


# ----------------------------------------------------------- settings handlers

# (section, key) of every field in the Settings panel, in UI order
SETTINGS_FIELDS = [
    ("llm", "base_url"), ("llm", "api_key"), ("llm", "model"),
    ("llm", "temperature"), ("llm", "max_tokens"), ("llm", "timeout"),
    ("llm", "disable_thinking"), ("llm", "allow_explicit"),
    ("local_server", "enabled"), ("local_server", "llama_server"),
    ("local_server", "model"), ("local_server", "ctx"), ("local_server", "ngl"),
    ("local_server", "extra_args"), ("local_server", "stop_on_generate"),
    ("tagger", "model_repo"), ("tagger", "model_dir"),
    ("tagger", "use_gpu"), ("tagger", "device_id"), ("tagger", "unload_on_generate"),
    ("tagger", "general_threshold"), ("tagger", "character_threshold"),
    ("booru", "danbooru_login"), ("booru", "danbooru_api_key"),
    ("output", "underscores"), ("output", "escape_parens"),
    ("output", "max_tags"), ("output", "dedupe"), ("output", "keep_artist"),
    ("tag_browser", "data_dir"),
    ("privacy", "clean_temp_on_start"),
]

_TAGGER_KEYS = {("tagger", "model_repo"), ("tagger", "model_dir"),
                ("tagger", "use_gpu"), ("tagger", "device_id")}


def _coerce(section: str, key: str, value):
    """Cast a UI value to the type of the default."""
    default = DEFAULTS[section][key]
    if value is None:
        return default
    if isinstance(default, bool):
        return bool(value)
    if isinstance(default, int):
        return int(value)
    if isinstance(default, float):
        return float(value)
    return str(value).strip()


def do_save_settings(*values):
    try:
        tagger_changed = False
        for (section, key), value in zip(SETTINGS_FIELDS, values):
            new = _coerce(section, key, value)
            if (section, key) in _TAGGER_KEYS and CFG.get(section, key) != new:
                tagger_changed = True
            CFG.set(section, key, new)
        CFG.save()
        _rebuild_clients()
        if tagger_changed:
            reset_tagger()
        return "✅ settings saved and applied"
    except Exception as exc:
        _log(f"save_settings: {exc}")
        return f"❌ {type(exc).__name__}: {exc}"


class _TempCfg:
    """Read-only view of the settings with a few values overridden (unsaved UI)."""

    def __init__(self, overrides: dict):
        self.overrides = overrides

    def get(self, *keys, default=None):
        if keys in self.overrides:
            return self.overrides[keys]
        return CFG.get(*keys, default=default)


def do_fetch_models(base_url, api_key, current):
    client = LLMClient(_TempCfg({("llm", "base_url"): base_url or "",
                                 ("llm", "api_key"): api_key or ""}))
    models = client.list_models(timeout=5)
    if not models:
        return gr.update(), f"❌ no answer from {client.base_url or '(empty URL)'}"
    value = current if current in models else models[0]
    return gr.update(choices=models, value=value), f"✅ {len(models)} model(s) found on {client.base_url}"


def do_download_tagger(repo, model_dir):
    repo = (repo or "").strip()
    if not repo:
        yield "⚠️ choose a model first"
        return
    dest = pathlib.Path(model_dir.strip()) if (model_dir or "").strip() else MODELS_DIR / repo.split("/")[-1]
    if model_present(dest):
        yield f"✅ already present in {dest}"
        return
    try:
        for line in download_model(repo, dest):
            yield f"⏳ {line}"
        reset_tagger()
        yield f"✅ downloaded to {dest}. Press **Save settings** if you changed the model."
    except Exception as exc:
        _log(f"download_tagger: {exc}")
        yield f"❌ {type(exc).__name__}: {exc}"


# --------------------------------------------------------------------------- #
# 4. The script: always-visible panel
# --------------------------------------------------------------------------- #

def _exclude_from_ui_config(block) -> None:
    """Mark a block and everything inside it as not saved to ui-config.json."""
    block.do_not_save_to_config = True
    for child in getattr(block, "children", None) or []:
        _exclude_from_ui_config(child)


class AnimaPromptStudio(scripts.Script):

    def title(self):
        return "Anima Prompt Studio"

    def show(self, is_img2img):
        return scripts.AlwaysVisible

    # ------------------------------------------------------------------ UI
    def ui(self, is_img2img):
        tab = "img2img" if is_img2img else "txt2img"
        pos_target = PROMPT_COMPONENTS.get(f"{tab}_prompt")
        neg_target = PROMPT_COMPONENTS.get(f"{tab}_neg_prompt")

        # outside the accordion, so the CSS is in the DOM even when collapsed
        _exclude_from_ui_config(gr.HTML(f"<style>{_CSS}</style>"))

        with gr.Accordion("Anima Prompt Studio", open=False, elem_id=f"anima_root_{tab}") as root:
            if not AVAILABLE:
                gr.Markdown(f"**Extension not active.**\n\n```\n{BOOT_ERROR}\n```")
                return []

            gr.Markdown(
                "Image or Danbooru link → tags → prompt. The tagger and the character/artist "
                "search run locally; the language model is only used by *Refine* and *Expand idea*."
            )

            # --- row 1: image on the left, URL + status on the right -------
            with gr.Row():
                with gr.Column(scale=1, min_width=220):
                    img_in = gr.Image(
                        label="Image", type="pil", height=170,
                        sources=["upload", "clipboard"],
                        elem_id=f"anima_img_{tab}",
                    )
                    _track_image_source(img_in)
                with gr.Column(scale=2):
                    url_in = gr.Textbox(
                        label="or Danbooru post URL",
                        placeholder="https://danbooru.donmai.us/posts/12345678",
                        lines=1,
                    )
                    with gr.Row():
                        btn_tag = gr.Button("Tag image", variant="primary")
                        btn_url = gr.Button("Analyze URL")
                    status_md = gr.Markdown("ready")
                    with gr.Accordion("Paste Danbooru post data (when Analyze URL is blocked)",
                                      open=False):
                        paste_in = gr.Textbox(
                            label="Open the post in your browser (normal page or its .json "
                                  "page), press Ctrl+A, Ctrl+C and paste everything here",
                            lines=3, max_lines=6,
                        )
                        btn_paste = gr.Button("Read pasted data")

            # --- row 2: tags, full width ------------------------------------
            tag_box = gr.CheckboxGroup(
                label="Tags — uncheck the ones you don't want",
                choices=[], value=[],
                elem_id=f"anima_tags_{tab}",
            )
            tags_meta = gr.State(_new_meta())

            # --- row 3: refinement -------------------------------------------
            with gr.Row():
                btn_refine = gr.Button("Refine with LLM", variant="primary",
                                       scale=2, min_width=220)
                nl_check = gr.Checkbox(label="as natural language", value=False,
                                       scale=1, min_width=200)
                btn_strip = gr.Button("Remove character tags", scale=1, min_width=200)
                btn_strip_clothes = gr.Button("Remove clothing", scale=1, min_width=160)

            # --- characters & artists from the local database ---------------
            with gr.Accordion("Characters & artists (offline)", open=False):
                gr.Markdown(
                    "Search Danbooru characters and artists in the local database (no "
                    "internet), click a row, then *Add to tags*. The tags go into the list "
                    "above and into the Positive prompt."
                )
                with gr.Row():
                    br_mode = gr.Radio([tb.CHARACTERS, tb.ARTISTS], value=tb.CHARACTERS,
                                       label="Type", scale=1, min_width=200)
                    br_query = gr.Textbox(label="Search (name or tags, comma = AND)",
                                          placeholder="e.g. frieren   or   blue hair, twintails",
                                          scale=3)
                    br_series = gr.Textbox(label="Series", placeholder="e.g. genshin impact",
                                           scale=2)
                with gr.Row():
                    br_min = gr.Slider(0, 5000, value=50, step=10,
                                       label="Minimum posts on Danbooru", scale=3)
                    br_page = gr.Number(value=1, precision=0, minimum=1, label="Page",
                                        scale=1, min_width=100)
                    btn_br_search = gr.Button("Search", variant="primary", scale=1)
                br_status = gr.Markdown("")
                br_results = gr.Dataframe(headers=["Name", "Posts", "Tags"],
                                          datatype=["str", "number", "str"],
                                          interactive=False, wrap=True, height=320,
                                          elem_id=f"anima_br_{tab}")
                br_sel = gr.State(None)
                br_rows = gr.State([])
                with gr.Row():
                    br_thumb = gr.Image(label="Preview", type="pil", interactive=False,
                                        height=200, scale=1, min_width=160)
                    with gr.Column(scale=3):
                        br_info = gr.Markdown("")
                        br_with_tags = gr.Checkbox(value=True,
                                                   label="Include appearance tags (characters)")
                        br_text = gr.Textbox(label="Tags to add (editable)", lines=2)
                        with gr.Row():
                            br_replace = gr.Checkbox(
                                value=True, label="Replace the previous character/artist")
                            br_strip = gr.Checkbox(
                                value=True,
                                label="Remove the image's character tags when adding a character")
                        with gr.Row():
                            btn_br_add = gr.Button("Add to tags", variant="primary")
                            btn_br_remove = gr.Button("Remove from tags", variant="stop")
                            btn_br_rebuild = gr.Button("Rebuild database from CSV")

            with gr.Row():
                pos_box = gr.Textbox(
                    label="Positive prompt", lines=4,
                    placeholder="Filled from the tags, or write your own",
                )
                neg_box = gr.Textbox(
                    label="Negative prompt", lines=4,
                    value=str(CFG.get("output", "negative_prompt", default="")),
                )

            with gr.Row():
                btn_send_pos = gr.Button("Send positive to Forge", variant="primary")
                btn_send_neg = gr.Button("Send negative")
                btn_send_both = gr.Button("Send both")

            with gr.Row():
                idea_in = gr.Textbox(
                    label="From an idea (any language) to a prompt",
                    placeholder="a witch under the moon, pointy hat, autumn atmosphere",
                    scale=3,
                )
                btn_idea = gr.Button("Expand idea", scale=1)

            with gr.Accordion("Tagger thresholds", open=False):
                with gr.Row():
                    gen_sl = gr.Slider(0.10, 0.90, step=0.01, label="General tags threshold",
                                       value=float(CFG.get("tagger", "general_threshold", default=0.35)))
                    char_sl = gr.Slider(0.30, 0.99, step=0.01, label="Character tags threshold",
                                        value=float(CFG.get("tagger", "character_threshold", default=0.85)))

            # --- default prompts ---------------------------------------------
            with gr.Accordion("Default prompts", open=False):
                default_pos = gr.Textbox(
                    label="Prefix added at the start of the positive prompt",
                    value=str(CFG.get("output", "quality_prefix", default="")),
                    lines=2,
                    placeholder="e.g. masterpiece, best quality, <lora:my_lora:1>",
                )
                default_neg = gr.Textbox(
                    label="Default negative prompt",
                    value=str(CFG.get("output", "negative_prompt", default="")),
                    lines=3,
                )
                btn_save_defaults = gr.Button("Save as default", variant="primary")
                gr.Markdown("The prefix is added every time the tags are rebuilt.")

            # --- language model server ---------------------------------------
            with gr.Accordion("Language model server", open=False):
                srv_md = gr.Markdown("⚪ state not checked")
                with gr.Row():
                    btn_srv_on = gr.Button("Start server")
                    btn_srv_off = gr.Button("Stop server")
                    btn_srv_state = gr.Button("Check status")
                gr.Markdown(
                    "*Check status* works with any server. *Start* / *Stop* only work when "
                    "*Settings → Auto start/stop* is on; LM Studio, Ollama, Jan and other "
                    "servers are started and stopped from their own app. A server started "
                    "here stops **by itself when you press Generate** and when the WebUI closes."
                )

            # --- settings (only in txt2img, so two copies can't overwrite each other)
            settings_inputs = []
            if not is_img2img:
                settings_inputs = self._settings_ui(gen_sl, char_sl)
            else:
                gr.Markdown("*Settings are in the txt2img tab.*")

            # ------------------------------------------------------- events
            tag_outputs = [tag_box, pos_box, neg_box, status_md, tags_meta]

            btn_tag.click(do_tag_image, inputs=[img_in, gen_sl, char_sl, tags_meta],
                          outputs=tag_outputs)
            btn_url.click(do_analyze_url, inputs=[url_in, tags_meta], outputs=tag_outputs + [img_in])
            btn_paste.click(do_paste_booru, inputs=[paste_in, tags_meta],
                            outputs=tag_outputs + [img_in])
            tag_box.change(do_rebuild, inputs=[tag_box], outputs=[pos_box])
            btn_refine.click(do_refine, inputs=[tag_box, pos_box, nl_check],
                             outputs=[pos_box, status_md])
            btn_strip.click(do_strip_character, inputs=[tag_box, tags_meta],
                            outputs=[tag_box, pos_box, status_md])
            btn_strip_clothes.click(do_strip_clothing, inputs=[tag_box, tags_meta],
                                    outputs=[tag_box, pos_box, status_md])
            btn_idea.click(do_idea, inputs=[idea_in, tags_meta],
                           outputs=[tag_box, pos_box, status_md, tags_meta])

            br_inputs = [br_mode, br_query, br_series, br_min, br_page]
            js_top = _JS_TABLE_TOP.replace("TAB", tab)
            br_outputs = [br_results, br_rows, br_status, br_sel]
            btn_br_search.click(do_browse, br_inputs, br_outputs, js=js_top)
            br_query.submit(do_browse, br_inputs, br_outputs, js=js_top)
            br_series.submit(do_browse, br_inputs, br_outputs, js=js_top)
            br_mode.change(lambda: 1, None, br_page).then(do_browse, br_inputs, br_outputs,
                                                          js=js_top)
            br_results.select(do_browse_click, [br_mode, br_rows, br_with_tags],
                              [br_thumb, br_info, br_text, br_sel])
            br_with_tags.change(do_browse_retag, [br_mode, br_rows, br_with_tags, br_sel],
                                [br_text])
            btn_br_add.click(do_browse_add,
                             [br_mode, br_text, tag_box, tags_meta, br_replace, br_strip],
                             [tag_box, pos_box, tags_meta, br_status])
            btn_br_remove.click(do_browse_remove, [br_mode, tag_box, tags_meta],
                                [tag_box, pos_box, tags_meta, br_status])
            btn_br_rebuild.click(lambda: BROWSER.build_db(), None, br_status)
            btn_save_defaults.click(
                do_save_defaults,
                inputs=[default_pos, default_neg, tag_box, pos_box],
                outputs=[pos_box, neg_box, status_md],
            )

            btn_srv_state.click(do_server_status, outputs=[srv_md])
            btn_srv_on.click(do_server_start, outputs=[srv_md])
            btn_srv_off.click(do_server_stop, outputs=[srv_md])

            # write into Forge's real prompt fields
            if pos_target is not None:
                btn_send_pos.click(lambda t: t, inputs=[pos_box], outputs=[pos_target])
            else:
                btn_send_pos.click(lambda: "⚠️ cannot reach Forge's prompt field: copy it by hand",
                                   outputs=[status_md])
            if neg_target is not None:
                btn_send_neg.click(lambda t: t, inputs=[neg_box], outputs=[neg_target])
            if pos_target is not None and neg_target is not None:
                btn_send_both.click(lambda p, n: (p, n), inputs=[pos_box, neg_box],
                                    outputs=[pos_target, neg_target])

        # Forge remembers component values in ui-config.json and restores them
        # over ours at every start: settings would show stale values and API
        # keys would be copied there. settings.json is the only source of truth.
        _exclude_from_ui_config(root)

        # nothing is passed to process(): the panel never changes generation
        return []

    # --------------------------------------------------------------- settings
    def _settings_ui(self, gen_sl, char_sl):
        """Build the Settings panel; returns nothing (events are wired here)."""
        c = lambda *k: CFG.get(*k)  # noqa: E731

        with gr.Accordion("Settings", open=False):
            with gr.Tab("Inference server"):
                gr.Markdown(
                    "Any **OpenAI-compatible** server. Default addresses: LM Studio "
                    "`http://127.0.0.1:1234/v1` · Ollama `http://127.0.0.1:11434/v1` · "
                    "llama.cpp `http://127.0.0.1:8080/v1` · Jan `http://127.0.0.1:1337/v1` · "
                    "Unsloth Studio `http://127.0.0.1:8888/v1` (needs its API key) · "
                    "KoboldCpp `http://127.0.0.1:5001/v1`. More in the README. "
                    "Only this address is ever contacted."
                )
                auto_note = gr.Markdown(
                    "⚠️ **Auto start/stop is on**: when this address does not answer, the "
                    "extension launches its own llama-server on it. Turn it off in the "
                    "*Auto start/stop* tab if you use LM Studio, Ollama or another server "
                    "that you start yourself.",
                    visible=bool(c("local_server", "enabled")),
                )
                base_url = gr.Textbox(label="Server URL (ending in /v1)", value=c("llm", "base_url"),
                                      placeholder="e.g. http://127.0.0.1:1234/v1 — if empty with "
                                                  "Auto start/stop on: http://127.0.0.1:8080/v1")
                api_key = gr.Textbox(label="API key (optional)", value=c("llm", "api_key"),
                                     type="password")
                with gr.Row():
                    model = gr.Dropdown(label="Model (empty = first one on the server)",
                                        choices=[c("llm", "model")] if c("llm", "model") else [],
                                        value=c("llm", "model") or None,
                                        allow_custom_value=True, scale=3)
                    btn_models = gr.Button("Fetch models / test connection", scale=1)
                with gr.Row():
                    temperature = gr.Slider(0.0, 2.0, step=0.05, label="Temperature",
                                            value=c("llm", "temperature"))
                    max_tokens = gr.Number(label="Max tokens", precision=0, value=c("llm", "max_tokens"))
                    timeout = gr.Number(label="Timeout (s)", precision=0, value=c("llm", "timeout"))
                with gr.Row():
                    disable_thinking = gr.Checkbox(label="Disable thinking (reasoning models)",
                                                   value=c("llm", "disable_thinking"))
                    allow_explicit = gr.Checkbox(label="Allow explicit (NSFW) tags",
                                                 value=c("llm", "allow_explicit"))

            with gr.Tab("Auto start/stop"):
                gr.Markdown(
                    "**Optional — only for llama.cpp users. With LM Studio, Ollama, Jan "
                    "(the app) or a remote server, leave this off.**\n\n"
                    "*Inference server* says *where* to send requests to a model that is "
                    "already running. This tab lets the extension *launch* llama.cpp's "
                    "`llama-server` itself, so its VRAM is used only when needed:\n\n"
                    "- it starts when *Refine* or *Expand idea* needs it;\n"
                    "- it stops when you press **Generate**, and always when the WebUI closes.\n\n"
                    "**What you need to fill in:** the two paths below. In *Inference server* "
                    "you can leave both *Server URL* (then `http://127.0.0.1:8080/v1` is used) "
                    "and *Model* empty.\n\n"
                    "Tip: Jan and LM Studio already contain `llama-server.exe` and `.gguf` "
                    "models, e.g. `%USERPROFILE%\\.lmstudio\\extensions\\backends\\llama.cpp-…\\"
                    "llama-server.exe` — see the README for the exact folders."
                )
                ls_enabled = gr.Checkbox(label="Start and stop a local llama-server automatically",
                                         value=c("local_server", "enabled"))
                with gr.Group(visible=bool(c("local_server", "enabled"))) as ls_group:
                    ls_exe = gr.Textbox(label="Path to llama-server executable",
                                        value=c("local_server", "llama_server"),
                                        placeholder=r"C:\llama.cpp\llama-server.exe")
                    ls_model = gr.Textbox(label="Path to the model (.gguf file)",
                                          value=c("local_server", "model"),
                                          placeholder=r"C:\models\my-model-Q4_K_M.gguf")
                    ls_stop = gr.Checkbox(label="Stop the server when Generate is pressed",
                                          value=c("local_server", "stop_on_generate"))
                    with gr.Accordion("Advanced (the defaults are fine for most models)", open=False):
                        with gr.Row():
                            ls_ctx = gr.Number(label="Context size (tokens)", precision=0,
                                               value=c("local_server", "ctx"))
                            ls_ngl = gr.Number(label="GPU layers (-ngl, 99 = whole model on GPU)",
                                               precision=0, value=c("local_server", "ngl"))
                        ls_extra = gr.Textbox(label="Extra llama-server arguments",
                                              value=c("local_server", "extra_args"),
                                              placeholder="--flash-attn on")
                ls_enabled.change(lambda v: (gr.update(visible=bool(v)), gr.update(visible=bool(v))),
                                  inputs=[ls_enabled], outputs=[ls_group, auto_note])

            with gr.Tab("Tagger"):
                gr.Markdown(
                    "WD v3 taggers by SmilingWolf. The model is downloaded once from "
                    "Hugging Face into `models/` inside this extension, or you can point "
                    "to a folder that already contains `model.onnx` and `selected_tags.csv`."
                )
                with gr.Row():
                    t_repo = gr.Dropdown(label="Model", choices=KNOWN_TAGGERS,
                                         value=c("tagger", "model_repo"),
                                         allow_custom_value=True, scale=3)
                    btn_download = gr.Button("Download model", scale=1)
                t_dir = gr.Textbox(label="Custom model folder (empty = models/<model name>)",
                                   value=c("tagger", "model_dir"))
                with gr.Row():
                    t_gpu = gr.Checkbox(label="Use GPU (DirectML / CUDA)", value=c("tagger", "use_gpu"))
                    t_dev = gr.Number(label="GPU device id (try 1 if a second GPU is used)",
                                      precision=0, value=c("tagger", "device_id"))
                t_unload = gr.Checkbox(
                    label="Unload the tagger when Generate is pressed (frees ~1 GB of VRAM; "
                          "the next tagging takes a few seconds longer)",
                    value=c("tagger", "unload_on_generate"))
                gr.Markdown("The thresholds are the sliders in *Tagger thresholds* above; saving stores them.")

            with gr.Tab("Danbooru"):
                gr.Markdown("Optional. Public posts work without an account.")
                b_login = gr.Textbox(label="Username", value=c("booru", "danbooru_login"))
                b_key = gr.Textbox(label="API key", value=c("booru", "danbooru_api_key"), type="password")

            with gr.Tab("Output"):
                with gr.Row():
                    o_us = gr.Checkbox(label="Keep underscores in tags", value=c("output", "underscores"))
                    o_par = gr.Checkbox(label=r"Escape parentheses: \( \)", value=c("output", "escape_parens"))
                    o_ded = gr.Checkbox(label="Remove duplicates", value=c("output", "dedupe"))
                o_max = gr.Number(label="Max tags (0 = no limit)", precision=0, value=c("output", "max_tags"))
                o_art = gr.Checkbox(label="Keep artist tags from Danbooru posts", value=c("output", "keep_artist"))
                gr.Markdown("Watermarks, signatures, artist names, Patreon/Twitter/pixiv usernames, "
                            "web addresses, logos and censorship tags are always left out of the tags.")

            with gr.Tab("Tag browser"):
                gr.Markdown(
                    "Data for *Characters & artists (offline)*: two CSV files from the "
                    "Hugging Face dataset `Laxhar/noob-wiki` (~70 MB), turned into a local "
                    "database. **Download data** is the only time this feature goes online; "
                    "searching is always offline."
                )
                tb_dir = gr.Textbox(label="Data folder (empty = data/ inside the extension)",
                                    value=c("tag_browser", "data_dir"))
                with gr.Row():
                    btn_tb_download = gr.Button("Download data")
                    btn_tb_status = gr.Button("Check data")
                tb_status = gr.Markdown(BROWSER.status())

            with gr.Tab("Privacy"):
                gr.Markdown(
                    "To show an image in the browser, the WebUI keeps a temporary copy on "
                    "disk (Gradio's folder, `%TEMP%\\gradio` on Windows, and Forge's `tmp`). "
                    "The copy of an image you tag here is always deleted right after "
                    "tagging. Copies of everything else shown in the WebUI — uploads, "
                    "previews, **your generated images** — stay until deleted.\n\n"
                    "With the option below, every temporary image is deleted when the WebUI "
                    "closes (Ctrl+C, restart, or closing its console window) and again when "
                    "it starts, in case it crashed or was killed. Your saved images in "
                    "`outputs` are never touched."
                )
                p_clean = gr.Checkbox(label="Delete all temporary images when the WebUI closes and starts",
                                      value=c("privacy", "clean_temp_on_start"))

            with gr.Row():
                btn_save = gr.Button("Save settings", variant="primary")
            settings_md = gr.Markdown("")

        fields = [
            base_url, api_key, model, temperature, max_tokens, timeout,
            disable_thinking, allow_explicit,
            ls_enabled, ls_exe, ls_model, ls_ctx, ls_ngl, ls_extra, ls_stop,
            t_repo, t_dir, t_gpu, t_dev, t_unload, gen_sl, char_sl,
            b_login, b_key,
            o_us, o_par, o_max, o_ded, o_art,
            tb_dir,
            p_clean,
        ]
        assert len(fields) == len(SETTINGS_FIELDS)

        btn_save.click(do_save_settings, inputs=fields, outputs=[settings_md])
        btn_models.click(do_fetch_models, inputs=[base_url, api_key, model],
                         outputs=[model, settings_md])
        btn_download.click(do_download_tagger, inputs=[t_repo, t_dir], outputs=[settings_md])
        btn_tb_download.click(do_download_data, inputs=[tb_dir], outputs=[tb_status])
        btn_tb_status.click(lambda d: tb.TagBrowser(_TempCfg({("tag_browser", "data_dir"): d or ""})).status(),
                            inputs=[tb_dir], outputs=[tb_status])
        return fields

    # ------------------------------------------------ before every generation
    def before_process(self, p, *args):
        """Free the GPU: unload the tagger and stop the managed language model server."""
        if not AVAILABLE:
            return
        try:
            if CFG.get("tagger", "unload_on_generate", default=True) and TAGGER_SERVICE is not None:
                reset_tagger()
                _log("Generate pressed: tagger unloaded")
        except Exception as exc:
            _log(f"before_process (tagger): {exc}")
        try:
            if CFG.get("local_server", "stop_on_generate", default=True) and SERVER.stop_if_managed():
                _log("Generate pressed: language model server stopped")
        except Exception as exc:
            _log(f"before_process: {exc}")
