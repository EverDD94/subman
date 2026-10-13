"""Mirar dentro del video para saber qué película es, cuando el nombre del archivo no basta.

* **Huella**: el hash de OpenSubtitles (el mismo que se usa para los subtítulos). Si alguien subió
  subtítulos para ese mismo archivo, OpenSubtitles dice qué película es, con su número de TheMovieDB.
  Buscar no gasta descargas.
* **Título interno**: muchos .mkv/.mp4 guardan el nombre de la película en sus etiquetas.
* **Fotos**: unos fotogramas (principio, donde suele salir el título, y escenas) para la IA y para
  enseñarlos en la página.
"""
from __future__ import annotations

import collections
import logging
import os
import re
import subprocess
import threading
from pathlib import Path

from . import audio
from .duplicates import normalize

log = logging.getLogger(__name__)
FRAMES = (0.03, 0.2, 0.45, 0.7)  # dónde se sacan las fotos (fracción de la duración)
# Etiquetas «title» que no son el nombre de una película: webs, grupos, la propia herramienta...
_JUNK_TITLE = re.compile(r"https?:|www\.|\.com|\.net|\.org|@|telegram|torrent|\byts\b|rarbg|"
                         r"handbrake|lavf|encoded|ripped|^\W*$", re.I)


def info(path: str) -> dict:
    """{"minutes": duración, "title": título guardado dentro del video (o None)}."""
    try:
        data = audio.probe(path)
    except Exception:
        return {"minutes": None, "title": None}
    fmt = data.get("format", {})
    try:
        minutes = round(float(fmt["duration"]) / 60, 1)
    except (KeyError, TypeError, ValueError):
        minutes = None
    tags = {k.lower(): v for k, v in (fmt.get("tags") or {}).items()}
    title = (tags.get("title") or "").strip()
    if not title or _JUNK_TITLE.search(title) or normalize(title, leet=False) == normalize(Path(path).stem, leet=False):
        title = None  # vacío, publicidad, o igual que el nombre del archivo (no aporta nada)
    return {"minutes": minutes, "title": title}


def frame(path: str, out_dir: Path, minutes: float | None, key: str, n: int) -> Path | None:
    """Una foto del video (la n-ésima de FRAMES), guardada para no sacarla dos veces."""
    out_dir.mkdir(parents=True, exist_ok=True)
    jpg = out_dir / f"{key}_{n}.jpg"
    if not jpg.exists():
        tmp = jpg.with_suffix(f".{threading.get_ident()}.tmp.jpg")
        try:
            subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-ss", f"{(minutes or 90) * 60 * FRAMES[n]:.0f}",
                            "-i", str(path), "-frames:v", "1", "-vf", "scale=480:-2", "-q:v", "5", str(tmp)],
                           check=True, capture_output=True, timeout=60)
            os.replace(tmp, jpg)
        except Exception as e:
            log.warning("no se pudo sacar la foto %d de %s: %s", n, path, e)
            tmp.unlink(missing_ok=True)
            return None
    return jpg if jpg.stat().st_size else None


def frames(path: str, out_dir: Path, minutes: float | None, key: str) -> list[Path]:
    """Todas las fotos del video. ``key`` identifica el archivo (id + fecha)."""
    return [p for p in (frame(path, out_dir, minutes, key, n) for n in range(len(FRAMES))) if p]


def hash_lookup(client, oshash: str | None) -> list[dict]:
    """Películas que OpenSubtitles asocia a esta huella, la más votada primero:
    [{"tmdb", "imdb", "name", "year", "votes"}]."""
    if not client or not oshash:
        return []
    try:
        data = client._request("GET", "/subtitles", params={"moviehash": oshash, "type": "movie"})
    except Exception as e:
        log.warning("OpenSubtitles: no se pudo buscar la huella: %s", e)
        return []
    votes: collections.Counter = collections.Counter()
    found: dict[str, dict] = {}
    for item in data.get("data", []):
        a = item.get("attributes", {})
        if not a.get("moviehash_match"):
            continue
        f = a.get("feature_details") or {}
        tmdb = f.get("tmdb_id")
        if not tmdb or f.get("feature_type", "Movie").lower() != "movie":
            continue
        key = str(tmdb)
        votes[key] += 1
        found.setdefault(key, {"tmdb": key, "imdb": f"tt{int(f['imdb_id']):07d}" if f.get("imdb_id") else None,
                               "name": f.get("title") or f.get("movie_name") or "", "year": f.get("year")})
    return [{**found[k], "votes": v} for k, v in votes.most_common()]
