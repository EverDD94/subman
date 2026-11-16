"""Buscar canales y grupos de Telegram para Ajustes › Proveedores.

La búsqueda de Telegram sólo da 2 o 3 resultados por consulta, así que se juntan tres fuentes:

1. **Telegram con variantes**: «one piece», «onepiece», «one piece latino», «one piece español»…
2. **Aliados**: los canales que mencionan o enlazan los canales que ya tienes.
3. **Web** (opcional, con clave gratis de Brave Search): ``site:t.me one piece``.

Todo se comprueba con tu cuenta de Telegram (nombre y suscriptores) antes de mostrarse, y se guarda unas
horas para no repetir consultas ni pasarse de los límites de Telegram.
"""
from __future__ import annotations

import logging
import re
import threading
import time
import unicodedata

import requests

from .telegram import _mentioned

log = logging.getLogger(__name__)

CACHE_S = 6 * 3600        # búsquedas y comprobaciones
ALLIES_S = 24 * 3600      # aliados de tus canales
MAX_RESOLVE = 30          # comprobaciones de @nombre nuevas por búsqueda (Telegram limita esto)
SUFFIXES = ["latino", "español", "castellano", "hd", "audio latino", "sub español", "temporada", "capitulos"]
BRAVE = "https://api.search.brave.com/res/v1/web/search"

_lock = threading.Lock()
_cache: dict[tuple, tuple[float, object]] = {}


def _get(key, ttl):
    with _lock:
        hit = _cache.get(key)
        return hit[1] if hit and time.time() - hit[0] < ttl else None


def _put(key, value):
    with _lock:
        _cache[key] = (time.time(), value)
    return value


def _plain(s: str) -> str:
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9]+", " ", s).strip()


def variants(q: str) -> list[str]:
    """Las búsquedas que se le hacen a Telegram para un texto."""
    q = " ".join(q.split())
    out = [q, q.replace(" ", ""), q.replace(" ", "_")] + [f"{q} {s}" for s in SUFFIXES]
    seen, res = set(), []
    for v in out:
        if len(v) >= 3 and v.lower() not in seen:
            seen.add(v.lower())
            res.append(v)
    return res


def matches(title: str, username: str, q: str) -> bool:
    """¿El canal tiene que ver con lo buscado? (todas las palabras, en el nombre o en el @nombre)."""
    hay = _plain(title) + " " + _plain(username) + " " + _plain(username).replace(" ", "")
    words = _plain(q).split()
    return all(w in hay or w in hay.replace(" ", "") for w in words)


def brave(key: str, q: str) -> list[str]:
    """@nombres de t.me que salen en la web para esa búsqueda (dos páginas de 20)."""
    names: dict[str, None] = {}
    for offset in (0, 1):
        try:
            r = requests.get(BRAVE, params={"q": f"site:t.me {q}", "count": 20, "offset": offset},
                             headers={"X-Subscription-Token": key, "Accept": "application/json"}, timeout=20)
        except requests.RequestException as e:
            raise ValueError(f"no se pudo buscar en la web ({e})")
        if r.status_code in (401, 403, 422):
            raise ValueError("la clave de Brave Search no vale")
        if r.status_code == 429:
            log.info("Brave: límite alcanzado")
            break
        r.raise_for_status()
        res = (r.json().get("web") or {}).get("results") or []
        for it in res:
            for n in _mentioned(it.get("url", "")):
                names.setdefault(n, None)
        if len(res) < 20:
            break
    return list(names)


class Finder:
    def __init__(self, tg, brave_key: str = ""):
        self.tg, self.brave_key = tg, brave_key

    def _resolve(self, names: list[str]) -> dict[str, dict | None]:
        """@nombre → info (de la caché o preguntando a Telegram, como mucho MAX_RESOLVE nuevos)."""
        out, todo = {}, []
        for n in names:
            hit = _get(("u", n.lower()), CACHE_S * 4)
            if hit is not None:
                out[n] = hit or None
            else:
                todo.append(n)
        todo = todo[:MAX_RESOLVE]
        for n, info in zip(todo, self.tg.resolve(todo) if todo else []):
            out[n] = _put(("u", n.lower()), info or {}) or None
        return out

    def allies(self, links: list[str]) -> list[dict]:
        """Canales que mencionan tus canales (comprobados), guardados un día."""
        key = ("allies", tuple(sorted(links)))
        hit = _get(key, ALLIES_S)
        if hit is None:
            names = self.tg.mentions(links)
            hit = _put(key, [c for c in self._resolve(names).values() if c])
        return hit

    def search(self, q: str, mine: list[str]) -> dict:
        q = " ".join((q or "").split())
        if len(q) < 3:
            raise ValueError("escribe al menos 3 letras")
        key = ("q", q.lower(), bool(self.brave_key), tuple(sorted(mine)))
        hit = _get(key, CACHE_S)
        if hit is not None:
            return hit
        found: dict[str, dict] = {}
        notes: list[str] = []

        def add(c: dict, via: str):
            k = c["username"].lower()
            if k in found:
                if via not in found[k]["via"]:
                    found[k]["via"].append(via)
                if c.get("count") and not found[k].get("count"):
                    found[k]["count"] = c["count"]
            else:
                found[k] = {**c, "via": [via]}

        for c in self.tg.search_public(variants(q)):
            add(c, "telegram")
        if self.brave_key:
            try:
                for c in self._resolve(brave(self.brave_key, q)).values():
                    if c:
                        add(c, "web")
            except ValueError as e:
                notes.append(str(e))
        public = [l for l in mine if not l.startswith(("t.me/+", "t.me/joinchat/"))]
        if public:
            for c in self.allies(public):
                if matches(c["title"], c["username"], q):
                    add(c, "aliados")
        have = {l.lower() for l in mine}
        rows = [r for r in found.values() if r["link"].lower() not in have]
        rows.sort(key=lambda r: (not matches(r["title"], r["username"], q), -(r.get("count") or 0)))
        return _put(key, {"results": rows, "notes": notes, "web": bool(self.brave_key)})
