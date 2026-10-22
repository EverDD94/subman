"""Tu bot de avisos (opcional): un bot de Telegram tuyo, creado con @BotFather, que te escribe cuando
te toca pulsar el anuncio. Al ser un chat sólo para esto, puedes dejarlo con sonido aunque tengas el
resto de Telegram silenciado. Usa la Bot API normal (https://api.telegram.org)."""
from __future__ import annotations

import logging

import requests

log = logging.getLogger(__name__)
API = "https://api.telegram.org/bot{}/{}"


def _call(token: str, method: str, **data) -> dict:
    r = requests.post(API.format(token, method), json=data, timeout=20)
    j = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    if not j.get("ok"):
        raise ValueError(j.get("description") or f"Telegram respondió {r.status_code}")
    return j["result"]


def bot_name(token: str) -> str:
    """El @nombre del bot (y de paso comprueba que el token vale)."""
    return _call(token, "getMe")["username"]


def send(token: str, chat_id: int | str, text: str, link: tuple[str, str] | None = None) -> None:
    """Manda ``text``; con ``link`` = (texto, url) añade un botón debajo."""
    extra = {"reply_markup": {"inline_keyboard": [[{"text": link[0], "url": link[1]}]]}} if link else {}
    _call(token, "sendMessage", chat_id=chat_id, text=text, disable_web_page_preview=True, **extra)
