"""Ayudar a Jellyfin a reconocer cada película y ordenarlas una por carpeta.

Jellyfin acierta casi siempre cuando cada película está en su propia carpeta con el título,
el año y el número de TheMovieDB: ``Peliculas/After (2019) [tmdbid-338952]/After (2019).mp4``.
Aquí se buscan las que no están así:

* **Por identificar**: sin año en el nombre, Jellyfin no las reconoce, o lo que dice Jellyfin no
  se parece al nombre del archivo (``after 1.mp4`` -> «After Porn Ends 2»). Se buscan opciones
  con el buscador de Jellyfin (TheMovieDB) y la persona elige.
* **Sin carpeta propia**: Jellyfin las reconoce bien pero están sueltas; se pueden ordenar de golpe.

Al aplicar se mueve el video y todo lo que lleva su nombre (subtítulos, pósters) a la carpeta
nueva, se anota para poder deshacerlo y se avisa a Jellyfin.
"""
from __future__ import annotations

import difflib
import json
import logging
import os
import re
import sqlite3
import time
from pathlib import Path

from . import jellyfin
from .duplicates import _tail, normalize
from .library import VIDEO_EXT, Library, Media

log = logging.getLogger(__name__)
_ORGANIZED = re.compile(r"\[(tmdbid|imdbid)-[^\]]+\]", re.I)
_HAS_YEAR = re.compile(r"(?<!\d)(19|20)\d{2}(?!\d)")
_BAD_CHARS = re.compile(r'[<>"/\\|?*\x00-\x1f]')
SIMILAR = 0.7
BUSY = "subman está ocupado revisando la biblioteca; no se ha cambiado nada, prueba otra vez en un momento"


def similarity(a: str, b: str) -> float:
    a, b = normalize(a), normalize(b)
    if not a or not b:
        return 0.0
    wa, wb = set(a.split()), set(b.split())
    ratio = difflib.SequenceMatcher(None, a, b).ratio()
    if wa <= wb or wb <= wa:  # «Agente 007 Casino Royale» contiene «Casino Royale»
        small, big = sorted((len(wa), len(wb)))
        ratio = max(ratio, 0.6 + 0.4 * small / big)
    return ratio


def safe_name(s: str) -> str:
    s = s.replace(":", " -")
    s = _BAD_CHARS.sub("", s)
    return re.sub(r"\s+", " ", s).strip(" .")


def folder_name(title: str, year: int | None, tmdb: str | None, imdb: str | None) -> str:
    base = safe_name(title) + (f" ({year})" if year else "")
    tag = f"[tmdbid-{tmdb}]" if tmdb else f"[imdbid-{imdb}]" if imdb else ""
    return f"{base} {tag}".strip()


def _alone(video: Path, roots: list[str]) -> bool:
    """¿El video está solo en su carpeta (y esa carpeta no es la de la biblioteca)?"""
    folder = video.parent
    if any(folder.resolve() == Path(r).resolve() for r in roots):
        return False
    for p in folder.iterdir():
        if p.is_dir() or (p != video and p.is_file() and p.suffix.lower() in VIDEO_EXT):
            return False
    return True


def _companions(video: Path) -> list[Path]:
    """El video y lo que lleva su nombre: .es.srt, -poster.jpg, .nfo..."""
    return sorted(p for p in video.parent.iterdir() if p.is_file() and (
        p == video or p.name.startswith(video.stem + ".") or p.name.startswith(video.stem + "-")))


def _queries(m: Media) -> list[str]:
    q = normalize(m.title)
    out = [q] if q else []
    base = re.sub(r"(\s+\d{1,2})+$", "", q)  # «after 2» -> también «after»
    if base and base != q:
        out.append(base)
    return out


def _rank(m: Media, cands: list[dict]) -> list[dict]:
    file_title = m.title
    for c in cands:
        score = similarity(file_title, c["name"])
        if m.year and c["year"]:
            score += 0.3 if abs(m.year - c["year"]) <= 1 else -0.3
        c["score"] = round(score, 3)
    return sorted(cands, key=lambda c: c["score"], reverse=True)


def review(lib: Library, cfg: dict, progress=lambda msg: None) -> dict:
    jf = cfg.get("jellyfin", {})
    if not (jf.get("url") and jf.get("api_key")):
        raise ValueError("pon la dirección y la API key de Jellyfin en Ajustes")
    roots = list(cfg["library"])
    progress("preguntando a Jellyfin")
    items = jellyfin.movies(jf["url"], jf["api_key"])
    by_tail, by_name = {}, {}
    for it in items:
        for p in it["paths"]:
            by_tail[_tail(p)] = it
            by_name.setdefault(_tail(p)[1], []).append(it)

    def jellyfin_item(path: str):  # las rutas de Jellyfin pueden empezar distinto (/data/movies/...)
        it = by_tail.get(_tail(path))
        same = by_name.get(_tail(path)[1], [])
        return it or (same[0] if len(same) == 1 else None)

    lang = (cfg.get("languages") or ["es"])[0]
    todo, loose = [], []
    movies = [m for m in lib.media("kind='movie'") if os.path.exists(m.path)]
    for n, m in enumerate(movies):
        video = Path(m.path)
        alone = _alone(video, roots)
        if alone and _ORGANIZED.search(video.parent.name):
            continue  # ya está como le gusta a Jellyfin
        it = jellyfin_item(m.path)
        name_has_year = bool(_HAS_YEAR.search(video.parent.name if alone else video.stem)) or bool(m.year)
        why = None
        if not it or not (it["tmdb"] or it["imdb"]):
            why = "Jellyfin no la reconoce"
        elif max(similarity(m.title, it["name"]), similarity(m.title, it.get("original") or "")) < SIMILAR:
            why = "lo que dice Jellyfin no se parece al nombre del archivo"
        elif m.year and it["year"] and abs(m.year - it["year"]) > 1:
            why = f"el año no coincide ({m.year} en el archivo, {it['year']} en Jellyfin)"
        elif not name_has_year:
            why = "el nombre no tiene año"
        entry = {"id": m.id, "path": m.path, "name": video.name, "folder": video.parent.name, "alone": alone,
                 "title": m.title, "year": m.year,
                 "jellyfin": {k: it[k] for k in ("name", "year", "tmdb", "imdb")} if it else None}
        if why:
            progress(f"buscando opciones: {len(todo) + 1} ({n + 1} de {len(movies)} películas)")
            cands: dict[str, dict] = {}
            for q in _queries(m):
                try:
                    for c in jellyfin.search_movie(jf["url"], jf["api_key"], q, m.year if q == _queries(m)[0] else None,
                                                   it["id"] if it else None, lang):
                        cands.setdefault(c["tmdb"] or c["imdb"], c)
                except Exception as e:
                    log.warning("Jellyfin: no se pudo buscar «%s»: %s", q, e)
            entry.update(why=why, options=_rank(m, list(cands.values()))[:6])
            todo.append(entry)
        else:
            entry["target"] = folder_name(it["name"], it["year"], it["tmdb"], it["imdb"])
            loose.append(entry)
    return {"todo": todo, "loose": loose}


def search(cfg: dict, media: Media, query: str) -> list[dict]:
    jf = cfg.get("jellyfin", {})
    year = None
    m = re.search(r"\b((?:19|20)\d{2})\b", query)
    if m:
        year = int(m.group(1))
        query = (query[:m.start()] + query[m.end():]).strip(" ()")
    res = jellyfin.search_movie(jf["url"], jf["api_key"], query, year, None, (cfg.get("languages") or ["es"])[0])
    return _rank(media, res)[:8]


def apply(lib: Library, cfg: dict, media: Media, title: str, year: int | None, tmdb: str | None,
          imdb: str | None) -> dict:
    """Mueve la película a «Título (Año) [tmdbid-N]/Título (Año).ext» con sus subtítulos."""
    if not (tmdb or imdb):
        raise ValueError("falta el número de TheMovieDB o IMDb")
    video = Path(media.path)
    if not video.exists():
        raise ValueError("el archivo ya no está donde estaba")
    roots = list(cfg["library"])
    name = folder_name(title, year, tmdb, imdb)
    stem = safe_name(title) + (f" ({year})" if year else "")
    alone = _alone(video, roots)
    parent = video.parent.parent if alone else video.parent
    new_dir = parent / name
    moves: list[tuple[str, str]] = []
    if alone and new_dir != video.parent:
        if new_dir.exists():
            raise ValueError(f"ya existe la carpeta {new_dir.name}")
        os.rename(video.parent, new_dir)
        moves.append((str(video.parent), str(new_dir)))
        video = new_dir / video.name
    elif not alone:
        if new_dir.exists():
            raise ValueError(f"ya existe la carpeta {new_dir.name}")
        new_dir.mkdir()
    try:
        for p in _companions(video):
            dest = new_dir / (stem + p.name[len(video.stem):])
            if dest == p:
                continue
            if dest.exists():
                raise ValueError(f"ya existe {dest.name}")
            os.rename(p, dest)
            moves.append((str(p), str(dest)))
    except Exception:
        _undo(moves, created=None if alone else new_dir)
        raise
    new_video = new_dir / (stem + video.suffix)
    try:
        _repath(lib, media.path, str(new_video))
        lib.db.execute("INSERT INTO renamed (title, moves, created, video_from, video_to, at) VALUES (?,?,?,?,?,?)",
                       (f"{title} ({year})" if year else title, json.dumps(moves, ensure_ascii=False),
                        "" if alone else str(new_dir), media.path, str(new_video), time.time()))
        lib.db.commit()
    except sqlite3.Error as e:  # sin anotarlo no se podría deshacer: mejor dejarlo como estaba
        lib.db.rollback()
        _undo(moves, created=None if alone else new_dir)
        raise ValueError(BUSY) from e
    log.info("identificada: %s -> %s", media.path, new_video)
    return {"dest": str(new_video)}


def _repath(lib: Library, old: str, new: str) -> None:
    """Que subman siga sabiendo cuál es el video y su subtítulo después de moverlo."""
    lib.db.execute("UPDATE media SET path=? WHERE path=?", (new, old))
    old_stem, new_stem = str(Path(old).with_suffix("")), str(Path(new).with_suffix(""))
    for r in lib.db.execute("SELECT id, path FROM subtitle WHERE path IS NOT NULL").fetchall():
        if r["path"].startswith(old_stem + "."):
            lib.db.execute("UPDATE subtitle SET path=? WHERE id=?", (new_stem + r["path"][len(old_stem):], r["id"]))


def _undo(moves: list, created: Path | None) -> None:
    for src, dest in reversed(moves):
        if os.path.exists(dest) and not os.path.exists(src):
            os.rename(dest, src)
    if created and created.exists() and not any(created.iterdir()):
        created.rmdir()


def history(lib: Library) -> list[dict]:
    rows = lib.db.execute("SELECT * FROM renamed ORDER BY at DESC LIMIT 500").fetchall()
    return [{"id": r["id"], "title": r["title"], "at": r["at"], "before": r["video_from"], "after": r["video_to"],
             "exists": os.path.exists(r["video_to"])} for r in rows]


def undo(lib: Library, row_id: int) -> str:
    r = lib.db.execute("SELECT * FROM renamed WHERE id=?", (row_id,)).fetchone()
    if not r:
        raise ValueError("no está en la lista de cambios")
    moves = json.loads(r["moves"])
    for src, dest in moves:
        if not os.path.exists(dest):
            raise ValueError(f"ya no está {dest}")
        if os.path.exists(src):
            raise ValueError(f"ya hay algo en {src}")
    try:
        _repath(lib, r["video_to"], r["video_from"])
        lib.db.execute("DELETE FROM renamed WHERE id=?", (row_id,))
    except sqlite3.Error as e:
        lib.db.rollback()
        raise ValueError(BUSY) from e
    _undo(moves, Path(r["created"]) if r["created"] else None)
    lib.db.commit()
    log.info("deshecho: %s", r["title"])
    return r["title"]
