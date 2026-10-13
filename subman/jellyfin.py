"""Aviso a Jellyfin para que vuelva a mirar la biblioteca y encuentre los subtítulos nuevos."""
from __future__ import annotations

import logging

import requests

log = logging.getLogger(__name__)


def _headers(api_key: str) -> dict:
    return {"Authorization": f'MediaBrowser Token="{api_key}"', "X-Emby-Token": api_key}


def test(url: str, api_key: str) -> dict:
    r = requests.get(url.rstrip("/") + "/System/Info", headers=_headers(api_key), timeout=10)
    r.raise_for_status()
    info = r.json()
    return {"name": info.get("ServerName", ""), "version": info.get("Version", "")}


def refresh(url: str, api_key: str) -> None:
    r = requests.post(url.rstrip("/") + "/Library/Refresh", headers=_headers(api_key), timeout=30)
    r.raise_for_status()
    log.info("Jellyfin: pedido que vuelva a escanear la biblioteca")


def movies(url: str, api_key: str) -> list[dict]:
    """Todas las películas que conoce Jellyfin, con sus identificadores (TMDb/IMDb) y archivos."""
    base = url.rstrip("/")
    params = {"Recursive": "true", "IncludeItemTypes": "Movie", "Fields": "Path,ProviderIds,MediaSources,OriginalTitle",
              "EnableImages": "false", "EnableUserData": "false"}
    try:
        r = requests.get(base + "/Items", headers=_headers(api_key), params=params, timeout=60)
        r.raise_for_status()
        items = r.json().get("Items", [])
    except requests.RequestException:
        items = []
    if not items:  # versiones antiguas: hace falta pedirlo en nombre de un usuario
        users = requests.get(base + "/Users", headers=_headers(api_key), timeout=30)
        users.raise_for_status()
        admins = [u for u in users.json() if u.get("Policy", {}).get("IsAdministrator")] or users.json()
        if not admins:
            return []
        r = requests.get(f"{base}/Users/{admins[0]['Id']}/Items", headers=_headers(api_key), params=params, timeout=60)
        r.raise_for_status()
        items = r.json().get("Items", [])
    out = []
    for it in items:
        paths = [s["Path"] for s in it.get("MediaSources") or [] if s.get("Path")]
        if not paths and it.get("Path"):
            paths = [it["Path"]]
        ids = {k.lower(): v for k, v in (it.get("ProviderIds") or {}).items() if v}
        out.append({"id": it.get("Id"), "name": it.get("Name", ""), "original": it.get("OriginalTitle", ""),
                    "year": it.get("ProductionYear"),
                    "tmdb": ids.get("tmdb"), "imdb": ids.get("imdb"), "paths": paths})
    return out


def search_movie(url: str, api_key: str, name: str, year: int | None = None, item_id: str | None = None,
                 language: str = "es") -> list[dict]:
    """Busca una película con los proveedores de Jellyfin (TheMovieDB...), como «Identificar»."""
    info = {"Name": name, "MetadataLanguage": language}
    if year:
        info["Year"] = year
    body = {"SearchInfo": info, "IncludeDisabledProviders": True}
    if item_id:
        body["ItemId"] = item_id
    r = requests.post(url.rstrip("/") + "/Items/RemoteSearch/Movie", headers=_headers(api_key), json=body, timeout=60)
    r.raise_for_status()
    out = []
    for it in r.json():
        ids = {k.lower(): v for k, v in (it.get("ProviderIds") or {}).items() if v}
        if not (ids.get("tmdb") or ids.get("imdb")):
            continue
        out.append({"name": it.get("Name", ""), "year": it.get("ProductionYear"), "tmdb": ids.get("tmdb"),
                    "imdb": ids.get("imdb"), "image": it.get("ImageUrl") or "",
                    "overview": (it.get("Overview") or "")[:300]})
    return out
