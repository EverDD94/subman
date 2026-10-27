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
from .library import VIDEO_EXT, Library, Media, parse_path, sidecars

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


_SPA = {"spa", "es", "esp", "spanish", "español", "lat", "es-419", "es-mx", "es-es"}


def _spanish(f: dict) -> bool:
    return any((a or "").lower() in _SPA for a in f.get("audio") or []) or any(
        (t or "").lower().split("-")[0] in _SPA for t in f.get("subs") or [])


def _same(a: float, b: float) -> bool:
    return abs(a - b) <= DURATION_TOLERANCE * max(a, b)


def pick(files: list[dict]) -> None:
    """Marca en cada copia keep (la que se queda), move (apartar sola), odd (no dura como las demás) y why.

    La duración «normal» es la que comparten más copias; las que no encajan (otro montaje u otra
    película con el mismo nombre) quedan aparte y no se tocan solas. Entre las normales gana la de
    español, salvo que sea de 720p o menos y haya una en otro idioma de 1080p o más."""
    durs = [f.get("duration") or 0 for f in files]
    ref = max((d for d in durs if d), key=lambda d: (sum(_same(d, x) for x in durs if x), d), default=0)
    agree = [f for f in files if ref and f.get("duration") and _same(f["duration"], ref)]
    clear = len(agree) >= 2  # al menos dos copias que duran lo mismo: esa es la duración buena
    for f in files:
        f["odd"] = not clear or f not in agree
        f["spanish"] = _spanish(f)
    pool = agree if clear else files
    h = lambda f: f.get("height") or 0
    spa = [f for f in pool if f["spanish"]]
    best_spa = max(spa, key=lambda f: (h(f), f["size"] or 0), default=None)
    best_any = max(pool, key=lambda f: (h(f), f["spanish"], f["size"] or 0))
    if best_spa and (h(best_spa) >= 1000 or h(best_any) < 1000 or h(best_spa) > 720):
        best = best_spa
        why = "en español" + (f" y {best['resolution']}" if h(best) else "")
    elif best_spa:
        best = best_any
        why = f"{best['resolution']}: la de español sólo es {best_spa['resolution']}"
    else:
        best = best_any
        why = "la de mejor calidad" + (f" ({best['resolution']})" if h(best) else "")
    if clear and len(agree) < len(files):
        why += "; dura lo normal"
    for f in files:
        f["keep"] = f is best
        f["move"] = clear and not f["odd"] and f is not best
        f["why"] = why if f is best else "no dura como las demás: ¿otro montaje u otra película?" if f["odd"] and clear else ""


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
        tag = re.search(r"\[tmdbid-(\d+)\]", Path(m.path).parent.name)
        if tag:  # carpeta ya identificada (p. ej. otra copia que «Nombres» metió junto a la que ya había)
            g = groups.setdefault(f"tmdb:{tag.group(1)}", {"title": m.title, "year": m.year, "how": "nombre", "media": {}})
            g["media"][m.id] = m
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
        pick(files)
        out.append({"key": f"{g['how']}:{g['title']}|{g['year']}", "title": g["title"], "year": g["year"],
                    "how": g["how"], "doubtful": any(f["odd"] for f in files), "files": files})
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


def move(lib: Library, media: Media, dest_root: str, media_root: str, roots: list[str], title: str,
         table: str = "moved") -> list[str]:
    """Aparta una película. Si está sola en su carpeta se lleva la carpeta entera (pósters,
    subtítulos, .nfo); si comparte carpeta, sólo el video y los archivos con su mismo nombre.
    table: «moved» para los duplicados, «discarded» para la papelera de videos."""
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
            lib.db.execute(f"INSERT INTO {table} (src, dest, title, moved) VALUES (?,?,?,?)",
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
        log.info("%s: %s -> %s", "duplicado apartado" if table == "moved" else "a la papelera de videos", src, dest)
    return [str(d) for _, d in done]


def empty_dirs(roots: list[str], keep: list[str] = ()) -> list[str]:
    """Borra las carpetas que han quedado vacías del todo dentro de la biblioteca (sin archivos,
    ni subtítulos, ni ocultos). Nunca las carpetas de la biblioteca ni las de ``keep``."""
    protect = {os.path.realpath(p) for p in [*roots, *keep] if p}
    removed = []
    for root in roots:
        if not os.path.isdir(root):
            continue
        for d, _, _ in os.walk(root, topdown=False):  # de dentro afuera: al vaciar una, su madre puede quedar vacía
            if os.path.realpath(d) in protect or os.path.islink(d):
                continue
            try:
                if not os.listdir(d):
                    os.rmdir(d)
                    removed.append(d)
            except OSError as e:
                log.warning("no se pudo borrar la carpeta vacía %s: %s", d, e)
    for d in removed:
        log.info("carpeta vacía borrada: %s", d)
    return removed


def moved_list(lib: Library, table: str = "moved") -> list[dict]:
    rows = lib.db.execute(f"SELECT * FROM {table} ORDER BY moved DESC").fetchall()
    return [{**dict(r), "exists": os.path.exists(r["dest"])} for r in rows]


def restore(lib: Library, row_id: int, table: str = "moved") -> str:
    r = lib.db.execute(f"SELECT * FROM {table} WHERE id=?", (row_id,)).fetchone()
    if not r:
        raise ValueError("no está en la lista de apartados")
    src, dest = Path(r["src"]), Path(r["dest"])
    if not dest.exists():
        raise ValueError(f"ya no está en {dest}")
    if src.exists():
        raise ValueError(f"ya hay algo en {src}; muévelo a mano")
    try:
        lib.db.execute(f"DELETE FROM {table} WHERE id=?", (row_id,))
    except sqlite3.Error as e:
        lib.db.rollback()
        raise ValueError("subman está ocupado revisando la biblioteca; prueba otra vez en un momento") from e
    src.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(dest), str(src))
    lib.db.commit()
    log.info("%s: %s -> %s", "duplicado devuelto" if table == "moved" else "devuelto de la papelera de videos", dest, src)
    return str(src)


_TAG = re.compile(r"\[tmdbid-(\d+)\]", re.I)


def _video_in(dest: Path) -> Path | None:
    if dest.is_file():
        return dest if dest.suffix.lower() in VIDEO_EXT else None
    vids = [p for p in dest.rglob("*") if p.is_file() and p.suffix.lower() in VIDEO_EXT] if dest.is_dir() else []
    return max(vids, key=lambda p: p.stat().st_size, default=None)


def recheck(lib: Library, cfg: dict, progress=lambda msg: None) -> list[dict]:
    """Vuelve a mirar lo apartado como duplicado, sin fiarse de lo que se decidió entonces: sólo es
    duplicado si en la biblioteca sigue habiendo otra copia de la misma película (misma etiqueta
    [tmdbid], o mismo título y año) que dura lo mismo. Lo que no, vuelve a su sitio (con sus subtítulos)."""
    from .identify import similarity  # (identify usa este módulo)
    rows = [r for r in moved_list(lib) if r["exists"]]
    heads = [r for r in rows if _video_in(Path(r["dest"]))]
    groups: dict[int, list[dict]] = {r["id"]: [r] for r in heads}
    for r in rows:  # los subtítulos y demás que se apartaron con un video van con él
        src = Path(r["src"])
        owner = next((h for h in heads if r["id"] not in groups and Path(h["src"]).parent == src.parent
                      and (src.name.startswith(Path(h["src"]).stem + ".") or src.name.startswith(Path(h["src"]).stem + "-"))),
                     None)
        if owner:
            groups[owner["id"]].append(r)
    movies = [m for m in lib.media("kind='movie'") if os.path.exists(m.path)]
    back = []
    for n, rs in enumerate(groups.values()):
        progress(f"rechequeando las apartadas: {n + 1} de {len(groups)}")
        main = rs[0]
        video = _video_in(Path(main["dest"]))
        src_video = Path(main["src"]) / video.relative_to(main["dest"]) if Path(main["dest"]).is_dir() else Path(main["src"])
        p = parse_path(src_video)
        tag = _TAG.search(src_video.parent.name)
        norm = normalize(p["title"] or "")
        twins = [m for m in movies if (tag and (t := _TAG.search(Path(m.path).parent.name)) and t.group(1) == tag.group(1))
                 or (norm and m.year == p["year"] and (normalize(m.title) == norm or similarity(m.title, p["title"]) >= 0.75))]
        dur = _probe(str(video)).get("duration") or 0
        why = ""
        if not twins:
            why = "no queda otra copia de esa película en la biblioteca"
        elif dur and not any((d := _probe(m.path).get("duration") or 0) and _same(d, dur) for m in twins):
            why = "la otra copia no dura lo mismo: es otra película u otro montaje"
        if not why:
            continue
        done = []
        for r in rs:
            try:
                done.append(restore(lib, r["id"]))
            except (ValueError, OSError) as e:
                log.warning("rechequear duplicados: %s: %s", r["dest"], e)
        if done:
            log.info("rechequear duplicados: %s vuelve a la biblioteca (%s)", src_video, why)
            back.append({"src": str(src_video), "title": main["title"], "why": why})
    return back
