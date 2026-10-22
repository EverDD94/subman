"""Actualizarse sola: le pide a Watchtower (otro contenedor del mismo proyecto) que baje la imagen nueva.

Watchtower sólo vigila el contenedor «subman» y no hace nada por su cuenta: Videotecario lo llama
con «Actualizar ahora» o una vez al día a la hora elegida. Si no hay imagen nueva, no se reinicia nada.
"""
from __future__ import annotations

import logging
import os
import re
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


def update() -> bool:
    """Pide la actualización. Si hay imagen nueva, Watchtower para este contenedor (esta llamada
    se corta: devuelve False) y arranca el nuevo con la misma configuración."""
    log.info("actualización: preguntando a Watchtower si hay versión nueva")
    try:
        r = requests.get(f"{URL}/v1/update", headers={"Authorization": f"Bearer {TOKEN}"}, timeout=600)
        r.raise_for_status()
        log.info("actualización: ya está la última versión")
        return True
    except requests.ConnectionError:
        return False  # normal si nos reinicia a mitad
    except requests.RequestException as e:
        log.warning("actualización: Watchtower dio un error: %s", e)
        raise


# --- cuánto hay que bajar y cuánto tardará (para la barra de progreso) -------------------------
IMAGE = os.environ.get("SUBMAN_IMAGE", "everdd94/subman")
_ACCEPT = ", ".join(["application/vnd.oci.image.index.v1+json", "application/vnd.docker.distribution.manifest.list.v2+json",
                     "application/vnd.oci.image.manifest.v1+json", "application/vnd.docker.distribution.manifest.v2+json"])


def _registry(token: str, path: str) -> dict:
    r = requests.get(f"https://ghcr.io/v2/{IMAGE}/{path}", timeout=15,
                     headers={"Authorization": f"Bearer {token}", "Accept": _ACCEPT})
    r.raise_for_status()
    return r.json()


def _image(token: str, tag: str) -> dict:
    """El manifiesto de la imagen linux/amd64 de esa etiqueta (las capas y su configuración)."""
    m = _registry(token, f"manifests/{tag}")
    if "manifests" in m:  # índice multiplataforma: la de amd64
        d = next(x["digest"] for x in m["manifests"] if (x.get("platform") or {}).get("architecture") == "amd64")
        m = _registry(token, f"manifests/{d}")
    return m


def check() -> dict:
    """¿La imagen «latest» de ghcr.io es distinta de la instalada, y cuántos bytes nuevos hay que bajar?
    Sólo cuentan las capas que no tiene ya la versión instalada (Docker reutiliza las demás)."""
    token = requests.get("https://ghcr.io/token", params={"scope": f"repository:{IMAGE}:pull"}, timeout=15).json()["token"]
    latest = _image(token, "latest")
    mine = None
    if BUILD != "dev":
        try:
            mine = _image(token, f"sha-{BUILD[:7]}")
        except Exception:
            pass  # no está la etiqueta de la instalada: se cuenta todo
    if mine and mine["config"]["digest"] == latest["config"]["digest"]:
        return {"new": False, "bytes": 0}
    have = {x["digest"] for x in mine["layers"]} if mine else set()
    return {"new": True, "bytes": sum(x.get("size", 0) for x in latest["layers"] if x["digest"] not in have)}


SPEED = 4_000_000  # bytes/s al principio (se va aprendiendo)
OVERHEAD = 30      # segundos de parar el contenedor, arrancar el nuevo y que conteste


def estimate(nbytes: int, learned: dict) -> float:
    """Segundos que tardará todo: bajar ``nbytes`` + reiniciar, con lo aprendido de las veces anteriores."""
    return learned.get("overhead", OVERHEAD) + nbytes / max(learned.get("speed", SPEED), 100_000)


def learn(learned: dict, nbytes: int, seconds: float) -> dict:
    """Tras una actualización que tardó ``seconds`` en total, ajusta la velocidad (media suave)."""
    over = learned.get("overhead", OVERHEAD)
    if nbytes > 5_000_000 and seconds > over + 3:
        speed = nbytes / (seconds - over)
        old = learned.get("speed")
        learned["speed"] = round(speed if old is None else 0.5 * old + 0.5 * speed)
    elif nbytes <= 5_000_000 and seconds > 5:  # casi nada que bajar: lo que tardó es el reinicio
        old = learned.get("overhead")
        learned["overhead"] = round(seconds if old is None else 0.5 * old + 0.5 * seconds, 1)
    return learned


def apply_timezone(tz: str) -> None:
    """La hora de la casa (la del navegador) para los horarios de noche."""
    if tz and os.environ.get("TZ") != tz and os.path.exists(f"/usr/share/zoneinfo/{tz}"):
        os.environ["TZ"] = tz
        time.tzset()
        log.info("zona horaria: %s (son las %s)", tz, time.strftime("%H:%M"))


# --- qué trae la versión nueva (GitHub) ------------------------------------------------
REPO = os.environ.get("SUBMAN_REPO", "EverDD94/subman")
GH = "https://api.github.com/repos/" + REPO
_news: dict = {"at": 0.0, "data": None}
_MERGE = re.compile(r"^Merge pull request #(\d+)")


def _gh(path: str, **params):
    r = requests.get(GH + path, params=params, timeout=15, headers={"Accept": "application/vnd.github+json"})
    r.raise_for_status()
    return r.json()


def _changes(commits: list[dict], limit: int = 8, points: bool = True) -> list[dict]:
    """Los cambios en palabras (``commits`` de más viejo a más nuevo): cada PR fusionado con su título y
    sus puntos; los commits sueltos, su primera línea."""
    out = []
    for c in reversed(commits):  # lo más nuevo primero
        msg = (c.get("commit") or {}).get("message") or ""
        lines = [x.strip() for x in msg.splitlines() if x.strip()]
        m = _MERGE.match(lines[0]) if lines else None
        if m:
            out.append({"pr": int(m.group(1)), "title": lines[1] if len(lines) > 1 else lines[0], "points": [],
                        "date": (c.get("commit") or {}).get("committer", {}).get("date")})
        elif lines and not c.get("_in_pr") and len(c.get("parents") or []) < 2:
            # commit suelto en main (sin PR); los de dentro de un PR ya van en el título del PR
            out.append({"pr": None, "title": lines[0], "points": [], "date": (c.get("commit") or {}).get("committer", {}).get("date")})
        if len(out) >= limit:
            break
    for o in out[:5] if points else []:  # los puntos de la descripción de cada PR («- …»)
        if o["pr"]:
            try:
                body = _gh(f"/pulls/{o['pr']}").get("body") or ""
                o["points"] = [re.sub(r"[*_`]", "", x.strip()[2:]).strip() for x in body.splitlines()
                               if x.strip().startswith(("- ", "* "))][:10]
            except Exception:
                pass
    return out


def news(force: bool = False) -> dict:
    """¿Hay versión nueva lista para instalar y qué trae? Se guarda 10 minutos (GitHub deja 60 consultas por hora)."""
    if not force and _news["data"] and time.time() - _news["at"] < 600:
        return _news["data"]
    data = {"build": BUILD, "new": False, "building": False, "items": [], "recent": [], "error": None}
    try:
        runs = _gh("/actions/workflows/docker.yml/runs", branch="main", per_page=5).get("workflow_runs") or []
        ready = next((r for r in runs if r.get("status") == "completed" and r.get("conclusion") == "success"), None)
        data["building"] = any(r.get("status") != "completed" for r in runs[:1])
        latest = ready["head_sha"] if ready else None
        if BUILD != "dev" and latest and latest != BUILD:
            cmp = _gh(f"/compare/{BUILD}...{latest}")
            mark_pr_commits(cmp.get("commits") or [])
            data["items"] = _changes(cmp.get("commits") or [])
            data["new"] = cmp.get("ahead_by", 0) > 0
        recent = _gh("/commits", sha=BUILD if BUILD != "dev" else "main", per_page=30)
        mark_pr_commits(recent)
        data["recent"] = _changes(list(reversed(recent)), limit=3, points=False)
    except Exception as e:
        log.info("novedades: no se pudo preguntar a GitHub: %s", e)
        data["error"] = "no se pudo preguntar a GitHub"
    _news.update(at=time.time(), data=data)
    return data


def mark_pr_commits(commits: list[dict]) -> None:
    """Marca los commits que entraron con un PR (son padres de un «Merge pull request»), para no repetirlos."""
    by_sha = {c["sha"]: c for c in commits if c.get("sha")}
    for c in commits:
        if _MERGE.match((c.get("commit") or {}).get("message") or ""):
            stack = [p["sha"] for p in (c.get("parents") or [])[1:]]
            while stack:
                s = stack.pop()
                if s in by_sha and not by_sha[s].get("_in_pr") and not _MERGE.match(by_sha[s]["commit"]["message"]):
                    by_sha[s]["_in_pr"] = True
                    stack += [p["sha"] for p in by_sha[s].get("parents") or []]
