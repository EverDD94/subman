"""Series: que cada capítulo quede con el nombre y la carpeta que mejor entiende Jellyfin.

``2 Broke Girls (2011) [tmdbid-39340]/Season 01/2 Broke Girls - S01E01 - Piloto.mkv``

El número de temporada y capítulo sale del nombre del archivo (``S01E01``, ``1x01``, ``Capitulo 5``,
``Cap 105``...) o de la carpeta de la temporada (``Temporada 1/05.mkv``). Con TheMovieDB se confirma
qué serie es (nombre bien escrito y año), que el capítulo existe y cómo se titula.

Nada se mueve sin vista previa: :func:`review` sólo propone, :func:`apply` mueve los capítulos marcados
con sus subtítulos y lo anota para poder deshacerlo de una vez (una fila de ``renamed`` por serie).
"""
from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
import time
from pathlib import Path

from .duplicates import normalize
from .identify import BUSY, _companions, _repath, folder_name, safe_name, similarity
from .library import VIDEO_EXT, Library, _clean, _YEAR, episode_numbers, season_dir
from .tmdb import TMDb

log = logging.getLogger(__name__)
_TAG = re.compile(r"\[tmdbid-(\d+)\]", re.I)
# Varios capítulos en un archivo: «S01E01E02», «S01E01-E02», «S01E01-02», «1x01-02».
_MORE = re.compile(r"^(?:[ ._-]*[Ee]|-|[ ._-]*-[ ._-]*[Ee]?)(\d{1,3})(?!\d)")
SURE = 0.85


def _db(cfg: dict) -> TMDb:
    key = cfg.get("tmdb", {}).get("api_key")
    if not key:
        raise ValueError("pon la clave de TheMovieDB en Ajustes")
    return TMDb(key, cfg.get("tmdb", {}).get("language") or "es-MX")


def _is_root(folder: Path, roots: list[str]) -> bool:
    return any(folder.resolve() == Path(r).resolve() for r in roots)


def _read(path: Path, roots: list[str]) -> dict | None:
    """Lo que dice el archivo: serie, temporada, capítulo(s) y en qué carpeta está la serie."""
    ep = episode_numbers(path)
    if not ep:
        return None
    season, episode, start, end = ep
    in_season = season_dir(path.parent.name) is not None
    series_dir = path.parent.parent if in_season else path.parent
    loose = _is_root(series_dir, roots)  # capítulos sueltos en la carpeta de la biblioteca
    title = _clean(path.stem[:start]) if loose or not series_dir.name else _clean(series_dir.name)
    title = title or _clean(path.stem[:start]) or _clean(series_dir.name)
    m = _MORE.match(path.stem[end:])
    last = int(m.group(1)) if m and episode < int(m.group(1)) <= episode + 3 else None
    y = None if loose else _YEAR.search(series_dir.name + " ")
    tag = None if loose else _TAG.search(series_dir.name)
    return {"title": title, "year": int(y.group(1)) if y else None, "season": season, "episode": episode,
            "last": last, "series_dir": str(series_dir), "loose": loose, "tmdb": tag.group(1) if tag else None}


def episode_name(show: dict, season: int, episode: int, last: int | None, titles: dict[int, str]) -> str:
    code = f"S{season:02d}E{episode:02d}" + (f"-E{last:02d}" if last else "")
    names = [titles.get(n, "") for n in range(episode, (last or episode) + 1)]
    title = " + ".join(n for n in names if n)
    title = safe_name(title)[:90].strip(" .-") if title else ""
    return f"{safe_name(show['name'])} - {code}" + (f" - {title}" if title else "")


def _season_folder(season: int) -> str:
    return f"Season {season:02d}"


def _fit(show: dict, eps: list[dict]) -> float:
    """Qué parte de los capítulos existe en esa serie (T1 con 22 capítulos no tiene E30)."""
    if not eps:
        return 0.0
    seasons = show.get("seasons") or {}
    ok = sum(1 for e in eps if (e["last"] or e["episode"]) <= seasons.get(e["season"], 0))
    return ok / len(eps)


def find_show(db: TMDb, title: str, year: int | None, eps: list[dict]) -> tuple[list[dict], str | None]:
    """Opciones de TheMovieDB, la mejor primero, y por qué hay duda (None si está clara)."""
    ids: list[int] = []
    for q, y in ((title, year), (title, None), (re.sub(r"\s+(us|uk)$", "", title, flags=re.I), None)):
        if q and not ids:
            try:
                ids = db.search_tv(q, y)
            except Exception as e:
                log.warning("TheMovieDB: no se pudo buscar la serie «%s»: %s", q, e)
    opts = []
    for i in ids[:5]:
        try:
            s = db.tv(i)
        except Exception as e:
            log.warning("TheMovieDB: serie %s: %s", i, e)
            continue
        sim = max(similarity(title, s["name"]), similarity(title, s["original"] or ""))
        score = sim + (0 if not (year and s["year"]) else 0.3 if abs(year - s["year"]) <= 1 else -0.3)
        fit = _fit(s, eps)
        opts.append({**s, "sim": round(sim, 3), "fit": round(fit, 3), "score": round(score + 0.5 * fit, 3)})
    opts.sort(key=lambda o: o["score"], reverse=True)
    if not opts:
        return [], "TheMovieDB no encuentra una serie con ese nombre"
    best = opts[0]
    if best["sim"] < SURE:
        return opts, "el nombre no se parece al de TheMovieDB"
    if best["fit"] < 0.5:  # uno suelto que no existe se marca en su fila; si fallan muchos, quizá es otra serie
        return opts, "TheMovieDB no tiene muchos de estos capítulos"
    rivals = [o for o in opts[1:] if o["sim"] >= SURE and o["fit"] >= 0.5 and o["score"] > best["score"] - 0.15]
    if rivals and not year:
        return opts, "hay varias series que se llaman así"
    return opts, None


def _plan_group(db: TMDb, g: dict, show: dict | None) -> dict:
    """Calcula el nombre nuevo de cada capítulo del grupo con la serie elegida."""
    g["show"] = {k: show[k] for k in ("tmdb", "name", "year", "image")} if show else None
    g["titles"] = {}
    if not show:
        for e in g["episodes"]:
            e.update(target="", dest="", ok=False, why=None)
        return g
    base = Path(g["series_dir"]) if g["loose"] else Path(g["series_dir"]).parent
    g["dest_dir"] = str(base / folder_name(show["name"], show["year"], show["tmdb"], None))
    for s in sorted({e["season"] for e in g["episodes"]}):
        try:
            g["titles"][s] = db.season(int(show["tmdb"]), s) if s in (show.get("seasons") or {}) else {}
        except Exception as e:
            log.warning("TheMovieDB: temporada %s de %s: %s", s, show["name"], e)
            g["titles"][s] = {}
    _targets(g, show)
    return g


def _targets(g: dict, show: dict) -> None:
    seasons = show.get("seasons") or {}
    seen: dict[str, list[dict]] = {}
    for e in g["episodes"]:
        titles = g["titles"].get(e["season"], {})
        name = episode_name(show, e["season"], e["episode"], e["last"], titles)
        dest = Path(g["dest_dir"]) / _season_folder(e["season"]) / (name + Path(e["path"]).suffix)
        e.update(target=str(dest.relative_to(Path(g["dest_dir"]).parent)), dest=str(dest), why=None)
        if seasons and (e["last"] or e["episode"]) > seasons.get(e["season"], 0):
            e["why"] = "TheMovieDB no tiene este capítulo"
        elif dest.exists() and str(dest) != e["path"]:
            e["why"] = "ya hay un archivo con ese nombre"
        seen.setdefault(str(dest).lower(), []).append(e)
    for same in seen.values():
        if len(same) > 1:
            for e in same:
                e["why"] = "hay otro archivo con el mismo capítulo (míralo en Duplicados)"
    for e in g["episodes"]:
        e["ok"] = e["why"] is None and not g.get("why")
        e["same"] = e["dest"] == e["path"]
    g["episodes"].sort(key=lambda e: (e["season"], e["episode"], e["name"]))


def review(lib: Library, cfg: dict, progress=lambda msg: None, only: set | None = None) -> dict:
    """Las series con capítulos que no tienen todavía el nombre correcto. only: sólo esos ids."""
    db = _db(cfg)
    roots = list(cfg["library"])
    groups: dict[str, dict] = {}
    for m in lib.media("kind='episode'"):
        if only is not None and m.id not in only:
            continue
        p = Path(m.path)
        if not p.exists():
            continue
        r = _read(p, roots)
        if not r:
            continue
        key = r["series_dir"] + ("|" + normalize(r["title"]) if r["loose"] else "")
        g = groups.setdefault(key, {"key": key, "title": r["title"], "year": r["year"], "series_dir": r["series_dir"],
                                    "loose": r["loose"], "tmdb": r["tmdb"], "episodes": []})
        g["episodes"].append({"id": m.id, "path": m.path, "name": p.name, "season": r["season"],
                              "episode": r["episode"], "last": r["last"]})
    out = []
    for n, g in enumerate(sorted(groups.values(), key=lambda g: g["title"].lower())):
        progress(f"mirando series: {n + 1} de {len(groups)} ({g['title']})")
        show, opts, why = None, [], None
        if g["tmdb"]:  # la carpeta ya dice qué serie es
            try:
                show = db.tv(int(g["tmdb"]))
            except Exception as e:
                log.warning("TheMovieDB: serie %s: %s", g["tmdb"], e)
        if not show:
            opts, why = find_show(db, g["title"], g["year"], g["episodes"])
            show = opts[0] if opts else None
        g["why"] = why
        g["options"] = [{k: o[k] for k in ("tmdb", "name", "year", "image")} for o in opts[:6]]
        _plan_group(db, g, show)
        if not all(e.get("same") for e in g["episodes"]):
            g["episodes"] = [e for e in g["episodes"] if not e.get("same")]
            out.append(g)
    return {"series": out}


def search(cfg: dict, query: str) -> list[dict]:
    db = _db(cfg)
    year = None
    m = re.search(r"\b((?:19|20)\d{2})\b", query)
    if m:
        year = int(m.group(1))
        query = (query[:m.start()] + query[m.end():]).strip(" ()")
    out = []
    for i in db.search_tv(query, year)[:8]:
        s = db.tv(i)
        out.append({k: s[k] for k in ("tmdb", "name", "year", "image")})
    return out


def pick(cfg: dict, g: dict, tmdb: str) -> dict:
    """La persona eligió la serie: se vuelve a calcular el grupo con ella (ya no hay duda de la serie)."""
    db = _db(cfg)
    show = db.tv(int(tmdb))
    g["why"] = None
    return _plan_group(db, g, show)


def renumber(cfg: dict, g: dict, changes: dict[int, tuple[int, int]]) -> dict:
    """Corregir a mano la temporada y el capítulo de algunos archivos."""
    db = _db(cfg)
    show = db.tv(int(g["show"]["tmdb"]))
    for e in g["episodes"]:
        if e["id"] in changes:
            e["season"], e["episode"] = changes[e["id"]]
            e["last"] = None
            e["fixed"] = True
    for s in {e["season"] for e in g["episodes"]} - set(g["titles"]):
        try:
            g["titles"][s] = db.season(int(show["tmdb"]), s) if s in show["seasons"] else {}
        except Exception as ex:
            log.warning("TheMovieDB: temporada %s: %s", s, ex)
            g["titles"][s] = {}
    _targets(g, show)
    return g


def _has_video(folder: Path) -> bool:
    return any(Path(d, f).suffix.lower() in VIDEO_EXT for d, _, fs in os.walk(folder) for f in fs)


def apply(lib: Library, g: dict, ids: set[int], roots: list[str], how: str = "manual") -> dict:
    """Mueve los capítulos marcados (con sus subtítulos) a su sitio y lo anota todo en una fila para deshacer."""
    if not g.get("show"):
        raise ValueError("elige primero qué serie es")
    eps = [e for e in g["episodes"] if e["id"] in ids and e["dest"] and not e["same"]]
    bad = [e for e in eps if e["why"] and e["why"] != "TheMovieDB no tiene este capítulo"]
    if bad:
        raise ValueError(f"{bad[0]['name']}: {bad[0]['why']}")
    moves: list[tuple[str, str]] = []
    created: list[str] = []
    videos: list[tuple[str, str]] = []
    old_dirs: set[Path] = set()

    def mkdir(d: Path):
        missing = []
        while not d.exists():
            missing.append(d)
            d = d.parent
        for x in reversed(missing):
            x.mkdir()
            created.append(str(x))

    try:
        for e in eps:
            video, dest = Path(e["path"]), Path(e["dest"])
            if not video.exists():
                raise ValueError(f"{e['name']}: ya no está donde estaba")
            mkdir(dest.parent)
            for p in _companions(video):
                to = dest.parent / (dest.stem + p.name[len(video.stem):])
                if to == p:
                    continue
                if to.exists():
                    raise ValueError(f"ya existe {to.name}")
                os.rename(p, to)
                moves.append((str(p), str(to)))
            videos.append((str(video), str(dest)))
            old_dirs.add(video.parent)
            if season_dir(video.parent.name) is not None:
                old_dirs.add(video.parent.parent)
        # carpetas viejas que se quedaron sin videos: lo que quede (póster, .nfo) pasa a la nueva y se quitan
        new_root = Path(g["dest_dir"])
        keep = {new_root, *new_root.parents, *(Path(e["dest"]).parent for e in eps)}
        for d in sorted(old_dirs, key=lambda d: len(d.parts), reverse=True):
            if d in keep or not d.exists() or _is_root(d, roots) or _has_video(d):
                continue
            n = season_dir(d.name)
            target = new_root / _season_folder(n) if n is not None else new_root
            for p in sorted(d.iterdir()):
                if p.is_file() and target.exists() and not (target / p.name).exists():
                    os.rename(p, target / p.name)
                    moves.append((str(p), str(target / p.name)))
            if not any(d.iterdir()):
                d.rmdir()
    except Exception:
        undo_moves(moves, created)
        raise
    title = f"{g['show']['name']} ({g['show']['year']})" if g["show"]["year"] else g["show"]["name"]
    n = len(videos)
    try:
        for old, new in videos:
            _repath(lib, old, new)
        lib.db.execute("INSERT INTO renamed (title, moves, created, video_from, video_to, at, how, extra) VALUES (?,?,?,?,?,?,?,?)",
                       (f"{title} · {n} capítulo{'s' if n != 1 else ''}", json.dumps(moves, ensure_ascii=False), "",
                        g["series_dir"], g["dest_dir"], time.time(), how,
                        json.dumps({"videos": videos, "dirs": created}, ensure_ascii=False)))
        lib.db.commit()
    except sqlite3.Error as e:
        lib.db.rollback()
        undo_moves(moves, created)
        raise ValueError(BUSY) from e
    log.info("serie %s: %d capítulos renombrados en %s", title, n, g["dest_dir"])
    done = {e["id"] for e in eps}
    g["episodes"] = [e for e in g["episodes"] if e["id"] not in done]
    return {"done": n, "dest": g["dest_dir"]}


def undo_moves(moves: list, created: list[str]) -> None:
    for src, dest in reversed(moves):
        if os.path.exists(dest) and not os.path.exists(src):
            Path(src).parent.mkdir(parents=True, exist_ok=True)
            os.rename(dest, src)
    for d in sorted(created, key=len, reverse=True):
        p = Path(d)
        if p.exists() and not any(p.iterdir()):
            p.rmdir()


def undo(lib: Library, r: sqlite3.Row) -> str:
    moves = json.loads(r["moves"])
    extra = json.loads(r["extra"])
    for src, dest in moves:
        if not os.path.exists(dest):
            raise ValueError(f"ya no está {dest}")
        if os.path.exists(src):
            raise ValueError(f"ya hay algo en {src}")
    try:
        for old, new in extra.get("videos", []):
            _repath(lib, new, old)
        lib.db.execute("DELETE FROM renamed WHERE id=?", (r["id"],))
    except sqlite3.Error as e:
        lib.db.rollback()
        raise ValueError(BUSY) from e
    undo_moves(moves, extra.get("dirs", []))
    lib.db.commit()
    log.info("deshecho: %s", r["title"])
    return r["title"]
