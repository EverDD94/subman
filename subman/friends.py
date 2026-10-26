"""Peticiones de los amigos: una página aparte (``/pedir``) donde tu familia y amigos piden películas y series.

Entran con su usuario y contraseña de Jellyfin (no hay cuentas nuevas). Sólo ven lo que hay en TheMovieDB, si
ya está en la biblioteca y cómo van sus peticiones. Tú las ves en Descubrir › Peticiones y decides: aceptar
(va a «Por conseguir» y a Descargas) o rechazar. Cuando llega a la biblioteca se marca sola como «Ya está».

Lo que llega desde internet (por el proxy inverso del NAS) sólo puede abrir esta página; el resto de
Videotecario pide tu usuario y contraseña de Ajustes, y sin ellos no se abre desde fuera.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import ipaddress
import json
import logging
import re
import sqlite3
import time
from pathlib import Path

import requests

from . import discover
from .library import Library

log = logging.getLogger(__name__)
COOKIE = "videotecario_amigo"
DAYS = 180
STATES = ("pendiente", "aceptada", "rechazada")
CLIENT = 'MediaBrowser Client="Videotecario", Device="Pedir", DeviceId="videotecario-pedir", Version="1"'

SCHEMA = """
CREATE TABLE IF NOT EXISTS friend_requests (
    kind TEXT NOT NULL,            -- movie | tv
    tmdb INTEGER NOT NULL,
    user_id TEXT NOT NULL,         -- el usuario de Jellyfin que lo pidió
    user_name TEXT NOT NULL,
    info TEXT,                     -- JSON con título, año, póster... (para enseñarla sin preguntar otra vez)
    state TEXT NOT NULL,           -- pendiente | aceptada | rechazada
    created REAL,
    decided REAL,
    PRIMARY KEY (kind, tmdb, user_id)
);
"""


# --- de dónde viene la visita ------------------------------------------------------------
def from_internet(headers) -> bool:
    """¿Llega por el proxy inverso (el dominio) en vez de directa por la red de casa o Tailscale?

    El proxy del DSM siempre añade X-Forwarded-For / X-Real-IP. Quien los añada a mano sólo consigue
    que le traten como de fuera (con menos permisos), así que no sirve para colarse."""
    if headers.get("X-Forwarded-For") or headers.get("X-Real-IP"):
        return True
    host = (headers.get("Host") or "").rsplit(":", 1)[0].strip("[]").lower()
    if not host or host == "localhost" or "." not in host or host.endswith(".local"):
        return False
    try:
        ipaddress.ip_address(host)
        return False
    except ValueError:
        return True  # un nombre de dominio: viene de fuera


def client_ip(headers, fallback: str) -> str:
    """La dirección de verdad de quien entra (detrás del proxy todas parecen la del NAS)."""
    return (headers.get("X-Real-IP") or (headers.get("X-Forwarded-For") or "").split(",")[0]).strip() or fallback


# --- entrar con el usuario de Jellyfin ---------------------------------------------------
def jellyfin_login(cfg: dict, user: str, password: str) -> dict | None:
    """``{"id", "name", "admin"}`` si Jellyfin acepta el usuario y la contraseña; None si no."""
    jf = cfg.get("jellyfin", {})
    if not jf.get("url"):
        raise ValueError("Videotecario todavía no está conectado a Jellyfin")
    base = jf["url"].rstrip("/")
    r = requests.post(base + "/Users/AuthenticateByName", json={"Username": user, "Pw": password},
                      headers={"X-Emby-Authorization": CLIENT}, timeout=15)
    if r.status_code in (400, 401, 403):
        return None
    r.raise_for_status()
    data = r.json()
    try:  # no hace falta que quede una sesión abierta en Jellyfin: la nuestra es la cookie
        requests.post(base + "/Sessions/Logout", headers={"X-Emby-Authorization": f'{CLIENT}, Token="{data["AccessToken"]}"'},
                      timeout=10)
    except Exception:
        pass
    return {"id": data["User"]["Id"], "name": data["User"]["Name"],
            "admin": bool((data["User"].get("Policy") or {}).get("IsAdministrator"))}


def is_admin(cfg: dict, user_id: str) -> bool:
    """¿Ese usuario es administrador de Jellyfin? (para quien entró antes como amigo y es el dueño)"""
    jf = cfg.get("jellyfin", {})
    try:
        r = requests.get(jf["url"].rstrip("/") + f"/Users/{user_id}", timeout=10,
                         headers={"X-Emby-Token": jf.get("api_key", ""), "X-Emby-Authorization": f'{CLIENT}, Token="{jf.get("api_key", "")}"'})
        return r.ok and bool((r.json().get("Policy") or {}).get("IsAdministrator"))
    except Exception:
        return False


def _key(cfg: dict) -> bytes:
    # depende de la clave de Jellyfin: si la cambias, todos tienen que volver a entrar
    return hashlib.sha256(b"videotecario-amigos|" + (cfg.get("jellyfin", {}).get("api_key") or "").encode()).digest()


def make_token(cfg: dict, user: dict) -> str:
    body = base64.urlsafe_b64encode(json.dumps({"id": user["id"], "name": user["name"],
                                                "exp": int(time.time()) + DAYS * 86400}).encode()).decode()
    return body + "." + hmac.new(_key(cfg), body.encode(), "sha256").hexdigest()


def user_from(cfg: dict, cookie_header: str | None) -> dict | None:
    token = ""
    for part in (cookie_header or "").split(";"):
        k, _, v = part.strip().partition("=")
        if k == COOKIE:
            token = v
    body, _, sig = token.partition(".")
    if not body or not cfg.get("jellyfin", {}).get("api_key"):
        return None
    if not hmac.compare_digest(sig, hmac.new(_key(cfg), body.encode(), "sha256").hexdigest()):
        return None
    try:
        data = json.loads(base64.urlsafe_b64decode(body.encode()))
    except ValueError:
        return None
    return data if data.get("exp", 0) > time.time() else None


def cookie(token: str, max_age: int = DAYS * 86400) -> str:
    return f"{COOKIE}={token}; Path=/; Max-Age={max_age}; HttpOnly; SameSite=Lax"


# --- los gustos de cada amigo ------------------------------------------------------------
class FriendLib:
    """Hace de ``Library`` para Descubrir: las calificaciones y «Mis gustos» de cada amigo van en su propio
    archivo (``amigos/<usuario>.sqlite`` junto a subman.toml), y la biblioteca de verdad se lee de la tuya
    (adjunta como ``lib``) para saber qué ya está en Jellyfin."""

    def __init__(self, config_path: str, cfg: dict, user_id: str):
        Library(cfg["database"]).db.close()  # que exista tu biblioteca (con su tabla) antes de adjuntarla
        folder = Path(config_path).parent / "amigos"
        folder.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(folder / (re.sub(r"[^A-Za-z0-9_-]", "", user_id) + ".sqlite")), timeout=30)
        self.db.row_factory = sqlite3.Row
        self.db.execute("ATTACH DATABASE ? AS lib", (str(Path(cfg["database"]).resolve()),))


# --- las peticiones ----------------------------------------------------------------------
class Friends:
    def __init__(self, dsc: discover.Discover):
        self.dsc = dsc
        self.db = dsc.db
        self.db.executescript(SCHEMA)

    def _status(self, row: dict, idx: dict, downloading: set) -> str:
        c = {"kind": row["kind"], "tmdb": row["tmdb"], "title": row.get("title", ""), "original": row.get("original", ""),
             "year": row.get("year")}
        if discover.Discover.in_library(c, idx):
            return "ya-esta"
        if row["state"] == "rechazada":
            return "rechazada"
        d = downloading.get((row["kind"], row["tmdb"]))
        if d and d[0] == "no-esta":  # Phono no la tiene: se sigue mirando a diario
            row["why"] = d[1] or "Phono aún no la tiene"
            return "no-disponible"
        if d:
            return "descargando"
        return row["state"]

    def _downloading(self) -> dict:
        """kind+tmdb → (estado, detalle) de lo que está en descargas. Si hay varias temporadas, manda la que avanza."""
        out: dict = {}
        try:
            for k, t, st, det in self.db.execute(
                    "SELECT kind, tmdb, state, detail FROM downloads WHERE state NOT IN ('cancelado', 'listo')"):
                if (k, t) not in out or out[(k, t)][0] == "no-esta":
                    out[(k, t)] = (st, det)
        except Exception:  # sin tabla de descargas todavía
            return {}
        return out

    def requesters(self, kind: str, tid: int) -> list[str]:
        """Quién pidió esto y sigue esperándolo (para avisarle)."""
        return [r[0] for r in self.db.execute("SELECT user_id FROM friend_requests WHERE kind=? AND tmdb=? "
                                               "AND state!='rechazada'", (kind, int(tid)))]

    def _rows(self, where: str = "", args: tuple = ()) -> list[dict]:
        out = []
        for r in self.db.execute(f"SELECT * FROM friend_requests {where} ORDER BY created DESC", args):
            row = dict(r)
            row.update(json.loads(row.pop("info") or "{}"))
            row["kind"], row["tmdb"] = r["kind"], r["tmdb"]
            out.append(row)
        return out

    def ask(self, user: dict, item: dict) -> None:
        kind, tid = item["kind"], int(item["tmdb"])
        if kind not in discover.KINDS:
            raise ValueError("tipo desconocido")
        info = {k: item.get(k) for k in ("title", "original", "year", "poster", "backdrop", "overview", "genres")}
        self.db.execute(
            """INSERT INTO friend_requests (kind, tmdb, user_id, user_name, info, state, created)
               VALUES (?,?,?,?,?,'pendiente',?)
               ON CONFLICT(kind, tmdb, user_id) DO UPDATE SET user_name=excluded.user_name""",
            (kind, tid, user["id"], user["name"], json.dumps(info, ensure_ascii=False), time.time()))
        # si ya la habías aceptado para otro, ésta también lo está
        self.db.execute("""UPDATE friend_requests SET state='aceptada', decided=? WHERE kind=? AND tmdb=? AND user_id=?
                           AND state='pendiente' AND EXISTS (SELECT 1 FROM friend_requests WHERE kind=? AND tmdb=?
                           AND state='aceptada')""", (time.time(), kind, tid, user["id"], kind, tid))
        self.db.commit()

    def cancel(self, user: dict, kind: str, tid: int) -> None:
        """El amigo quita su petición (sólo mientras no la has decidido)."""
        self.db.execute("DELETE FROM friend_requests WHERE kind=? AND tmdb=? AND user_id=? AND state='pendiente'",
                        (kind, int(tid), user["id"]))
        self.db.commit()

    def mine(self, user: dict) -> list[dict]:
        idx, dl = self.dsc.owned_index(), self._downloading()
        rows = self._rows("WHERE user_id=?", (user["id"],))
        for r in rows:
            r["status"] = self._status(r, idx, dl)
            for k in ("user_id", "user_name", "decided"):
                r.pop(k, None)
        return rows

    def asked(self, user: dict) -> dict:
        """kind+tmdb → estado de lo que ya pidió este amigo (para marcar las tarjetas)."""
        return {f"{r['kind']}{r['tmdb']}": r["state"] for r in self.db.execute(
            "SELECT kind, tmdb, state FROM friend_requests WHERE user_id=?", (user["id"],))}

    def all(self) -> list[dict]:
        """Para ti: una fila por película o serie, con quién la pidió."""
        idx, dl = self.dsc.owned_index(), self._downloading()
        items: dict[tuple, dict] = {}
        for r in self._rows():
            key = (r["kind"], r["tmdb"])
            it = items.get(key)
            if not it:
                it = items[key] = {k: v for k, v in r.items() if k not in ("user_id", "user_name", "state", "decided")}
                it["who"], it["state"] = [], r["state"]
            it["who"].append(r["user_name"])
            it["created"] = min(it["created"], r["created"])
            if r["state"] == "pendiente":
                it["state"] = "pendiente"  # basta con que alguien esté esperando
        out = list(items.values())
        for it in out:
            it["status"] = self._status(it, idx, dl)
        order = {"pendiente": 0, "aceptada": 1, "descargando": 1, "no-disponible": 1, "ya-esta": 2, "rechazada": 3}
        out.sort(key=lambda it: (order.get(it["status"], 9), -it["created"]))
        return out

    def pending(self) -> int:
        return self.db.execute("SELECT COUNT(DISTINCT kind || tmdb) FROM friend_requests WHERE state='pendiente'").fetchone()[0]

    def decide(self, kind: str, tid: int, accept: bool) -> dict | None:
        """Acepta o rechaza la petición de todos los que la pidieron. Aceptar la pone en «Por conseguir»."""
        rows = self._rows("WHERE kind=? AND tmdb=?", (kind, int(tid)))
        if not rows:
            return None
        self.db.execute("UPDATE friend_requests SET state=?, decided=? WHERE kind=? AND tmdb=?",
                        ("aceptada" if accept else "rechazada", time.time(), kind, int(tid)))
        self.db.commit()
        item = rows[0]
        if accept:
            self.dsc.mark(item, have=0)
        return item

    def forget(self, kind: str, tid: int) -> None:
        self.db.execute("DELETE FROM friend_requests WHERE kind=? AND tmdb=?", (kind, int(tid)))
        self.db.commit()

    def browse(self, user: dict, section: str, page: int = 1, query: str = "") -> list[dict]:
        """Novedades, populares o una búsqueda, con si ya está en la biblioteca y si ya la pidió."""
        d = self.dsc
        if query:
            res = d.api._get("/search/multi", query=query, include_adult="false")
            cards = [discover.card(x["media_type"], x, d.genres()) for x in res.get("results", [])
                     if x.get("media_type") in discover.KINDS and x.get("id")]
        else:
            cards = d.news(page) if section == "novedades" else d.classics(page)
        idx, asked = d.owned_index(), self.asked(user)
        out = []
        for c in cards:
            out.append({k: c.get(k) for k in ("kind", "tmdb", "title", "original", "year", "poster", "backdrop",
                                              "overview", "genres", "vote")}
                       | {"have": d.in_library(c, idx), "asked": asked.get(f"{c['kind']}{c['tmdb']}")})
        return out
