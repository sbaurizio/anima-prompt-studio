"""Offline Danbooru character/artist browser.

The data are two CSV files from the Hugging Face dataset Laxhar/noob-wiki
(characters and artists, with appearance tags and post counts). They are
turned once into a local SQLite database; searching never uses the network.
Only download_data() goes online, and only when the user presses the button.
"""
from __future__ import annotations

import csv
import os
import pathlib
import sqlite3
import threading
import time
from typing import Iterator

import requests

EXT_DIR = pathlib.Path(__file__).resolve().parent.parent
DEFAULT_DATA_DIR = EXT_DIR / "data"

CHAR_CSV = "danbooru_character.csv"
ARTIST_CSV = "danbooru_artist.csv"
HF_DATASET = "https://huggingface.co/datasets/Laxhar/noob-wiki/resolve/main/"
ILLEGAL_FS_CHARS = set('<>:"/\\|?*')
PAGE_SIZE = 50

CHARACTERS, ARTISTS = "Characters", "Artists"
# the table name cannot be a query parameter: it only ever comes from here
_TABLES = {CHARACTERS: "characters", ARTISTS: "artists"}
_FOLDERS = {CHARACTERS: "characters", ARTISTS: "artists"}

# one lock for building and reading: a build never replaces the file while
# a search has it open (that fails on Windows), and two builds never overlap
_DB_LOCK = threading.RLock()


class TagBrowser:
    def __init__(self, cfg):
        self.cfg = cfg

    # ------------------------------------------------------------- paths
    @property
    def data_dir(self) -> pathlib.Path:
        custom = str(self.cfg.get("tag_browser", "data_dir", default="") or "").strip()
        if custom:
            return pathlib.Path(os.path.expandvars(custom))
        return DEFAULT_DATA_DIR

    @property
    def db_path(self) -> pathlib.Path:
        return self.data_dir / "tags.sqlite"

    def find_csv(self, name: str) -> pathlib.Path | None:
        root = self.data_dir
        for p in (root / "noob-wiki" / name, root / name):
            if p.is_file():
                return p
        return None

    def status(self) -> str:
        found = [n for n in (CHAR_CSV, ARTIST_CSV) if self.find_csv(n)]
        db = "built" if self.db_path.is_file() else "not built yet (built on the first search)"
        if not found:
            return (f"⚠️ no CSV in `{self.data_dir}`: press **Download data**, "
                    "or copy the files there (see the README).")
        return f"✅ data folder `{self.data_dir}` · CSV: {', '.join(found)} · database {db}"

    # ---------------------------------------------------------- database
    def build_db(self) -> str:
        with _DB_LOCK:
            return self._build_db()

    def _build_db(self) -> str:
        char_csv, artist_csv = self.find_csv(CHAR_CSV), self.find_csv(ARTIST_CSV)
        if not char_csv and not artist_csv:
            return (f"⚠️ no CSV found in `{self.data_dir}` (expected `noob-wiki\\{CHAR_CSV}`). "
                    "Press *Download data* in Settings → Tag browser.")
        csv.field_size_limit(10_000_000)
        db = self.db_path
        db.parent.mkdir(parents=True, exist_ok=True)
        tmp = db.with_suffix(".tmp")
        tmp.unlink(missing_ok=True)
        con = sqlite3.connect(tmp)
        try:
            con.executescript("""
                CREATE TABLE characters (character TEXT PRIMARY KEY, copyright TEXT,
                    trigger TEXT, core_tags TEXT, count INTEGER, search TEXT);
                CREATE TABLE artists (artist TEXT PRIMARY KEY, trigger TEXT,
                    count INTEGER, search TEXT);
            """)
            n_chars = n_artists = 0
            if char_csv:
                with open(char_csv, encoding="utf-8", newline="") as f:
                    rows = [(r["character"], r.get("copyright", ""), r.get("trigger", ""),
                             r.get("core_tags", ""), _to_int(r.get("count")),
                             " ".join((r["character"], r.get("trigger", ""), r.get("copyright", ""),
                                       r.get("core_tags", ""))).lower().replace("_", " "))
                            for r in csv.DictReader(f) if r.get("character")]
                con.executemany("INSERT OR REPLACE INTO characters VALUES (?,?,?,?,?,?)", rows)
                n_chars = len(rows)
            if artist_csv:
                with open(artist_csv, encoding="utf-8", newline="") as f:
                    rows = [(r["artist"], r.get("trigger") or r["artist"].replace("_", " "),
                             _to_int(r.get("count")),
                             (r["artist"] + " " + (r.get("trigger") or "")).lower().replace("_", " "))
                            for r in csv.DictReader(f) if r.get("artist")]
                con.executemany("INSERT OR REPLACE INTO artists VALUES (?,?,?,?)", rows)
                n_artists = len(rows)
            con.execute("CREATE INDEX idx_c_count ON characters(count DESC)")
            con.execute("CREATE INDEX idx_a_count ON artists(count DESC)")
            con.commit()
        finally:
            con.close()
        os.replace(tmp, db)
        return f"✅ database built: {n_chars:,} characters, {n_artists:,} artists."

    def _connect(self) -> sqlite3.Connection:
        """Read-only connection; call with _DB_LOCK held."""
        if not self.db_path.exists():
            self._build_db()
        if not self.db_path.exists():
            raise FileNotFoundError("no data: press Download data in Settings → Tag browser")
        # read-only: searching can never change the database
        con = sqlite3.connect(f"{self.db_path.resolve().as_uri()}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        return con

    # ------------------------------------------------------------ search
    def search(self, mode: str, query: str, series: str, min_count, page) -> list[dict]:
        table = _TABLES.get(mode, "characters")
        where, args = [], []
        for w in (query or "").lower().replace("_", " ").split(","):
            if w.strip():
                where.append("search LIKE ? ESCAPE '!'")
                args.append(f"%{_like(w.strip())}%")
        if table == "characters" and (series or "").strip():
            where.append("copyright LIKE ? ESCAPE '!'")
            args.append(f"%{_like(series.strip().lower().replace(' ', '_'))}%")
        where.append("count >= ?")
        args.append(int(min_count or 0))
        args += [PAGE_SIZE, (max(int(page or 1), 1) - 1) * PAGE_SIZE]
        sql = (f"SELECT * FROM {table} WHERE {' AND '.join(where)} "
               "ORDER BY count DESC LIMIT ? OFFSET ?")
        with _DB_LOCK:
            con = self._connect()
            try:
                return [dict(r) for r in con.execute(sql, args)]
            finally:
                con.close()

    def lookup_characters(self, tags) -> dict[str, dict]:
        """Rows of character tags as the tagger or Danbooru writes them, in one query.

        Returns {tag: row} for the tags found; empty if there is no database.
        """
        keys = {_char_key(t): t for t in tags}
        if not keys or not self.db_path.exists():
            return {}
        sql = f"SELECT * FROM characters WHERE character IN ({','.join('?' * len(keys))})"
        with _DB_LOCK:
            con = self._connect()
            try:
                rows = con.execute(sql, list(keys)).fetchall()
            finally:
                con.close()
        return {keys[r["character"]]: dict(r) for r in rows if r["character"] in keys}

    # ----------------------------------------------------- tags / thumbs
    def thumb(self, mode: str, trigger: str) -> str | None:
        p = self.data_dir / _FOLDERS.get(mode, "characters") / "thumbs" / (_sanitize_filename(trigger) + ".webp")
        return str(p) if p.is_file() else None


def entry_tags(mode: str, row: dict, with_tags: bool) -> list[str]:
    """Raw tags of a result row; escaping is done later by build_positive."""
    if mode == ARTISTS:
        return ["@" + row["trigger"].strip()]
    tags = [s.strip() for s in (row.get("trigger") or "").split(",") if s.strip()]
    if with_tags:
        tags += [t.strip() for t in (row.get("core_tags") or "").split(",") if t.strip()]
    return list(dict.fromkeys(tags))


def download_data(dest: pathlib.Path) -> Iterator[str]:
    """Download the two CSV files from Hugging Face into dest/noob-wiki.

    Only these two public files are requested; nothing about the user is sent.
    Both are downloaded first and replace the old ones only when both arrived,
    so a failed download never leaves new and old files mixed.
    """
    dest = pathlib.Path(dest) / "noob-wiki"
    dest.mkdir(parents=True, exist_ok=True)
    parts = {name: dest / (name + ".part") for name in (CHAR_CSV, ARTIST_CSV)}
    try:
        for name, part in parts.items():
            with requests.get(HF_DATASET + name, stream=True, timeout=60) as r:
                r.raise_for_status()
                done, last = 0, 0.0
                with open(part, "wb") as fh:
                    for chunk in r.iter_content(chunk_size=1024 * 1024):
                        fh.write(chunk)
                        done += len(chunk)
                        if time.time() - last > 1.0:
                            last = time.time()
                            yield f"{name}: {done / 1e6:.0f} MB"
            yield f"{name}: downloaded"
        for name, part in parts.items():
            part.replace(dest / name)
    finally:
        for part in parts.values():
            part.unlink(missing_ok=True)


def _sanitize_filename(name: str) -> str:
    """Thumbnails are named after the trigger: <trigger>.webp

    Same rule as the thumbnails already made: path separators are replaced, so
    the name can never point outside the thumbs folder.
    """
    cleaned = "".join("_" if c in ILLEGAL_FS_CHARS else c for c in name)
    return cleaned.rstrip(" .") or "unnamed"


def _like(text: str) -> str:
    """Make % and _ typed by the user literal in a LIKE pattern."""
    return text.replace("!", "!!").replace("%", "!%").replace("_", "!_")


def _char_key(tag: str) -> str:
    r"""Database key of a character tag: fern \(sousou no frieren\) -> fern_(sousou_no_frieren)"""
    return tag.strip().replace(r"\(", "(").replace(r"\)", ")").lower().replace(" ", "_")


def _to_int(v) -> int:
    try:
        return int(float(v or 0))
    except ValueError:
        return 0
