"""Descargas: lo que está en «Por conseguir» se busca en Phono, se pide al bot y se coloca en la biblioteca.

Cada cosa pasa por estos pasos (``state``):

* ``cola``        esperando su turno (va de una en una, con una pausa entre pedido y pedido);
* ``buscando``    en Phono, por el número de TheMovieDB;
* ``pidiendo``    al bot de Telegram (anuncio incluido);
* ``manual``      el bot no aceptó la pulsación automática: pulsa tú los botones en Telegram;
* ``descargando`` bajando el archivo;
* ``listo``       ya está en su carpeta («Título (Año) [tmdbid-N]/…» o «Serie/Season 01/…»);
* ``no-esta``     Phono aún no la tiene (se vuelve a mirar cada día);
* ``error`` / ``cancelado``.

Una serie se pide por temporadas: Phono sólo tiene un botón por temporada (con todos sus capítulos).
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import time
from pathlib import Path

from . import audio, discover, phono
from .identify import folder_name, safe_name
from .library import LANG_ALIASES, VIDEO_EXT, episode_numbers
from .series import _season_folder, episode_name
from .tmdb import TMDb

log = logging.getLogger(__name__)
ACTIVE = ("buscando", "pidiendo", "manual", "descargando")
RETRY_MISSING = 24 * 3600

SCHEMA = """
CREATE TABLE IF NOT EXISTS downloads (
    id INTEGER PRIMARY KEY,
    kind TEXT NOT NULL,            -- movie | tv
    tmdb INTEGER NOT NULL,
    season INTEGER,                -- series: la temporada (NULL = todavía no se sabe cuáles hay)
    title TEXT NOT NULL,
    year INTEGER,
    state TEXT NOT NULL,
    detail TEXT,                   -- en palabras: qué está haciendo o qué falló
    file TEXT,                     -- el archivo elegido en Phono
    size INTEGER,
    done INTEGER,                  -- bytes bajados
    dest TEXT,                     -- JSON: dónde quedaron los videos
    how TEXT,                      -- auto | manual
    created REAL,
    updated REAL,
    retry REAL,                    -- no-esta / error: cuándo volver a intentarlo
    bot TEXT,                      -- el bot de Telegram en el que hay que pulsar el anuncio
    got TEXT                       -- JSON: archivos ya recibidos del bot (para no volver a bajarlos)
);
"""


class Downloads:
    def __init__(self, lib, cfg: dict):
        self.db = lib.db
        self.cfg = cfg
        self.db.executescript(SCHEMA)
        self.db.executescript(discover.SCHEMA)  # «Por conseguir» vive ahí
        cols = {r[1] for r in self.db.execute("PRAGMA table_info(downloads)")}
        for col in ("bot", "got"):  # bases de antes
            if col not in cols:
                self.db.execute(f"ALTER TABLE downloads ADD COLUMN {col} TEXT")
                self.db.commit()

    # --- la cola ------------------------------------------------------------------------
    def rows(self) -> list[dict]:
        return [dict(r) for r in self.db.execute("SELECT * FROM downloads ORDER BY created DESC, season")]

    def get(self, row_id: int) -> dict | None:
        r = self.db.execute("SELECT * FROM downloads WHERE id=?", (row_id,)).fetchone()
        return dict(r) if r else None

    def has(self, kind: str, tmdb: int, cancelled: bool = False) -> bool:
        """¿Ya se está pidiendo (o se pidió)? Con ``cancelled`` también cuentan las que cancelaste."""
        q = "SELECT 1 FROM downloads WHERE kind=? AND tmdb=?" + ("" if cancelled else " AND state!='cancelado'")
        return self.db.execute(q, (kind, tmdb)).fetchone() is not None

    def seasons_in(self, tmdb: int) -> set[int]:
        """Temporadas de una serie que ya están en Descargas (sin contar las canceladas)."""
        return {r[0] for r in self.db.execute("SELECT season FROM downloads WHERE kind='tv' AND tmdb=? "
                                              "AND state!='cancelado' AND season IS NOT NULL", (tmdb,))}

    def add(self, kind: str, tmdb: int, title: str, year: int | None, how: str = "manual",
            season: int | None = None) -> int:
        now = time.time()
        cur = self.db.execute(
            "INSERT INTO downloads (kind, tmdb, season, title, year, state, detail, how, created, updated) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)", (kind, int(tmdb), season, title, year, "cola", "", how, now, now))
        self.db.commit()
        return cur.lastrowid

    def set(self, row_id: int, **kw) -> None:
        kw["updated"] = time.time()
        cols = ", ".join(f"{k}=?" for k in kw)
        self.db.execute(f"UPDATE downloads SET {cols} WHERE id=?", (*kw.values(), row_id))
        self.db.commit()

    def next(self) -> dict | None:
        r = self.db.execute("SELECT * FROM downloads WHERE state='cola' OR (state IN ('no-esta','error') "
                            "AND retry IS NOT NULL AND retry<?) ORDER BY created, season LIMIT 1",
                            (time.time(),)).fetchone()
        return dict(r) if r else None

    def today(self) -> int:
        """Pedidos al bot en las últimas 24 horas (para no pasarse)."""
        return self.db.execute("SELECT COUNT(*) FROM downloads WHERE file IS NOT NULL AND updated>?",
                               (time.time() - 86400,)).fetchone()[0]

    def wanted(self) -> list[dict]:
        """Lo que pusiste a propósito en tu lista («No la tengo» / «Agregar a mi lista»)."""
        out = []
        for r in self.db.execute("SELECT kind, tmdb, info FROM discover WHERE have=0"):
            info = json.loads(r["info"] or "{}")
            out.append({"kind": r["kind"], "tmdb": r["tmdb"], "title": info.get("title") or "",
                        "original": info.get("original") or "", "year": info.get("year")})
        return out

    # --- un pedido ----------------------------------------------------------------------
    def find(self, row: dict, api: phono.Phono, db: TMDb) -> dict | None:
        """La ficha de Phono, buscando por el título en español, el original y el de TheMovieDB."""
        info = db.tv(row["tmdb"]) if row["kind"] == "tv" else db.movie(row["tmdb"])
        titles = [row["title"], info.get("original"), info.get("name")]
        seen = []
        for t in titles:
            if t and t not in seen:
                seen.append(t)
        return api.find(row["kind"], row["tmdb"], seen, row["year"] or info.get("year"))

    def target(self, row: dict, video: Path, db: TMDb) -> Path:
        """Dónde va en la biblioteca: la carpeta que entiende Jellyfin."""
        d = self.cfg["downloads"]
        if row["kind"] == "movie":
            m = db.movie(row["tmdb"])
            title, year = m["name"] or row["title"], m["year"] or row["year"]
            stem = safe_name(title) + (f" ({year})" if year else "")
            return Path(d["movies_dir"]) / folder_name(title, year, str(row["tmdb"]), None) / (stem + video.suffix.lower())
        show = db.tv(row["tmdb"])
        base = Path(d["series_dir"]) / folder_name(show["name"] or row["title"], show["year"], str(row["tmdb"]), None)
        ep = episode_numbers(video)
        season = ep[0] if ep else row["season"] or 1
        folder = base / _season_folder(season)
        if not ep:
            return folder / video.name
        try:
            titles = db.season(row["tmdb"], season)
        except Exception as e:
            log.info("TheMovieDB: títulos de la temporada %s: %s", season, e)
            titles = {}
        last = None
        m = re.match(r"^-?[Ee](\d{1,3})(?!\d)", video.stem[ep[3]:])  # «S01E01E02»: dos capítulos
        if m and ep[1] < int(m.group(1)) <= ep[1] + 3:
            last = int(m.group(1))
        return folder / (episode_name(show, season, ep[1], last, titles) + video.suffix.lower())


def place(src: Path, dest: Path) -> Path:
    """Mueve el video a su sitio sin pisar nada (si ya existe, «- 2», «- 3»…)."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    final, n = dest, 2
    while final.exists():
        final = dest.with_name(f"{dest.stem} - {n}{dest.suffix}")
        n += 1
    shutil.move(str(src), str(final))
    return final


def extract_subs(video: Path, langs: list[str]) -> list[Path]:
    """Saca a un .srt los subtítulos de texto que trae el MKV en tus idiomas («Película.spa.srt»);
    luego el trabajo de siempre los comprueba contra el audio y deja el bueno como .es.srt."""
    out = []
    try:
        info = audio.probe(video)
    except Exception as e:
        log.info("%s: no se pudo mirar por dentro: %s", video.name, e)
        return out
    count: dict[str, int] = {}
    for s in info.get("streams", []):
        if s.get("codec_type") != "subtitle" or s.get("codec_name") not in audio.TEXT_SUB_CODECS:
            continue
        tag = (s.get("tags", {}).get("language") or "").lower()
        lang = next((code for code, aliases in LANG_ALIASES.items() if tag in aliases), None)
        if lang not in langs:
            continue
        n = count[lang] = count.get(lang, -1) + 1
        tag = "spa" if lang == "es" else lang  # .spa: para que no se confunda con el .es.srt ya comprobado
        dest = video.with_name(f"{video.stem}.{tag}{'.' + str(n) if n else ''}.srt")
        if dest.exists():
            continue
        r = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-i", str(video), "-map", f"0:{s['index']}",
                            "-f", "srt", str(dest)], capture_output=True)
        if r.returncode == 0 and dest.exists() and dest.stat().st_size > 0:
            out.append(dest)
            log.info("%s: subtítulo %s sacado del video", video.name, lang)
        elif dest.exists():
            dest.unlink()
    return out


def videos(paths: list[Path]) -> list[Path]:
    return [p for p in paths if p.suffix.lower() in VIDEO_EXT]


def free_gb(path: str) -> float:
    p = Path(path)
    while not p.exists() and p != p.parent:
        p = p.parent
    try:
        return shutil.disk_usage(p).free / 2**30
    except OSError:
        return float("inf")


def cleanup(folder: Path) -> None:
    """Quita los .part que quedaron a medias (al reiniciar)."""
    if folder.is_dir():
        for p in folder.glob("*.part"):
            try:
                os.unlink(p)
            except OSError:
                pass
