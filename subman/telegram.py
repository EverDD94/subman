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
CONNECTIONS = 4        # conexiones propias a Telegram para un archivo (como tdl); una sola se queda en ~5 MB/s
PER_CONNECTION = 3     # trozos pedidos a la vez por cada conexión
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

    # --- canales y grupos (Ajustes › Proveedores) ---------------------------------------
    async def _chat(self, link: str, join: bool = False):
        """La entidad de ``t.me/nombre`` o de un enlace de invitación ``t.me/+abc`` (con ``join`` entra si hace falta)."""
        from telethon.tl.functions.channels import JoinChannelRequest
        from telethon.tl.functions.messages import CheckChatInviteRequest, ImportChatInviteRequest
        from telethon.tl.types import ChatInviteAlready
        tail = link.split("/", 1)[1]
        if tail.startswith("+") or tail.startswith("joinchat/"):
            code = tail.split("/")[-1].lstrip("+")
            r = await self.client(CheckChatInviteRequest(code))
            if isinstance(r, ChatInviteAlready):
                return r.chat
            if not join:
                raise ValueError("todavía no estás dentro de ese grupo")
            return (await self.client(ImportChatInviteRequest(code))).chats[0]
        ent = await self.client.get_entity(tail)
        if join and getattr(ent, "left", False):
            try:
                await self.client(JoinChannelRequest(ent))
            except Exception as e:
                log.info("Telegram: no se pudo entrar en %s (%s)", link, e)
        return ent

    def chat_title(self, link: str) -> str:
        """Comprueba el enlace (entrando si es una invitación) y devuelve su nombre."""
        async def go():
            ent = await self._chat(link, join=True)
            return getattr(ent, "title", None) or getattr(ent, "username", None) or link
        return self._run(go())

    @staticmethod
    def _video(m) -> dict | None:
        f = getattr(m, "file", None)
        if f is None:
            return None
        name = f.name or ""
        big = (f.mime_type or "").startswith("video/") and (f.size or 0) > 50 * 2**20
        if not (_VIDEO_NAME.search(name) or big) or _SAMPLE.search(name):
            return None
        return {"id": m.id, "name": name, "text": m.message or "", "size": f.size or 0}

    def videos(self, link: str, query: str = "", min_id: int = 0, limit: int = 100) -> list[dict]:
        """Videos del canal: con ``query`` los que Telegram encuentra con ese texto; si no, los últimos
        (sólo los más nuevos que ``min_id``). [{id, name, text, size}], del más nuevo al más viejo."""
        async def go():
            ent = await self._chat(link)
            out = []
            async for m in self.client.iter_messages(ent, search=query or None, min_id=min_id, limit=limit):
                v = self._video(m)
                if v:
                    out.append(v)
            return out
        return self._run(go(), timeout=180)

    def last_id(self, link: str) -> int:
        async def go():
            msgs = await self.client.get_messages(await self._chat(link), limit=1)
            return msgs[0].id if msgs else 0
        return self._run(go())

    def take(self, link: str, ids: list[int], dest: Path, progress=lambda what, done, total: None,
             stop=lambda: False, skip=lambda name, size: False, on_file=lambda path, info: None) -> list[Path]:
        """Descarga esos mensajes del canal, uno detrás de otro."""
        async def go():
            ent = await self._chat(link)
            got = []
            for mid in ids:
                if stop():
                    break
                m = await self.client.get_messages(ent, ids=mid)
                if not m or not m.file:
                    log.info("Telegram: el mensaje %s de %s ya no está", mid, link)
                    continue
                if skip(_safe(m.file.name or f"phono-{m.id}.mkv"), m.file.size or 0):
                    continue
                path = await self._download(m, Path(dest), progress, stop)
                got.append(path)
                await asyncio.get_running_loop().run_in_executor(None, on_file, path, {"id": mid})
            return got
        return self._run(go(), timeout=None)

    # --- pedir al bot -------------------------------------------------------------------
    def fetch(self, code: str, dest: Path, bot: str = BOT, progress=lambda what, done, total: None,
              stop=lambda: False, on_manual=lambda bot: None, skip=lambda name, size: False,
              on_file=lambda path, info: None, remind: str = "", on_seen=lambda bot, mid: None) -> list[tuple[Path, dict]]:
        """Pide ``code`` al bot y descarga lo que mande en ``dest`` (una película o los capítulos de
        una temporada). Devuelve [(ruta, info)].

        ``skip(nombre, tamaño)``: los que ya se bajaron antes (tras un reinicio) no se vuelven a bajar.
        ``on_file(ruta, info)``: se llama en cuanto termina cada archivo (en otro hilo).
        ``remind``: si se pasa, cuando toca pulsar el anuncio te llega un recordatorio a «Mensajes guardados»
        con ese nombre.
        ``on_seen(bot, id)``: en cuanto llega cada archivo, antes de bajarlo, para poder retomarlo tras un
        reinicio sin volver a pedirlo (ni pulsar otra vez el anuncio)."""
        return self._run(self._fetch(bot, code, Path(dest), progress, stop, on_manual, skip, on_file, remind, on_seen),
                         timeout=None)

    async def _remind(self, bot: str, what: str) -> None:
        """Recordatorio en «Mensajes guardados». Un mensaje normal a ti mismo no suena; uno programado
        («recordatorio») sí avisa, por eso sale programado para dentro de unos segundos."""
        text = (f"🚨 Te toca pulsar el anuncio\n\n{what}\n\n"
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
                     on_file=lambda p, i: None, remind="", on_seen=lambda b, i: None):
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
                    await asyncio.get_running_loop().run_in_executor(None, on_seen, name, m.id)
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
            size = m.file.size or 0
            try:  # 1) varias conexiones; 2) una conexión con varios trozos; 3) de uno en uno
                await self._multi(m, part, size, each)
            except InterruptedError:
                raise
            except Exception as e:
                log.warning("Telegram: con varias conexiones falló (%s); sigo con una", e)
                try:
                    await self._parallel(m, part, size, each)
                except InterruptedError:
                    raise
                except Exception as e:
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
        """Descarga por la conexión de siempre pidiendo ``PARALLEL`` trozos a la vez."""
        async def reader(i: int, workers: int, pieces: int):
            n, pos = len(range(i, pieces, workers)), i * PIECE
            if not n:
                return
            async for chunk in self.client.iter_download(m.media, offset=pos, stride=workers * PIECE, limit=n,
                                                         request_size=PIECE, chunk_size=PIECE, file_size=size):
                yield pos, chunk
                pos += workers * PIECE
        await self._pieces(part, size, each, PARALLEL, reader)

    async def _multi(self, m, part: Path, size: int, each) -> None:
        """Como tdl: abre ``CONNECTIONS`` conexiones propias al centro de datos donde está el archivo y
        reparte los trozos entre ellas (una sola conexión no pasa de unos 5 MB/s)."""
        from telethon import utils
        from telethon.errors import RPCError
        from telethon.tl import types
        from telethon.tl.functions.upload import GetFileRequest

        dc_id, location = utils.get_input_location(m.media)
        senders = await self._senders(dc_id or self.client.session.dc_id, CONNECTIONS)
        try:
            async def reader(i: int, workers: int, pieces: int):
                sender = senders[i % len(senders)]
                for k in range(i, pieces, workers):
                    req = GetFileRequest(location, offset=k * PIECE, limit=PIECE)
                    for attempt in range(3):
                        try:
                            r = await sender.send(req)
                            break
                        except (ConnectionError, asyncio.TimeoutError, RPCError) as e:
                            if attempt == 2 or (isinstance(e, RPCError) and "FLOOD" not in str(e) and "TIMEOUT" not in str(e)):
                                raise
                            await asyncio.sleep(2 * (attempt + 1))
                    if isinstance(r, types.upload.FileCdnRedirect):
                        raise ValueError("el archivo está en la red de reparto (CDN)")
                    yield k * PIECE, r.bytes
            await self._pieces(part, size, each, len(senders) * PER_CONNECTION, reader)
        finally:
            for s in senders:
                try:
                    await s.disconnect()
                except Exception:
                    pass

    async def _senders(self, dc_id: int, n: int) -> list:
        """``n`` conexiones nuevas al centro de datos ``dc_id`` con tu sesión (lo mismo que hace Telethon
        para una, pero varias)."""
        from telethon import functions
        from telethon.network import MTProtoSender
        from telethon.tl.alltlobjects import LAYER
        c = self.client
        same = dc_id == c.session.dc_id
        dc = await c._get_dc(dc_id)
        auth = None if same else await c(functions.auth.ExportAuthorizationRequest(dc_id))
        out = []
        try:
            for _ in range(n):
                s = MTProtoSender(c.session.auth_key if same else None, loggers=c._log)
                await s.connect(c._connection(dc.ip_address, dc.port, dc.id, loggers=c._log,
                                              proxy=c._proxy, local_addr=c._local_addr))
                out.append(s)
                if not same:
                    c._init_request.query = functions.auth.ImportAuthorizationRequest(id=auth.id, bytes=auth.bytes)
                    await s.send(functions.InvokeWithLayerRequest(LAYER, c._init_request))
        except Exception:
            for s in out:
                await s.disconnect()
            raise
        log.info("Telegram: %d conexiones al centro de datos %s", len(out), dc_id)
        return out

    async def _pieces(self, part: Path, size: int, each, workers: int, reader) -> None:
        """Reparte el archivo en trozos de ``PIECE`` entre ``workers`` tareas; cada trozo se escribe en su
        sitio. ``reader(i, workers, trozos)`` da (posición, bytes) de los trozos i, i+workers, i+2·workers…"""
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
                async for pos, chunk in reader(i, workers, pieces):
                    os.pwrite(fd, chunk, pos)
                    done += len(chunk)
                    each(done, size)
            tasks = [asyncio.ensure_future(worker(i)) for i in range(workers)]
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
