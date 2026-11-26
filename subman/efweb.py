"""Everflix en el navegador: entrar con el usuario de Jellyfin y hablar con Jellyfin a través de Videotecario.

La página (static/everflix.html) no habla con Jellyfin directamente: todo pasa por ``/ef/jf/...`` con el
token del usuario guardado en una cookie que JavaScript no puede leer. Así funciona igual en casa (:8787)
que desde fuera (:8921), sin problemas de CORS ni de mezclar http con https.
"""
from __future__ import annotations

import uuid

import requests

from .friends import _hdr

COOKIE = "efjf"
DAYS = 90
# cabeceras de Jellyfin que dejamos pasar al navegador (vídeo con saltos, imágenes con caché)
PASS = ("Content-Type", "Content-Length", "Content-Range", "Accept-Ranges", "Cache-Control", "ETag",
        "Last-Modified", "Content-Disposition", "Content-Encoding")


def _client(device_id: str, token: str = "") -> dict:
    value = f'MediaBrowser Client="Everflix Web", Device="Navegador", DeviceId="{device_id}", Version="1.0.0"'
    if token:
        value += f', Token="{token}"'
    return _hdr(value)


def base(cfg: dict) -> str:
    jf = cfg.get("jellyfin", {})
    if not jf.get("url"):
        raise RuntimeError("Videotecario todavía no está conectado a Jellyfin")
    return jf["url"].rstrip("/")


def login(cfg: dict, user: str, password: str) -> tuple[str, dict] | None:
    """(cookie, usuario) si Jellyfin acepta; None si la contraseña no vale. La sesión queda abierta en Jellyfin
    (como la de la tele): así sale en «Viendo ahora» y guarda por dónde vas."""
    device = "everflix-web-" + uuid.uuid4().hex[:12]
    r = requests.post(base(cfg) + "/Users/AuthenticateByName", json={"Username": user, "Pw": password},
                      headers=_client(device), timeout=15)
    if r.status_code in (400, 401, 403):
        return None
    r.raise_for_status()
    data = r.json()
    return f"{data['AccessToken']}|{device}", data["User"]


def token_from(cookie_header: str | None) -> tuple[str, str]:
    for part in (cookie_header or "").split(";"):
        k, _, v = part.strip().partition("=")
        if k == COOKIE and "|" in v:
            tok, _, dev = v.partition("|")
            if tok.isalnum() and len(tok) < 100 and len(dev) < 60:
                return tok, dev
    return "", ""


def cookie(value: str, max_age: int = DAYS * 86400) -> str:
    return f"{COOKIE}={value}; Path=/; Max-Age={max_age}; HttpOnly; SameSite=Lax"


def logout(cfg: dict, token: str, device: str) -> None:
    try:
        requests.post(base(cfg) + "/Sessions/Logout", headers=_client(device, token), timeout=10)
    except Exception:
        pass


def forward(cfg: dict, method: str, path_qs: str, token: str, device: str, body: bytes | None,
            ctype: str | None, rng: str | None) -> requests.Response:
    """La petición tal cual a Jellyfin, con el token del usuario. Respuesta en trozos (vídeo, imágenes)."""
    headers = _client(device, token)
    # sólo gzip: con brotli/zstd instalados (los trae yt-dlp) requests pediría br/zstd, que Chrome no
    # acepta en http sin cifrar (ERR_CONTENT_DECODING_FAILED) y la respuesta pasa comprimida tal cual
    headers["Accept-Encoding"] = "gzip"
    if ctype:
        headers["Content-Type"] = ctype
    if rng:
        headers["Range"] = rng
    return requests.request(method, base(cfg) + path_qs, headers=headers, data=body, stream=True,
                            timeout=(10, 120), allow_redirects=False)
