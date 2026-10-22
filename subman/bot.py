"""Tu bot de Videotecario en Telegram: además de avisarte, te deja pedir cosas desde ahí.

* Le escribes un título («Dune») y te contesta con las opciones (póster, año, de qué va) y un botón
  «⬇️ Descargar». En las series, te pregunta qué temporadas.
* ``/cola``: qué se está bajando, a qué velocidad y qué espera turno.
* ``/hoy``: el resumen del día cuando quieras (también llega solo por la noche, a la hora de Ajustes).
* Una vez al día mira si las series que tienes sacaron temporada nueva en Phono y te avisa con un botón.

Sólo te contesta a ti (al chat que conectaste en Ajustes); a cualquier otro lo ignora.
"""
from __future__ import annotations

import json
import logging
import re
import threading
import time

from . import downloads, notify, phono, tmdb

log = logging.getLogger(__name__)

SCHEMA = """
CREATE TABLE IF NOT EXISTS bot_kv (k TEXT PRIMARY KEY, v TEXT);   -- lo que el bot recuerda (avisos ya dados…)
"""
STATE = {"cola": "⏳", "buscando": "🔎", "pidiendo": "📨", "manual": "🚨", "descargando": "⬇️",
         "listo": "✅", "no-esta": "❌", "error": "⚠️"}
HELP = ("🎬 Soy tu bot de Videotecario.\n\n"
        "• Escríbeme el nombre de una película o serie y te muestro las opciones con un botón para descargarla.\n"
        "• /cola: qué se está bajando y qué espera turno.\n"
        "• /hoy: el resumen del día.\n\n"
        "También te aviso cuando te toque pulsar el anuncio, cuando algo llega a la biblioteca, si hay "
        "problemas y si una serie que tienes saca temporada nueva.")
_SEASON_DIR = re.compile(r"[\\/](?:Season|Temporada)\s*0*(\d{1,2})[\\/]", re.I)
_TMDBID = re.compile(r"\[tmdbid-(\d+)\]")


def label(kind: str, title: str, year=None, season=None) -> str:
    """«🎬 Película: Título (2026)» o «📺 Serie: Título (2026) · temporada 1»."""
    name = title + (f" ({year})" if year else "")
    if kind == "movie":
        return f"🎬 Película: {name}"
    return f"📺 Serie: {name}" + (f" · temporada {season}" if season else "")


class Bot:
    def __init__(self, app):
        self.app = app

    # --- ajustes y memoria -----------------------------------------------------------------
    def _conf(self):
        cfg = self.app.cfg()
        t = cfg.get("telegram", {})
        return cfg, t.get("bot_token") or "", str(t.get("chat_id") or "")

    def _kv(self, cfg):
        db = self.app.library(cfg).db
        db.executescript(SCHEMA)
        return db

    def get(self, cfg, k: str, default=None):
        r = self._kv(cfg).execute("SELECT v FROM bot_kv WHERE k=?", (k,)).fetchone()
        return r[0] if r else default

    def put(self, cfg, k: str, v) -> None:
        db = self._kv(cfg)
        db.execute("INSERT OR REPLACE INTO bot_kv (k, v) VALUES (?, ?)", (k, str(v)))
        db.commit()

    def say(self, text: str, **kw) -> bool:
        """Te escribe por el bot (si está conectado). No falla nunca: un aviso no puede parar nada."""
        cfg, token, chat = self._conf()
        if not (token and chat):
            return False
        try:
            notify.send(token, chat, text, **kw)
            return True
        except Exception as e:
            log.warning("bot: %s", e)
            return False

    # --- el bucle: lo que le escribes y lo que toca a su hora -------------------------------
    def run(self) -> None:
        offset = None
        while True:
            cfg, token, chat = self._conf()
            if not (token and chat):
                time.sleep(60)
                continue
            try:
                self.tick(cfg)
                if offset is None:
                    offset = int(self.get(cfg, "offset", 0) or 0)
                for u in notify.updates(token, offset, wait=50):
                    offset = u["update_id"] + 1
                    self.put(cfg, "offset", offset)
                    try:
                        self.handle(cfg, token, chat, u)
                    except Exception as e:
                        log.exception("bot: no pude atender un mensaje")
                        notify.send(token, chat, f"⚠️ Algo falló: {e}")
            except Exception as e:
                log.info("bot: %s", e)
                time.sleep(15)

    def tick(self, cfg) -> None:
        """Una vez al día, a la hora del resumen: temporadas nuevas y el resumen."""
        d = cfg["downloads"]
        hour = d.get("summary_hour", 22)
        today = time.strftime("%Y-%m-%d")
        if hour is None or int(hour) < 0 or time.localtime().tm_hour < int(hour) or self.get(cfg, "summary") == today:
            return
        self.put(cfg, "summary", today)
        threading.Thread(target=self._daily, args=(cfg,), name="bot-resumen", daemon=True).start()

    def _daily(self, cfg) -> None:
        try:
            self.new_seasons(cfg)
        except Exception as e:
            log.warning("bot: temporadas nuevas: %s", e)
        text = self.summary(cfg, since=time.time() - 24 * 3600, quiet=True)
        if text:
            self.say(text)

    # --- mensajes ---------------------------------------------------------------------------
    def handle(self, cfg, token: str, chat: str, u: dict) -> None:
        if "callback_query" in u:
            cb = u["callback_query"]
            if str(cb.get("from", {}).get("id")) != chat:
                return notify.answer(token, cb["id"], "Este bot es privado")
            return self.button(cfg, token, chat, cb)
        m = u.get("message") or {}
        if str(m.get("chat", {}).get("id")) != chat:
            return  # sólo tú
        text = (m.get("text") or "").strip()
        if not text:
            return
        cmd = text.split()[0].lower().split("@")[0]
        if cmd in ("/start", "/ayuda", "/help"):
            notify.send(token, chat, HELP)
        elif cmd == "/cola":
            notify.send(token, chat, self.queue(cfg))
        elif cmd == "/hoy":
            notify.send(token, chat, self.summary(cfg, since=time.time() - 24 * 3600))
        elif text.startswith("/"):
            notify.send(token, chat, "No conozco esa orden. " + HELP.split("\n\n", 1)[1])
        else:
            self.search(cfg, token, chat, text)

    def search(self, cfg, token: str, chat: str, query: str) -> None:
        if not cfg.get("tmdb", {}).get("api_key"):
            return notify.send(token, chat, "Falta la clave de TheMovieDB en Ajustes.")
        items = [c for c in self.app._discover().search(query)][:5]
        if not items:
            return notify.send(token, chat, f"No encontré nada con «{query}». Prueba con otro nombre.")
        dl = downloads.Downloads(self.app.library(cfg), cfg)
        for c in items:
            head = label(c["kind"], c["title"], c["year"]) + (f"  ⭐ {c['vote']}" if c.get("vote") else "")
            over = c.get("overview") or ""
            if len(over) > 350:
                over = over[:350].rsplit(" ", 1)[0] + "…"
            notes = []
            if c.get("have"):
                notes.append("✅ Ya la tienes en la biblioteca.")
            if dl.has(c["kind"], c["tmdb"]):
                notes.append("⏳ Ya está en Descargas.")
            caption = "\n\n".join(x for x in (head, over, " ".join(notes)) if x)
            data = f"d:{c['kind']}:{c['tmdb']}" if c["kind"] == "movie" else f"s:{c['tmdb']}"
            buttons = [[{"text": "⬇️ Descargar" if c["kind"] == "movie" else "⬇️ Elegir temporadas", "callback_data": data}]]
            try:
                if c.get("poster"):
                    notify.send_photo(token, chat, c["poster"], caption, buttons=buttons)
                    continue
            except Exception as e:
                log.info("bot: póster: %s", e)
            notify.send(token, chat, caption, buttons=buttons)

    def button(self, cfg, token: str, chat: str, cb: dict) -> None:
        data = cb.get("data") or ""
        parts = data.split(":")
        db = tmdb.TMDb(cfg["tmdb"]["api_key"], cfg["tmdb"].get("language") or "es-MX")
        if parts[0] == "d" and len(parts) == 3:  # una película (o una serie entera)
            kind, tid = parts[1], int(parts[2])
            info = db.movie(tid) if kind == "movie" else db.tv(tid)
            r = self.app.downloads_add({"kind": kind, "tmdb": tid, "title": info.get("name") or "", "year": info.get("year")})
            msg = "En la cola de descargas ✅" if r.get("ok") else r.get("error", "no se pudo")
            notify.answer(token, cb["id"], msg)
            if r.get("ok"):
                notify.send(token, chat, f"⏳ {label(kind, info.get('name') or '', info.get('year'))}\nEn la cola. Te aviso cuando te toque pulsar el anuncio.")
        elif parts[0] == "s" and len(parts) == 2:  # qué temporadas tiene Phono
            tid = int(parts[1])
            info = db.tv(tid)
            notify.answer(token, cb["id"], "Mirando en Phono…")
            r = self.app.downloads_seasons({"tmdb": tid, "title": info.get("name") or "", "year": info.get("year")})
            if not r.get("ok"):
                return notify.send(token, chat, f"❌ {info.get('name')}: {r.get('error')}")
            have = set(r["have"])
            rows, row = [], []
            for n in r["seasons"]:
                row.append({"text": f"✅ T{n}" if n in have else f"T{n}", "callback_data": f"t:{tid}:{n}"})
                if len(row) == 4:
                    rows.append(row)
                    row = []
            if row:
                rows.append(row)
            if len(set(r["seasons"]) - have) > 1:
                rows.append([{"text": "⬇️ Todas las que faltan", "callback_data": f"t:{tid}:all"}])
            notify.send(token, chat, f"{label('tv', info.get('name') or '', info.get('year'))}\n¿Qué temporada quieres? "
                        "(✅ = ya está en Descargas)", buttons=rows)
        elif parts[0] == "t" and len(parts) == 3:  # pedir temporada(s)
            tid = int(parts[1])
            info = db.tv(tid)
            if parts[2] == "all":
                r = self.app.downloads_seasons({"tmdb": tid, "title": info.get("name") or "", "year": info.get("year")})
                seasons = [n for n in r.get("seasons", []) if n not in set(r.get("have", []))]
            else:
                seasons = [int(parts[2])]
            r = self.app.downloads_add({"kind": "tv", "tmdb": tid, "title": info.get("name") or "",
                                        "year": info.get("year"), "seasons": seasons}) if seasons else {"ok": False, "error": "ya están todas"}
            notify.answer(token, cb["id"], "En la cola ✅" if r.get("ok") else r.get("error", "no se pudo"))
            if r.get("ok"):
                which = ", ".join(str(n) for n in seasons)
                notify.send(token, chat, f"⏳ {label('tv', info.get('name') or '', info.get('year'))}\n"
                            f"Temporada{'s' if len(seasons) > 1 else ''} {which} en la cola.")
        else:
            notify.answer(token, cb["id"])

    # --- textos -----------------------------------------------------------------------------
    def queue(self, cfg) -> str:
        st = self.app.dl
        rows = [r for r in downloads.Downloads(self.app.library(cfg), cfg).rows() if r["state"] not in ("listo", "cancelado")]
        lines = []
        if st.get("total"):
            pct = 100 * st["done"] / st["total"]
            speed = st.get("speed") or 0
            extra = f" · {speed / 2**20:.1f} MB/s" if speed else ""
            if speed:
                left = (st["total"] - st["done"]) / speed
                extra += f" · faltan {int(left // 60) + 1} min"
            lines.append(f"⬇️ Ahora: {st.get('message', '')}\n{pct:.0f} % de {st['total'] / 2**30:.2f} GB{extra}")
        elif st.get("message"):
            lines.append(f"Ahora: {st['message']}")
        order = ["manual", "descargando", "pidiendo", "buscando", "cola", "error", "no-esta"]
        rows.sort(key=lambda r: (order.index(r["state"]) if r["state"] in order else 9, r["created"], r["season"] or 0))
        for r in rows[:20]:
            lines.append(f"{STATE.get(r['state'], '•')} {label(r['kind'], r['title'], r['year'], r['season'])}")
        if len(rows) > 20:
            lines.append(f"… y {len(rows) - 20} más")
        return "\n".join(lines) if lines else "No hay nada en Descargas."

    def summary(self, cfg, since: float, quiet: bool = False) -> str:
        """El resumen del día. Con ``quiet``, nada si no pasó nada ni queda nada pendiente."""
        rows = downloads.Downloads(self.app.library(cfg), cfg).rows()
        done = [r for r in rows if r["state"] == "listo" and (r["updated"] or 0) >= since]
        waiting = [r for r in rows if r["state"] in ("cola", "buscando", "pidiendo", "descargando")]
        manual = [r for r in rows if r["state"] == "manual"]
        missing = [r for r in rows if r["state"] == "no-esta"]
        if quiet and not (done or waiting or manual):
            return ""
        out = ["🌙 Resumen del día"]
        if done:
            out.append("\n✅ Llegó a la biblioteca:")
            for r in done:
                n = len(json.loads(r["dest"] or "[]")) if r["kind"] == "tv" else 0
                out.append(f"• {label(r['kind'], r['title'], r['year'], r['season'])}" + (f" ({n} capítulos)" if n > 1 else ""))
        else:
            out.append("\nHoy no llegó nada nuevo.")
        if manual:
            out.append(f"\n🚨 Esperando que pulses el anuncio: {len(manual)}")
        out.append(f"⏳ En cola: {len(waiting)}")
        if missing:
            out.append(f"❌ Phono aún no tiene: {len(missing)}")
        free = downloads.free_gb(cfg["downloads"]["tmp_dir"])
        if free != float("inf"):
            out.append(f"💾 Espacio libre: {free:.0f} GB")
        return "\n".join(out)

    # --- temporadas nuevas ----------------------------------------------------------------
    def owned_seasons(self, cfg) -> dict[int, int]:
        """{número de TheMovieDB: última temporada que tienes} de las series con su número en la carpeta
        (las que ordenó Nombres o bajó Videotecario)."""
        out: dict[int, int] = {}
        for r in self.app.library(cfg).db.execute("SELECT path, season FROM media WHERE kind!='movie'"):
            m = _TMDBID.search(r["path"])
            if not m:
                continue
            s = r["season"]
            if s is None:
                ms = _SEASON_DIR.search(r["path"])
                s = int(ms.group(1)) if ms else None
            if s:
                tid = int(m.group(1))
                out[tid] = max(out.get(tid, 0), int(s))
        return out

    def new_seasons(self, cfg) -> None:
        """Si TheMovieDB dice que hay una temporada después de la última que tienes y Phono ya la tiene,
        te avisa (una vez por temporada) con un botón para pedirla."""
        if not cfg.get("tmdb", {}).get("api_key"):
            return
        db = tmdb.TMDb(cfg["tmdb"]["api_key"], cfg["tmdb"].get("language") or "es-MX")
        dl = downloads.Downloads(self.app.library(cfg), cfg)
        api = phono.Phono()
        for tid, last in self.owned_seasons(cfg).items():
            try:
                show = db.tv(tid)
                newer = sorted(n for n, eps in show["seasons"].items() if n > last and eps)
                newer = [n for n in newer if not self.get(cfg, f"season:{tid}:{n}") and n not in dl.seasons_in(tid)]
                if not newer:
                    continue
                card = dl.find({"kind": "tv", "tmdb": tid, "title": show["name"], "year": show["year"]}, api, db)
                if not card:
                    continue
                there = phono.seasons(api.buttons(card["content_id"]))
                for n in newer:
                    if n in there:
                        self.put(cfg, f"season:{tid}:{n}", int(time.time()))
                        self.say(f"🆕 Temporada nueva en Phono\n\n{label('tv', show['name'], show['year'], n)}\n"
                                 f"Tienes hasta la temporada {last}.",
                                 buttons=[[{"text": f"⬇️ Pedir la temporada {n}", "callback_data": f"t:{tid}:{n}"}]])
                time.sleep(2)  # sin prisa: no cargar a Phono
            except Exception as e:
                log.info("bot: temporadas nuevas de %s: %s", tid, e)


def bar(frac: float, width: int = 12) -> str:
    n = max(0, min(width, round(frac * width)))
    return "▓" * n + "░" * (width - n)


class Status:
    """Un solo mensaje por descarga en tu bot, que se va editando: 🚨 el anuncio → ⬇️ descargando
    (capítulo, barra, velocidad) → ✅ finalizado. Los recordatorios ⏰ son mensajes aparte (para que
    suenen) y se borran en cuanto empieza a bajar. Sin bot conectado no hace nada."""
    EVERY = 6  # segundos entre ediciones (Telegram no deja editar mucho más seguido)

    def __init__(self, cfg: dict):
        t = cfg.get("telegram", {})
        on = t.get("bot_token") and t.get("chat_id") and cfg["downloads"].get("remind", True)
        self.token, self.chat = (t["bot_token"], t["chat_id"]) if on else (None, None)
        self.mid = None
        self.want = self.shown = None
        self.final = False
        self.nags: list[int] = []
        self.cv = threading.Condition()
        if self.token:
            threading.Thread(target=self._run, name="bot-estado", daemon=True).start()

    def show(self, text: str, buttons: list[list[dict]] | None = None, final: bool = False) -> None:
        if not self.token:
            return
        with self.cv:
            if self.final:
                return
            self.want = (text, buttons)
            self.final = final
            self.cv.notify()

    def close(self) -> None:
        """Termina: lo que quede por mostrar se muestra y el hilo se acaba."""
        if self.token:
            with self.cv:
                self.final = True
                self.cv.notify()

    def nag(self, text: str, link: tuple[str, str]) -> None:
        """Recordatorio aparte (un mensaje nuevo sí suena; una edición no)."""
        if not self.token or self.final:
            return
        try:
            self.nags.append(notify.send(self.token, self.chat, text, link=link)["message_id"])
        except Exception as e:
            log.warning("bot: %s", e)

    def _run(self) -> None:
        while True:
            with self.cv:
                while self.want == self.shown:
                    if self.final:
                        return self._clean()
                    self.cv.wait(60)
                want, final = self.want, self.final
            text, buttons = want
            try:
                if self.mid is None:
                    self.mid = notify.send(self.token, self.chat, text, buttons=buttons)["message_id"]
                else:
                    notify.edit(self.token, self.chat, self.mid, text, buttons=buttons)
                self.shown = want
            except Exception as e:
                log.info("bot: estado: %s", e)
                if "Too Many Requests" in str(e):
                    time.sleep(self.EVERY)
                else:
                    self.shown = want  # no insistir con el mismo texto
            if text.startswith("⬇️"):
                self._clean()
            if not final:
                time.sleep(self.EVERY)

    def _clean(self) -> None:
        while self.nags:
            notify.delete(self.token, self.chat, self.nags.pop())
