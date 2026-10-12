"""OpenSubtitles.com (API REST v1). Necesita una API key de
https://www.opensubtitles.com/consumers y el usuario/contraseña de la cuenta (VIP sube el
límite de descargas diarias)."""
from __future__ import annotations

import logging
import time

import requests

from .. import __version__
from ..library import Media
from .base import Candidate, Provider, QuotaExceeded

log = logging.getLogger(__name__)
API = "https://api.opensubtitles.com/api/v1"


class OpenSubtitles(Provider):
    name = "opensubtitles"

    def __init__(self, api_key: str, username: str = "", password: str = "", timeout: float = 30):
        self.base = API
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
        self.login()
        params = {"languages": ",".join(sorted(l.lower() for l in languages))}
        if media.oshash:
            params["moviehash"] = media.oshash
        if media.kind == "episode":
            params.update(query=media.title, season_number=media.season, episode_number=media.episode,
                          type="episode")
        else:
            params.update(query=media.title, type="movie")
            if media.year:
                params["year"] = media.year
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
