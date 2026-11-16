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
    created REAL,
    audio TEXT,                    -- lo que suele traer el canal: es,ja,en… (si los archivos no lo dicen)
    subs TEXT                      -- idem para subtítulos; «-» = sin subtítulos
);
"""
CHECK_EVERY = 30 * 60  # cada cuánto se miran los canales automáticos
_LINK = re.compile(r"^(?:https?://)?(?:t\.me|telegram\.me|telegram\.dog)/(\+[\w-]+|joinchat/[\w-]+|[A-Za-z]\w{3,})/?$")
_JUNK = re.compile(r"\b(1080p|720p|480p|2160p|4k|x264|x265|h\.?264|h\.?265|hevc|web-?dl|webrip|bluray|brrip|hdtv|"
                   r"dual|latino|castellano|spanish|english|sub(?:s|titulado)?|mkv|mp4|aac|ddp?5\.?1|10bit)\b", re.I)


_TEMP = re.compile(r"\b(?:temp(?:orada)?|t)\s*(\d{1,2})\W{0,3}\s*(cap(?:itulo|ítulo)?|ep(?:isodio)?|debate|gala)\.?\s*(\d{1,3})\b", re.I)


def norm_link(text: str) -> str:
    """«@nombre», «t.me/nombre» o «https://t.me/+abc» → «t.me/nombre» / «t.me/+abc». Error si no lo es."""
    t = (text or "").strip()
    if re.fullmatch(r"@[A-Za-z]\w{3,}", t):
        return "t.me/" + t[1:]
    m = re.fullmatch(r"(?:https?://)?(?:t\.me|telegram\.me|telegram\.dog)/c/(\d{5,})(?:/(\d+))?(?:/\d+)?/?", t) \
        or re.fullmatch(r"-?(?:100)?(\d{5,})()", t)
    if m:  # grupo/canal privado al que ya perteneces: t.me/c/ID[/tema] o su ID numérico
        return "t.me/c/" + m.group(1) + ("/" + m.group(2) if m.group(2) else "")
    m = _LINK.match(t)
    if not m:
        raise ValueError("pega el enlace del canal o grupo (t.me/…, t.me/c/… de un mensaje) o su @nombre")
    return "t.me/" + m.group(1)


_ABS = re.compile(r"\b(?:cap(?:itulo|ítulo)?|ep(?:isodio|isode)?|e|#)\s*\.?\s*(\d{1,4})\b", re.I)
_BARE = re.compile(r"(?:^|\s)-?\s*(\d{1,4})(?=\s|\[|\(|$)")
_SXE = re.compile(r"\bS\d{1,2}\s*E\d{1,3}\b|\b\d{1,2}x\d{1,3}\b", re.I)


def absolute(msg: dict, seasons: dict[int, int]) -> dict:
    """Animes numerados de corrido («One Piece Cap 1085»): pasa el número a temporada/capítulo
    según las temporadas de TheMovieDB ({temporada: capítulos}). Devuelve el mensaje con el nombre cambiado."""
    hay = f"{Path(msg.get('name') or '').stem} {(msg.get('text') or '').splitlines()[0] if msg.get('text') else ''}"
    if _SXE.search(hay) or _TEMP.search(hay):
        return msg
    m = _ABS.search(hay) or next((x for x in _BARE.finditer(hay)
                                  if x.group(1) not in ("480", "576", "720", "1080", "2160")
                                  and not 1930 <= int(x.group(1)) <= 2049), None)
    if not m:
        return msg
    n, left = int(m.group(1)), int(m.group(1))
    for s in sorted(k for k in seasons if k > 0):
        if left <= seasons[s]:
            return {**msg, "name": f"{Path(msg.get('name') or 'x.mkv').stem.replace(m.group(0), ' ')} S{s:02d}E{left:02d}.mkv",
                    "text": (msg.get("text") or "").replace(m.group(0), " ")}
        left -= seasons[s]
    return msg


def words(text: str) -> list[str]:
    t = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode().lower()
    t = re.sub(r"[\[\](){}._\-–—:,;!?'’\"|/]+", " ", t)
    return [w for w in t.split() if w not in ("the", "el", "la", "los", "las", "a", "an", "of", "de", "y", "and")]


def describe(name: str, text: str = "") -> dict:
    """Lo que se puede sacar de un mensaje: año, temporada/capítulo y las palabras del título."""
    out = {"year": None, "season": None, "episode": None, "label": None}
    hay = f"{Path(name).stem if name else ''} {text or ''}"
    m = _TEMP.search(hay)  # «Temp 11 Cap 15», «Temp 11 Debate 6» (realitys)
    if m:
        part = "Debate" if m.group(2).lower().startswith("deb") else "Gala" if m.group(2).lower() == "gala" else "Capitulo"
        out.update(season=int(m.group(1)), episode=int(m.group(3)), part=part, label=f"{part} {int(m.group(3))}")
    ep = None if m else episode_numbers(Path(name)) if name else None
    if ep is None and text:
        ep = episode_numbers(Path(text.splitlines()[0] + ".mkv"))
    if ep:
        out["season"], out["episode"] = ep[0], ep[1]
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
        e = (x["season"], x.get("part"), x["episode"])
        if e not in by_ep or quality(m) > quality(by_ep[e]):
            by_ep[e] = m
    return [by_ep[e] for e in sorted(by_ep)]


# --- Qué trae cada archivo: formato, calidad, audio y subtítulos (por el nombre y el texto) ---------
LANGS = {"es": "Español", "lat": "Latino", "ja": "Japonés", "en": "Inglés", "ko": "Coreano", "fr": "Francés",
         "it": "Italiano", "de": "Alemán", "pt": "Portugués", "zh": "Chino"}
_AUDIO_WORDS = [
    ("lat", r"latino|lat|espanol latino|audio latino"),
    ("es", r"castellano|espanol|spanish|esp|spa|cast"),
    ("ja", r"japones|japanese|jap|jpn|japo"),
    ("en", r"ingles|english|eng|ing"),
    ("ko", r"coreano|korean|kor"),
    ("fr", r"frances|french"),
    ("it", r"italiano|italian|ita"),
    ("de", r"aleman|german|ger|deu"),
    ("pt", r"portugues|portuguese"),
    ("zh", r"chino|chinese|mandarin"),
]
_SUB = re.compile(r"\b(?:sub(?:s|titulos?|titulado|bed|titles?)?|vose|vosi|multisub|multi-sub|hardsub|softsub)\b")
_SUBLANG = re.compile(r"\b(?:sub(?:s|titulos?|titulado|titles?)?)\s*(?:en\s+|in\s+|:\s*)?([a-z]+(?:\s*(?:\+|/|,|y|e)\s*[a-z]+)*)")
_RES = re.compile(r"\b(2160|1080|720|576|480|360)p\b|\b(4k|uhd)\b")
_CODEC = re.compile(r"\b(x265|h\.?265|hevc|x264|h\.?264|avc|av1)\b")


def _lang(word: str) -> str | None:
    return next((c for c, rx in _AUDIO_WORDS if re.fullmatch(rx, word)), None)


def media(name: str, text: str = "", channel: dict | None = None) -> dict:
    """{format, quality, codec, hdr, audio: [códigos], subs: [códigos] | [""] (sí, sin saber cuál) | [],
    guess: {audio, subs} (True = viene de la marca del canal, no del archivo)}."""
    head = "\n".join((text or "").splitlines()[:4])  # la ficha del mensaje, no la sinopsis entera
    raw = unicodedata.normalize("NFKD", f"{name or ''} {head}").encode("ascii", "ignore").decode().lower()
    t = re.sub(r"[\[\](){}._|\-]+", " ", raw)
    ext = Path(name or "").suffix.lstrip(".").upper()
    r = _RES.search(t)
    quality = (r.group(1) + "p") if r and r.group(1) else ("2160p" if r else "")
    c = _CODEC.search(t)
    codec = {"hevc": "x265", "h265": "x265", "h.265": "x265", "h264": "x264", "h.264": "x264", "avc": "x264"}.get(c.group(1), c.group(1)) if c else ""
    # subtítulos primero, para no contar «sub español» como audio en español
    subs: list[str] = []
    for m in _SUBLANG.finditer(t):
        for w in re.split(r"\s*(?:\+|/|,|\by\b|\be\b)\s*", m.group(1)):
            code = _lang(w.strip())
            if code and code not in subs:
                subs.append("es" if code == "lat" else code)
    no_subs = re.sub(_SUBLANG, " ", t)
    if re.search(r"\bvose\b", t):
        subs = subs or ["es"]
    if re.search(r"\bvosi\b", t):
        subs = subs or ["en"]
    if not subs and _SUB.search(t):
        subs = [""]
    audio: list[str] = []
    if re.search(r"\bdual\b", no_subs):
        audio.append("dual")
    for w in no_subs.split():
        code = _lang(w)
        if code and code not in audio:
            audio.append(code)
    if re.search(r"\bvos[ei]?\b|\bv\s?o\b|\boriginal\b", no_subs) and not [a for a in audio if a != "dual"]:
        audio.append("orig")
    guess = {"audio": False, "subs": False}
    ch = channel or {}
    if not [a for a in audio if a != "dual"] and ch.get("audio"):
        audio += [x for x in ch["audio"].split(",") if x and x not in audio]
        guess["audio"] = True
    if not subs and ch.get("subs"):
        subs = [] if ch["subs"] == "-" else [x for x in ch["subs"].split(",") if x]
        guess["subs"] = True
    return {"format": ext, "quality": quality, "codec": codec, "hdr": bool(re.search(r"\bhdr(?:10)?\b|dolby vision|\bdv\b", t)),
            "audio": audio, "subs": subs, "guess": guess}


class Channels:
    def __init__(self, db):
        self.db = db
        self.db.executescript(SCHEMA)
        have = {r[1] for r in self.db.execute("PRAGMA table_info(channels)")}
        for col in ("audio", "subs"):
            if col not in have:
                self.db.execute(f"ALTER TABLE channels ADD COLUMN {col} TEXT")
        self.db.commit()

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
