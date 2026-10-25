"""Índice de la biblioteca (series y películas) en SQLite.

Entiende la estructura que usa Jellyfin:
    Series/Nombre (Año)/Season 01/Nombre - S01E02 - Título.mkv
    Peliculas/Título (Año)/Título (Año).mkv
"""
from __future__ import annotations

import os
import re
import sqlite3
import struct
import time
from dataclasses import dataclass
from pathlib import Path

VIDEO_EXT = {".mkv", ".mp4", ".avi", ".m4v", ".mov", ".ts", ".wmv", ".webm", ".mpg", ".mpeg", ".m2ts", ".flv",
             ".divx", ".ogm", ".3gp"}
OTHER_VIDEO_EXT = {".iso", ".vob", ".rmvb", ".rm", ".strm"}  # no se procesan, sólo se cuentan
SUB_EXT = {".srt", ".vtt", ".ass", ".ssa", ".sub"}
_EP = re.compile(r"[Ss]0?(\d{1,2})[ ._-]*[Ee](\d{1,3})|\b(\d{1,2})x(\d{2,3})\b"
                 r"|(?<![A-Za-z])[Tt](\d{1,2})[ ._-]*[Ee][Pp]?(\d{1,3})(?!\d)"  # T01E05
                 r"|(?:temporada|season|temp)[ ._-]*(\d{1,2})[ ._-]*(?:cap[ií]tulo|cap|episodio|episode|ep|e)[ ._-]*(\d{1,3})(?!\d)",
                 re.I)
_YEAR = re.compile(r"[(\[ ._-]((?:19|20)\d{2})[)\] ._-]?")
# Capítulo sin temporada: «Spartacus.Gods.of.the.Arena.E01.720p», «Serie - Ep 05», «Capitulo 5», «Cap.105»
# (temporada 1 o la de la carpeta; «Cap 105» es T1 E05).
_EP_ONLY = re.compile(r"(?:^|[ ._-])(?:(cap[ií]tulo|cap|chapter)|episodio|episode|[Ee][Pp]?)[ ._]*(\d{1,4})(?=[ ._-]|$)", re.I)
# Dentro de una carpeta de temporada basta un número suelto: «Temporada 1/05.mkv», «Temporada 1/Serie 05 720p.mkv».
_LONE_NUM = re.compile(r"(?<![\dA-Za-z])(\d{1,4})(?![\dA-Za-z])")
_SEASON_NUM = re.compile(r"(\d+)")
_SEASON_DIR = re.compile(r"^(season|temporada|temp|t|s)[ ._-]*\d{1,2}(?!\d)([ ._-].*)?$", re.I)
_TAG = re.compile(r"\s*\[(tmdbid|imdbid|tvdbid)-[^\]]*\]", re.I)
# Basura de descargas (Telegram, webs, calidad, idioma) que no forma parte del título.
_TELEGRAM = re.compile(r"^\d{6,}[ _]\d+[ _](?:\d[ _])?")  # «1955370863_1755_SISU latino»
_JUNK = re.compile(r"(?:@|t\.me/)\S+|\b(?:latino|lat|castellano|subesp|subtitulad[oa]|vo|online|hdts|hdrip|hd|cam|ts|"
                   r"webrip|web-dl|bluray|brrip|dvdrip|x264|x265|hevc|aac\S*|\d{3,4}p|remake|"
                   r"movistar plus\+?|pelisfox\S*|gnula|doodstream|ybhs)\b|^(?:ver|gran estreno)\b|[^\w\s\-:'&,.!?()\[\]]", re.I)
# Lo que queda y no es un nombre: sólo números (id de Telegram) o «Año».
_NO_NAME = re.compile(r"^(?:[\d\s_-]{5,}|a[ñn]o|special|especial|pel[ií]cula)?$", re.I)

SCHEMA = """
CREATE TABLE IF NOT EXISTS media (
    id INTEGER PRIMARY KEY,
    path TEXT UNIQUE NOT NULL,
    kind TEXT NOT NULL,            -- episode | movie
    title TEXT NOT NULL,
    year INTEGER,
    season INTEGER,
    episode INTEGER,
    size INTEGER,
    mtime REAL,
    oshash TEXT,
    seen REAL
);
CREATE TABLE IF NOT EXISTS subtitle (
    id INTEGER PRIMARY KEY,
    media_id INTEGER NOT NULL REFERENCES media(id) ON DELETE CASCADE,
    lang TEXT NOT NULL,
    path TEXT,
    source TEXT,                   -- local | opensubtitles | ...
    status TEXT NOT NULL,          -- ok | revisar | falla | pendiente
    detail TEXT,                   -- informe de sincronización (JSON)
    updated REAL,
    UNIQUE (media_id, lang)
);
CREATE TABLE IF NOT EXISTS rejected (  -- subtítulos que el usuario descartó desde la web
    media_id INTEGER NOT NULL REFERENCES media(id) ON DELETE CASCADE,
    lang TEXT NOT NULL,
    source TEXT NOT NULL,
    PRIMARY KEY (media_id, lang, source)
);
CREATE TABLE IF NOT EXISTS moved (  -- películas duplicadas apartadas desde la web (se pueden devolver)
    id INTEGER PRIMARY KEY,
    src TEXT NOT NULL,             -- dónde estaba (archivo o carpeta)
    dest TEXT NOT NULL,            -- dónde está ahora
    title TEXT,
    moved REAL
);
CREATE TABLE IF NOT EXISTS discarded (  -- películas que no se sabe qué son, mandadas a la papelera de videos
    id INTEGER PRIMARY KEY,
    src TEXT NOT NULL,
    dest TEXT NOT NULL,
    title TEXT,
    moved REAL
);
CREATE TABLE IF NOT EXISTS learned (  -- lo que identificaste tú a mano: nombre limpio del archivo -> película
    key TEXT PRIMARY KEY,
    tmdb TEXT NOT NULL,
    at REAL
);
CREATE TABLE IF NOT EXISTS unknown (  -- «Identificar todo» lo probó todo y no sabe qué película es
    path TEXT PRIMARY KEY,
    minutes REAL,
    at REAL
);
CREATE TABLE IF NOT EXISTS renamed (  -- películas y series ordenadas desde la web (se pueden deshacer)
    id INTEGER PRIMARY KEY,
    title TEXT,
    moves TEXT NOT NULL,           -- JSON [[antes, después], ...] en el orden en que se hicieron
    created TEXT,                  -- carpeta creada (se quita al deshacer si queda vacía)
    video_from TEXT,
    video_to TEXT,
    at REAL,
    how TEXT,                      -- manual | auto | ia
    extra TEXT                     -- series: JSON {"videos": [[antes, después], ...], "dirs": [carpetas creadas]}
);
"""


@dataclass
class Media:
    id: int
    path: str
    kind: str
    title: str
    year: int | None
    season: int | None
    episode: int | None
    size: int
    oshash: str | None

    @property
    def label(self) -> str:
        if self.kind == "episode":
            return f"{self.title} S{self.season:02d}E{self.episode:02d}"
        return f"{self.title} ({self.year})" if self.year else self.title


def _clean(name: str) -> str:
    name = _TAG.sub("", name)
    name = _TELEGRAM.sub("", name.strip())
    name = re.sub(r"[._]", " ", name)
    name = re.sub(r"(?i)dcp\d\S*", " ", name)  # «Coyote vs. AcmeDCP1080L4T»
    name = _JUNK.sub(" ", name)
    name = re.sub(r"\b(?=\w*\d)[0-9a-f]{5,}\b", " ", name)  # números largos y códigos («5e9777dd», «2555195»)
    name = re.sub(r"[\[(]\s*[\])]", " ", name)
    name = re.sub(r"(?<=\S)\s*[\[(]?(19|20)\d{2}[\])]?\s*$", "", name.strip(" -"))  # «1917» sola se queda
    name = re.sub(r"\s+", " ", name).strip(" -")
    return "" if _NO_NAME.match(name) else name


def season_dir(name: str) -> int | None:
    """«Season 01», «Temporada 1», «S1», «T2 (2012)» -> número de temporada."""
    return int(_SEASON_NUM.search(name).group(1)) if _SEASON_DIR.match(name) else None


def episode_numbers(path: Path) -> tuple[int, int, int, int] | None:
    """(temporada, capítulo, inicio, fin) del número de capítulo en el nombre del archivo, o None."""
    stem = path.stem
    folder = season_dir(path.parent.name)
    m = _EP.search(stem)
    if m:
        g = [x for x in m.groups() if x is not None]
        return int(g[0]), int(g[1]), m.start(), m.end()
    m = _EP_ONLY.search(stem)
    if not m and folder is not None:
        m = next((x for x in _LONE_NUM.finditer(stem) if not 1900 <= int(x.group(1)) <= 2099), None)
        if m:
            n = m.group(1)
            if len(n) >= 3 and int(n[:-2]) == folder:  # «Temporada 1/105.mkv»
                return folder, int(n[-2:]), m.start(), m.end()
            return folder, int(n), m.start(), m.end()
    if not m:
        return None
    n = m.group(m.lastindex)
    if len(n) >= 3 and ((folder is not None and int(n[:-2]) == folder) or (folder is None and m.group(1))):
        return int(n[:-2]), int(n[-2:]), m.start(), m.end()
    return (folder if folder is not None else 1), int(n), m.start(), m.end()


def parse_path(path: Path) -> dict:
    stem = path.stem
    ep = episode_numbers(path)
    if ep:
        season, episode, start, _ = ep
        parent = path.parent
        series_dir = parent.parent if _SEASON_DIR.match(parent.name) else parent
        title = _clean(stem[:start]) or _clean(series_dir.name)
        if series_dir.name and not _SEASON_DIR.match(series_dir.name):
            title = _clean(series_dir.name)  # la carpeta de la serie es más fiable que el archivo
        y = _YEAR.search(series_dir.name + " ")
        return {"kind": "episode", "title": title, "season": season, "episode": episode,
                "year": int(y.group(1)) if y else None}
    folder = path.parent.name
    src = folder if _YEAR.search(folder + " ") else stem
    y = _YEAR.search(src + " ")
    title = _clean(src[: y.start()] if y else src)
    return {"kind": "movie", "title": title, "year": int(y.group(1)) if y else None,
            "season": None, "episode": None}


def opensubtitles_hash(path: str | Path) -> str:
    """Hash de OpenSubtitles: tamaño + sumas de los primeros y últimos 64 KiB."""
    size = os.path.getsize(path)
    block = 65536
    h = size
    with open(path, "rb") as f:
        for pos in (0, max(0, size - block)):
            f.seek(pos)
            buf = f.read(block).ljust(block, b"\0")
            for (v,) in struct.iter_unpack("<Q", buf):
                h = (h + v) & 0xFFFFFFFFFFFFFFFF
    return f"{h:016x}"


_FLAGS = {"forced", "sdh", "cc", "default", "hi", "orig"}

LANG_ALIASES = {"es": ("es", "spa", "esp", "es-es", "es-419", "es-mx", "spanish", "español", "espanol", "castellano"),
                "en": ("en", "eng", "english"), "pt": ("pt", "por", "pt-br", "pob"),
                "fr": ("fr", "fre", "fra"), "it": ("it", "ita"), "de": ("de", "ger", "deu")}
_ALIAS_TO_LANG = {a: lang for lang, aliases in LANG_ALIASES.items() for a in aliases}


def sidecars(video: Path) -> list[tuple[str, Path]]:
    """Todos los subtítulos externos de un video como (idioma de la etiqueta, ruta).

    Nombre.spa.srt -> es, Nombre.en.hi.srt -> en, Nombre.spa.0.srt -> es, Nombre.srt -> und.
    """
    out = []
    for p in sorted(video.parent.glob(f"{glob_escape(video.stem)}.*")):
        if p.suffix.lower() not in SUB_EXT or not p.is_file():
            continue
        parts = p.name[len(video.stem):].lower().split(".")[1:-1]
        tag = next((x for x in parts if x not in _FLAGS and not x.isdigit()), "und")
        out.append((_ALIAS_TO_LANG.get(tag, tag), p))
    return out


def glob_escape(s: str) -> str:
    return re.sub(r"([\[\]*?])", r"[\1]", s)


class Library:
    def __init__(self, db_path: str | Path):
        self.db = sqlite3.connect(str(db_path), timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA foreign_keys = ON")
        self.db.executescript(SCHEMA)
        cols = {r[1] for r in self.db.execute("PRAGMA table_info(renamed)")}
        if "how" not in cols:  # bases de datos de antes
            self.db.execute("ALTER TABLE renamed ADD COLUMN how TEXT")
            self.db.commit()
        if "extra" not in cols:
            self.db.execute("ALTER TABLE renamed ADD COLUMN extra TEXT")
            self.db.commit()

    def scan(self, roots: list[str | Path], hash_files: bool = True, progress=None, exclude=()) -> dict:
        now = time.time()
        skip = {os.path.normpath(str(e)) for e in exclude if e}
        stats = {"nuevos": 0, "cambiados": 0, "borrados": 0, "total": 0, "por_carpeta": {}, "ignorados": {}}
        for root in roots:
            before = stats["total"]
            for dirpath, dirnames, filenames in os.walk(root):
                dirnames[:] = [d for d in dirnames if not d.startswith(("@", "."))  # @eaDir de Synology
                               and os.path.normpath(os.path.join(dirpath, d)) not in skip]
                for fn in filenames:
                    p = Path(dirpath) / fn
                    ext = p.suffix.lower()
                    if ext not in VIDEO_EXT or Path(fn).stem.lower() in ("sample", "muestra") or Path(fn).stem.lower().endswith(("-sample", ".sample")):
                        if ext in OTHER_VIDEO_EXT:
                            stats["ignorados"][ext] = stats["ignorados"].get(ext, 0) + 1
                        continue
                    st = p.stat()
                    row = self.db.execute("SELECT id, size, mtime FROM media WHERE path=?", (str(p),)).fetchone()
                    stats["total"] += 1
                    if progress and stats["total"] % 25 == 0:
                        progress(stats["total"])
                    if stats["total"] % 200 == 0:
                        self.db.commit()  # no tener la base de datos bloqueada todo el escaneo (la web también escribe)
                    if row and row["size"] == st.st_size and row["mtime"] == st.st_mtime:
                        info = parse_path(p)  # rápido; así una mejora al leer nombres llega también a lo ya visto
                        self.db.execute("UPDATE media SET seen=?, kind=?, title=?, year=?, season=?, episode=? WHERE id=?",
                                        (now, info["kind"], info["title"], info["year"], info["season"], info["episode"],
                                         row["id"]))
                        continue
                    info = parse_path(p)
                    oshash = opensubtitles_hash(p) if hash_files else None
                    self.db.execute(
                        """INSERT INTO media (path, kind, title, year, season, episode, size, mtime, oshash, seen)
                           VALUES (?,?,?,?,?,?,?,?,?,?)
                           ON CONFLICT(path) DO UPDATE SET kind=excluded.kind, title=excluded.title,
                             year=excluded.year, season=excluded.season, episode=excluded.episode,
                             size=excluded.size, mtime=excluded.mtime, oshash=excluded.oshash, seen=excluded.seen""",
                        (str(p), info["kind"], info["title"], info["year"], info["season"], info["episode"],
                         st.st_size, st.st_mtime, oshash, now))
                    stats["cambiados" if row else "nuevos"] += 1
            stats["por_carpeta"][Path(root).name] = stats["total"] - before
        cur = self.db.execute("DELETE FROM media WHERE seen < ?", (now,))
        stats["borrados"] = cur.rowcount
        self.db.commit()
        return stats

    def media(self, where: str = "1", args: tuple = (), order: str = "title, season, episode") -> list[Media]:
        rows = self.db.execute(
            f"SELECT id, path, kind, title, year, season, episode, size, oshash FROM media WHERE {where} "
            f"ORDER BY {order}", args).fetchall()
        return [Media(**dict(r)) for r in rows]

    def subtitle_status(self, media_id: int, lang: str) -> sqlite3.Row | None:
        return self.db.execute("SELECT * FROM subtitle WHERE media_id=? AND lang=?", (media_id, lang)).fetchone()

    def set_subtitle(self, media_id: int, lang: str, path: str | None, source: str, status: str, detail: str = ""):
        self.db.execute(
            """INSERT INTO subtitle (media_id, lang, path, source, status, detail, updated) VALUES (?,?,?,?,?,?,?)
               ON CONFLICT(media_id, lang) DO UPDATE SET path=excluded.path, source=excluded.source,
                 status=excluded.status, detail=excluded.detail, updated=excluded.updated""",
            (media_id, lang, path, source, status, detail, time.time()))
        self.db.commit()

    def statuses(self) -> dict[int, dict[str, sqlite3.Row]]:
        out: dict[int, dict[str, sqlite3.Row]] = {}
        for r in self.db.execute("SELECT * FROM subtitle"):
            out.setdefault(r["media_id"], {})[r["lang"]] = r
        return out

    def reset(self, media_id: int, lang: str | None = None) -> None:
        if lang:
            self.db.execute("DELETE FROM subtitle WHERE media_id=? AND lang=?", (media_id, lang))
        else:
            self.db.execute("DELETE FROM subtitle WHERE media_id=?", (media_id,))
        self.db.commit()

    def reject(self, media_id: int, lang: str, source: str) -> None:
        self.db.execute("INSERT OR IGNORE INTO rejected VALUES (?,?,?)", (media_id, lang, source))
        self.db.commit()

    def rejected(self, media_id: int, lang: str) -> set[str]:
        return {r[0] for r in self.db.execute(
            "SELECT source FROM rejected WHERE media_id=? AND lang=?", (media_id, lang))}
