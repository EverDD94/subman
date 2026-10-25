"""Mirar dentro del video para saber qué película es, cuando el nombre del archivo no basta.

* **Huella**: el hash de OpenSubtitles (el mismo que se usa para los subtítulos). Si alguien subió
  subtítulos para ese mismo archivo, OpenSubtitles dice qué película es, con su número de TheMovieDB.
  Buscar no gasta descargas.
* **Título interno**: muchos .mkv/.mp4 guardan el nombre de la película en sus etiquetas.
* **Fotos**: unos fotogramas (principio, donde suele salir el título, y escenas) para la IA y para
  enseñarlos en la página.
* **Segundo intento**: más fotos de los primeros minutos (título, créditos) y un minuto de audio con
  diálogo, para la IA, cuando las fotos de escenas no bastaron.
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
OPENING = (15, 45, 90, 150, 240, 360)  # fotos del principio para el segundo intento (segundos)
CLIP_AT, CLIP_SECONDS = 0.3, 60  # trozo de audio: un minuto hacia el 30 % (lejos de la música del principio)
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


def opening(path: str, out_dir: Path, minutes: float | None, key: str) -> list[Path]:
    """Fotos de los primeros minutos, donde suelen salir el título y los créditos."""
    out_dir.mkdir(parents=True, exist_ok=True)
    out = []
    for n, sec in enumerate(OPENING):
        if minutes and sec > minutes * 60 * 0.5:
            break
        jpg = out_dir / f"{key}_p{n}.jpg"
        if not jpg.exists():
            tmp = jpg.with_suffix(f".{threading.get_ident()}.tmp.jpg")
            try:
                subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-ss", str(sec), "-i", str(path),
                                "-frames:v", "1", "-vf", "scale=640:-2", "-q:v", "5", str(tmp)],
                               check=True, capture_output=True, timeout=60)
                os.replace(tmp, jpg)
            except Exception as e:
                log.warning("no se pudo sacar la foto del principio %d de %s: %s", n, path, e)
                tmp.unlink(missing_ok=True)
                continue
        if jpg.stat().st_size:
            out.append(jpg)
    return out


ENDING = (420, 300, 180, 90)  # fotos de los créditos finales (segundos antes del final)


def ending(path: str, out_dir: Path, minutes: float | None, key: str) -> list[Path]:
    """Fotos de los créditos del final: suelen poner el título y los actores."""
    if not minutes or minutes < 20:
        return []
    out_dir.mkdir(parents=True, exist_ok=True)
    out = []
    for n, back in enumerate(ENDING):
        jpg = out_dir / f"{key}_f{n}.jpg"
        if not jpg.exists():
            tmp = jpg.with_suffix(f".{threading.get_ident()}.tmp.jpg")
            try:
                subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-ss", str(max(0, minutes * 60 - back)),
                                "-i", str(path), "-frames:v", "1", "-vf", "scale=640:-2", "-q:v", "5", str(tmp)],
                               check=True, capture_output=True, timeout=60)
                os.replace(tmp, jpg)
            except Exception as e:
                log.warning("no se pudo sacar la foto del final %d de %s: %s", n, path, e)
                tmp.unlink(missing_ok=True)
                continue
        if jpg.stat().st_size:
            out.append(jpg)
    return out


def clip(path: str, out_dir: Path, minutes: float | None, key: str, at: float = CLIP_AT,
         seconds: int = CLIP_SECONDS) -> Path | None:
    """Un trozo de audio (mp3 mono, pequeño) para que la IA escuche el diálogo; at=0: el principio
    (en las series, la canción de la cabecera)."""
    out_dir.mkdir(parents=True, exist_ok=True)
    mp3 = out_dir / f"{key}_audio.mp3" if at == CLIP_AT else out_dir / f"{key}_audio{int(at * 100)}.mp3"
    if not mp3.exists():
        tmp = mp3.with_suffix(f".{threading.get_ident()}.tmp.mp3")
        start = (minutes or 90) * 60 * at
        try:
            subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-y", "-ss", f"{start:.0f}", "-i", str(path),
                            "-t", str(seconds), "-vn", "-map", "0:a:0", "-ac", "1", "-ar", "16000",
                            "-b:a", "32k", str(tmp)], check=True, capture_output=True, timeout=120)
            os.replace(tmp, mp3)
        except Exception as e:
            log.warning("no se pudo sacar el audio de %s: %s", path, e)
            tmp.unlink(missing_ok=True)
            return None
    return mp3 if mp3.stat().st_size else None


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
