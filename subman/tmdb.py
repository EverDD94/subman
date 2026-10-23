"""TheMovieDB: buscar películas y saber su duración y a qué saga pertenecen (y en qué orden);
buscar series y los títulos de sus capítulos.

La clave es gratis: themoviedb.org > Ajustes > API. Vale la «clave de la API» (v3) o el
«token de acceso de lectura» (v4, empieza por «eyJ»).
"""
from __future__ import annotations

import logging
import re
import time
import unicodedata

import requests

log = logging.getLogger(__name__)
API = "https://api.themoviedb.org/3"
IMG = "https://image.tmdb.org/t/p/w185"


class TMDb:
    def __init__(self, key: str, language: str = "es-ES", timeout: float = 20):
        self.http = requests.Session()
        self.params = {"language": language}
        if key.startswith("eyJ"):
            self.http.headers["Authorization"] = f"Bearer {key}"
        else:
            self.params["api_key"] = key
        self.timeout = timeout
        self._movies: dict[int, dict] = {}
        self._collections: dict[int, list[int]] = {}
        self._shows: dict[int, dict] = {}
        self._seasons: dict[tuple[int, int], dict[int, str]] = {}
        self._runtimes: dict[tuple[int, int], list[int]] = {}

    def _get(self, path: str, **params) -> dict:
        for attempt in range(4):
            r = self.http.get(API + path, params={**self.params, **params}, timeout=self.timeout)
            if r.status_code == 429:
                time.sleep(1 + attempt)
                continue
            r.raise_for_status()
            return r.json()
        r.raise_for_status()
        return {}

    def test(self) -> dict:
        d = self.movie(603)  # Matrix
        return {"title": d["name"]}

    def search(self, query: str, year: int | None = None) -> list[int]:
        params = {"query": query, "include_adult": "false"}
        if year:
            params["year"] = year
        return [r["id"] for r in self._get("/search/movie", **params).get("results", [])[:8]]

    def movie(self, movie_id: int) -> dict:
        if movie_id not in self._movies:
            d = self._get(f"/movie/{movie_id}")
            col = d.get("belongs_to_collection") or None
            self._movies[movie_id] = {
                "tmdb": str(d["id"]), "imdb": d.get("imdb_id") or None, "name": d.get("title", ""),
                "original": d.get("original_title", ""), "year": int(d["release_date"][:4]) if d.get("release_date") else None,
                "runtime": d.get("runtime") or None, "image": IMG + d["poster_path"] if d.get("poster_path") else "",
                "collection": {"id": col["id"], "name": col.get("name", "")} if col else None,
                "overview": (d.get("overview") or "")[:300],
                "genre_ids": [g["id"] for g in d.get("genres") or []], "lang": d.get("original_language") or ""}
        return self._movies[movie_id]

    def collection(self, collection_id: int) -> list[int]:
        """Las películas de la saga en orden de estreno."""
        if collection_id not in self._collections:
            parts = self._get(f"/collection/{collection_id}").get("parts", [])
            parts = [p for p in parts if p.get("release_date")]
            self._collections[collection_id] = [p["id"] for p in sorted(parts, key=lambda p: p["release_date"])]
        return self._collections[collection_id]

    def search_tv(self, query: str, year: int | None = None) -> list[int]:
        params = {"query": query, "include_adult": "false"}
        if year:
            params["first_air_date_year"] = year
        return [r["id"] for r in self._get("/search/tv", **params).get("results", [])[:8]]

    def tv(self, tv_id: int) -> dict:
        """La serie y cuántos capítulos tiene cada temporada."""
        if tv_id not in self._shows:
            d = self._get(f"/tv/{tv_id}")
            local, original = d.get("name", ""), d.get("original_name", "")
            self._shows[tv_id] = {  # el título original («2 Broke Girls»), salvo que esté en otro alfabeto
                "tmdb": str(d["id"]), "name": original if _latin(original) else local or original,
                "original": local,  # el otro nombre, para comparar con el de la carpeta
                "year": int(d["first_air_date"][:4]) if d.get("first_air_date") else None,
                "image": IMG + d["poster_path"] if d.get("poster_path") else "",
                "seasons": {s["season_number"]: s.get("episode_count") or 0 for s in d.get("seasons", [])},
                "genre_ids": [g["id"] for g in d.get("genres") or []], "lang": d.get("original_language") or ""}
        return self._shows[tv_id]

    def season(self, tv_id: int, number: int) -> dict[int, str]:
        """{capítulo: título}. Si en el idioma elegido sólo pone «Episodio 5», se usa el título en inglés."""
        key = (tv_id, number)
        if key not in self._seasons:
            try:
                eps = self._get(f"/tv/{tv_id}/season/{number}").get("episodes", [])
            except requests.HTTPError as e:
                if e.response is not None and e.response.status_code == 404:
                    eps = []
                else:
                    raise
            self._runtimes[key] = [e["runtime"] for e in eps if e.get("runtime")]
            names = {e["episode_number"]: (e.get("name") or "").strip() for e in eps}
            if any(_placeholder(n) for n in names.values()):
                try:
                    en = {e["episode_number"]: (e.get("name") or "").strip()
                          for e in self._get(f"/tv/{tv_id}/season/{number}", language="en-US").get("episodes", [])}
                    names = {k: en.get(k) or v if _placeholder(v) else v for k, v in names.items()}
                except requests.RequestException as e:
                    log.warning("TheMovieDB: títulos en inglés de la temporada %s: %s", number, e)
            self._seasons[key] = {k: "" if _placeholder(v) else v for k, v in names.items()}
        return self._seasons[key]

    def runtime(self, tv_id: int, number: int) -> float | None:
        """Lo que suele durar un capítulo de esa temporada (minutos), o None si TheMovieDB no lo sabe."""
        self.season(tv_id, number)
        rt = sorted(self._runtimes.get((tv_id, number)) or [])
        return float(rt[len(rt) // 2]) if rt else None


def _latin(text: str) -> bool:
    """¿Se escribe con nuestras letras? (no japonés, coreano, cirílico...)."""
    return bool(text) and all(unicodedata.name(c, "").startswith("LATIN") for c in text if c.isalpha())


def _placeholder(name: str) -> bool:
    return not name or bool(re.fullmatch(r"(episodio|episode|cap[ií]tulo|folge|épisode)\s*\d+", name, re.I))
