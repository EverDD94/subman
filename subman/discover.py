"""Descubrir: novedades, clásicos y recomendaciones de TheMovieDB, con tus calificaciones.

Calificaciones: 2 = me encanta, 1 = me gusta, -1 = no me gusta, 0 = no la he visto (sólo para no volver
a preguntarla en «Mis gustos»). «La tengo» se sabe sola mirando la
biblioteca (carpetas con ``[tmdbid-N]``, Jellyfin, o el mismo título y año), y se puede corregir a mano.
«Para ti» junta las recomendaciones de TheMovieDB de lo que te encanta (cuenta doble) y te gusta,
sube los géneros que te gustan, baja los que no, y quita lo que ya tienes o ya calificaste.

Además aprende de tus calificaciones (``learned``): cada «me encanta», «me gusta» o «no me gusta»
suma o resta a sus géneros, a su década, a películas o series y a su reparto y dirección. Esos pesos
ordenan «Para ti», «Mis gustos» y «Estrenos», y la gente que aparece en varias que te gustan trae
más títulos suyos.
"""
from __future__ import annotations

import json
import logging
import random
import re
import time
import unicodedata
from datetime import date, timedelta

from . import jellyfin
from .library import Library
from .tmdb import TMDb

log = logging.getLogger(__name__)
IMG = "https://image.tmdb.org/t/p/w342"
BACKDROP = "https://image.tmdb.org/t/p/w780"
KINDS = ("movie", "tv")
_TMDBID = re.compile(r"\[tmdbid-(\d+)\]", re.I)

SCHEMA = """
CREATE TABLE IF NOT EXISTS discover (
    kind TEXT NOT NULL,            -- movie | tv
    tmdb INTEGER NOT NULL,
    rating INTEGER,                -- 2 me encanta | 1 me gusta | -1 no me gusta | 0 no la he visto | NULL
    have INTEGER,                  -- 1 la tengo | 0 no la tengo (a mano) | NULL = lo que diga la biblioteca
    info TEXT,                     -- JSON con título, año, póster... (para enseñarla sin preguntar otra vez)
    updated REAL,
    PRIMARY KEY (kind, tmdb)
);
CREATE TABLE IF NOT EXISTS credits (  -- reparto principal y dirección de lo calificado (no cambia)
    kind TEXT NOT NULL,
    tmdb INTEGER NOT NULL,
    data TEXT NOT NULL,
    PRIMARY KEY (kind, tmdb)
);
CREATE TABLE IF NOT EXISTS taste (  -- respuestas de «Mis gustos» (una sola fila)
    id INTEGER PRIMARY KEY CHECK (id = 1),
    data TEXT NOT NULL
);
"""
PROFILE = {"done": False, "genres": {}, "people": [], "people_no": [], "people_seen": [], "kinds": "both", "era": "all"}
PROFILE_IMG = "https://image.tmdb.org/t/p/w185"
TV_ALIAS = {10759: (28, 12), 10765: (878, 14), 10768: (10752,)}  # géneros de series con nombre en inglés
TV_SKIP = {10763, 10767}  # noticias y programas de entrevistas
RW = {2: 2.0, 1: 1.0, -1: -2.0}  # lo que pesa cada calificación al aprender (un «no me gusta» pesa como un «me encanta»)
CAST_FETCH = 40  # repartos nuevos que se piden a TheMovieDB cada vez (el resto, en la siguiente)


def _gids(ids) -> list[int]:
    """Géneros de película y de serie en la misma escala (Acción y aventura de series = Acción + Aventura)."""
    out = []
    for x in ids or []:
        for y in TV_ALIAS.get(int(x), (int(x),)):
            if y not in TV_SKIP and y not in out:
                out.append(y)
    return out


def _decade(year) -> int | None:
    return int(year) // 10 * 10 if year else None


def _norm(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    s = re.sub(r"^(the|el|la|los|las)\s+", "", s.strip())
    return re.sub(r"[^a-z0-9]+", "", s)


def card(kind: str, d: dict, genres: dict) -> dict:
    """Un resultado de TheMovieDB (película o serie) en el formato de la web."""
    when = d.get("release_date") if kind == "movie" else d.get("first_air_date")
    return {"kind": kind, "tmdb": int(d["id"]),
            "title": d.get("title") or d.get("name") or "",
            "original": d.get("original_title") or d.get("original_name") or "",
            "year": int(when[:4]) if when else None, "date": when or "",
            "poster": IMG + d["poster_path"] if d.get("poster_path") else "",
            "backdrop": BACKDROP + d["backdrop_path"] if d.get("backdrop_path") else "",
            "overview": d.get("overview") or "",
            "genres": [genres.get(g, "") for g in d.get("genre_ids") or [] if genres.get(g)],
            "genre_ids": d.get("genre_ids") or [],
            "vote": round(d.get("vote_average") or 0, 1)}


class Discover:
    def __init__(self, lib: Library, cfg: dict):
        self.lib = lib
        self.cfg = cfg
        self.db = lib.db
        self.db.executescript(SCHEMA)
        t = cfg.get("tmdb", {})
        self.language = t.get("language") or "es-MX"
        self.api = TMDb(t["api_key"], self.language)

    # --- lo que guarda el usuario -----------------------------------------------------
    def marks(self) -> dict[tuple[str, int], dict]:
        out = {}
        for r in self.db.execute("SELECT kind, tmdb, rating, have, info FROM discover"):
            out[(r["kind"], r["tmdb"])] = {"rating": r["rating"], "have": r["have"],
                                           "info": json.loads(r["info"] or "{}")}
        return out

    def mark(self, item: dict, rating=..., have=...) -> None:
        """Guarda la calificación y/o «la tengo» (``None`` la quita; ``...`` la deja como está)."""
        kind, tid = item["kind"], int(item["tmdb"])
        if kind not in KINDS:
            raise ValueError("tipo desconocido")
        row = self.db.execute("SELECT rating, have FROM discover WHERE kind=? AND tmdb=?", (kind, tid)).fetchone()
        r = row["rating"] if row else None
        h = row["have"] if row else None
        if rating is not ...:
            if rating not in (None, 2, 1, 0, -1):
                raise ValueError("calificación no válida")
            r = rating
        if have is not ...:
            h = None if have is None else int(bool(have))
        info = {k: item.get(k) for k in ("title", "original", "year", "date", "poster", "backdrop", "overview", "genres",
                                         "genre_ids", "vote")}
        if r is None and h is None:
            self.db.execute("DELETE FROM discover WHERE kind=? AND tmdb=?", (kind, tid))
        else:
            self.db.execute(
                """INSERT INTO discover (kind, tmdb, rating, have, info, updated) VALUES (?,?,?,?,?,?)
                   ON CONFLICT(kind, tmdb) DO UPDATE SET rating=excluded.rating, have=excluded.have,
                     info=CASE WHEN excluded.info IS NOT NULL AND excluded.info != '{}' THEN excluded.info
                          ELSE discover.info END, updated=excluded.updated""",
                (kind, tid, r, h, json.dumps(info, ensure_ascii=False) if item.get("title") else None, time.time()))
        self.db.commit()

    # --- lo que ya hay en la biblioteca ------------------------------------------------
    _owned: dict = {}

    def owned_index(self) -> dict:
        """Qué hay en la biblioteca: números de TheMovieDB conocidos y títulos (normalizados)."""
        hit = Discover._owned.get("idx")
        if hit and time.time() - hit[0] < 300:  # Jellyfin tarda: se recuerda unos minutos
            return hit[1]
        ids = {"movie": set(), "tv": set()}
        movies: dict[str, set] = {}
        series: set[str] = set()
        for r in self.db.execute("SELECT path, kind, title, year FROM media"):
            if r["kind"] == "movie":
                m = _TMDBID.search(r["path"])
                if m:
                    ids["movie"].add(int(m.group(1)))
                movies.setdefault(_norm(r["title"]), set()).add(r["year"])
            else:
                series.add(_norm(r["title"]))
        jf = self.cfg.get("jellyfin", {})
        if jf.get("url") and jf.get("api_key"):
            try:
                for it in jellyfin.movies(jf["url"], jf["api_key"]):
                    if it.get("tmdb") and str(it["tmdb"]).isdigit():
                        ids["movie"].add(int(it["tmdb"]))
            except Exception as e:
                log.info("descubrir: Jellyfin no respondió (%s); se mira sólo la carpeta", e)
        idx = {"ids": ids, "movies": movies, "series": series}
        Discover._owned["idx"] = (time.time(), idx)
        return idx

    @staticmethod
    def in_library(c: dict, idx: dict) -> bool:
        if c["tmdb"] in idx["ids"][c["kind"]]:
            return True
        names = {_norm(c["title"]), _norm(c.get("original", ""))} - {""}
        if c["kind"] == "tv":
            return bool(names & idx["series"])
        for n in names:
            years = idx["movies"].get(n)
            if years and (c["year"] is None or None in years or any(abs(y - c["year"]) <= 1 for y in years if y)):
                return True
        return False

    def _decorate(self, cards: list[dict], idx: dict, marks: dict) -> list[dict]:
        for c in cards:
            mk = marks.get((c["kind"], c["tmdb"]), {})
            c["rating"] = mk.get("rating")
            c["auto_have"] = self.in_library(c, idx)
            c["have"] = bool(mk["have"]) if mk.get("have") is not None else c["auto_have"]
            c["have_manual"] = mk.get("have") is not None
        return cards

    # --- TheMovieDB --------------------------------------------------------------------
    _genres: dict = {}

    def genres(self) -> dict[int, str]:
        if self.language not in Discover._genres:
            g = {}
            for kind in KINDS:
                for x in self.api._get(f"/genre/{kind}/list").get("genres", []):
                    g[x["id"]] = x["name"]
            Discover._genres[self.language] = g
        return Discover._genres[self.language]

    def news(self, page: int = 1) -> list[dict]:
        today = date.today()
        movies = self._page("movie", "/discover/movie", page, sort_by="popularity.desc", include_adult="false",
                            **{"primary_release_date.gte": (today - timedelta(days=60)).isoformat(),
                               "primary_release_date.lte": (today + timedelta(days=30)).isoformat()})
        series = self._page("tv", "/discover/tv", page, sort_by="popularity.desc", include_adult="false",
                            **{"first_air_date.gte": (today - timedelta(days=90)).isoformat(),
                               "first_air_date.lte": (today + timedelta(days=30)).isoformat()})
        return _mix(movies, series)

    def classics(self, page: int = 1) -> list[dict]:
        trending = self._page("movie", "/trending/movie/week", page) + self._page("tv", "/trending/tv/week", page)
        best = self._page("movie", "/movie/top_rated", page) + self._page("tv", "/tv/top_rated", page)
        return _mix(trending, best)

    def _page(self, kind: str, path: str, page: int, **params) -> list[dict]:
        res = self.api._get(path, page=page, **params)
        return [card(kind, d, self.genres()) for d in res.get("results", []) if d.get("id")]

    _recs: dict = {}

    def _recommendations(self, kind: str, tid: int) -> list[dict]:
        key = (self.language, kind, tid)
        hit = Discover._recs.get(key)
        if not hit or time.time() - hit[0] > 24 * 3600:
            try:
                hit = (time.time(), self._page(kind, f"/{kind}/{tid}/recommendations", 1))
            except Exception as e:
                log.info("descubrir: sin recomendaciones para %s %s (%s)", kind, tid, e)
                hit = (time.time(), [])
            Discover._recs[key] = hit
        return [dict(c) for c in hit[1]]

    # --- lo que se aprende de tus calificaciones ---------------------------------------
    def _cast(self, kind: str, tid: int, fetch: bool = True) -> list[dict] | None:
        """Reparto principal y dirección de un título (se guarda en la base: no cambia)."""
        row = self.db.execute("SELECT data FROM credits WHERE kind=? AND tmdb=?", (kind, tid)).fetchone()
        if row:
            return json.loads(row["data"])
        if not fetch:
            return None
        try:
            d = self.api._get(f"/{kind}/{tid}/credits")
        except Exception as e:
            log.info("descubrir: sin reparto de %s %s (%s)", kind, tid, e)
            return None
        people = [{"id": x["id"], "name": x.get("name", "")} for x in (d.get("cast") or [])[:6] if x.get("id")]
        people += [{"id": x["id"], "name": x.get("name", "")} for x in d.get("crew") or []
                   if x.get("job") == "Director" and x.get("id")][:2]
        self.db.execute("INSERT OR REPLACE INTO credits (kind, tmdb, data) VALUES (?,?,?)",
                        (kind, tid, json.dumps(people, ensure_ascii=False)))
        self.db.commit()
        return people

    def learned(self, marks: dict | None = None, fetch: int = CAST_FETCH) -> dict:
        """Pesos aprendidos de tus calificaciones: géneros, décadas, películas/series y gente.

        Cada peso es la suma de lo que valen sus calificaciones dividida entre (cuántas + 2): con una sola
        no se fía mucho, y cuantas más coinciden, más pesa. La gente cuenta a partir de dos títulos.
        """
        marks = self.marks() if marks is None else marks
        rated = {k: v for k, v in marks.items() if v["rating"] in RW}
        acc: dict[str, dict] = {"genres": {}, "decades": {}, "kinds": {}, "people": {}}
        names: dict[int, str] = {}
        budget = fetch

        def add(table, key, w):
            s = acc[table].setdefault(key, [0.0, 0])
            s[0] += w
            s[1] += 1
        for (kind, tid), v in rated.items():
            w, info = RW[v["rating"]], v["info"]
            for g in _gids(info.get("genre_ids")):
                add("genres", g, w)
            if _decade(info.get("year")):
                add("decades", _decade(info.get("year")), w)
            add("kinds", kind, w)
            cast = self._cast(kind, tid, fetch=False)
            if cast is None and budget > 0:  # sin guardar todavía: se pide (unas pocas cada vez)
                budget -= 1
                cast = self._cast(kind, tid)
            if cast is None:
                continue
            for p in cast:
                add("people", p["id"], w)
                names[p["id"]] = p["name"]
        # la época se fía mucho menos: si te salen sobre todo viejas, que te gusten no quiere decir que prefieras lo viejo
        out = {k: {key: s / (n + (8 if k == "decades" else 2)) for key, (s, n) in t.items()}
               for k, t in acc.items() if k != "people"}
        out["people"] = sorted(({"id": pid, "name": names.get(pid, ""), "w": s / (n + 2), "n": n}
                                for pid, (s, n) in acc["people"].items() if n >= 2), key=lambda p: -abs(p["w"]))
        out["count"] = len(rated)
        return out

    @staticmethod
    def affinity(c: dict, L: dict) -> float:
        """Cuánto encaja un título con lo aprendido (0 = nada que decir; negativo = no es lo tuyo)."""
        gs = [L["genres"].get(g, 0.0) for g in _gids(c.get("genre_ids"))]
        a = sum(gs) / len(gs) if gs else 0.0
        if gs and min(gs) < 0:
            a += 0.5 * min(gs)  # un género que no te gusta pesa más que uno indiferente
        if _decade(c.get("year")):
            a += 0.15 * L["decades"].get(_decade(c.get("year")), 0.0)
        if (c.get("year") or 0) >= date.today().year - 5:
            a += 0.15  # lo reciente, un empujoncito
        a += 0.3 * L["kinds"].get(c.get("kind"), 0.0)
        return a

    def _with_learned(self, prof: dict, L: dict) -> dict:
        """«Mis gustos» completado con lo aprendido (lo que marcaste a mano manda)."""
        prof = {**prof, "genres": dict(prof["genres"])}
        for g, w in L["genres"].items():
            if str(g) not in prof["genres"] and abs(w) >= 0.6:
                prof["genres"][str(g)] = 1 if w > 0 else -1
        return prof

    def learned_summary(self) -> dict:
        """Lo aprendido, en palabras, para enseñarlo en «Mis gustos»."""
        L = self.learned()
        names = self.genres()
        g = sorted(L["genres"].items(), key=lambda kv: -kv[1])
        dec = sorted(((d, w) for d, w in L["decades"].items() if w >= 0.5), key=lambda kv: -kv[1])[:3]
        label = lambda d: f"años {d}" if d >= 2000 else f"años {d % 100:02d}"
        kinds = L["kinds"]
        dec_n: dict[int, int] = {}
        dec_r: dict[int, int] = {}
        kind_n = {k: 0 for k in KINDS}
        for (kind, _), v in self.marks().items():  # para las gráficas: lo que te gusta, por década y tipo
            if v["rating"] in RW and _decade(v["info"].get("year")):
                dec_r[_decade(v["info"].get("year"))] = dec_r.get(_decade(v["info"].get("year")), 0) + 1
            if (v["rating"] or 0) > 0:
                kind_n[kind] = kind_n.get(kind, 0) + 1
                if _decade(v["info"].get("year")):
                    dec_n[_decade(v["info"].get("year"))] = dec_n.get(_decade(v["info"].get("year")), 0) + 1
        return {"count": L["count"],
                "genre_w": {str(k): round(w, 2) for k, w in L["genres"].items()},
                "decade_n": [{"label": f"{d}" if d >= 2000 else f"{d % 100:02d}", "n": n, "of": dec_r.get(d, n)}
                             for d, n in sorted(dec_n.items()) if d >= 1950],
                "kind_n": kind_n,
                "likes": [names[k] for k, w in g if w >= 0.5 and k in names][:6],
                "dislikes": [names[k] for k, w in reversed(g) if w <= -0.5 and k in names][:6],
                "decades": [label(d) for d, _ in dec],
                "kind": ("series" if kinds.get("tv", 0) > kinds.get("movie", 0) + 0.4 else
                         "películas" if kinds.get("movie", 0) > kinds.get("tv", 0) + 0.4 else ""),
                "people": [p["name"] for p in L["people"] if p["w"] >= 0.5][:8],
                "people_no": [p["name"] for p in L["people"] if p["w"] <= -0.5][:6]}

    def for_you(self, limit: int = 60) -> list[dict]:
        marks = self.marks()
        prof = self.profile()
        liked = sorted(((k, v) for k, v in marks.items() if (v["rating"] or 0) > 0),
                       key=lambda kv: -kv[1]["rating"])[:40]
        if not liked and not prof["done"]:
            return []
        L = self.learned(marks)
        taste: dict[int, float] = {int(g): 3 * v for g, v in prof["genres"].items()}  # lo que marcaste a mano
        score: dict[tuple, float] = {}
        cards: dict[tuple, dict] = {}
        why: dict[tuple, list] = {}
        for c, reason in self._seed(self._with_learned(prof, L)):  # «Mis gustos» + lo aprendido
            k = (c["kind"], c["tmdb"])
            score[k] = score.get(k, 0) + 0.6
            cards[k] = c
            why.setdefault(k, []).append(reason)
        for (kind, tid), v in liked:
            for rank, c in enumerate(self._recommendations(kind, tid)):
                k = (c["kind"], c["tmdb"])
                score[k] = score.get(k, 0) + v["rating"] * (1 - rank / 40)
                cards[k] = c
                why.setdefault(k, []).append(v["info"].get("title") or "")
        for p in [x for x in L["people"] if x["w"] >= 0.5][:6]:  # gente que sale en varias que te gustan
            for c in self._credits(p["id"]):
                k = (c["kind"], c["tmdb"])
                score[k] = score.get(k, 0) + 0.5 * p["w"]
                cards.setdefault(k, c)
                why.setdefault(k, []).append(f"sale {p['name']}")
        bad = prof["people_no"][:20] + [x for x in L["people"] if x["w"] <= -0.5][:10]
        for p in bad:  # y la gente que no te gusta
            for c in self._credits(p["id"]):
                k = (c["kind"], c["tmdb"])
                if k in score:
                    score[k] -= 0.5
        for (kind, tid), v in marks.items():  # lo que no te gusta también aleja lo parecido
            if v["rating"] == -1:
                for c in self._recommendations(kind, tid)[:10]:
                    k = (c["kind"], c["tmdb"])
                    if k in score:
                        score[k] -= 0.5
        idx = self.owned_index()
        out = []
        for k, c in cards.items():
            mk = marks.get(k)
            if mk and (mk["rating"] not in (None, 0) or mk["have"] == 1):
                continue  # ya calificada o ya la tienes
            if not (mk and mk["have"] == 0) and self.in_library(c, idx):
                continue
            g = sum(taste.get(x, 0) for x in c["genre_ids"]) / max(1, len(c["genre_ids"]))
            if prof["kinds"] in KINDS and c["kind"] != prof["kinds"]:
                continue
            aff = self.affinity(c, L)
            recent = 0.5 if (c.get("year") or 0) >= date.today().year - 5 else 0.0  # que salgan también nuevas
            c["score"] = score[k] + 0.15 * g + 0.8 * aff + c["vote"] / 50 + recent
            c["because"] = [w for w in dict.fromkeys(why[k]) if w][:3]
            if not c["because"] and aff >= 0.5:
                top = max(_gids(c["genre_ids"]), key=lambda x: L["genres"].get(x, 0), default=None)
                if top in self.genres():
                    c["because"] = [f"te gusta: {self.genres()[top]}"]
            out.append(c)
        out.sort(key=lambda c: -c["score"])
        return self._decorate(out[:limit], idx, marks)

    def wanted(self) -> list[dict]:
        """Por conseguir: sólo lo que marcaste «No la tengo» / «Agregar a mi lista». Lo que puntúas va a
        Mis gustos: que algo te encante no quiere decir que lo quieras bajar."""
        idx = self.owned_index()
        marks = self.marks()
        out = []
        for (kind, tid), v in marks.items():
            c = {"kind": kind, "tmdb": tid, **v["info"]}
            c.setdefault("title", "")
            c.setdefault("genre_ids", [])
            c.setdefault("year", None)
            if v["have"] == 0:
                out.append(c)
        out.sort(key=lambda c: (-(marks[(c["kind"], c["tmdb"])]["rating"] or 0), c["title"]))
        return self._decorate(out, idx, marks)

    def rated(self) -> list[dict]:
        marks = self.marks()
        rows = self.db.execute("SELECT kind, tmdb FROM discover WHERE rating IS NOT NULL AND rating != 0 ORDER BY updated DESC")
        out = [{"kind": r["kind"], "tmdb": r["tmdb"], "genre_ids": [], "year": None, "title": "",
                **marks[(r["kind"], r["tmdb"])]["info"]} for r in rows]
        return self._decorate(out, self.owned_index(), marks)

    def search(self, query: str) -> list[dict]:
        res = self.api._get("/search/multi", query=query, include_adult="false")
        cards = [card(d["media_type"], d, self.genres()) for d in res.get("results", [])
                 if d.get("media_type") in KINDS and d.get("id")]
        return self._decorate(cards, self.owned_index(), self.marks())

    # --- Mis gustos -----------------------------------------------------------------------
    def profile(self) -> dict:
        row = self.db.execute("SELECT data FROM taste WHERE id=1").fetchone()
        return {**PROFILE, **(json.loads(row["data"]) if row else {})}

    def save_profile(self, data: dict) -> dict:
        prof = {**PROFILE, **{k: data[k] for k in PROFILE if k in data}}
        prof["genres"] = {str(int(g)): (1 if v > 0 else -1) for g, v in (prof["genres"] or {}).items() if v}
        for f in ("people", "people_no"):
            prof[f] = [{k: p.get(k) for k in ("id", "name", "photo", "known")} for p in prof[f] or [] if p.get("id")][:60]
        prof["people_seen"] = [int(x) for x in prof["people_seen"] or []][-3000:]
        if prof["kinds"] not in ("movie", "tv", "both"):
            prof["kinds"] = "both"
        if prof["era"] not in ("new", "all", "classic"):
            prof["era"] = "all"
        self.db.execute("INSERT INTO taste (id, data) VALUES (1, ?) ON CONFLICT(id) DO UPDATE SET data=excluded.data",
                        (json.dumps(prof, ensure_ascii=False),))
        self.db.commit()
        return prof

    def reset_profile(self) -> None:
        self.db.execute("DELETE FROM taste")
        self.db.commit()

    def genre_list(self) -> list[dict]:
        """Géneros de películas y series juntos (los que se llaman igual, una sola vez)."""
        out: dict[str, dict] = {}
        by_id: dict[int, dict] = {}
        for kind in KINDS:
            for x in self.api._get(f"/genre/{kind}/list").get("genres", []):
                if x["id"] in TV_SKIP:
                    continue
                if x["id"] in TV_ALIAS:  # «Action & Adventure» de series va con Acción y Aventura de películas
                    for m in TV_ALIAS[x["id"]]:
                        if m in by_id and x["id"] not in by_id[m]["ids"]:
                            by_id[m]["ids"].append(x["id"])
                    if any(m in by_id for m in TV_ALIAS[x["id"]]):
                        continue
                g = out.setdefault(x["name"], {"id": x["id"], "name": x["name"], "ids": []})
                if x["id"] not in g["ids"]:
                    g["ids"].append(x["id"])
                by_id[x["id"]] = g
        return sorted(out.values(), key=lambda g: g["name"])

    def people(self, query: str) -> list[dict]:
        res = self.api._get("/search/person", query=query, include_adult="false")
        return [{"id": p["id"], "name": p.get("name", ""),
                 "photo": PROFILE_IMG + p["profile_path"] if p.get("profile_path") else "",
                 "known": ", ".join(k.get("title") or k.get("name") or "" for k in p.get("known_for") or [])[:120],
                 "job": p.get("known_for_department", "")}
                for p in res.get("results", [])[:8]]

    def popular_people(self, page: int = 1) -> list[dict]:
        """Actores y directores famosos para conocerlos por tarjetas (con sus títulos más conocidos)."""
        res = self.api._get("/person/popular", page=page)
        out = []
        for p in res.get("results", []):
            known = [k for k in p.get("known_for") or [] if k.get("media_type") in KINDS]
            if (p.get("adult") or not p.get("profile_path") or p.get("known_for_department") not in ("Acting", "Directing")
                    or not any((k.get("vote_count") or 0) >= 500 for k in known)):
                continue  # sólo gente conocida por películas o series que mucha gente ha visto
            out.append({"id": p["id"], "name": p.get("name", ""), "photo": PROFILE_IMG + p["profile_path"],
                        "job": "Dirige" if p.get("known_for_department") == "Directing" else "Actúa",
                        "known": ", ".join(k.get("title") or k.get("name") or "" for k in known)[:120],
                        "titles": [{"title": k.get("title") or k.get("name") or "",
                                    "poster": IMG + k["poster_path"] if k.get("poster_path") else ""} for k in known[:3]]})
        return out

    def _dates(self, era: str, kind: str) -> dict:
        field = "primary_release_date" if kind == "movie" else "first_air_date"
        year = date.today().year
        if era == "new":
            return {f"{field}.gte": f"{year - 6}-01-01"}
        if era == "classic":
            return {f"{field}.lte": f"{year - 20}-12-31"}
        return {}

    def _seed(self, prof: dict, pages=(1,)) -> list[tuple[dict, str]]:
        """Títulos sacados de tus respuestas: géneros que te gustan y tus actores o directores."""
        out = []
        like = [g for g, v in prof["genres"].items() if v > 0]
        hate = [g for g, v in prof["genres"].items() if v < 0]
        names = self.genres()
        kinds = [prof["kinds"]] if prof["kinds"] in KINDS else list(KINDS)
        for kind in kinds:
            if not like and not prof["people"]:
                like_k = []
            else:
                like_k = [g for g in like if int(g) in self._kind_genres(kind)]
            if like_k or not prof["people"]:
                for page in pages:
                    params = {"sort_by": "popularity.desc", "include_adult": "false", "vote_count.gte": 150,
                              **self._dates(prof["era"], kind)}
                    if like_k:
                        params["with_genres"] = "|".join(like_k)
                    hate_k = [g for g in hate if int(g) in self._kind_genres(kind)]
                    if hate_k:
                        params["without_genres"] = ",".join(hate_k)
                    try:
                        for c in self._page(kind, f"/discover/{kind}", page, **params):
                            g = next((names[int(x)] for x in like_k if int(x) in c["genre_ids"]), "")
                            out.append((c, f"te gusta: {g}" if g else "popular"))
                    except Exception as e:
                        log.info("descubrir: %s", e)
                if prof["era"] == "all" and like_k:  # sin época elegida, también lo reciente de tus géneros
                    try:
                        for c in self._page(kind, f"/discover/{kind}", pages[0], sort_by="popularity.desc",
                                            include_adult="false", **{"vote_count.gte": 50},
                                            with_genres="|".join(like_k), **self._dates("new", kind)):
                            out.append((c, "reciente"))
                    except Exception as e:
                        log.info("descubrir: %s", e)
        for p in prof["people"][:12]:
            for c in self._credits(p["id"]):
                if c["kind"] in kinds:
                    out.append((c, f"sale {p['name']}"))
        return out

    _kg: dict = {}

    def _kind_genres(self, kind: str) -> set[int]:
        if kind not in Discover._kg:
            Discover._kg[kind] = {x["id"] for x in self.api._get(f"/genre/{kind}/list").get("genres", [])}
        return Discover._kg[kind]

    _credit_cache: dict = {}

    def _credits(self, person: int) -> list[dict]:
        key = (self.language, person)
        hit = Discover._credit_cache.get(key)
        if not hit or time.time() - hit[0] > 24 * 3600:
            try:
                d = self.api._get(f"/person/{person}/combined_credits")
                rows = [x for x in d.get("cast", []) + [x for x in d.get("crew", []) if x.get("job") == "Director"]
                        if x.get("media_type") in KINDS and x.get("id") and (x.get("vote_count") or 0) >= 50
                        and 10767 not in (x.get("genre_ids") or []) and 10763 not in (x.get("genre_ids") or [])]
                rows.sort(key=lambda x: -(x.get("popularity") or 0))
                seen, cards = set(), []
                for x in rows:
                    if (x["media_type"], x["id"]) not in seen:
                        seen.add((x["media_type"], x["id"]))
                        cards.append(card(x["media_type"], x, self.genres()))
                hit = (time.time(), cards[:15])
            except Exception as e:
                log.info("descubrir: sin títulos de la persona %s (%s)", person, e)
                hit = (time.time(), [])
            Discover._credit_cache[key] = hit
        return [dict(c) for c in hit[1]]

    def deck(self, size: int = 20) -> list[dict]:
        """Tarjetas para calificar de una en una: según tus respuestas, sin repetir las ya contestadas."""
        prof = self.profile()
        marks = self.marks()
        n_done = sum(1 for v in marks.values() if v["rating"] is not None)
        pages = random.sample(range(1, 8), 2) if n_done > 20 else [1, 2]
        seen, out = set(), []
        L = self.learned(marks, fetch=8)
        pool = self._seed(self._with_learned(prof, L), pages)
        # también parecidas a lo que te encanta, para afinar
        for (kind, tid), v in sorted(marks.items(), key=lambda kv: -(kv[1]["rating"] or 0))[:5]:
            if (v["rating"] or 0) > 0:
                pool += [(c, f"parecida a {v['info'].get('title', '')}") for c in self._recommendations(kind, tid)[:8]]
        # primero lo que más encaja con lo aprendido, con algo de azar para no repetir siempre lo mismo
        random.shuffle(pool)
        pool.sort(key=lambda cw: -(self.affinity(cw[0], L) + random.random() * 0.8))
        for c, why in pool:
            k = (c["kind"], c["tmdb"])
            if k in seen or k in marks or not c["poster"]:
                continue
            if L["count"] >= 8 and self.affinity(c, L) <= -0.8:
                continue  # claramente no es lo tuyo
            seen.add(k)
            c["because"] = [why]
            out.append(c)
            if len(out) >= size:
                break
        return self._decorate(out, self.owned_index(), marks)

    def detail(self, kind: str, tid: int) -> dict:
        if kind not in KINDS:
            raise ValueError("tipo desconocido")
        lang = self.language.split("-")[0]
        d = self.api._get(f"/{kind}/{tid}", append_to_response="credits,videos",
                          include_video_language=f"{lang},en,null")
        c = card(kind, {**d, "genre_ids": [g["id"] for g in d.get("genres", [])]}, self.genres())
        if not c["overview"]:  # sin resumen en español: el de inglés
            try:
                c["overview"] = self.api._get(f"/{kind}/{tid}", language="en-US").get("overview") or ""
                c["overview_lang"] = "en"
            except Exception:
                pass
        cr = d.get("credits") or {}
        c["cast"] = [{"name": x.get("name", ""), "character": x.get("character", ""),
                      "photo": PROFILE_IMG + x["profile_path"] if x.get("profile_path") else ""}
                     for x in (cr.get("cast") or [])[:10]]
        c["directors"] = [x["name"] for x in cr.get("crew") or [] if x.get("job") == "Director"][:3]
        c["creators"] = [x.get("name", "") for x in d.get("created_by") or []]
        c["runtime"] = d.get("runtime") or (d.get("episode_run_time") or [None])[0]
        c["seasons"] = d.get("number_of_seasons")
        c["episodes"] = d.get("number_of_episodes")
        c["status"] = d.get("status", "")
        c["tagline"] = d.get("tagline", "")
        vids = [v for v in (d.get("videos") or {}).get("results", []) if v.get("site") == "YouTube"
                and v.get("type") in ("Trailer", "Teaser")]
        vids.sort(key=lambda v: (v.get("type") != "Trailer", v.get("iso_639_1") != lang))
        c["trailer"] = f"https://www.youtube.com/watch?v={vids[0]['key']}" if vids else ""
        if d.get("backdrop_path"):  # más grande para la ficha a pantalla completa
            c["backdrop"] = "https://image.tmdb.org/t/p/w1280" + d["backdrop_path"]
        return self._decorate([c], self.owned_index(), self.marks())[0]

    def releases(self, kind: str, page: int = 1) -> list[dict]:
        """Estrenos de los últimos 12 meses (películas o series nuevas) que todavía no has contestado."""
        if kind not in KINDS:
            raise ValueError("tipo desconocido")
        today = date.today()
        field = "primary_release_date" if kind == "movie" else "first_air_date"
        marks = self.marks()
        res = self.api._get(f"/discover/{kind}", page=page, sort_by="popularity.desc", include_adult="false",
                            **{f"{field}.gte": (today - timedelta(days=365)).isoformat(),
                               f"{field}.lte": today.isoformat(), "vote_count.gte": 5})
        cards = [card(kind, d, self.genres()) for d in res.get("results", []) if d.get("id") and d.get("poster_path")]
        cards = [c for c in cards if (kind, c["tmdb"]) not in marks]
        L = self.learned(marks, fetch=8)  # que los estrenos salgan rápido
        if L["count"] >= 5:  # dentro de cada tanda, primero lo que más encaja contigo
            cards.sort(key=lambda c: -self.affinity(c, L))
        for c in cards:
            if not c["overview"]:  # sin resumen en español: el de inglés
                try:
                    c["overview"] = self.api._get(f"/{kind}/{c['tmdb']}", language="en-US").get("overview") or ""
                    c["overview_lang"] = "en"
                except Exception:
                    pass
        return self._decorate(cards, self.owned_index(), marks)

    def releases_summary(self) -> dict:
        """Para la portada de Estrenos: cuántos hay este año, cuántos llevas contestados, y los pósters de los más vistos."""
        today = date.today()
        since = (today - timedelta(days=365)).isoformat()
        marks = self.marks()
        out = {}
        for kind in KINDS:
            field = "primary_release_date" if kind == "movie" else "first_air_date"
            res = self.api._get(f"/discover/{kind}", page=1, sort_by="popularity.desc", include_adult="false",
                                **{f"{field}.gte": since, f"{field}.lte": today.isoformat(), "vote_count.gte": 5})
            top = [card(kind, d, self.genres()) for d in res.get("results", []) if d.get("id") and d.get("poster_path")]
            done = sum(1 for (k, _), m in marks.items() if k == kind and m["rating"] is not None
                       and (m["info"].get("date") or "") >= since)
            total = int(res.get("total_results") or 0)
            out[kind] = {"total": total, "done": done, "left": max(total - done, 0),
                         "posters": [c["poster"] for c in top[:5]], "top": [c["title"] for c in top[:3]]}
        return out

    def stats(self) -> dict:
        rows = self.db.execute("SELECT rating, COUNT(*) n FROM discover WHERE rating IS NOT NULL GROUP BY rating")
        return {str(r["rating"]): r["n"] for r in rows}

    def mood(self, mood: str, kind: str = "") -> dict[str, list[dict]]:
        """Filas de la Portada para un ánimo: se piden a TheMovieDB y solo cuenta el género principal."""
        gs = MOODS.get(mood)
        if not gs:
            raise ValueError("Ánimo desconocido")
        today = date.today()
        L = self.learned(fetch=0)
        rows: dict[str, list[list[dict]]] = {"para-ti": [], "novedades": [], "mejores": []}
        for k in [kind] if kind in KINDS else KINDS:
            ids = gs[k]
            if not ids:
                continue
            base = {"with_genres": "|".join(map(str, ids)), "include_adult": "false"}
            date_key = "primary_release_date" if k == "movie" else "first_air_date"
            asks = {"para-ti": dict(sort_by="popularity.desc", **{"vote_count.gte": 50}),
                    "novedades": dict(sort_by="popularity.desc", **{f"{date_key}.gte": (today - timedelta(days=365)).isoformat(),
                                                                    f"{date_key}.lte": today.isoformat()}),
                    "mejores": dict(sort_by="vote_average.desc", **{"vote_count.gte": 500})}
            for row, params in asks.items():
                got = []
                for page in range(1, 6 if row == "para-ti" else 4):  # «Para ti» se ordena por tus gustos: que haya donde elegir
                    got += [c for c in self._page(k, f"/discover/{k}", page, **base, **params)
                            if set(c["genre_ids"][:2]) & set(ids)]  # su género principal o el segundo
                rows[row].append(got)
            # y lo parecido a lo que te gustó de este ánimo
            liked = [tid for (kk, tid), m in self.marks().items() if kk == k and (m["rating"] or 0) > 0
                     and (m["info"].get("genre_ids") or [None])[0] in ids]
            similar = [c for tid in liked[:8] for c in self._recommendations(k, tid) if set(c["genre_ids"][:2]) & set(ids)]
            rows["para-ti"].append(similar)
        marks, idx = self.marks(), self.owned_index()
        out = {}
        for row, lists in rows.items():
            cards = self._decorate(_mix(*lists) if lists else [], idx, marks)
            if row == "para-ti":
                cards = [c for c in cards if c["rating"] is None]
                cards.sort(key=lambda c: -self.affinity(c, L))
            out[row] = cards
        return out

    def section(self, name: str, page: int = 1) -> list[dict]:
        if name == "para-ti":
            return self.for_you()
        if name == "por-conseguir":
            return self.wanted()
        if name == "calificadas":
            return self.rated()
        cards = self.news(page) if name == "novedades" else self.classics(page)
        return self._decorate(cards, self.owned_index(), self.marks())


# ánimos de la Portada → géneros de TheMovieDB (películas / series); el título cuenta si su género principal está aquí
MOODS = {
    "reir": {"movie": [35], "tv": [35]}, "accion": {"movie": [28], "tv": [10759]}, "miedo": {"movie": [27], "tv": []},
    "aventura": {"movie": [12], "tv": [10759]}, "futuro": {"movie": [878], "tv": [10765]}, "magia": {"movie": [14], "tv": [10765]},
    "intriga": {"movie": [9648], "tv": [9648]}, "crimen": {"movie": [80], "tv": [80]}, "nervios": {"movie": [53], "tv": [9648, 80]},
    "amor": {"movie": [10749], "tv": []}, "llorar": {"movie": [18, 10749], "tv": [18]},
    "pensar": {"movie": [878, 99, 36], "tv": [99, 10768]}, "dibujos": {"movie": [16], "tv": [16]},
    "familia": {"movie": [10751], "tv": [10751, 10762]}, "historia": {"movie": [36], "tv": [10768]},
    "guerra": {"movie": [10752], "tv": [10768]}, "oeste": {"movie": [37], "tv": [37]}, "musica": {"movie": [10402], "tv": []},
    "real": {"movie": [99], "tv": [99]},
}


def _mix(*lists: list[dict]) -> list[dict]:
    """Intercala listas (películas y series) sin repetir."""
    out, seen = [], set()
    for row in zip(*[l + [None] * (max(map(len, lists)) - len(l)) for l in lists]):
        for c in row:
            if c and (c["kind"], c["tmdb"]) not in seen:
                seen.add((c["kind"], c["tmdb"]))
                out.append(c)
    return out
