"""Actualizarse sola: le pide a Watchtower (otro contenedor del mismo proyecto) que baje la imagen nueva.

Watchtower sólo vigila el contenedor «subman» y no hace nada por su cuenta: Videotecario lo llama
con «Actualizar ahora» o una vez al día a la hora elegida. Si no hay imagen nueva, no se reinicia nada.
"""
from __future__ import annotations

import logging
import os
import socket
import time
from urllib.parse import urlparse

import requests

log = logging.getLogger(__name__)
URL = os.environ.get("WATCHTOWER_URL", "http://subman-actualizar:8080")
TOKEN = os.environ.get("WATCHTOWER_TOKEN", "videotecario")
BUILD = os.environ.get("SUBMAN_BUILD", "dev")
BUILT = os.environ.get("SUBMAN_BUILT", "")


def available() -> bool:
    """¿Está Watchtower en el proyecto? (su nombre se resuelve dentro de la red del compose)."""
    try:
        socket.getaddrinfo(urlparse(URL).hostname, None)
        return True
    except OSError:
        return False


def update() -> None:
    """Pide la actualización. Si hay imagen nueva, Watchtower para este contenedor (esta llamada
    se corta) y arranca el nuevo con la misma configuración."""
    log.info("actualización: preguntando a Watchtower si hay versión nueva")
    try:
        r = requests.get(f"{URL}/v1/update", headers={"Authorization": f"Bearer {TOKEN}"}, timeout=600)
        r.raise_for_status()
        log.info("actualización: ya está la última versión")
    except requests.ConnectionError:
        pass  # normal si nos reinicia a mitad
    except requests.RequestException as e:
        log.warning("actualización: Watchtower dio un error: %s", e)
        raise


def apply_timezone(tz: str) -> None:
    """La hora de la casa (la del navegador) para los horarios de noche."""
    if tz and os.environ.get("TZ") != tz and os.path.exists(f"/usr/share/zoneinfo/{tz}"):
        os.environ["TZ"] = tz
        time.tzset()
        log.info("zona horaria: %s (son las %s)", tz, time.strftime("%H:%M"))
