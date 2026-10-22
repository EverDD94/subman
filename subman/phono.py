"""Phono (phono.cc): buscar una película o serie y ver qué archivos tiene, sin cuenta.

Lo mismo que hace la web al buscar y al pulsar «Download»:

* ``/svc/api/search?q=…`` busca; cada resultado trae su número de Phono (``content_id``);
* ``/svc/api/content/{id}`` es la ficha, con el número de TheMovieDB (así no hay dudas de cuál es);
* ``/svc/api/content/{id}/files`` son los archivos de una película: nombre, calidad, tamaño, idiomas
  del audio, si trae subtítulos en inglés y el enlace al bot (``t.me/phonofilmbot?start=<código>``);
* ``/svc/api/content/{id}/buttons`` son, en las series, los botones por temporada («SEASON 1»…), que
  llevan a otros bots (``telegram.dog/IrisFileStore510Bot?start=…``). No hay capítulos sueltos gratis.
"""
from __future__ import annotations

import logging
import re
from urllib.parse import parse_qs, urlparse

import requests

log = logging.getLogger(__name__)
API = "https://phono.cc/svc/api"
UA = "Mozilla/5.0 (compatible; Videotecario)"
# Calidad preferida: 1080p, luego 720p; 4K pesa mucho y lo de menos de 720p se ve mal.
QUALITY = {"1080p": 0, "720p": 1, "2160p": 2, "4k": 2, "480p": 3, "360p": 4}
LANG = {"es": ("spanish", "español", "espanol", "castellano", "latino", "spa"), "en": ("english", "eng")}
_SEASON = re.compile(r"\b(?:season|temporada|s)\s*0*(\d{1,2})\b", re.I)


class Phono:
    def __init__(self, timeout: float = 30):
        self.http = requests.Session()
        self.http.headers.update({"User-Agent": UA, "Accept": "application/json"})
        self.timeout = timeout

    def _get(self, path: str, **params):
        r = self.http.get(API + path, params=params, timeout=self.timeout)
        r.raise_for_status()
        return r.json()

    def search(self, query: str) -> list[dict]:
        return self._get("/search", q=query, limit=20, content_type="all", offset=0).get("results", [])

    def content(self, content_id) -> dict:
        return self._get(f"/content/{content_id}")

    def files(self, content_id) -> list[dict]:
        """Los archivos sin repetir (uno con varios audios sale bajo cada idioma)."""
        seen, out = set(), []
        for by_quality in (self._get(f"/content/{content_id}/files") or {}).values():
            for files in (by_quality or {}).values():
                for f in files or []:
                    if f.get("id") not in seen and f.get("download_url"):
                        seen.add(f.get("id"))
                        out.append(f)
        return out

    def buttons(self, content_id) -> list[dict]:
        rows = (self._get(f"/content/{content_id}/buttons") or {}).get("buttons") or []
        return [b for row in rows for b in row if b.get("url")]

    def find(self, kind: str, tmdb: int, titles: list[str], year: int | None) -> dict | None:
        """La ficha de Phono con ese número de TheMovieDB (se busca por cada título)."""
        want = "SERIES" if kind == "tv" else "MOVIE"
        tried = set()
        for title in titles:
            if not title:
                continue
            for r in self.search(title):
                cid = r.get("content_id")
                if r.get("content_type") != want or cid in tried:
                    continue
                y = (r.get("release_date") or "")[:4]
                if year and y.isdigit() and abs(int(y) - year) > 1:
                    continue
                tried.add(cid)
                d = self.content(cid)
                if str(d.get("tmdb_id") or "") == str(tmdb):
                    return {**d, "content_id": cid}
        return None


def lang_of(name: str) -> str | None:
    n = name.lower()
    return next((code for code, names in LANG.items() if any(x in n for x in names)), None)


def pick(files: list[dict], audio: list[str] = ("es", "en")) -> dict | None:
    """El mejor archivo: audio en el primer idioma que haya, luego 1080p, que se vea en la tele,
    con subtítulos y el más ligero."""
    def key(f):
        langs = [lang_of(x) for x in f.get("languages") or []]
        rank = min((audio.index(l) for l in langs if l in audio), default=len(audio))
        q = QUALITY.get((f.get("resolution_label") or "").lower(), 5)
        return (rank, q, bool(f.get("is_hdr")), not f.get("for_tv"), not f.get("eng_subtitle"),
                f.get("file_size_bytes") or 0)
    ok = [f for f in files if f.get("download_url")]
    if not ok:
        return None
    best = min(ok, key=key)
    if key(best)[0] == len(audio):  # ni español ni inglés
        return None
    return best


def bot_link(url: str) -> tuple[str, str] | None:
    """``t.me/phonofilmbot?start=ABC`` (o telegram.dog / telegram.me) -> (bot, código)."""
    u = urlparse(url)
    if u.netloc.lower() not in ("t.me", "telegram.me", "telegram.dog", "www.t.me"):
        return None
    bot = u.path.strip("/").split("/")[0]
    code = (parse_qs(u.query).get("start") or [""])[0]
    return (bot, code) if bot and code else None


def seasons(buttons: list[dict]) -> dict[int, dict]:
    """Botones de temporada -> {número: {bot, code, text}}. Los extras y canales no cuentan."""
    out = {}
    for b in buttons:
        text = b.get("text") or ""
        m = _SEASON.search(text)
        link = bot_link(b["url"])
        if not (m and link):
            continue
        n = int(m.group(1))
        q = re.search(r"(\d{3,4})p", text)  # «SEASON 2 (720p x265)» / «SEASON 2 (1080p x265)»: la mejor
        rank = QUALITY.get(q.group(0).lower(), 5) if q else -1  # sin calidad: el bot la pregunta después
        if n not in out or rank < out[n]["rank"]:
            out[n] = {"bot": link[0], "code": link[1], "text": text, "rank": rank}
    return out
