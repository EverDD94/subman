"""Tu cuenta de Telegram (Telethon) para pedirle películas y capítulos al bot de Phono.

Es lo mismo que haces tú con el botón «Free on Telegram» de phono.cc:

1. se le manda ``/start <código>`` a @phonofilmbot (el código es de cada archivo);
2. el bot contesta con dos botones: «Watch Ads» e «I have clicked ads». Esos los pulsas tú en
   Telegram (Videotecario te avisa en Descubrir › Descargas); mientras, se queda escuchando;
   (en las series, antes de eso el bot de la temporada pregunta la calidad: «480p x264», «1080p x264»…;
   eso se elige solo: 1080p x264, que se ve directo en la tele);
3. el bot manda primero un vídeo corto de muestra («Sample Video from this file») y después el archivo
   de verdad (un MKV con su nombre y tamaño). La muestra se ignora y se descarga el archivo.

Para entrar hace falta un «API ID» y un «API hash» de https://my.telegram.org (gratis, una vez) y el
código que Telegram te manda al móvil. La sesión se guarda en /config/telegram.session; nunca se
guarda el código ni la contraseña.
"""
from __future__ import annotations

import asyncio
import logging
import re
import threading
import time
from pathlib import Path

log = logging.getLogger(__name__)
BOT = "phonofilmbot"
ASK_AFTER = 120         # si en 2 minutos no llega nada (ni el anuncio), se te avisa igual
GIVE_UP = 12 * 3600     # cuánto se espera como mucho a que llegue el archivo
QUIET = 120             # tras el último archivo, cuánto se espera por si manda más (capítulos)
_SAMPLE = re.compile(r"\bsample\b", re.I)
_VIDEO_NAME = re.compile(r"\.(mkv|mp4|avi|m4v|mov|ts|webm)$", re.I)


# Calidad que se elige cuando el bot pregunta (de mejor a peor): H.264 se ve directo en todos lados.
OPTIONS = ("1080p x264", "1080p", "720p x264", "720p", "2160p", "480p")


def options(msg) -> list[tuple[str, str, str]]:
    """Botones «Choose Options» que llevan a otro pedido: [(texto, bot, código)]."""
    from .phono import bot_link
    out = []
    for row in getattr(msg, "buttons", None) or []:
        for b in row:
            link = bot_link(getattr(b, "url", None) or "")
            if link:
                out.append(((getattr(b, "text", "") or "").strip(), *link))
    return out


def choose(opts: list[tuple[str, str, str]]) -> tuple[str, str, str] | None:
    def rank(o):
        t = o[0].lower()
        return next((i for i, want in enumerate(OPTIONS) if all(w in t for w in want.split())), len(OPTIONS))
    return min(opts, key=rank) if opts else None


def classify(msg) -> str:
    """Qué es un mensaje del bot: ``ad`` (botones del anuncio), ``options`` (elegir calidad),
    ``sample``, ``file`` u ``other``."""
    text = getattr(msg, "message", None) or ""
    f = getattr(msg, "file", None)
    if f is not None:
        name = getattr(f, "name", None) or ""
        if _SAMPLE.search(text) or _SAMPLE.search(name):
            return "sample"
        if _VIDEO_NAME.search(name):
            return "file"
        if (getattr(f, "mime_type", "") or "").startswith("video/") and (getattr(f, "size", 0) or 0) > 300 * 2**20:
            return "file"  # sin nombre pero grande: no es la muestra
        return "other"
    if ad_buttons(msg):
        return "ad"
    if options(msg):
        return "options"
    return "other"


def ad_buttons(msg) -> bool:
    """¿Es el mensaje de los botones del anuncio («Watch Ads» / «I have clicked ads»)?"""
    texts = [(getattr(b, "text", "") or "").lower() for row in (getattr(msg, "buttons", None) or []) for b in row]
    return any("ads" in t or "watch" in t for t in texts)


def info(msg) -> dict:
    """Lo que el bot cuenta del archivo en el mensaje (audios, subtítulos, si se ve en la tele)."""
    text = getattr(msg, "message", None) or ""
    return {"tv": "can't be played on tv" not in text.lower(),
            "subs": re.findall(r"has (\w+) subtitles", text, re.I),
            "audio": re.findall(r"—\s*(\w+)\s*$", text, re.M)}


class Telegram:
    """Cliente de Telegram con su propio bucle asyncio en un hilo; los métodos son síncronos."""

    def __init__(self, session: str | Path, api_id: int | str, api_hash: str):
        from telethon import TelegramClient
        self.loop = asyncio.new_event_loop()
        threading.Thread(target=self.loop.run_forever, name="telegram", daemon=True).start()
        self.client = self._run(self._make(TelegramClient, str(session), int(api_id), api_hash))
        self._phone = None
        self._hash = None

    async def _make(self, cls, session, api_id, api_hash):
        c = cls(session, api_id, api_hash, device_model="Videotecario", app_version="1.0",
                connection_retries=2, retry_delay=2, timeout=15)
        await c.connect()
        return c

    def _run(self, coro, timeout: float | None = 120):
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)

    def close(self) -> None:
        try:
            self._run(self.client.disconnect(), 20)
        except Exception:
            pass
        self.loop.call_soon_threadsafe(self.loop.stop)

    # --- entrar -----------------------------------------------------------------------
    def me(self) -> dict | None:
        async def go():
            if not await self.client.is_user_authorized():
                return None
            u = await self.client.get_me()
            return {"name": " ".join(x for x in (u.first_name, u.last_name) if x) or u.username or "", "phone": u.phone}
        return self._run(go())

    def send_code(self, phone: str) -> None:
        phone = re.sub(r"[^\d+]", "", phone)
        r = self._run(self.client.send_code_request(phone))
        self._phone, self._hash = phone, r.phone_code_hash

    def sign_in(self, code: str = "", password: str = "") -> str:
        """``ok``, o ``password`` si la cuenta tiene verificación en dos pasos y falta la contraseña."""
        from telethon.errors import SessionPasswordNeededError

        async def go():
            if password:
                await self.client.sign_in(password=password)
                return "ok"
            if not self._phone:
                raise ValueError("pide primero el código")
            try:
                await self.client.sign_in(self._phone, re.sub(r"\D", "", code), phone_code_hash=self._hash)
            except SessionPasswordNeededError:
                return "password"
            return "ok"
        return self._run(go())

    def log_out(self) -> None:
        self._run(self.client.log_out())

    # --- pedir al bot -------------------------------------------------------------------
    def fetch(self, code: str, dest: Path, bot: str = BOT, progress=lambda what, done, total: None,
              stop=lambda: False, on_manual=lambda: None) -> list[tuple[Path, dict]]:
        """Pide ``code`` al bot y descarga lo que mande en ``dest`` (una película o los capítulos de
        una temporada). Devuelve [(ruta, info)]."""
        return self._run(self._fetch(bot, code, Path(dest), progress, stop, on_manual), timeout=None)

    async def _fetch(self, name, code, dest, progress, stop, on_manual):
        bot = await self.client.get_entity(name)
        last = await self.client.get_messages(bot, limit=1)
        last_id = last[0].id if last else 0
        await self.client.send_message(bot, f"/start {code}")
        log.info("Telegram: pedido %s a @%s", code, name)
        progress("esperando al bot", 0, 0)
        start = time.time()
        asked = chose = False
        got: list[tuple[Path, dict]] = []
        last_file = None
        while not stop():
            msgs = await self.client.get_messages(bot, min_id=last_id, limit=50)
            for m in sorted(msgs, key=lambda m: m.id):
                last_id = max(last_id, m.id)
                if m.out:
                    continue
                kind = classify(m)
                if kind == "ad" and not asked:
                    asked = True
                    on_manual()
                elif kind == "options" and not chose:
                    text, other, code2 = choose(options(m))
                    chose = True
                    log.info("Telegram: elegida «%s»", text)
                    progress(f"pidiendo {text}", 0, 0)
                    if other.lower() != name.lower():
                        name, bot = other, await self.client.get_entity(other)
                        last = await self.client.get_messages(bot, limit=1)
                        last_id = last[0].id if last else 0
                    await self.client.send_message(bot, f"/start {code2}")
                    break
                elif kind == "file":
                    got.append((await self._download(m, dest, progress, stop), info(m)))
                    last_file = time.time()
                elif kind == "sample":
                    log.info("Telegram: muestra ignorada")
                elif m.message:
                    log.info("Telegram: el bot dice: %s", m.message[:200].replace("\n", " "))
            now = time.time()
            if got and now - last_file > QUIET:
                break
            if not got and not asked and now - start > ASK_AFTER:
                asked = True
                on_manual()
            if not got and now - start > GIVE_UP:
                raise TimeoutError("el bot no mandó el archivo")
            await asyncio.sleep(5)
        return got

    async def _download(self, m, dest: Path, progress, stop) -> Path:
        name = _safe(m.file.name or f"phono-{m.id}.mkv")
        dest.mkdir(parents=True, exist_ok=True)
        part = dest / (name + ".part")
        progress(name, 0, m.file.size or 0)

        def each(done, total):
            if stop():
                raise InterruptedError("cancelada")
            progress(name, done, total)
        try:
            await self.client.download_media(m, file=str(part), progress_callback=each)
        except InterruptedError:
            part.unlink(missing_ok=True)
            raise
        final = dest / name
        part.replace(final)
        log.info("Telegram: descargado %s (%.0f MB)", name, (m.file.size or 0) / 2**20)
        return final


def _safe(name: str) -> str:
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", name).strip(" .") or "video.mkv"
