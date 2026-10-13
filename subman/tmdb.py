"""TheMovieDB: buscar películas y saber su duración y a qué saga pertenecen (y en qué orden).

La clave es gratis: themoviedb.org > Ajustes > API. Vale la «clave de la API» (v3) o el
«token de acceso de lectura» (v4, empieza por «eyJ»).
"""
from __future__ import annotations

import logging
import time

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
                "overview": (d.get("overview") or "")[:300]}
        return self._movies[movie_id]

    def collection(self, collection_id: int) -> list[int]:
        """Las películas de la saga en orden de estreno."""
        if collection_id not in self._collections:
            parts = self._get(f"/collection/{collection_id}").get("parts", [])
            parts = [p for p in parts if p.get("release_date")]
            self._collections[collection_id] = [p["id"] for p in sorted(parts, key=lambda p: p["release_date"])]
        return self._collections[collection_id]
