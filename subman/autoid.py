"""«Identificar todo»: elegir la película de cada archivo sin preguntar, cuando no hay dudas.

Para cada película que la revisión de Nombres marcó (por identificar o sin carpeta propia):

1. Busca opciones en TheMovieDB con su duración y su saga (y el puesto en la saga).
2. Una opción es **segura** si la duración encaja con la del archivo (unos minutos), el año no
   choca, y el nombre se parece o el número de la saga coincide («Fast 7» = 7.ª de su saga).
   Si hay exactamente una segura, se aplica.
3. Las demás se le pasan a Gemini (si hay clave) en grupos de 10; si Gemini está seguro y su
   elección encaja en duración, se aplica.
4. Lo que queda se deja para revisar a mano, con la mejor opción marcada.

Todo lo aplicado queda en «Cambios hechos» (con quién lo decidió) y se puede deshacer.
"""
from __future__ import annotations

import logging
import re

from . import audio, identify
from .duplicates import normalize
from .gemini import Gemini
from .identify import similarity
from .library import Library
from .tmdb import TMDb

log = logging.getLogger(__name__)
_PART = re.compile(r"(?:^|\s)(\d{1,2})$")
_COLLECTION_WORDS = re.compile(r"\b(colecci[oó]n|collection|saga|la saga|trilog[ií]a)\b", re.I)
BATCH = 10


def _minutes(path: str) -> float | None:
    try:
        return round(float(audio.probe(path)["format"]["duration"]) / 60, 1)
    except Exception:
        return None


_ROMAN = {"i": 1, "ii": 2, "iii": 3, "iv": 4, "v": 5, "vi": 6, "vii": 7, "viii": 8, "ix": 9, "x": 10}


def _arabic(text: str) -> str:
    """«el senor de los anillos iii» -> «el senor de los anillos 3» (no la primera palabra: «X-Men»)."""
    words = text.split()
    return " ".join(str(_ROMAN[w]) if i and w in _ROMAN else w for i, w in enumerate(words))


def _part_number(title: str) -> int | None:
    """El número de la saga: «Fast 7», «after 2», «El Efecto Mariposa 3 720p...», «Anillos III - ...»."""
    text = _arabic(normalize(title, leet=False))
    m = _PART.search(text)
    n = int(m.group(1)) if m else None
    if not n:
        n = next((int(w) for w in text.split()[1:] if w.isdigit() and 1 <= int(w) <= 30), None)
    return n if n and 1 <= n <= 30 else None


def _duration_ok(runtime, minutes) -> bool | None:
    if not runtime or not minutes:
        return None
    return abs(runtime - minutes) <= max(5, 0.06 * minutes)


def _all_queries(m) -> list[str]:
    """Además de las de Nombres: sin lo de entre corchetes, cada trozo separado por «-», los números
    romanos como números, y cortar en el primer número pequeño («el efecto mariposa 3 hls 2413» ->
    «el efecto mariposa 3» y «el efecto mariposa»)."""
    out = identify._queries(m)

    def add(q):
        q = " ".join(w for w in q.split() if re.fullmatch(r"[a-z0-9ñ]+", w))  # fuera «ᴍx» y similares
        if q and q not in out:
            out.append(q)

    raw = re.sub(r"\[[^\]]*\]|\([^)]*\)", " ", m.title)
    variants = [raw, *[p for p in re.split(r"\s+-\s*|\s*-\s+", raw) if len(p.split()) >= 2]]
    for v in variants:
        q = normalize(v, leet=False)
        add(q)
        add(_arabic(q))
        roman_free = " ".join(w for i, w in enumerate(q.split()) if not (i and w in _ROMAN))
        add(roman_free)
        words = _arabic(q).split()
        for i, w in enumerate(words):
            if w.isdigit() and 1 <= int(w) <= 30 and i > 0:
                add(" ".join(words[:i + 1]))
                add(" ".join(words[:i]))
                break
    return out[:8]


def candidates(db: TMDb, m, minutes: float | None, extra: list[int] = (), more: list[str] = ()) -> list[dict]:
    queries = _all_queries(m)
    for q in more:  # el nombre que le puso Jellyfin, o el que sugiere la IA
        q = normalize(q, leet=False)
        if q and q not in queries:
            queries.append(q)
    ids: list[int] = list(extra)
    lists: list[list[int]] = []
    for i, q in enumerate(queries):
        try:
            found = db.search(q, m.year) if m.year else []
            if not found:
                found = db.search(q)
        except Exception as e:
            log.warning("TheMovieDB: no se pudo buscar «%s»: %s", q, e)
            continue
        lists.append(found[:6])
    for rank in range(6):  # primero lo mejor de cada búsqueda, luego lo demás
        ids += [found[rank] for found in lists if len(found) > rank and found[rank] not in ids]
    part = _part_number(m.title)
    cands: dict[str, dict] = {}
    for movie_id in ids[:12]:
        try:
            c = dict(db.movie(movie_id))
        except Exception as e:
            log.warning("TheMovieDB: no se pudo leer %s: %s", movie_id, e)
            continue
        cands[c["tmdb"]] = c
    # Saga: el puesto de cada opción y, si el archivo lleva número, la película de ese puesto.
    for c in list(cands.values()):
        col = c.get("collection")
        if not col:
            continue
        try:
            parts = db.collection(col["id"])
        except Exception:
            continue
        if int(c["tmdb"]) in parts:
            c["part"] = parts.index(int(c["tmdb"])) + 1
        c["saga"] = col["name"]
        if part and len(parts) >= part and str(parts[part - 1]) not in cands:
            try:
                n = dict(db.movie(parts[part - 1]))
                n.update(part=part, saga=col["name"])
                cands[n["tmdb"]] = n
            except Exception:
                pass
    out = []
    for c in cands.values():
        saga = _COLLECTION_WORDS.sub("", c.get("saga") or "").strip(" -")
        base = re.sub(r"(\s+\d{1,2})+$", "", normalize(m.title))
        sim = max(similarity(m.title, c["name"]), similarity(m.title, c["original"]),
                  similarity(base, saga) if saga and base else 0)
        dur = _duration_ok(c["runtime"], minutes)
        seq = bool(part and c.get("part") == part)
        year_ok = not m.year or (c["year"] and abs(c["year"] - m.year) <= 1)
        score = sim + (0.5 if dur else -0.5 if dur is False and abs(c["runtime"] - minutes) > 15 else 0) \
            + (0.6 if seq else 0) + (0.2 if m.year and year_ok else -0.5 if m.year else 0)
        c.update(score=round(score, 3), sim=round(sim, 2), duration_ok=dur, part_ok=seq,
                 strong=bool(dur and year_ok and (sim >= 0.8 or seq)))
        out.append(c)
    return sorted(out, key=lambda c: c["score"], reverse=True)


def run(lib: Library, cfg: dict, review: dict, progress=lambda msg: None, stop=lambda: False,
        counts=lambda c: None) -> dict:
    """counts recibe cómo va: {total, ok (aplicadas), review (con opciones para revisar), fail (sin opciones), ai (esperando a la IA)}."""
    key = cfg.get("tmdb", {}).get("api_key")
    if not key:
        raise ValueError("pon la clave de TheMovieDB en Ajustes")
    db = TMDb(key, cfg.get("tmdb", {}).get("language") or "es-MX")
    gkey = cfg.get("gemini", {}).get("api_key")
    ai = Gemini(gkey) if gkey else None

    entries = [*review.get("loose", []), *review.get("todo", [])]
    applied, pending, left, unknown = [], [], [], []

    def report():
        done = {a["id"] for a in applied} | {e["id"] for e in left}
        review_n = sum(1 for e in left if e.get("options"))
        counts({"total": len(entries), "ok": len(applied), "review": review_n, "fail": len(left) - review_n,
                "ai": len({e["id"] for e in [*pending, *unknown]} - done)})

    _progress = progress

    def progress(msg):
        report()
        _progress(msg)

    def do_apply(e, c, how):
        media = lib.media("id=?", (e["id"],))
        if not media:
            return
        try:
            r = identify.apply(lib, cfg, media[0], c["name"], c["year"], c["tmdb"], c.get("imdb"), how=how)
            applied.append({"id": e["id"], "name": e["name"], "to": f"{c['name']} ({c['year']})", "how": how,
                            "copy": r.get("copy", False)})
        except (ValueError, OSError) as ex:
            msg = str(ex)
            e["why"] = (f"ya tienes esta película en otra carpeta ({msg.removeprefix('ya existe la carpeta ')}): "
                        "seguramente es un duplicado, mírala en Duplicados") if msg.startswith("ya existe la carpeta") \
                else f"no se pudo aplicar: {msg}"
            e["options"] = [c] + [o for o in e.get("options", []) if o is not c]
            left.append(e)

    for n, e in enumerate(entries):
        if stop():
            left.extend(entries[n:])
            break
        progress(f"identificando: {n + 1} de {len(entries)} ({len(applied)} hechas)")
        media = lib.media("id=?", (e["id"],))
        if not media:
            continue
        m = media[0]
        minutes = _minutes(m.path)
        jf = (e.get("jellyfin") or {}).get("tmdb")
        jname = (e.get("jellyfin") or {}).get("name")
        cands = candidates(db, m, minutes, [int(jf)] if jf and str(jf).isdigit() else [], [jname] if jname else [])
        e["minutes"] = minutes
        e["_m"] = m
        e["options"] = cands[:6]
        # Sin carpeta propia y Jellyfin ya la reconocía: basta con que la duración no lo contradiga.
        if "target" in e and jf:
            mine = next((c for c in cands if c["tmdb"] == str(jf)), None)
            if mine and mine["duration_ok"] is not False:
                do_apply(e, mine, "auto")
                continue
        strong = [c for c in cands if c["strong"]]
        by_part = [c for c in strong if c["part_ok"]]  # «after 2»: la 2.ª de la saga gana a «After» a secas
        if len(strong) == 1 or len(by_part) == 1:
            do_apply(e, by_part[0] if len(by_part) == 1 else strong[0], "auto")
            continue
        e["why"] = ("hay varias que encajan" if len(strong) > 1 else
                    "ninguna encaja con la duración del archivo" if cands else "no se encontró nada con ese nombre")
        if ai:
            (pending if cands else unknown).append(e)
        else:
            left.append(e)

    # Sin opciones: que la IA diga qué película cree que es y se busca con ese nombre.
    for i in range(0, len(unknown), BATCH):
        group = unknown[i:i + BATCH]
        if stop():
            left.extend(unknown[i:])
            unknown = []
            break
        progress(f"la IA busca el nombre: {i + 1}-{i + len(group)} de {len(unknown)} ({len(applied)} hechas)")
        try:
            guesses = ai.guess([{"n": k + 1, "file": e["name"], "folder": e["folder"], "minutes": e["minutes"]}
                                for k, e in enumerate(group)])
        except Exception as ex:
            log.warning("Gemini: %s", ex)
            guesses = {}
        for k, e in enumerate(group):
            names = guesses.get(k + 1) or []
            ids: list[int] = []
            for title, year in names:
                try:
                    ids += [x for x in db.search(normalize(title, leet=False) or title, year)[:3] if x not in ids]
                except Exception as ex:
                    log.warning("TheMovieDB: no se pudo buscar «%s»: %s", title, ex)
            cands = candidates(db, e["_m"], e["minutes"], ids, [t for t, _ in names]) if ids else []
            e["options"] = cands[:6]
            ok = [c for c in cands if c["duration_ok"] and int(c["tmdb"]) in ids[:1]] or [c for c in cands if c["strong"]][:1]
            if ok:  # la primera que dijo la IA y encaja en duración
                do_apply(e, ok[0], "ia")
            elif cands:
                e["why"] = "la IA sugirió estas, pero ninguna encaja seguro"
                pending.append(e)
            else:
                e["why"] = "ni TheMovieDB ni la IA la reconocen"
                left.append(e)

    for i in range(0, len(pending), BATCH):
        group = pending[i:i + BATCH]
        if stop():
            left.extend(pending[i:])
            break
        progress(f"preguntando a la IA: {i + 1}-{i + len(group)} de {len(pending)} ({len(applied)} hechas)")
        items = [{"n": k + 1, "file": e["name"], "folder": e["folder"], "minutes": e["minutes"],
                  "options": e["options"]} for k, e in enumerate(group)]
        try:
            picks = ai.choose(items)
        except Exception as ex:
            log.warning("Gemini: %s", ex)
            picks = {}
        for k, e in enumerate(group):
            pick, sure = picks.get(k + 1, (None, False))
            c = next((o for o in e["options"] if o["tmdb"] == pick), None)
            if c and sure and c["duration_ok"] is not False:
                do_apply(e, c, "ia")
                continue
            if c:  # su elección, la primera para revisar
                e["options"] = [c] + [o for o in e["options"] if o is not c]
                e["why"] = "la IA no está segura" if not sure else "la IA eligió una cuya duración no encaja"
            elif k + 1 in picks:
                e["why"] = "la IA cree que no es ninguna de estas"
            left.append(e)

    report()
    for e in left:
        e.pop("_m", None)
    log.info("identificar todo: %d aplicadas (%d con IA), %d para revisar", len(applied),
             sum(a["how"] == "ia" for a in applied), len(left))
    return {"applied": applied, "todo": left}


def search(cfg: dict, m, query: str, minutes: float | None) -> list[dict]:
    """La búsqueda a mano de Nombres («Otro nombre»), con TheMovieDB."""
    db = TMDb(cfg["tmdb"]["api_key"], cfg.get("tmdb", {}).get("language") or "es-MX")
    year = None
    y = re.search(r"\b((?:19|20)\d{2})\b", query)
    if y:
        year = int(y.group(1))
        query = (query[:y.start()] + query[y.end():]).strip(" ()")
    ids = db.search(query, year) or (db.search(query) if year else [])
    probe = type(m)(**{**m.__dict__, "title": query, "year": year or m.year})
    return candidates(db, probe, minutes, ids)[:8]
