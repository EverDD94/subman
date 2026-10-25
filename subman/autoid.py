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
import time
from pathlib import Path

from . import audio, identify, peek
from .duplicates import normalize
from .gemini import Gemini, GeminiError
from .identify import similarity
from .library import Library
from .providers.opensubtitles import OpenSubtitles
from .tmdb import TMDb

log = logging.getLogger(__name__)
_PART = re.compile(r"(?:^|\s)(\d{1,2})$")
_COLLECTION_WORDS = re.compile(r"\b(colecci[oó]n|collection|saga|la saga|trilog[ií]a)\b", re.I)
BATCH = 10
# Carpetas que no dicen nada de la película (se ignoran como pista).
_GENERIC_DIR = re.compile(r"pel[ií]cula|movie|film|descarga|download|video|v[ií]deo|media|complet|temporada|season|"
                          r"\bnuevas?\b|\bnew\b|\btorrent|\bhd\b|\b4k\b|\b1080|\b720|\bdvd|\bcine\b|sagas?\b|colecci", re.I)
CLEAR_MARGIN = 0.5  # sin ninguna «segura»: la primera se aplica si saca esta ventaja a la segunda
VOTES_RATIO = 10    # entre varias «seguras»: gana la mucho más conocida (votos en TheMovieDB)


def _folder_hint(m) -> str | None:
    """El nombre de la carpeta, si parece el de una película (no «Peliculas», «Descargas»...)."""
    folder = Path(m.path).parent.name
    q = normalize(re.sub(r"\[[^\]]*\]|\([^)]*\)", " ", folder), leet=False)
    if len(q) < 3 or _GENERIC_DIR.search(folder) or q == normalize(m.title, leet=False):
        return None
    return q


def learn(lib: Library, m, tmdb) -> None:
    """Recuerda lo que identificaste a mano para que la próxima copia con ese nombre se haga sola."""
    key = normalize(m.title, leet=False)
    if key and tmdb and str(tmdb).isdigit():
        lib.db.execute("INSERT OR REPLACE INTO learned (key, tmdb, at) VALUES (?,?,?)", (key, str(tmdb), time.time()))
        lib.db.commit()


def learned(lib: Library, m) -> str | None:
    row = lib.db.execute("SELECT tmdb FROM learned WHERE key=?", (normalize(m.title, leet=False),)).fetchone()
    return row[0] if row else None


def _close(runtime, minutes) -> bool:
    """Duración parecida con margen amplio (montajes del director, créditos, PAL): TheMovieDB a veces
    tiene la de otra versión (Toy Story 2: 92 en el archivo, 99 en TMDb)."""
    return not runtime or not minutes or abs(runtime - minutes) <= max(12, 0.15 * minutes)


def _same_part(m, c) -> bool:
    part = _part_number(m.title)
    return not part or not c.get("part") or c["part"] == part


def sure_by_name(m, cands: list[dict], jf, minutes) -> tuple[dict | None, str]:
    """Cuando el nombre no deja dudas: lo que dice Jellyfin si encaja con el nombre, o una única
    opción con el nombre casi idéntico; en los dos casos la duración sólo tiene que no ser disparatada."""
    year_ok = lambda c: not m.year or not c["year"] or abs(c["year"] - m.year) <= 1
    ok = lambda c, s: c["sim"] >= s and year_ok(c) and _same_part(m, c) and _close(c["runtime"], minutes)
    if jf:
        mine = next((c for c in cands if c["tmdb"] == str(jf)), None)
        if mine and ok(mine, 0.85):
            return mine, "jellyfin"
    near = [c for c in cands if ok(c, 0.95)]
    if len(near) > 1:  # «Shrek 2» y «Shrek»: gana la del número del archivo, luego la de la duración
        near = [c for c in near if c.get("part_ok")] or [c for c in near if c["duration_ok"]] or near
    return (near[0], "por el nombre") if len(near) == 1 else (None, "")


def clear_winner(cands: list[dict]) -> dict | None:
    """Cuando no hay una sola «segura»: decide por puntos en vez de dejarla para revisar."""
    strong = [c for c in cands if c["strong"]]
    if len(strong) > 1:
        strong.sort(key=lambda c: c["score"], reverse=True)
        a, b = strong[0], strong[1]
        if a["score"] - b["score"] >= CLEAR_MARGIN or a.get("votes", 0) >= VOTES_RATIO * max(1, b.get("votes", 0)):
            return a
        return None
    if not cands or strong:
        return None
    a = cands[0]
    b = cands[1]["score"] if len(cands) > 1 else -9
    if a["duration_ok"] and a["sim"] >= 0.6 and a["score"] - b >= CLEAR_MARGIN:
        return a
    return None


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


_info = peek.info  # duración y título guardado dentro del video


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
    hint = _folder_hint(m)
    for q in [*more, *([hint] if hint else [])]:  # el nombre que le puso Jellyfin, o el que sugiere la IA
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
        names = [c["name"], c["original"], *c.get("alt", [])]
        sim = max([similarity(m.title, x) for x in names] + [similarity(base, saga) if saga and base else 0]
                  + ([similarity(hint, x) for x in names] if hint else []))
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
        counts=lambda c: None, frames_dir: Path | None = None) -> dict:
    """counts recibe cómo va: {total, ok (aplicadas), review (con opciones para revisar), fail (sin opciones), ai (esperando a la IA)}."""
    key = cfg.get("tmdb", {}).get("api_key")
    if not key:
        raise ValueError("pon la clave de TheMovieDB en Ajustes")
    db = TMDb(key, cfg.get("tmdb", {}).get("language") or "es-MX")
    gkey = cfg.get("gemini", {}).get("api_key")
    ai = Gemini(gkey) if gkey else None
    oskey = cfg.get("providers", {}).get("opensubtitles", {}).get("api_key")
    osub = OpenSubtitles(oskey) if oskey else None  # sólo para la huella: buscar no gasta descargas

    entries = [*review.get("loose", []), *review.get("todo", [])]
    applied, pending, left, unknown = [], [], [], []
    # las que ya se probaron enteras sin saber qué son no se vuelven a preguntar (gastaría cupo de la IA cada noche)
    known_unknown = {r["path"]: r["minutes"] for r in lib.db.execute("SELECT path, minutes FROM unknown")}
    for e in [e for e in entries if e["path"] in known_unknown]:
        entries.remove(e)
        left.append({**e, "unknown": True, "options": e.get("options") or [], "minutes": known_unknown[e["path"]],
                     "why": "no se sabe qué película es"})

    ai_state = {"error": None, "off": False, "fails": 0}

    def report():
        done = {a["id"] for a in applied} | {e["id"] for e in left}
        review_n = sum(1 for e in left if e.get("options"))
        counts({"total": len(entries), "ok": len(applied), "review": review_n, "fail": len(left) - review_n,
                "ai": len({e["id"] for e in [*pending, *unknown]} - done),
                "ai_error": ai_state["error"], "ai_off": ai_state["off"]})

    def ask_ai(fn, items):
        """Llama a la IA; si falla lo apunta para que se vea en la página, y tras dos fallos seguidos
        (o con la clave mala) deja de preguntarle en esta pasada."""
        if ai_state["off"]:
            return None
        try:
            out = fn(items)
            ai_state.update(fails=0, error=None)
            return out
        except GeminiError as ex:
            log.warning("Gemini: %s", ex)
            ai_state["fails"] += 1
            ai_state["error"] = str(ex).rsplit(" (", 1)[0]
            if ex.kind == "key" or ai_state["fails"] >= 2:
                ai_state["off"] = True
        except Exception as ex:
            log.warning("Gemini: %s", ex)
            ai_state.update(fails=ai_state["fails"] + 1, error=f"la IA (Gemini) dio un error: {ex}")
            if ai_state["fails"] >= 2:
                ai_state["off"] = True
        report()
        return None

    def no_ai(e):
        e["why"] = f"{e.get('why') or 'para revisar'}; no se pudo preguntar a la IA ({ai_state['error']})"

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
        inf = _info(m.path)
        minutes = inf["minutes"]
        jf = (e.get("jellyfin") or {}).get("tmdb")
        jname = (e.get("jellyfin") or {}).get("name")
        hits = peek.hash_lookup(osub, m.oshash)  # la huella del archivo en OpenSubtitles
        e["hashed"] = bool(hits)
        known = learned(lib, m)
        extra = [int(h["tmdb"]) for h in hits[:2]] + ([int(jf)] if jf and str(jf).isdigit() else []) \
            + ([int(known)] if known else [])
        more = [x for x in (inf["title"], jname) if x]
        cands = candidates(db, m, minutes, extra, more)
        e["minutes"] = minutes
        e["inside"] = inf["title"]
        e["_m"] = m
        e["options"] = cands[:6]
        if known:  # ya la identificaste tú a mano con este mismo nombre
            mine = next((c for c in cands if c["tmdb"] == known), None)
            if mine and mine["duration_ok"] is not False:
                do_apply(e, mine, "aprendido")
                continue
        if hits:  # ese mismo archivo ya lo conoce OpenSubtitles: es esa película salvo que la duración no cuadre
            mine = next((c for c in cands if c["tmdb"] == hits[0]["tmdb"]), None)
            if mine and mine["duration_ok"] is not False:
                do_apply(e, mine, "huella")
                continue
        if inf["title"]:  # el título guardado dentro del video cuenta como un buen nombre
            for c in cands:
                if c["duration_ok"] and max(similarity(inf["title"], c["name"]), similarity(inf["title"], c["original"])) >= 0.85:
                    c["strong"] = True
        # Sin carpeta propia y Jellyfin ya la reconocía: basta con que la duración no lo contradiga.
        if "target" in e and jf:
            mine = next((c for c in cands if c["tmdb"] == str(jf)), None)
            if mine and mine["duration_ok"] is not False:
                do_apply(e, mine, "auto")
                continue
        if m.title:
            pick, how = sure_by_name(m, cands, jf, minutes)
            if pick:
                do_apply(e, pick, how)
                continue
        strong = [c for c in cands if c["strong"]]
        by_part = [c for c in strong if c["part_ok"]]  # «after 2»: la 2.ª de la saga gana a «After» a secas
        if len(strong) == 1 or len(by_part) == 1:
            do_apply(e, by_part[0] if len(by_part) == 1 else strong[0], "auto")
            continue
        win = clear_winner(cands)
        if win:
            do_apply(e, win, "por puntos")
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
        guesses = ask_ai(ai.guess, [{"n": k + 1, "file": e["name"], "folder": e["folder"], "minutes": e["minutes"]}
                                    for k, e in enumerate(group)])
        if guesses is None:
            for e in group:
                no_ai(e)
                left.append(e)
            continue
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
        picks = ask_ai(ai.choose, items)
        if picks is None:
            for e in group:
                no_ai(e)
                left.append(e)
            continue
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

    def look(e, n, total, second):
        """La IA mira el video (second: fotos del principio y un minuto de audio). True si se aplicó."""
        progress(f"la IA {'escucha' if second else 'mira'} el video: {n + 1} de {total} ({len(applied)} hechas)")
        m = e["_m"]
        try:
            stamp = int(Path(m.path).stat().st_mtime)
        except OSError:
            return False
        key = f"{m.id}_{stamp}"
        if second:
            imgs = [p.read_bytes() for p in peek.opening(m.path, frames_dir, e["minutes"], key)]
            mp3 = peek.clip(m.path, frames_dir, e["minutes"], key)
            sound = mp3.read_bytes() if mp3 else None
            seen = ask_ai(lambda _: ai.listen(imgs, sound, e["name"], e["minutes"], e.get("options") or []), None) \
                if imgs or sound else None
        else:
            imgs = [p.read_bytes() for p in peek.frames(m.path, frames_dir, e["minutes"], key)]
            seen = ask_ai(lambda _: ai.see(imgs, e["name"], e["minutes"], e.get("options") or []), None) if imgs else None
        if seen is not None:
            e["looked"] = e.get("looked", 0) + 1
        if not seen or not seen[0]:
            return False
        names, sure = seen
        e["ai_guess"] = True
        ids: list[int] = []
        for title, year in names:
            try:
                ids += [x for x in db.search(normalize(title, leet=False) or title, year)[:3] if x not in ids]
            except Exception as ex:
                log.warning("TheMovieDB: no se pudo buscar «%s»: %s", title, ex)
        known = [int(o["tmdb"]) for o in e.get("options") or []]
        cands = candidates(db, m, e["minutes"], ids + [k for k in known if k not in ids], [t for t, _ in names]) if ids else []
        first = [c for c in cands if int(c["tmdb"]) in ids[:1]]
        if first and sure and first[0]["duration_ok"]:
            do_apply(e, first[0], "ia")
            return True
        if first:
            e["options"] = first + [c for c in cands if c is not first[0]][:5]
            e["why"] = (f"la IA {'escuchó' if second else 'miró'} el video y cree que es «{first[0]['name']}», "
                        "pero no está segura")
        return False

    # Lo que queda: que la IA mire unas fotos del video y, si no basta, el principio y un trozo de audio.
    if ai and frames_dir:
        for second in (False, True):
            look_at, left[:] = list(left), []
            for n, e in enumerate(look_at):
                if stop() or ai_state["off"] or not e.get("_m") or (second and e.get("ai_guess")):
                    left.append(e)
                elif not look(e, n, len(look_at), second):
                    left.append(e)

    # Ni el nombre, ni la huella, ni el título de dentro, ni la IA mirando y escuchando: no se sabe qué es.
    now = time.time()
    for e in left:
        if "unknown" in e:  # de una pasada anterior
            continue
        tried_all = e.get("looked", 0) >= 2 and not e.get("ai_guess")
        e["unknown"] = bool(not e.get("inside") and not e.get("hashed") and tried_all)
        for k in ("looked", "ai_guess", "hashed"):
            e.pop(k, None)
        if e["unknown"]:
            e["why"] = "no se sabe qué película es: ni el nombre, ni la huella, ni la IA mirando y escuchando el video"
            lib.db.execute("INSERT OR REPLACE INTO unknown (path, minutes, at) VALUES (?,?,?)", (e["path"], e.get("minutes"), now))
    lib.db.commit()

    report()
    for e in left:
        e.pop("_m", None)
    log.info("identificar todo: %d aplicadas (%d con IA), %d para revisar", len(applied),
             sum(a["how"] == "ia" for a in applied), len(left))
    return {"applied": applied, "todo": left}


def enrich(lib: Library, cfg: dict, review: dict, progress=lambda msg: None) -> dict:
    """«Revisar nombres»: la duración del archivo, el título de dentro del video y, si hay clave de
    TheMovieDB, opciones con su duración (las de Jellyfin no la traen) para poder comparar a mano."""
    key = cfg.get("tmdb", {}).get("api_key")
    db = TMDb(key, cfg.get("tmdb", {}).get("language") or "es-MX") if key else None
    todo = review.get("todo", [])
    for n, e in enumerate(todo):
        media = lib.media("id=?", (e["id"],))
        if not media:
            continue
        m = media[0]
        progress(f"mirando duraciones: {n + 1} de {len(todo)}")
        inf = _info(m.path)
        e["minutes"], e["inside"] = inf["minutes"], inf["title"]
        if not db:
            continue
        jf = e.get("jellyfin") or {}
        extra = [int(x) for x in [jf.get("tmdb"), *(o.get("tmdb") for o in e.get("options", []))]
                 if x and str(x).isdigit()]
        extra = list(dict.fromkeys(extra))
        cands = candidates(db, m, inf["minutes"], extra, [x for x in (inf["title"], jf.get("name")) if x])
        if cands:
            e["options"] = cands[:6]
            if e.get("why") == "no se encontró nada con ese nombre":
                e.pop("why")
    return review


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
