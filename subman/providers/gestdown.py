"""Gestdown (https://api.gestdown.info): espejo gratuito de Addic7ed, muy bueno para series.
No necesita cuenta. Sólo sirve para capítulos de series."""
from __future__ import annotations

import logging

import requests

from .. import __version__
from ..library import Media
from .base import Candidate, Provider

log = logging.getLogger(__name__)
API = "https://api.gestdown.info"
# nombres de idioma tal como los usa Addic7ed
_LANGS = {"es": ["Spanish (Spain)", "Spanish (Latin America)", "Spanish"], "en": ["English"]}


class Gestdown(Provider):
    name = "gestdown"
    keyless = True

    def __init__(self, timeout: float = 30):
        self.timeout = timeout
        self.http = requests.Session()
        self.http.headers.update({"User-Agent": f"subman v{__version__}", "Accept": "application/json"})
        self._shows: dict[str, str | None] = {}

    def _show(self, title: str) -> str | None:
        if title not in self._shows:
            r = self.http.get(f"{API}/shows/search/{requests.utils.quote(title)}", timeout=self.timeout)
            shows = r.json().get("shows", []) if r.ok else []
            self._shows[title] = shows[0]["id"] if shows else None
        return self._shows[title]

    def search(self, media: Media, languages: list[str]) -> list[Candidate]:
        if media.kind != "episode" or media.season is None or media.episode is None:
            return []
        show = self._show(media.title)
        if not show:
            return []
        out = []
        for lang in languages:
            for name in _LANGS.get(lang, []):
                r = self.http.get(f"{API}/subtitles/get/{show}/{media.season}/{media.episode}/"
                                  f"{requests.utils.quote(name)}", timeout=self.timeout)
                if r.status_code in (400, 404, 423):  # idioma desconocido, no hay, o lo están actualizando
                    continue
                r.raise_for_status()
                for s in r.json().get("matchingSubtitles", []):
                    if not s.get("completed", True) or not s.get("downloadUri"):
                        continue
                    out.append(Candidate(
                        provider=self.name, id=s["downloadUri"], lang=lang,
                        release=s.get("version", "") or name, downloads=s.get("downloadCount", 0),
                        hearing_impaired=bool(s.get("hearingImpaired")),
                    ))
        return sorted(out, key=Candidate.rank, reverse=True)

    def download(self, candidate: Candidate) -> bytes:
        uri = candidate.id if candidate.id.startswith("http") else API + candidate.id
        r = self.http.get(uri, timeout=self.timeout)
        r.raise_for_status()
        return r.content
