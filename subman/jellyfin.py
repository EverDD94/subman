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
