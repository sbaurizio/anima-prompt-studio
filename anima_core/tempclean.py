"""Delete temporary image copies made by the WebUI.

To show an image in the browser, Gradio keeps a copy of it on disk. These
helpers delete such copies when the WebUI starts and when it closes. They only
ever remove image files inside the given temporary folders, and skip any
folder that overlaps a protected one (Forge's output folders).
"""
from __future__ import annotations

import atexit
import os
import pathlib
import threading
from typing import Callable, Iterable

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp"}


def _overlaps(a: pathlib.Path, b: pathlib.Path) -> bool:
    return a == b or a.is_relative_to(b) or b.is_relative_to(a)


def clean_dirs(dirs: Iterable[pathlib.Path], protected: Iterable[pathlib.Path],
               log: Callable[[str], None] = lambda m: None) -> int:
    """Delete image files inside `dirs`. Returns how many were removed."""
    protected = [pathlib.Path(p).resolve() for p in protected]
    removed = 0
    for d in dirs:
        d = pathlib.Path(d).resolve()
        if not d.is_dir():
            continue
        if any(_overlaps(d, p) for p in protected):
            log(f"skipped {d}: it overlaps an output folder")
            continue
        for f in d.rglob("*"):
            if f.is_file() and f.suffix.lower() in IMAGE_EXTS:
                try:
                    f.unlink()
                    removed += 1
                except Exception:
                    pass
        # drop the empty per-file folders Gradio creates
        for sub in sorted((p for p in d.rglob("*") if p.is_dir()),
                          key=lambda p: len(p.parts), reverse=True):
            try:
                sub.rmdir()
            except OSError:
                pass
    return removed


_EXIT_HANDLER = None  # keep a reference, or Windows would call freed memory


def on_exit(fn: Callable[[], None]) -> None:
    """Run `fn` once when the WebUI closes.

    * normal exit, Ctrl+C, restart: Python's atexit;
    * console window closed with X, log off, shutdown (Windows): the console
      control handler, which Windows calls with a few seconds to spare.
    A crash or a killed process cannot run anything; the startup cleanup
    covers that case.
    """
    global _EXIT_HANDLER
    done = threading.Event()

    def run_once():
        if done.is_set():
            return
        done.set()
        try:
            fn()
        except Exception:
            pass

    atexit.register(run_once)

    if os.name != "nt":
        return
    try:
        import ctypes
        from ctypes import wintypes

        HANDLER = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.DWORD)
        CLOSE_EVENTS = {2, 5, 6}  # CTRL_CLOSE, CTRL_LOGOFF, CTRL_SHUTDOWN

        def handler(event):
            if event in CLOSE_EVENTS:
                run_once()
            return False  # let Windows / Python continue as usual

        _EXIT_HANDLER = HANDLER(handler)
        ctypes.windll.kernel32.SetConsoleCtrlHandler(_EXIT_HANDLER, True)
    except Exception:
        pass
