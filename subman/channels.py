"""Canales y grupos de Telegram como proveedores (Ajustes › Proveedores), además de Phono.

Cada canal tiene un modo:

* ``pedido``      sólo se busca en él cuando pides algo (Descubrir, peticiones, el bot) y Phono no lo tiene;
* ``auto``        además se miran sus mensajes nuevos y se baja lo que encaje con la serie o película que
                  le digas que sigue (``follow``). Así no se llena el disco con todo lo que suban.

Lo que se baja de un canal pasa por la misma cola de Descargas y acaba en su carpeta, como lo de Phono.
"""
from __future__ import annotations

import re
import time
import unicodedata
from pathlib import Path

from .library import episode_numbers

SCHEMA = """
CREATE TABLE IF NOT EXISTS channels (
    id INTEGER PRIMARY KEY,
    link TEXT NOT NULL UNIQUE,     -- t.me/nombre, @nombre o enlace de invitación t.me/+...
    title TEXT,                    -- el nombre que tiene en Telegram
    kinds TEXT NOT NULL,           -- movie,tv
    mode TEXT NOT NULL,            -- pedido | auto
    follow_kind TEXT,              -- auto: movie | tv
    follow_tmdb INTEGER,           -- auto: lo que sigue
    follow_title TEXT,
    enabled INTEGER NOT NULL DEFAULT 1,
    last_id INTEGER,               -- auto: último mensaje mirado
    checked REAL,
    note TEXT,                     -- en palabras: qué pasó la última vez
    created REAL
);
"""
CHECK_EVERY = 30 * 60  # cada cuánto se miran los canales automáticos
_LINK = re.compile(r"^(?:https?://)?(?:t\.me|telegram\.me|telegram\.dog)/(\+[\w-]+|joinchat/[\w-]+|[A-Za-z]\w{3,})/?$")
_JUNK = re.compile(r"\b(1080p|720p|480p|2160p|4k|x264|x265|h\.?264|h\.?265|hevc|web-?dl|webrip|bluray|brrip|hdtv|"
                   r"dual|latino|castellano|spanish|english|sub(?:s|titulado)?|mkv|mp4|aac|ddp?5\.?1|10bit)\b", re.I)


def norm_link(text: str) -> str:
    """«@nombre», «t.me/nombre» o «https://t.me/+abc» → «t.me/nombre» / «t.me/+abc». Error si no lo es."""
    t = (text or "").strip()
    if re.fullmatch(r"@[A-Za-z]\w{3,}", t):
        return "t.me/" + t[1:]
    m = _LINK.match(t)
    if not m:
        raise ValueError("pega el enlace del canal o grupo (t.me/…) o su @nombre")
    return "t.me/" + m.group(1)


def words(text: str) -> list[str]:
    t = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode().lower()
    t = re.sub(r"[\[\](){}._\-–—:,;!?'’\"|/]+", " ", t)
    return [w for w in t.split() if w not in ("the", "el", "la", "los", "las", "a", "an", "of", "de", "y", "and")]


def describe(name: str, text: str = "") -> dict:
    """Lo que se puede sacar de un mensaje: año, temporada/capítulo y las palabras del título."""
    out = {"year": None, "season": None, "episode": None}
    ep = episode_numbers(Path(name)) if name else None
    if ep is None and text:
        ep = episode_numbers(Path(text.splitlines()[0] + ".mkv"))
    if ep:
        out["season"], out["episode"] = ep[0], ep[1]
    hay = f"{Path(name).stem if name else ''} {text or ''}"
    y = re.findall(r"(?<!\d)(19[3-9]\d|20[0-4]\d)(?!\d)", hay)
    out["year"] = int(y[0]) if y else None
    out["words"] = set(words(_JUNK.sub(" ", hay)))
    return out


def score(msg: dict, titles: list[str], year: int | None, kind: str, season: int | None = None) -> float:
    """0 = no es; cuanto más alto, más seguro. ``msg``: {name, text, size}."""
    d = describe(msg.get("name") or "", msg.get("text") or "")
    if kind == "tv":
        if d["episode"] is None or (season is not None and d["season"] != season):
            return 0
    elif d["episode"] is not None:
        return 0  # un capítulo no es una película
    best = 0.0
    for t in titles:
        want = words(t)
        if want and all(w in d["words"] for w in want):
            best = max(best, 1 + len(want) / 10)
    if not best:
        return 0
    if year and d["year"]:
        if abs(d["year"] - year) > 1:
            return 0 if kind == "movie" else best
        best += 0.5
    return best


def pick(msgs: list[dict], titles: list[str], year: int | None, kind: str, season: int | None = None) -> list[dict]:
    """La película (una) o los capítulos de la temporada (uno por capítulo), lo más grande que no pase de 4 GB
    en 1080p: se prefiere el que diga 1080p y, si no, el más pesado."""
    hits = [(score(m, titles, year, kind, season), m) for m in msgs]
    hits = [(s, m) for s, m in hits if s > 0]
    if not hits:
        return []

    def quality(m):
        t = f"{m.get('name')} {m.get('text')}".lower()
        return ("1080" in t, "x264" in t or "h264" in t, m.get("size") or 0)
    if kind == "movie":
        top = max(s for s, _ in hits)
        return [max((m for s, m in hits if s == top), key=quality)]
    by_ep: dict[int, dict] = {}
    for _, m in hits:
        x = describe(m.get("name") or "", m.get("text") or "")
        e = (x["season"], x["episode"])
        if e not in by_ep or quality(m) > quality(by_ep[e]):
            by_ep[e] = m
    return [by_ep[e] for e in sorted(by_ep)]


class Channels:
    def __init__(self, db):
        self.db = db
        self.db.executescript(SCHEMA)

    def rows(self, enabled: bool = False) -> list[dict]:
        q = "SELECT * FROM channels" + (" WHERE enabled=1" if enabled else "") + " ORDER BY created"
        return [dict(r) for r in self.db.execute(q)]

    def get(self, cid: int) -> dict | None:
        r = self.db.execute("SELECT * FROM channels WHERE id=?", (cid,)).fetchone()
        return dict(r) if r else None

    def add(self, link: str, title: str, kinds: str, mode: str) -> int:
        cur = self.db.execute("INSERT INTO channels (link, title, kinds, mode, enabled, created) VALUES (?,?,?,?,1,?)",
                              (link, title, kinds, mode, time.time()))
        self.db.commit()
        return cur.lastrowid

    def set(self, cid: int, **kw) -> None:
        cols = ", ".join(f"{k}=?" for k in kw)
        self.db.execute(f"UPDATE channels SET {cols} WHERE id=?", (*kw.values(), cid))
        self.db.commit()

    def delete(self, cid: int) -> None:
        self.db.execute("DELETE FROM channels WHERE id=?", (cid,))
        self.db.commit()

    def for_kind(self, kind: str) -> list[dict]:
        return [c for c in self.rows(enabled=True) if kind in (c["kinds"] or "").split(",")]

    def due(self) -> list[dict]:
        """Canales automáticos a los que ya les toca mirar si hay algo nuevo."""
        now = time.time()
        return [c for c in self.rows(enabled=True)
                if c["mode"] == "auto" and c["follow_tmdb"] and now - (c["checked"] or 0) >= CHECK_EVERY]
