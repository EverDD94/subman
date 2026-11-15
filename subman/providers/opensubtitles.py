"""OpenSubtitles.com (API REST v1). Necesita una API key de
https://www.opensubtitles.com/consumers y el usuario/contraseña de la cuenta (VIP sube el
límite de descargas diarias)."""
from __future__ import annotations

import difflib
import logging
import re
import time
import unicodedata

import requests

from .. import __version__
from ..library import Media
from .base import Candidate, Provider, QuotaExceeded

log = logging.getLogger(__name__)
API = "https://api.opensubtitles.com/api/v1"


class OpenSubtitles(Provider):
    name = "opensubtitles"

    def __init__(self, api_key: str, username: str = "", password: str = "", timeout: float = 30,
                 tmdb_key: str = ""):
        self.base = API
        self.tmdb_key = tmdb_key  # para buscar por ID de TheMovieDB cuando por nombre no sale nada
        self.timeout = timeout
        self.username, self.password = username, password
        self.token: str | None = None
        self.user: dict = {}
        self.http = requests.Session()
        self.http.headers.update({
            "Api-Key": api_key,
            "User-Agent": f"subman v{__version__}",
            "Accept": "application/json",
        })

    def _request(self, method: str, path: str, **kw) -> dict:
        for attempt in range(4):
            r = self.http.request(method, self.base + path, timeout=self.timeout, **kw)
            if r.status_code == 429:  # límite de peticiones por segundo
                time.sleep(1 + attempt)
                continue
            r.raise_for_status()
            return r.json()
        r.raise_for_status()
        return {}

    def login(self) -> None:
        if self.token or not self.username:
            return
        data = self._request("POST", "/login", json={"username": self.username, "password": self.password})
        self.token = data["token"]
        self.user = data.get("user", {})
        self.http.headers["Authorization"] = f"Bearer {self.token}"
        if data.get("base_url"):  # las cuentas VIP reciben su propio servidor
            self.base = f"https://{data['base_url']}/api/v1"
        log.info("OpenSubtitles: sesión iniciada (%s)", data.get("user", {}).get("level", ""))

    def test(self) -> dict:
        """Comprueba la API key y la cuenta. Devuelve nivel y descargas disponibles."""
        self.login()
        info = self._request("GET", "/infos/user").get("data", {}) if self.token else {}
        return {"level": info.get("level") or self.user.get("level", ""),
                "vip": bool(info.get("vip", self.user.get("vip", False))),
                "allowed_downloads": info.get("allowed_downloads", self.user.get("allowed_downloads")),
                "remaining_downloads": info.get("remaining_downloads")}

    def search(self, media: Media, languages: list[str]) -> list[Candidate]:
        """Por hash + nombre; si no sale nada, sin hash ni año; y si tampoco, por ID de TheMovieDB."""
        self.login()
        langs = {l.lower() for l in languages}
        if "es" in langs:
            langs.add("ea")  # OpenSubtitles guarda el español latino aparte
        base = {"languages": ",".join(sorted(langs))}
        if media.kind == "episode":
            base.update(season_number=media.season, episode_number=media.episode, type="episode")
        else:
            base["type"] = "movie"
        tries = []
        first = {**base, "query": media.title}
        if media.oshash:
            first["moviehash"] = media.oshash
        if media.kind != "episode" and media.year:
            first["year"] = media.year
        tries.append(first)
        tries.append({**base, "query": media.title})
        out: list[Candidate] = []
        tid_cache: list = []

        def tid():
            if not tid_cache:
                tid_cache.append(self._tmdb_id(media))
            return tid_cache[0]
        for params in tries:
            out = self._keep_ours(media, self._search(params), tid)
            if out:
                return out
        if tid():
            key = "parent_tmdb_id" if media.kind == "episode" else "tmdb_id"
            out = self._keep_ours(media, self._search({**base, key: tid()}), tid)
            if out:
                log.info("OpenSubtitles: %s encontrado por ID de TheMovieDB (%s)", media.label, tid())
        return out

    def _keep_ours(self, media: Media, found: list[Candidate], tid) -> list[Candidate]:
        """Quita los subtítulos que OpenSubtitles devuelve de OTRA película o serie (pasa con títulos
        poco conocidos: da lo más parecido que tiene). Así no se gastan descargas ni procesador en ellos."""
        keep, drop = [], []
        for c in found:
            (keep if c.hash_match or _same(media, c.extra.get("feature") or {}, tid) else drop).append(c)
        if drop:
            names = sorted({_feature_name(c.extra.get("feature") or {}) or c.release for c in drop})
            log.info("OpenSubtitles: %s: descartados %d de otras películas/series (%s)",
                     media.label, len(drop), ", ".join(names[:4]) + ("…" if len(names) > 4 else ""))
        return keep

    def _tmdb_id(self, media: Media) -> int | None:
        if not self.tmdb_key:
            return None
        from ..tmdb import TMDb
        try:
            t = TMDb(self.tmdb_key)
            ids = (t.search_tv(media.title) if media.kind == "episode" else t.search(media.title, media.year))
            return ids[0] if ids else None
        except Exception as e:
            log.warning("TheMovieDB: no se pudo buscar %s: %s", media.label, e)
            return None

    def _search(self, params: dict) -> list[Candidate]:
        data = self._request("GET", "/subtitles", params=dict(sorted(params.items())))
        out = []
        for item in data.get("data", []):
            a = item["attributes"]
            for f in a.get("files", [])[:1]:
                out.append(Candidate(
                    provider=self.name, id=str(f["file_id"]), lang=a.get("language", ""),
                    release=a.get("release", "") or f.get("file_name", ""),
                    hash_match=bool(a.get("moviehash_match")), downloads=a.get("download_count", 0),
                    trusted=bool(a.get("from_trusted")),
                    machine_translated=bool(a.get("machine_translated") or a.get("ai_translated")),
                    hearing_impaired=bool(a.get("hearing_impaired")), fps=a.get("fps"),
                    extra={"feature": a.get("feature_details") or {}},
                ))
        return sorted(out, key=Candidate.rank, reverse=True)

    def download(self, candidate: Candidate) -> bytes:
        self.login()
        try:
            data = self._request("POST", "/download", json={"file_id": int(candidate.id), "sub_format": "srt"})
        except requests.HTTPError as e:
            if e.response is not None and e.response.status_code == 406:  # cupo diario agotado
                raise QuotaExceeded(e.response.text[:200]) from e
            raise
        log.info("OpenSubtitles: quedan %s descargas hoy", data.get("remaining"))
        if not data.get("link"):
            raise QuotaExceeded(data.get("message", "sin enlace de descarga"))
        r = requests.get(data["link"], timeout=self.timeout)
        r.raise_for_status()
        return r.content


def _norm(t: str) -> str:
    t = unicodedata.normalize("NFKD", t or "").encode("ascii", "ignore").decode().lower()
    t = re.sub(r"\(\d{4}\)|[^a-z0-9 ]", " ", t)
    t = re.sub(r"^(the|el|la|los|las|le|les|a|an) ", "", t.strip())
    return " ".join(t.split())


def _feature_name(fd: dict) -> str:
    return fd.get("parent_title") or fd.get("title") or fd.get("movie_name") or ""


def _same(media: Media, fd: dict, tid) -> bool:
    """¿Es de la misma película/serie? Sin datos para decidirlo, se da por buena (como antes)."""
    if not fd:
        return True
    ep = media.kind == "episode"
    their_id = fd.get("parent_tmdb_id") if ep else fd.get("tmdb_id")
    names = [fd.get(k) for k in (("parent_title", "movie_name") if ep else ("title", "movie_name", "original_title")) if fd.get(k)]
    mine = _norm(media.title)
    for n in names:
        n = _norm(n)
        if n and mine and (n == mine or mine in n or n in mine or difflib.SequenceMatcher(None, n, mine).ratio() >= 0.75):
            return True
    if their_id:
        t = tid()
        if t:
            return int(their_id) == int(t)
    return True  # nombre distinto pero sin ID para comprobarlo: puede ser el título en otro idioma
