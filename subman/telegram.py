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
import os
import re
import threading
import time
from datetime import timedelta
from pathlib import Path

log = logging.getLogger(__name__)
BOT = "phonofilmbot"
ASK_AFTER = 120         # si en 2 minutos no llega nada (ni el anuncio), se te avisa igual
GIVE_UP = 12 * 3600     # cuánto se espera como mucho a que llegue el archivo
QUIET = 120             # tras el último archivo, cuánto se espera por si manda más (capítulos)
PARALLEL = 8           # trozos que se piden a la vez (como tdl); uno solo va a 1-3 MB/s
PIECE = 512 * 1024     # lo más que Telegram da por petición
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
            return {"name": " ".join(x for x in (u.first_name, u.last_name) if x) or u.username or "", "phone": u.phone,
                    "id": u.id}
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

    def say(self, to: str, text: str) -> None:
        """Escribe desde tu cuenta (p. ej. «/start» a tu bot de avisos, para que pueda escribirte)."""
        self._run(self.client.send_message(to, text))

    def log_out(self) -> None:
        self._run(self.client.log_out())

    # --- pedir al bot -------------------------------------------------------------------
    def fetch(self, code: str, dest: Path, bot: str = BOT, progress=lambda what, done, total: None,
              stop=lambda: False, on_manual=lambda bot: None, skip=lambda name, size: False,
              on_file=lambda path, info: None, remind: str = "") -> list[tuple[Path, dict]]:
        """Pide ``code`` al bot y descarga lo que mande en ``dest`` (una película o los capítulos de
        una temporada). Devuelve [(ruta, info)].

        ``skip(nombre, tamaño)``: los que ya se bajaron antes (tras un reinicio) no se vuelven a bajar.
        ``on_file(ruta, info)``: se llama en cuanto termina cada archivo (en otro hilo).
        ``remind``: si se pasa, cuando toca pulsar el anuncio te llega un recordatorio a «Mensajes guardados»
        con ese nombre."""
        return self._run(self._fetch(bot, code, Path(dest), progress, stop, on_manual, skip, on_file, remind),
                         timeout=None)

    async def _remind(self, bot: str, what: str) -> None:
        """Recordatorio en «Mensajes guardados». Un mensaje normal a ti mismo no suena; uno programado
        («recordatorio») sí avisa, por eso sale programado para dentro de unos segundos."""
        text = (f"🎬 Videotecario: {what} está esperando.\n"
                f"Abre @{bot} y pulsa «Watch Ads» y después «I have clicked ads».\nhttps://t.me/{bot}")
        try:
            await self.client.send_message("me", text, schedule=timedelta(seconds=15), link_preview=False)
        except Exception as e:
            log.warning("Telegram: no se pudo programar el recordatorio (%s); va como mensaje normal", e)
            try:
                await self.client.send_message("me", text, link_preview=False)
            except Exception as e2:
                log.warning("Telegram: no se pudo mandar el aviso: %s", e2)

    async def _fetch(self, name, code, dest, progress, stop, on_manual, skip=lambda n, s: False,
                     on_file=lambda p, i: None, remind=""):
        bot = await self.client.get_entity(name)
        last = await self.client.get_messages(bot, limit=1)
        last_id = last[0].id if last else 0
        await self.client.send_message(bot, f"/start {code}")
        log.info("Telegram: pedido %s a @%s", code, name)
        progress("esperando al bot", 0, 0)
        start = time.time()
        asked = chose = seen_file = False
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
                    on_manual(name)
                    if remind:
                        await self._remind(name, remind)
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
                    fname = _safe(m.file.name or f"phono-{m.id}.mkv")
                    if skip(fname, m.file.size or 0):
                        log.info("Telegram: %s ya estaba bajado", fname)
                    else:
                        path, inf = await self._download(m, dest, progress, stop), info(m)
                        got.append((path, inf))
                        await asyncio.get_running_loop().run_in_executor(None, on_file, path, inf)
                    seen_file = True
                    last_file = time.time()
                elif kind == "sample":
                    log.info("Telegram: muestra ignorada")
                elif m.message:
                    log.info("Telegram: el bot dice: %s", m.message[:200].replace("\n", " "))
            now = time.time()
            if seen_file and now - last_file > QUIET:
                break
            if not seen_file and not asked and now - start > ASK_AFTER:
                asked = True
                on_manual(name)
                if remind:
                    await self._remind(name, remind)
            if not seen_file and now - start > GIVE_UP:
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
            try:
                await self._parallel(m, part, m.file.size or 0, each)
            except InterruptedError:
                raise
            except Exception as e:  # si falla en paralelo, a la manera de siempre (de uno en uno)
                log.warning("Telegram: descarga en paralelo falló (%s); sigo de uno en uno", e)
                await self.client.download_media(m, file=str(part), progress_callback=each)
        except InterruptedError:
            part.unlink(missing_ok=True)
            raise
        final = dest / name
        part.replace(final)
        log.info("Telegram: descargado %s (%.0f MB)", name, (m.file.size or 0) / 2**20)
        return final


    async def _parallel(self, m, part: Path, size: int, each) -> None:
        """Descarga pidiendo ``PARALLEL`` trozos a la vez; cada uno se escribe en su sitio del archivo."""
        if size <= 0:
            raise ValueError("tamaño desconocido")
        pieces = (size + PIECE - 1) // PIECE
        done = 0
        with open(part, "wb") as f:
            f.truncate(size)
        fd = os.open(part, os.O_WRONLY)
        try:
            async def worker(i: int):
                nonlocal done
                n, pos = len(range(i, pieces, PARALLEL)), i * PIECE
                if not n:
                    return
                async for chunk in self.client.iter_download(m.media, offset=pos, stride=PARALLEL * PIECE, limit=n,
                                                             request_size=PIECE, chunk_size=PIECE, file_size=size):
                    os.pwrite(fd, chunk, pos)
                    pos += PARALLEL * PIECE
                    done += len(chunk)
                    each(done, size)
            tasks = [asyncio.ensure_future(worker(i)) for i in range(PARALLEL)]
            try:
                await asyncio.gather(*tasks)
            except BaseException:
                for t in tasks:
                    t.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
                raise
        finally:
            os.close(fd)
        if done != size:
            raise IOError(f"llegaron {done} de {size} bytes")


def _safe(name: str) -> str:
    return re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", name).strip(" .") or "video.mkv"
