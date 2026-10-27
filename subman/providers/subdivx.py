"""Subdivx, a través de SubX (https://subx-api.duckdns.org): la mayor colección de subtítulos
en español latino. Necesita una API key de SubX (se pone en Ajustes)."""
from __future__ import annotations

import io
import logging
import time
import zipfile

import requests

from .. import __version__
from ..library import Media
from .base import Candidate, Provider

log = logging.getLogger(__name__)
API = "https://subx-api.duckdns.org/api"
_SUBS = (".srt", ".ssa", ".ass", ".sub")


class Subdivx(Provider):
    name = "subdivx"

    def __init__(self, api_key: str, timeout: float = 30):
        self.timeout = timeout
        self.http = requests.Session()
        self.http.headers.update({"Authorization": f"Bearer {api_key}", "User-Agent": f"subman v{__version__}",
                                  "Accept": "application/json"})

    def _get(self, path: str, **kw) -> requests.Response:
        for attempt in range(3):
            r = self.http.get(API + path, timeout=self.timeout, **kw)
            if r.status_code == 429:  # demasiadas peticiones: esperar lo que diga
                time.sleep(min(30, float(r.headers.get("Retry-After") or 2 + attempt)))
                continue
            r.raise_for_status()
            return r
        r.raise_for_status()
        return r

    def test(self) -> dict:
        n = self._get("/subtitles/search", params={"query": "Friends", "limit": 1}).json().get("total", 0)
        return {"found": n}

    def search(self, media: Media, languages: list[str]) -> list[Candidate]:
        if "es" not in languages:
            return []
        params: dict = {"title": media.title, "limit": 50}
        if media.kind == "episode":
            params.update(video_type="episode", season=media.season, episode=media.episode)
        else:
            params["video_type"] = "movie"
            if media.year:
                params["year"] = media.year
        items = self._get("/subtitles/search", params=params).json().get("items", [])
        if not items and media.kind != "episode" and media.year:  # el año a veces está mal en un lado u otro
            params.pop("year")
            items = self._get("/subtitles/search", params=params).json().get("items", [])
        out = [Candidate(provider=self.name, id=str(it["id"]), lang="es",
                         release=(it.get("description") or it.get("title") or "")[:120],
                         downloads=it.get("downloads") or 0)
               for it in items]
        return sorted(out, key=Candidate.rank, reverse=True)

    def download(self, candidate: Candidate) -> bytes:
        data = self._get(f"/subtitles/{candidate.id}/download", headers={"Accept": "*/*"}).content
        if data[:2] == b"PK":  # zip: el subtítulo más grande de dentro
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                names = [n for n in z.namelist() if n.lower().endswith(_SUBS)]
                if not names:
                    raise ValueError("el zip no trae subtítulos")
                return z.read(max(names, key=lambda n: z.getinfo(n).file_size))
        if data[:4] == b"Rar!":
            raise ValueError("viene en .rar (no se puede abrir)")
        return data
