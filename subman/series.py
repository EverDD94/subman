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
import shutil
import sqlite3
import time
from pathlib import Path

from . import peek
from .duplicates import normalize
from .identify import BUSY, _companions, _repath, folder_name, safe_name, similarity
from .library import VIDEO_EXT, Library, _clean, _YEAR, episode_numbers, season_dir
from .tmdb import TMDb

log = logging.getLogger(__name__)
_TAG = re.compile(r"\[tmdbid-(\d+)\]", re.I)
# Varios capítulos en un archivo: «S01E01E02», «S01E01-E02», «S01E01-02», «1x01-02».
_MORE = re.compile(r"^(?:[ ._-]*[Ee]|-|[ ._-]*-[ ._-]*[Ee]?)(\d{1,3})(?!\d)")
SURE = 0.85
# palabras que la gente pone en la carpeta y no son parte del nombre
_NOISE = re.compile(r"\b(?:la\s+)?(?:series?|tv|completa|complete|temporadas?|seasons?|full|hd|1080p|720p|latino|castellano|español|espanol|dual)\b", re.I)


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
    fy = _YEAR.search(" " + path.stem[:start])  # «Dark.Matter.2024.S01E01»: el año sólo está en el archivo
    tag = None if loose else _TAG.search(series_dir.name)
    return {"title": title, "year": int(y.group(1)) if y else None, "file_year": int(fy.group(1)) if fy else None,
            "season": season, "episode": episode,
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


def find_show(db: TMDb, title: str, year: int | None, eps: list[dict], soft: bool = False) -> tuple[list[dict], str | None]:
    """Opciones de TheMovieDB, la mejor primero, y por qué hay duda (None si está clara).
    soft: el año sale del nombre de los archivos (ayuda si coincide, pero no resta si no)."""
    ids: list[int] = []
    bare = _NOISE.sub(" ", title).strip()  # «Daredevil Serie», «Friends Serie Completa» → «Daredevil», «Friends»
    for q, y in ((title, year), (title, None), (bare, year), (bare, None),
                 (re.sub(r"\s+(us|uk)$", "", bare, flags=re.I), None)):
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
        sim = max(similarity(t, n) for t in {title, bare} if t for n in (s["name"], s["original"] or ""))
        score = sim + (0 if not (year and s["year"]) else 0.3 if abs(year - s["year"]) <= 1 else 0 if soft else -0.3)
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
    if rivals and not (year and best["year"] and abs(year - best["year"]) <= 1):
        return opts, "hay varias series que se llaman así"
    return opts, None


def _minutes(g: dict) -> float | None:
    """Lo que duran los capítulos de verdad (mediana de unos pocos)."""
    if "minutes" not in g:
        mins = sorted(m for m in (peek.info(e["path"])["minutes"] for e in g["episodes"][:3]) if m)
        g["minutes"] = mins[len(mins) // 2] if mins else None
    return g["minutes"]


def _inner(title: str | None) -> str:
    """El nombre de la serie en el título guardado dentro del video («Dark Matter S01E01 - ...» → «Dark Matter»)."""
    if not title:
        return ""
    return _clean(re.split(r"[ ._-]+(?:S\d{1,2}E\d|\d{1,2}x\d)", title, maxsplit=1, flags=re.I)[0])


def settle(db: TMDb, g: dict, opts: list[dict], why: str, ai=None, frames: Path | None = None,
           memo: dict | None = None) -> tuple[list[dict], str | None, str | None]:
    """Cuando hay duda, intenta decidir solo, de lo más barato a lo más caro: duración de los capítulos,
    título guardado dentro del video y, como último recurso, la IA mirando el principio de un capítulo.
    -> (opciones con la elegida primero, por qué sigue la duda o None, cómo se decidió)."""
    cands = [o for o in opts if o["fit"] >= 0.5 and o["sim"] >= 0.6]
    if not cands:
        return opts, why, None
    first = lambda o: ([o] + [x for x in opts if x is not o], None)
    minutes = _minutes(g)
    season = max({e["season"] for e in g["episodes"]}, key=lambda s: sum(e["season"] == s for e in g["episodes"]))
    for o in cands:
        try:
            o["runtime"] = db.runtime(int(o["tmdb"]), season) if season in (o.get("seasons") or {}) else None
        except Exception as e:
            log.warning("TheMovieDB: duración de %s: %s", o["name"], e)
            o["runtime"] = None
    if minutes:  # las que duran claramente otra cosa (un podcast, capítulos de 20 min...) fuera
        near = lambda o: abs(o["runtime"] - minutes) <= max(6, 0.2 * o["runtime"])
        cands = [o for o in cands if not o["runtime"] or near(o)] or cands
        if len(cands) == 1 and cands[0]["runtime"] and cands[0]["sim"] >= SURE:
            return (*first(cands[0]), "por la duración de los capítulos")
    inner = _inner(peek.info(g["episodes"][0]["path"])["title"])
    if inner:
        hits = [o for o in cands if max(similarity(inner, o["name"]), similarity(inner, o["original"] or "")) >= SURE]
        if len(hits) == 1:
            return (*first(hits[0]), "por el título guardado en el video")
    if not ai or frames is None or (memo or {}).get("_off"):
        return opts, why, None
    e = g["episodes"][0]
    ck = f"{e['path']}|{','.join(sorted(o['tmdb'] for o in cands))}"
    if memo is not None and ck in memo:
        pick, sure = memo[ck]
    else:
        try:
            stamp = int(os.stat(e["path"]).st_mtime)
            imgs = [p.read_bytes() for p in peek.opening(e["path"], frames, minutes, f"{e['id']}_{stamp}")[:5]]
            mp3 = peek.clip(e["path"], frames, minutes, f"{e['id']}_{stamp}", at=0, seconds=120)
            pick, sure = ai.show(imgs, Path(g["series_dir"]).name, e["name"], minutes, inner or None, cands,
                                 mp3.read_bytes() if mp3 else None) if imgs else (None, False)
        except Exception as ex:  # sin cupo, sin conexión...: no se le vuelve a preguntar en esta pasada
            log.warning("Gemini (series): %s", ex)
            if memo is not None:
                memo["_off"] = True
            return opts, why, None
        if memo is not None:
            memo[ck] = [pick, sure]
    o = next((o for o in cands if o["tmdb"] == pick), None)
    if not o:
        return opts, why, None
    return [o] + [x for x in opts if x is not o], None if sure else why, "la IA, mirando y escuchando el principio de un capítulo"


def _plan_group(db: TMDb, g: dict, show: dict | None) -> dict:
    """Calcula el nombre nuevo de cada capítulo del grupo con la serie elegida."""
    g["show"] = {k: show[k] for k in ("tmdb", "name", "year", "image")} if show else None
    g["titles"] = {}
    if not show:
        for e in g["episodes"]:
            e.update(target="", dest="", ok=False, why=None)
        return g
    # el nombre en el idioma que ya tiene («Al este del Edén» o «East of Eden»); si no se parece a ninguno, el original
    local = show.get("original") or ""
    if local and similarity(g["title"], local) >= SURE and similarity(g["title"], local) > similarity(g["title"], show["name"]):
        show = {**show, "name": local}
        g["show"]["name"] = local
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


def review(lib: Library, cfg: dict, progress=lambda msg: None, only: set | None = None,
           frames: Path | None = None) -> dict:
    """Las series con capítulos que no tienen todavía el nombre correcto. only: sólo esos ids.
    frames: carpeta de las fotos; con ella (y la clave de Gemini) la IA ayuda en las dudosas."""
    db = _db(cfg)
    prune(list(cfg["library"]))
    gkey = cfg.get("gemini", {}).get("api_key")
    ai = None
    if gkey:
        from .gemini import Gemini
        ai = Gemini(gkey)
    memo_file = frames.parent / "series_ia.json" if frames else None
    try:
        memo = json.loads(memo_file.read_text()) if memo_file and memo_file.exists() else {}
    except (OSError, ValueError):
        memo = {}
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
                                    "loose": r["loose"], "tmdb": r["tmdb"], "file_year": None, "episodes": []})
        g["file_year"] = g["file_year"] or r["file_year"]
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
        how = None
        if not show:
            year, soft = (g["year"], False) if g["year"] else (g["file_year"], True)
            opts, why = find_show(db, g["title"], year, g["episodes"], soft)
            if why and opts:
                progress(f"mirando series: {n + 1} de {len(groups)} ({g['title']}): buscando cuál es")
                opts, why, how = settle(db, g, opts, why, ai, frames, memo)
            show = opts[0] if opts else None
        g["why"], g["how"] = why, how
        g["options"] = [{k: o[k] for k in ("tmdb", "name", "year", "image")} for o in opts[:6]]
        _plan_group(db, g, show)
        if not all(e.get("same") for e in g["episodes"]):
            g["episodes"] = [e for e in g["episodes"] if not e.get("same")]
            out.append(g)
    if memo_file:
        memo.pop("_off", None)
        try:
            memo_file.parent.mkdir(parents=True, exist_ok=True)
            memo_file.write_text(json.dumps(memo))
        except OSError as e:
            log.warning("no se pudo guardar %s: %s", memo_file, e)
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
    g["why"], g["how"] = None, None
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


# lo que el sistema deja solo en las carpetas (miniaturas del Synology, de Windows o del Mac): no cuenta
_JUNK_DIRS = {"@eadir", ".appledouble"}
_JUNK_FILES = {".ds_store", "thumbs.db", "desktop.ini"}


def _empty(folder: Path) -> bool:
    """¿No queda nada de verdad dentro (ni un archivo, salvo la basura del sistema)?"""
    for p in folder.iterdir():
        if p.is_symlink():
            return False
        if p.is_dir():
            if p.name.lower() not in _JUNK_DIRS and not _empty(p):
                return False
        elif p.name.lower() not in _JUNK_FILES and not p.name.startswith("._"):
            return False
    return True


def prune(roots: list[str]) -> list[str]:
    """Quita las carpetas de series que se quedaron vacías (sus «Season 01»... sin nada dentro), p. ej. de
    renombrados anteriores. Sólo las que tienen carpetas de temporada y ni un archivo de verdad."""
    gone = []
    for r in roots:
        try:
            kids = [d for d in Path(r).iterdir() if d.is_dir() and not d.is_symlink()]
        except OSError:
            continue
        for d in kids:
            try:
                if any(season_dir(x.name) is not None for x in d.iterdir() if x.is_dir()) and _empty(d):
                    shutil.rmtree(d)
                    gone.append(str(d))
            except OSError as e:
                log.warning("no se pudo quitar la carpeta vacía %s: %s", d, e)
    if gone:
        log.info("carpetas de series vacías quitadas: %s", ", ".join(gone))
    return gone


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
            if _empty(d):  # vacía del todo (como mucho, miniaturas del sistema): fuera, con sus «Season» vacías
                shutil.rmtree(d)
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
