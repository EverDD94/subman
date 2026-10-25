"""Usuario y contraseña para entrar en la página (opcional).

Mientras no se pongan en Ajustes, entra cualquiera de la red de casa, como siempre. Con ellos
puestos, la página pide entrar y recuerda el móvil o el ordenador 90 días con una cookie firmada.
Si se olvida la contraseña: entrar con un administrador de Jellyfin (y poner otra en Ajustes), o borrar la línea
``password_hash`` de ``[access]`` en subman.toml.
"""
from __future__ import annotations

import hashlib
import hmac
import secrets
import threading
import time

COOKIE = "videotecario"
DAYS = 90
ITERATIONS = 200_000
MAX_FAILS = 8           # intentos fallidos seguidos desde una misma dirección...
LOCK_SECONDS = 10 * 60  # ...antes de hacerla esperar

_fails: dict[str, list[float]] = {}
_lock = threading.Lock()


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    h = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, ITERATIONS)
    return f"pbkdf2_sha256${ITERATIONS}${salt.hex()}${h.hex()}"


def check_password(password: str, stored: str) -> bool:
    try:
        _, iterations, salt, h = stored.split("$")
        got = hashlib.pbkdf2_hmac("sha256", password.encode(), bytes.fromhex(salt), int(iterations))
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(got.hex(), h)


def enabled(cfg: dict) -> bool:
    a = cfg.get("access") or {}
    return bool(a.get("user") and a.get("password_hash"))


def _sign(cfg: dict, expires: int) -> str:
    a = cfg["access"]
    # la firma depende de la contraseña: al cambiarla se cierran todas las sesiones abiertas
    key = (a.get("secret", "") + a["password_hash"]).encode()
    return hmac.new(key, f"{a['user']}|{expires}".encode(), "sha256").hexdigest()


def make_token(cfg: dict) -> str:
    expires = int(time.time()) + DAYS * 86400
    return f"{expires}.{_sign(cfg, expires)}"


def token_ok(cfg: dict, token: str) -> bool:
    try:
        expires, sig = token.split(".", 1)
        expires = int(expires)
    except ValueError:
        return False
    return expires > time.time() and hmac.compare_digest(sig, _sign(cfg, expires))


def cookie(token: str, max_age: int = DAYS * 86400) -> str:
    return f"{COOKIE}={token}; Path=/; Max-Age={max_age}; HttpOnly; SameSite=Lax"


def token_from(cookie_header: str | None) -> str:
    for part in (cookie_header or "").split(";"):
        k, _, v = part.strip().partition("=")
        if k == COOKIE:
            return v
    return ""


def blocked(addr: str) -> bool:
    with _lock:
        now = time.time()
        recent = [t for t in _fails.get(addr, []) if now - t < LOCK_SECONDS]
        _fails[addr] = recent
        return len(recent) >= MAX_FAILS


def note(addr: str, good: bool) -> None:
    """Apunta un intento de entrar (los fallidos seguidos acaban bloqueando esa dirección un rato)."""
    with _lock:
        if good:
            _fails.pop(addr, None)
        else:
            _fails.setdefault(addr, []).append(time.time())


def login(cfg: dict, addr: str, user: str, password: str) -> str | None:
    """Devuelve el token si el usuario y la contraseña son correctos."""
    a = cfg.get("access") or {}
    good = hmac.compare_digest((user or "").strip().lower(), (a.get("user") or "").lower()) \
        and check_password(password or "", a.get("password_hash", ""))
    note(addr, good)
    if not good:
        time.sleep(1)  # frena a quien pruebe contraseñas a lo loco
        return None
    return make_token(cfg)


def set_access(cfg: dict, user: str, password: str | None) -> None:
    """Pone o quita el acceso. ``password`` None = dejar la que hay; usuario vacío = quitarlo."""
    a = cfg.setdefault("access", {})
    user = (user or "").strip()
    if not user:
        a.update(user="", password_hash="")
        return
    if password is None and not a.get("password_hash"):
        raise ValueError("pon una contraseña")
    if password is not None:
        if len(password) < 6:
            raise ValueError("la contraseña tiene que tener al menos 6 caracteres")
        a["password_hash"] = hash_password(password)
    a["user"] = user
    if not a.get("secret"):
        a["secret"] = secrets.token_hex(32)
