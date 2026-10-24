"""Avisos para tu familia y amigos: la campanita de su página y las notificaciones en su móvil.

- Cuando llega a Jellyfin algo que alguien pidió, se le avisa («Ya está Dune»).
- Quien sigue una serie (la pidió, lo marcó en la ficha, o tú le apuntaste) recibe cada capítulo nuevo,
  llegue por Phono o lo copies tú a mano: se le pregunta a Jellyfin cada pocos minutos qué se añadió.
- Tú puedes mandar un aviso escrito a quien quieras.

Las notificaciones al móvil son «Web Push» (las del navegador), cifradas como piden los navegadores
(RFC 8291, aes128gcm) y firmadas con una clave propia (VAPID, RFC 8292) que se crea sola en
``push-key.pem`` junto a subman.toml. En iPhone hace falta añadir la página a la pantalla de inicio.
"""
from __future__ import annotations

import base64
import json
import logging
import os
import struct
import time
from pathlib import Path
from urllib.parse import urlparse

import requests
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.asymmetric.utils import decode_dss_signature
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

from . import friends, jellyfin

log = logging.getLogger(__name__)
SUBJECT = "https://github.com/EverDD94/subman"  # quién manda las notificaciones (lo piden Apple y Google)
KEEP = 60 * 86400  # los avisos de más de dos meses se borran

SCHEMA = """
CREATE TABLE IF NOT EXISTS friend_users (     -- quien ha entrado alguna vez con su usuario de Jellyfin
    id TEXT PRIMARY KEY, name TEXT NOT NULL, seen REAL
);
CREATE TABLE IF NOT EXISTS notices (
    id INTEGER PRIMARY KEY, user_id TEXT NOT NULL, title TEXT NOT NULL, body TEXT, url TEXT,
    created REAL, read INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS push_subs (        -- cada móvil u ordenador donde activó las notificaciones
    endpoint TEXT PRIMARY KEY, user_id TEXT NOT NULL, p256dh TEXT NOT NULL, auth TEXT NOT NULL, created REAL
);
CREATE TABLE IF NOT EXISTS follows (          -- series de las que avisar cada capítulo nuevo
    user_id TEXT NOT NULL, tmdb INTEGER NOT NULL, info TEXT, created REAL, PRIMARY KEY (user_id, tmdb)
);
CREATE TABLE IF NOT EXISTS jf_seen (          -- lo de Jellyfin que ya se miró (para avisar sólo de lo nuevo)
    id TEXT PRIMARY KEY, seen REAL
);
"""


def b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def unb64u(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


# --- Web Push ------------------------------------------------------------------------------
def _point(key) -> bytes:
    return key.public_key().public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint) \
        if hasattr(key, "public_key") else key.public_bytes(serialization.Encoding.X962,
                                                            serialization.PublicFormat.UncompressedPoint)


def vapid_key(path: Path):
    """La clave para firmar (se crea la primera vez y se guarda: si se pierde, hay que volver a activar)."""
    if path.exists():
        return serialization.load_pem_private_key(path.read_bytes(), None)
    key = ec.generate_private_key(ec.SECP256R1())
    path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                       serialization.NoEncryption()))
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return key


def public_key(path: Path) -> str:
    """Lo que necesita el navegador para suscribirse (applicationServerKey)."""
    return b64u(_point(vapid_key(path)))


def _jwt(key, audience: str) -> str:
    head = b64u(json.dumps({"typ": "JWT", "alg": "ES256"}).encode())
    claims = b64u(json.dumps({"aud": audience, "exp": int(time.time()) + 12 * 3600, "sub": SUBJECT}).encode())
    r, s = decode_dss_signature(key.sign(f"{head}.{claims}".encode(), ec.ECDSA(hashes.SHA256())))
    return f"{head}.{claims}.{b64u(r.to_bytes(32, 'big') + s.to_bytes(32, 'big'))}"


def _hkdf(salt: bytes, ikm: bytes, info: bytes, length: int) -> bytes:
    return HKDF(algorithm=hashes.SHA256(), length=length, salt=salt, info=info).derive(ikm)


def encrypt(payload: bytes, p256dh: str, auth: str) -> bytes:
    """Cifra el mensaje para ese navegador (RFC 8291, un solo bloque aes128gcm)."""
    ua_public = unb64u(p256dh)
    ua_key = ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), ua_public)
    mine = ec.generate_private_key(ec.SECP256R1())
    as_public = _point(mine)
    secret = mine.exchange(ec.ECDH(), ua_key)
    ikm = _hkdf(unb64u(auth), secret, b"WebPush: info\x00" + ua_public + as_public, 32)
    salt = os.urandom(16)
    cek = _hkdf(salt, ikm, b"Content-Encoding: aes128gcm\x00", 16)
    nonce = _hkdf(salt, ikm, b"Content-Encoding: nonce\x00", 12)
    body = AESGCM(cek).encrypt(nonce, payload + b"\x02", None)
    return salt + struct.pack("!IB", 4096, len(as_public)) + as_public + body


def push(key_path: Path, sub: dict, data: dict) -> int:
    """Manda una notificación a un navegador. Devuelve el código HTTP (404/410 = ya no existe)."""
    key = vapid_key(key_path)
    u = urlparse(sub["endpoint"])
    r = requests.post(sub["endpoint"], data=encrypt(json.dumps(data, ensure_ascii=False).encode(), sub["p256dh"], sub["auth"]),
                      headers={"TTL": str(4 * 86400), "Content-Encoding": "aes128gcm", "Content-Type": "application/octet-stream",
                               "Urgency": "normal",
                               "Authorization": f"vapid t={_jwt(key, f'{u.scheme}://{u.netloc}')}, k={b64u(_point(key))}"},
                      timeout=20)
    return r.status_code


# --- los avisos ---------------------------------------------------------------------------
class Notices:
    def __init__(self, db, key_path: Path):
        self.db = db
        self.key_path = key_path
        self.db.executescript(SCHEMA)
        self.db.executescript(friends.SCHEMA)
        cols = {r[1] for r in self.db.execute("PRAGMA table_info(friend_requests)")}
        if "told" not in cols:  # bases de antes: cuándo se le avisó de que ya está
            self.db.execute("ALTER TABLE friend_requests ADD COLUMN told REAL")
            self.db.commit()

    # quién hay
    def touch(self, user: dict) -> None:
        self.db.execute("INSERT INTO friend_users (id, name, seen) VALUES (?,?,?) ON CONFLICT(id) DO UPDATE SET "
                        "name=excluded.name, seen=excluded.seen", (user["id"], user["name"], time.time()))
        self.db.commit()

    def users(self) -> list[dict]:
        rows = {r["id"]: r["name"] for r in self.db.execute("SELECT id, name FROM friend_users")}
        try:  # quien pidió algo antes de que existiera esta lista
            for r in self.db.execute("SELECT DISTINCT user_id, user_name FROM friend_requests"):
                rows.setdefault(r["user_id"], r["user_name"])
        except Exception:
            pass
        phones = {r[0] for r in self.db.execute("SELECT DISTINCT user_id FROM push_subs")}
        return sorted(({"id": k, "name": v, "push": k in phones} for k, v in rows.items()), key=lambda u: u["name"].lower())

    # mandar
    def send(self, user_ids, title: str, body: str = "", url: str = "/") -> int:
        """Lo deja en la campanita de cada uno y lo manda a sus móviles. Devuelve cuántos móviles lo recibieron."""
        now, sent = time.time(), 0
        for uid in dict.fromkeys(user_ids):
            self.db.execute("INSERT INTO notices (user_id, title, body, url, created) VALUES (?,?,?,?,?)",
                            (uid, title, body, url, now))
            for s in self.db.execute("SELECT * FROM push_subs WHERE user_id=?", (uid,)).fetchall():
                try:
                    code = push(self.key_path, dict(s), {"title": title, "body": body, "url": url})
                except Exception as e:
                    log.warning("avisos: no se pudo mandar al móvil de %s (%s)", uid, e)
                    continue
                if code in (404, 410):  # desactivó las notificaciones o borró la página del móvil
                    self.db.execute("DELETE FROM push_subs WHERE endpoint=?", (s["endpoint"],))
                elif code < 300:
                    sent += 1
                else:
                    log.warning("avisos: el servicio de notificaciones contestó %s", code)
        self.db.execute("DELETE FROM notices WHERE created<?", (now - KEEP,))
        self.db.commit()
        return sent

    def list(self, user_id: str, limit: int = 50) -> list[dict]:
        return [dict(r) for r in self.db.execute("SELECT id, title, body, url, created, read FROM notices WHERE user_id=? "
                                                 "ORDER BY created DESC LIMIT ?", (user_id, limit))]

    def unread(self, user_id: str) -> int:
        return self.db.execute("SELECT COUNT(*) FROM notices WHERE user_id=? AND read=0", (user_id,)).fetchone()[0]

    def read_all(self, user_id: str) -> None:
        self.db.execute("UPDATE notices SET read=1 WHERE user_id=?", (user_id,))
        self.db.commit()

    # móviles
    def subscribe(self, user_id: str, sub: dict) -> None:
        keys = sub.get("keys") or {}
        if not (str(sub.get("endpoint", "")).startswith("https://") and keys.get("p256dh") and keys.get("auth")):
            raise ValueError("suscripción no válida")
        self.db.execute("INSERT OR REPLACE INTO push_subs (endpoint, user_id, p256dh, auth, created) VALUES (?,?,?,?,?)",
                        (sub["endpoint"], user_id, keys["p256dh"], keys["auth"], time.time()))
        self.db.commit()

    def unsubscribe(self, user_id: str, endpoint: str) -> None:
        self.db.execute("DELETE FROM push_subs WHERE endpoint=? AND user_id=?", (endpoint, user_id))
        self.db.commit()

    # series que se siguen
    def follow(self, user_id: str, item: dict, on: bool = True) -> None:
        tid = int(item["tmdb"])
        if on:
            info = {k: item.get(k) for k in ("title", "year", "poster")}
            self.db.execute("INSERT OR IGNORE INTO follows (user_id, tmdb, info, created) VALUES (?,?,?,?)",
                            (user_id, tid, json.dumps(info, ensure_ascii=False), time.time()))
        else:
            self.db.execute("DELETE FROM follows WHERE user_id=? AND tmdb=?", (user_id, tid))
        self.db.commit()

    def set_followers(self, item: dict, user_ids: list[str]) -> None:
        """Tú decides quién sigue esta serie (los que no estén en la lista dejan de seguirla)."""
        tid = int(item["tmdb"])
        keep = set(user_ids)
        for (uid,) in self.db.execute("SELECT user_id FROM follows WHERE tmdb=?", (tid,)).fetchall():
            if uid not in keep:
                self.db.execute("DELETE FROM follows WHERE user_id=? AND tmdb=?", (uid, tid))
        for uid in keep:
            self.follow(uid, item)
        self.db.commit()

    def following(self, user_id: str) -> list[int]:
        return [r[0] for r in self.db.execute("SELECT tmdb FROM follows WHERE user_id=?", (user_id,))]

    def all_follows(self) -> list[dict]:
        names = {u["id"]: u["name"] for u in self.users()}
        out: dict[int, dict] = {}
        for r in self.db.execute("SELECT user_id, tmdb, info FROM follows ORDER BY created"):
            it = out.setdefault(r["tmdb"], {"tmdb": r["tmdb"], **json.loads(r["info"] or "{}"), "who": []})
            it["who"].append({"id": r["user_id"], "name": names.get(r["user_id"], "?")})
        return list(out.values())

    # lo nuevo en Jellyfin
    def check_jellyfin(self, cfg: dict) -> int:
        """Mira lo último que se añadió a Jellyfin y avisa a quien toque. Devuelve cuántos avisos mandó."""
        jf = cfg.get("jellyfin", {})
        if not (jf.get("url") and jf.get("api_key")):
            return 0
        items = jellyfin.latest(jf["url"], jf["api_key"])
        if not items:
            return 0
        first = self.db.execute("SELECT COUNT(*) FROM jf_seen").fetchone()[0] == 0
        seen = {r[0] for r in self.db.execute("SELECT id FROM jf_seen")}
        new = [it for it in items if it["id"] not in seen]
        now = time.time()
        self.db.executemany("INSERT OR IGNORE INTO jf_seen (id, seen) VALUES (?,?)", [(it["id"], now) for it in new])
        self.db.commit()
        if first or not new:  # la primera vez sólo se apunta lo que ya había: nada de avisar de toda la biblioteca
            return 0
        count = 0
        series: dict[int, list[dict]] = {}
        for it in new:
            if it["type"] == "Movie" and it.get("tmdb"):
                count += self._arrived("movie", int(it["tmdb"]), it["name"])
            elif it["type"] == "Episode" and it.get("series_tmdb"):
                series.setdefault(int(it["series_tmdb"]), []).append(it)
        for tid, eps in series.items():
            name = eps[0]["series"]
            told = self._arrived("tv", tid, name)
            asked = {r[0] for r in self.db.execute("SELECT user_id FROM friend_requests WHERE kind='tv' AND tmdb=?", (tid,))} \
                if told else set()
            for uid in asked:  # quien pidió la serie quiere enterarse también de los capítulos que vengan
                self.follow(uid, {"tmdb": tid, "title": name})
            count += told
            followers = [r[0] for r in self.db.execute("SELECT user_id FROM follows WHERE tmdb=?", (tid,)) if r[0] not in asked]
            if followers:
                eps.sort(key=lambda e: (e.get("season") or 0, e.get("episode") or 0))
                tags = [f"T{e['season']}E{e['episode']:02d}" for e in eps if e.get("season") is not None and e.get("episode") is not None]
                if len(eps) == 1:
                    title = f"📺 Nuevo capítulo de {name}"
                    body = (tags[0] + (f" · {eps[0]['name']}" if eps[0].get("name") else "")) if tags else eps[0].get("name", "")
                else:
                    title = f"📺 {len(eps)} capítulos nuevos de {name}"
                    body = f"{tags[0]} a {tags[-1]}" if tags else ""
                self.send(followers, title, body + ("\nYa puedes verlo en Jellyfin." if body else "Ya puedes verlo en Jellyfin."))
                count += len(followers)
        return count

    def _arrived(self, kind: str, tid: int, name: str) -> int:
        try:
            rows = self.db.execute("SELECT user_id, info FROM friend_requests WHERE kind=? AND tmdb=? AND told IS NULL",
                                   (kind, tid)).fetchall()
        except Exception:
            return 0
        if not rows:
            return 0
        title = json.loads(rows[0]["info"] or "{}").get("title") or name
        what = "🎬 Ya está la película" if kind == "movie" else "📺 Ya está la serie"
        self.send([r["user_id"] for r in rows], f"{what} {title}", "La que pediste. Ya puedes verla en Jellyfin.")
        self.db.execute("UPDATE friend_requests SET told=? WHERE kind=? AND tmdb=?", (time.time(), kind, tid))
        self.db.commit()
        return len(rows)
