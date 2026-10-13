"""Películas duplicadas: encontrarlas, apartarlas a otra carpeta y devolverlas.

Dos formas de saber que dos archivos son la misma película:

* **Jellyfin** (si está configurado): ya identificó cada película con su ficha de TMDb/IMDb,
  así que dos archivos con la misma ficha son la misma película aunque se llamen distinto.
* **Nombre**: para lo que Jellyfin no reconoció, el título limpio de etiquetas (1080p, LAT,
  BluRay, @canal...) y el año.

En los dos casos se mira la duración real de cada archivo: si no se parecen (versión extendida,
otra película con el mismo nombre) el grupo queda como dudoso y no se marca nada solo.

Nada se borra: lo que se aparta se mueve (con sus subtítulos) a ``duplicates_dir`` con la misma
estructura de carpetas, y queda anotado para poder devolverlo a su sitio.
"""
from __future__ import annotations

import logging
import os
import re
import shutil
import sqlite3
import time
import unicodedata
from pathlib import Path

from . import audio, jellyfin
from .library import VIDEO_EXT, Library, Media, sidecars

log = logging.getLogger(__name__)

# Palabras que no forman parte del título: calidad, idioma, origen, códecs.
_JUNK = set("""lat latino latam castellano espanol español esp spa dual eng ingles subs sub subtitulado
1080p 720p 480p 576p 2160p 1080 720 2160 4k uhd hd fhd sd bluray blu ray brrip bdrip bdremux remux dvdrip dvd webrip
web dl webdl hdrip hdts hdtv ts cam x264 x265 h264 h265 hevc avc 10bit 8bit aac ac3 dts ddp hdr
req mkv mp4 avi online""".split())
_SIZE = re.compile(r"\b\d+[.,]?\d*\s*(gb|mb)\b")
_RF = re.compile(r"\b\d{2}rf\b")             # «El perfecto asesino- 24rf»
_CHANNELS = re.compile(r"\b[257]\.1\b")   # audio 5.1 / 7.1
_HANDLE = re.compile(r"@\w+")                # «@cinemaximo»
_LEETS = str.maketrans({"0": "o", "v": "u"})  # «Jvrassic+W0rld» (sólo para comparar nombres)

DURATION_TOLERANCE = 0.04  # 4 %: más diferencia que esto ya puede ser otro montaje


def normalize(title: str, leet: bool = True) -> str:
    """Título limpio para comparar. Con leet=False (para buscar), sin cambiar letras: «vida» sigue siendo «vida»."""
    s = unicodedata.normalize("NFKD", title.lower())
    s = "".join(c for c in s if not unicodedata.combining(c))
    s = _HANDLE.sub(" ", s)
    s = _SIZE.sub(" ", s)
    s = _RF.sub(" ", s)
    s = _CHANNELS.sub(" ", s)
    s = re.sub(r"[\[\](){}+._,:;!¡?¿'\"-]", " ", s)
    words = [w for w in s.split() if w not in _JUNK]
    if len(words) > 1:  # el año va aparte (si no, «Batalla Naval 2012» y «Batalla Naval» no casan)
        words = [w for w in words if not re.fullmatch(r"(19|20)\d{2}", w)] or words
    return " ".join(w if w.isdigit() or not leet else w.translate(_LEETS) for w in words)


def _tail(path: str) -> tuple[str, str]:
    p = path.replace("\\", "/").rstrip("/").split("/")
    return (p[-2] if len(p) > 1 else "", p[-1])


def _probe(path: str) -> dict:
    try:
        info = audio.probe(path)
    except Exception as e:
        return {"error": str(e)[:200]}
    video = next((s for s in info.get("streams", []) if s.get("codec_type") == "video"
                  and not s.get("disposition", {}).get("attached_pic")), {})
    langs = [s.get("tags", {}).get("language", "?") for s in audio.audio_streams(info)]
    return {"duration": float(info.get("format", {}).get("duration") or 0),
            "width": video.get("width"), "height": video.get("height"), "codec": video.get("codec_name", ""),
            "audio": langs}


def _resolution(height) -> str:
    if not height:
        return "?"
    for h, name in ((2000, "4K"), (1000, "1080p"), (700, "720p"), (540, "576p")):
        if height >= h:
            return name
    return f"{height}p"


def find(lib: Library, cfg: dict, progress=lambda msg: None) -> list[dict]:
    movies = lib.media("kind='movie'")
    by_tail: dict[tuple[str, str], Media] = {}
    by_name: dict[str, list[Media]] = {}
    for m in movies:
        by_tail[_tail(m.path)] = m
        by_name.setdefault(_tail(m.path)[1], []).append(m)

    def local(path: str) -> Media | None:
        m = by_tail.get(_tail(path))
        if m:
            return m
        same = by_name.get(_tail(path)[1], [])
        return same[0] if len(same) == 1 else None

    groups: dict[str, dict] = {}
    matched: set[int] = set()
    jf = cfg.get("jellyfin", {})
    if jf.get("url") and jf.get("api_key"):
        progress("preguntando a Jellyfin")
        try:
            items = jellyfin.movies(jf["url"], jf["api_key"])
        except Exception as e:
            log.warning("Jellyfin: no se pudo leer la lista de películas: %s", e)
            items = []
        for it in items:
            key = f"tmdb:{it['tmdb']}" if it["tmdb"] else f"imdb:{it['imdb']}" if it["imdb"] else f"jf:{it['id']}"
            g = groups.setdefault(key, {"title": it["name"], "year": it["year"], "how": "jellyfin", "media": {}})
            for p in it["paths"]:
                m = local(p)
                if m:
                    g["media"][m.id] = m
                    matched.add(m.id)
        log.info("duplicados: Jellyfin conoce %d películas, %d archivos encontrados aquí", len(items), len(matched))

    # Lo que Jellyfin no reconoció (o todo, si no hay Jellyfin) se agrupa por nombre; si el nombre
    # coincide con un grupo de Jellyfin, se une a ese grupo.
    by_norm: dict[tuple, str] = {}
    for key, g in groups.items():
        for m in g["media"].values():
            by_norm.setdefault((normalize(m.title), m.year), key)
        by_norm.setdefault((normalize(g["title"]), g["year"]), key)
    for m in movies:
        if m.id in matched:
            continue
        norm = normalize(m.title)
        if not norm:
            continue
        key = by_norm.get((norm, m.year)) or (by_norm.get((norm, None)) if m.year else None)
        if not key and not m.year:  # sin año: sólo si hay un único grupo con ese nombre
            keys = {k for (n, _), k in by_norm.items() if n == norm}
            key = keys.pop() if len(keys) == 1 else None
        if not key:
            key = f"nombre:{norm}|{m.year or ''}"
            groups[key] = {"title": m.title, "year": m.year, "how": "nombre", "media": {}}
            by_norm[(norm, m.year)] = key
        groups[key]["media"][m.id] = m
        if groups[key]["how"] == "jellyfin":
            groups[key]["how"] = "jellyfin+nombre"

    dupes = [g for g in groups.values() if len(g["media"]) > 1]
    out = []
    for n, g in enumerate(sorted(dupes, key=lambda g: normalize(g["title"]))):
        progress(f"mirando duplicados: {n + 1} de {len(dupes)}")
        files = []
        for m in g["media"].values():
            if not os.path.exists(m.path):
                continue
            info = _probe(m.path)
            subs = sorted({tag for tag, _ in sidecars(Path(m.path))})
            files.append({"id": m.id, "path": m.path, "name": Path(m.path).name, "folder": Path(m.path).parent.name,
                          "size": m.size, "subs": subs, "resolution": _resolution(info.get("height")), **info})
        if len(files) < 2:
            continue
        durs = [f["duration"] for f in files if f.get("duration")]
        doubtful = len(durs) < len(files) or (max(durs) - min(durs)) > DURATION_TOLERANCE * max(durs)
        best = max(files, key=lambda f: (f.get("height") or 0, bool(f["subs"]), f["size"] or 0))
        for f in files:
            f["keep"] = f is best
            f["move"] = not doubtful and f is not best
        out.append({"key": f"{g['how']}:{g['title']}|{g['year']}", "title": g["title"], "year": g["year"],
                    "how": g["how"], "doubtful": doubtful, "files": files})
    return out


def _unique(dest: Path) -> Path:
    base, n = dest, 2
    while dest.exists():
        dest = base.with_name(f"{base.stem} ({n}){base.suffix}") if base.suffix else base.with_name(f"{base.name} ({n})")
        n += 1
    return dest


def _relative(path: Path, roots: list[str]) -> Path:
    for r in roots:
        try:
            return path.resolve().relative_to(Path(r).resolve())
        except ValueError:
            continue
    return Path(path.name)


def move(lib: Library, media: Media, dest_root: str, media_root: str, roots: list[str], title: str) -> list[str]:
    """Aparta una película. Si está sola en su carpeta se lleva la carpeta entera (pósters,
    subtítulos, .nfo); si comparte carpeta, sólo el video y los archivos con su mismo nombre."""
    video = Path(media.path)
    folder = video.parent
    others = [p for p in folder.iterdir() if p != video and p.is_file() and p.suffix.lower() in VIDEO_EXT]
    is_root = any(folder.resolve() == Path(r).resolve() for r in [*roots, media_root])
    has_subdirs = any(p.is_dir() for p in folder.iterdir())
    if not others and not is_root and not has_subdirs:
        items = [folder]
    else:
        items = [p for p in folder.iterdir() if p.is_file() and (p == video or p.name.startswith(video.stem + ".")
                                                                  or p.name.startswith(video.stem + "-"))]
    done: list[tuple[Path, Path]] = []
    try:
        for src in items:
            dest = _unique(Path(dest_root) / _relative(src, [media_root, *roots]))
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(src), str(dest))
            done.append((src, dest))
            lib.db.execute("INSERT INTO moved (src, dest, title, moved) VALUES (?,?,?,?)",
                           (str(src), str(dest), title, time.time()))
        lib.db.execute("DELETE FROM media WHERE id=?", (media.id,))
        lib.db.commit()
    except (OSError, sqlite3.Error) as e:  # sin anotarlo no se podría devolver: se deja como estaba
        lib.db.rollback()
        for src, dest in reversed(done):
            shutil.move(str(dest), str(src))
        if isinstance(e, sqlite3.Error):
            raise OSError("subman está ocupado revisando la biblioteca; no se ha movido nada, "
                          "prueba otra vez en un momento") from e
        raise
    for src, dest in done:
        log.info("duplicado apartado: %s -> %s", src, dest)
    return [str(d) for _, d in done]


def moved_list(lib: Library) -> list[dict]:
    rows = lib.db.execute("SELECT * FROM moved ORDER BY moved DESC").fetchall()
    return [{**dict(r), "exists": os.path.exists(r["dest"])} for r in rows]


def restore(lib: Library, row_id: int) -> str:
    r = lib.db.execute("SELECT * FROM moved WHERE id=?", (row_id,)).fetchone()
    if not r:
        raise ValueError("no está en la lista de apartados")
    src, dest = Path(r["src"]), Path(r["dest"])
    if not dest.exists():
        raise ValueError(f"ya no está en {dest}")
    if src.exists():
        raise ValueError(f"ya hay algo en {src}; muévelo a mano")
    try:
        lib.db.execute("DELETE FROM moved WHERE id=?", (row_id,))
    except sqlite3.Error as e:
        lib.db.rollback()
        raise ValueError("subman está ocupado revisando la biblioteca; prueba otra vez en un momento") from e
    src.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(dest), str(src))
    lib.db.commit()
    log.info("duplicado devuelto: %s -> %s", dest, src)
    return str(src)
