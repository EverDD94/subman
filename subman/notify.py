"""Tu bot de avisos (opcional): un bot de Telegram tuyo, creado con @BotFather, que te escribe cuando
te toca pulsar el anuncio. Al ser un chat sólo para esto, puedes dejarlo con sonido aunque tengas el
resto de Telegram silenciado. Usa la Bot API normal (https://api.telegram.org)."""
from __future__ import annotations

import logging

import requests

log = logging.getLogger(__name__)
API = "https://api.telegram.org/bot{}/{}"


def _call(token: str, method: str, timeout: float = 20, **data) -> dict:
    r = requests.post(API.format(token, method), json=data, timeout=timeout)
    j = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
    if not j.get("ok"):
        raise ValueError(j.get("description") or f"Telegram respondió {r.status_code}")
    return j["result"]


def bot_name(token: str) -> str:
    """El @nombre del bot (y de paso comprueba que el token vale)."""
    return _call(token, "getMe")["username"]


def _markup(link, buttons) -> dict:
    """Botones debajo del mensaje: ``link`` = (texto, url) o ``buttons`` = filas de
    {"text", "url"} / {"text", "callback_data"}."""
    rows = [[{"text": link[0], "url": link[1]}]] if link else []
    rows += buttons or []
    return {"reply_markup": {"inline_keyboard": rows}} if rows else {}


def send(token: str, chat_id: int | str, text: str, link: tuple[str, str] | None = None,
         buttons: list[list[dict]] | None = None) -> dict:
    """Manda ``text``; con ``link`` = (texto, url) añade un botón debajo."""
    return _call(token, "sendMessage", chat_id=chat_id, text=text, disable_web_page_preview=True,
                 **_markup(link, buttons))


def send_photo(token: str, chat_id: int | str, photo: str, caption: str,
               buttons: list[list[dict]] | None = None) -> dict:
    """Una foto (el póster, por su dirección web) con texto debajo (como mucho 1024 letras)."""
    return _call(token, "sendPhoto", chat_id=chat_id, photo=photo, caption=caption[:1024], **_markup(None, buttons))


def answer(token: str, callback_id: str, text: str = "") -> None:
    """Contesta a un botón pulsado (el aviso pequeño que sale arriba en Telegram)."""
    try:
        _call(token, "answerCallbackQuery", callback_query_id=callback_id, text=text[:200])
    except Exception as e:
        log.info("bot: answerCallbackQuery: %s", e)


def updates(token: str, offset: int, wait: int = 50) -> list[dict]:
    """Lo que te escribe al bot (mensajes y botones pulsados), esperando hasta ``wait`` segundos."""
    r = requests.post(API.format(token, "getUpdates"), json={"offset": offset, "timeout": wait,
                                                             "allowed_updates": ["message", "callback_query"]},
                      timeout=wait + 15)
    j = r.json()
    if not j.get("ok"):
        raise ValueError(j.get("description") or f"Telegram respondió {r.status_code}")
    return j["result"]
