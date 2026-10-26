"""Interfaz web + trabajador en segundo plano.

Un solo proceso: un hilo recorre la biblioteca cada ``interval_minutes`` (o cuando se pulsa
"Procesar ahora") y un servidor HTTP sirve la página y una pequeña API JSON. Pensado para la
red de casa (y para entrar desde fuera con Tailscale): si en Ajustes se pone usuario y
contraseña, la página los pide (ver auth.py).
"""
from __future__ import annotations

import collections
import json
import logging
import logging.handlers
import os
import re
import shutil
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import __version__, auth, autoid, bot, config, convert, diagnose, discover, downloads, duplicates, friends, notices, formats, gemini, identify, jellyfin, manager, notify, peek, phono, providers, series, tmdb, updater
from .library import Library

log = logging.getLogger(__name__)
STATIC = Path(__file__).parent / "static"
MEDIA_ROOT = os.environ.get("SUBMAN_MEDIA_ROOT", "/media")
SECRET_FIELDS = ("api_key", "password", "api_hash", "bot_token")
# lo que se sirve sin haber entrado (la pantalla de entrada necesita el icono)
PUBLIC = {"/manifest.webmanifest": ("manifest.webmanifest", "application/manifest+json"),
          "/icon.svg": ("icon.svg", "image/svg+xml"),
          "/icon-192.png": ("icon-192.png", "image/png"),
          "/icon-512.png": ("icon-512.png", "image/png"),
          "/apple-touch-icon.png": ("apple-touch-icon.png", "image/png"),
          "/sw.js": ("sw.js", "text/javascript; charset=utf-8")}  # las notificaciones al móvil
MASK = "••••••••"
# lo que un amigo (entrado con su usuario de Jellyfin) puede guardar en Descubrir, además de pedir
FRIEND_POST = ("/api/discover/mark", "/api/discover/taste", "/api/discover/taste/reset")


class LogBuffer(logging.Handler):
    def __init__(self, size: int = 1000):
        super().__init__()
        self.lines: collections.deque = collections.deque(maxlen=size)
        self.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S"))

    def emit(self, record):
        self.lines.append(self.format(record))


class App:
    def __init__(self, config_path: str):
        self.config_path = config_path
        self.who = threading.local()  # .friend = el amigo de Jellyfin que hace esta petición (None = tú)
        self.wake = threading.Event()
        self.lock = threading.Lock()
        self.state = {"running": False, "current": "", "done": 0, "total": 0,
                      "last_start": None, "last_end": None, "next_run": None, "message": "", "scan": None}
        self.dupes = {"running": False, "message": "", "groups": None, "at": None}
        self.names = {"running": False, "message": "", "result": None, "at": None, "auto": None, "stop": False}
        self.series = {"running": False, "message": "", "error": None, "result": None, "at": None, "auto": None}
        self.auto = {"message": "", "last": None}
        self.upd = {"running": False, "phase": None, "message": "", "asked": None, "bytes": None, "eta": None}
        self.auto_wake = threading.Event()
        self.auto_lock = threading.Lock()
        self.fmt = {"running": False, "message": "", "done": 0, "total": 0, "error": None, "stop": False}
        self.conv = {"current": "", "what": "", "progress": 0.0, "message": "apagado", "started": None,
                     "done": 0, "stop": False}
        self.conv_wake = threading.Event()
        self.dl = {"message": "apagado", "current": None, "what": "", "done": 0, "total": 0, "last": 0.0, "stop": None}
        self.dl_wake = threading.Event()
        self.tg = None          # telegram.Telegram (se crea al poner el API ID y el API hash)
        self.tg_key = None
        self.tg_lock = threading.Lock()
        self.logs = LogBuffer()
        logging.getLogger().addHandler(self.logs)
        try:  # registro también en /config/subman.log (sobrevive a reinicios)
            fh = logging.handlers.RotatingFileHandler(Path(config_path).parent / "subman.log",
                                                      maxBytes=2_000_000, backupCount=3, encoding="utf-8")
            fh.setFormatter(self.logs.formatter)
            logging.getLogger().addHandler(fh)
            self._load_log_tail(Path(config_path).parent / "subman.log")
        except OSError as e:
            log.warning("no se puede escribir el registro en disco: %s", e)

    def _load_log_tail(self, path: Path) -> None:
        if path.exists():
            lines = path.read_text(encoding="utf-8", errors="replace").splitlines()[-300:]
            self.logs.lines.extend(lines)

    # --- configuración -------------------------------------------------------------
    def cfg(self) -> dict:
        return config.load(self.config_path)

    def library(self, cfg: dict | None = None) -> Library:
        return Library((cfg or self.cfg())["database"])

    # --- trabajador ----------------------------------------------------------------
    def worker(self) -> None:
        while True:
            cfg = self.cfg()
            blocking = config.blocking(cfg)
            if not cfg["enabled"] or blocking:
                self.state.update(message="en pausa: actívalo en Ajustes" if not cfg["enabled"]
                                  else "; ".join(blocking), next_run=None)
                self.wake.wait(30)
                self.wake.clear()
                continue
            self.run_once(cfg)
            nxt = time.time() + cfg["interval_minutes"] * 60
            self.state.update(next_run=nxt)
            self.wake.wait(max(1, nxt - time.time()))
            self.wake.clear()

    def run_once(self, cfg: dict) -> None:
        self.state.update(running=True, last_start=time.time(), done=0, total=0, message="escaneando")
        try:
            settings = config.settings(cfg, self.config_path)
            provs = providers.load(config.active_providers(cfg))
            lib = self.library(cfg)
            log.info("buscando videos en %s", ", ".join(cfg["library"]))
            stats = lib.scan(cfg["library"], progress=lambda n: self.state.update(
                message=f"buscando videos: {n} encontrados"), exclude=config.excluded(cfg))
            log.info("escaneo: %s", stats)
            self.state.update(scan={"por_carpeta": stats["por_carpeta"], "ignorados": stats["ignorados"]})
            items = lib.media(order="mtime DESC")  # lo más reciente primero (lo que acaba de llegar)
            self.state.update(total=len(items), message="trabajando")
            changed = 0
            for n, m in enumerate(items):
                self.state.update(current=m.label, done=n)
                try:
                    res = manager.process(m, lib, provs, settings)
                    if res:
                        log.info("%s: %s", m.label, res)
                        changed += sum(v in ("ok", "revisar") for v in res.values())
                except Exception as e:
                    log.exception("%s: error", m.label)
                    for lang in settings.languages:  # para no repetirlo en cada pasada
                        row = lib.subtitle_status(m.id, lang)
                        if not row or row["status"] != "ok":
                            lib.set_subtitle(m.id, lang, row["path"] if row else None,
                                             row["source"] if row else "", "error", str(e)[:500])
                if not self.cfg()["enabled"]:
                    log.info("pausado desde la web")
                    break
            self.state.update(done=len(items), message="terminado")
            jf = cfg.get("jellyfin", {})
            if changed and jf.get("url") and jf.get("api_key") and not settings.dry_run:
                try:
                    jellyfin.refresh(jf["url"], jf["api_key"])
                except Exception as e:
                    log.warning("Jellyfin: no se pudo avisar: %s", e)
        except Exception as e:
            log.exception("fallo en la pasada")
            self.state.update(message=f"error: {e}")
        finally:
            self.state.update(running=False, current="", last_end=time.time())

    # --- API -----------------------------------------------------------------------
    def api_config(self) -> dict:
        cfg = self.cfg()
        pub = json.loads(json.dumps(cfg))
        for prov in [*pub.get("providers", {}).values(), pub.get("jellyfin", {}), pub.get("tmdb", {}),
                     pub.get("gemini", {}), pub.get("telegram", {})]:
            for f in SECRET_FIELDS:
                if f in prov:
                    prov[f] = MASK if prov.get(f) else ""
        a = pub.pop("access", {}) or {}
        pub["access"] = {"user": a.get("user", ""), "password": MASK if a.get("password_hash") else "",
                         "on": auth.enabled(cfg)}
        return {"config": pub, "problems": config.problems(cfg), "media_root": MEDIA_ROOT}

    def save_config(self, new: dict) -> dict:
        with self.lock:
            cfg = self.cfg()
            for k in ("enabled", "library", "languages", "candidates", "keep_review", "sync_existing",
                      "retry_hours", "archive_languages", "dry_run", "interval_minutes", "duplicates_dir", "discard_dir",
                      "timezone"):
                if k in new:
                    cfg[k] = new[k]
            sections = [(cfg.setdefault("providers", {}).setdefault(name, {}), values)
                        for name, values in new.get("providers", {}).items()]
            for name in ("jellyfin", "tmdb", "gemini"):
                if name in new:
                    sections.append((cfg.setdefault(name, {}), new[name]))
            for cur, values in sections:
                for k, v in values.items():
                    if k in SECRET_FIELDS and v == MASK:
                        continue  # sin cambios
                    cur[k] = v.strip() if isinstance(v, str) else v
            if "access" in new:
                pw = new["access"].get("password") or ""
                auth.set_access(cfg, new["access"].get("user", ""), None if pw in ("", MASK) else pw)
            cfg["candidates"] = int(cfg["candidates"])
            cfg["interval_minutes"] = max(5, int(cfg["interval_minutes"]))
            cfg["retry_hours"] = float(cfg["retry_hours"])
            config.save(self.config_path, cfg)
        updater.apply_timezone(cfg.get("timezone", ""))
        log.info("ajustes guardados")
        self.wake.set()
        return self.api_config()

    def test_opensubtitles(self, values: dict) -> dict:
        stored = self.cfg()["providers"].get("opensubtitles", {})
        creds = {k: (stored.get(k, "") if values.get(k) == MASK else values.get(k, "")) for k in
                 ("api_key", "username", "password")}
        if not creds["api_key"]:
            return {"ok": False, "error": "falta la API key"}
        try:
            info = providers.OpenSubtitles(**creds).test()
            return {"ok": True, **info}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def test_jellyfin(self, values: dict) -> dict:
        stored = self.cfg().get("jellyfin", {})
        key = stored.get("api_key", "") if values.get("api_key") == MASK else values.get("api_key", "")
        url = values.get("url", "").strip()
        if not url or not key:
            return {"ok": False, "error": "faltan la dirección o la API key"}
        try:
            return {"ok": True, **jellyfin.test(url, key)}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def _keep_key(self, section: str, value: str) -> None:
        """Si la clave nueva funciona, se guarda ya (si no, se perdía al cambiar de pestaña sin «Guardar»)."""
        if value and value != MASK and value.strip() != self.cfg().get(section, {}).get("api_key"):
            self.save_config({section: {"api_key": value}})

    def _key(self, section: str, value: str) -> str:
        return self.cfg().get(section, {}).get("api_key", "") if value == MASK else (value or "").strip()

    def test_tmdb(self, values: dict) -> dict:
        key = self._key("tmdb", values.get("api_key"))
        if not key:
            return {"ok": False, "error": "falta la clave"}
        try:
            res = {"ok": True, **tmdb.TMDb(key).test()}
            self._keep_key("tmdb", values.get("api_key"))
            return res
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def test_gemini(self, values: dict) -> dict:
        key = self._key("gemini", values.get("api_key"))
        if not key:
            return {"ok": False, "error": "falta la clave"}
        try:
            res = {"ok": True, **gemini.Gemini(key).test()}
            self._keep_key("gemini", values.get("api_key"))
            return res
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def folders(self, path: str) -> dict:
        root = Path(MEDIA_ROOT).resolve()
        p = Path(path or MEDIA_ROOT).resolve()
        if root != p and root not in p.parents:
            p = root
        dirs = []
        try:
            for d in sorted(p.iterdir(), key=lambda x: x.name.lower()):
                if d.is_dir() and not d.name.startswith(("@", ".", "#")):
                    dirs.append({"name": d.name, "path": str(d)})
        except OSError as e:
            return {"path": str(p), "parent": None, "dirs": [], "error": str(e)}
        parent = str(p.parent) if p != root else None
        return {"path": str(p), "parent": parent, "dirs": dirs}

    def library_list(self) -> dict:
        cfg = self.cfg()
        lib = self.library(cfg)
        st = lib.statuses()
        items = []
        for m in lib.media():
            langs = {}
            for lang in cfg["languages"]:
                r = st.get(m.id, {}).get(lang)
                langs[lang] = {"status": r["status"] if r else "pendiente",
                               "source": r["source"] if r else "",
                               "updated": r["updated"] if r else None,
                               "reason": diagnose.reason(r, m.label)["code"]}
            items.append({"id": m.id, "label": m.label, "kind": m.kind, "title": m.title,
                          "season": m.season, "episode": m.episode, "path": m.path, "langs": langs})
        return {"items": items, "languages": cfg["languages"]}

    def item(self, media_id: int) -> dict:
        lib = self.library()
        m = lib.media("id=?", (media_id,))
        if not m:
            return {"error": "no existe"}
        out = {"id": media_id, "label": m[0].label, "path": m[0].path, "langs": {}}
        for lang, r in lib.statuses().get(media_id, {}).items():
            detail = json.loads(r["detail"]) if r["detail"] and r["detail"].startswith("{") else {"text": r["detail"]}
            out["langs"][lang] = {"status": r["status"], "path": r["path"], "source": r["source"],
                                  "updated": r["updated"], "detail": detail}
        return out

    def retry(self, media_id: int) -> dict:
        self.library().reset(media_id)
        self.wake.set()
        return {"ok": True}

    def accept(self, media_id: int, lang: str) -> dict:
        lib = self.library()
        r = lib.subtitle_status(media_id, lang)
        if not r or not r["path"]:
            return {"ok": False, "error": "no hay subtítulo guardado que aceptar"}
        lib.set_subtitle(media_id, lang, r["path"], r["source"], "ok", r["detail"])
        log.info("aceptado a mano: %s", r["path"])
        return {"ok": True}

    def reject(self, media_id: int, lang: str) -> dict:
        cfg = self.cfg()
        lib = self.library(cfg)
        r = lib.subtitle_status(media_id, lang)
        if not r:
            return {"ok": False, "error": "nada que rechazar"}
        settings = config.settings(cfg, self.config_path)
        if r["path"] and Path(r["path"]).exists():
            manager._move(Path(r["path"]), settings.trash_dir, settings, "rechazado a mano")
        if r["source"] and not r["source"].startswith("local:"):
            lib.reject(media_id, lang, r["source"])
        lib.reset(media_id, lang)
        self.wake.set()  # que busque otro
        return {"ok": True}

    def trash(self) -> dict:
        cfg = self.cfg()
        settings = config.settings(cfg, self.config_path)
        out = []
        for kind, root in (("papelera", settings.trash_dir), ("archivo", settings.archive_dir)):
            base = Path(root)
            if not base.exists():
                continue
            for f in sorted(base.rglob("*")):
                if f.is_file():
                    st = f.stat()
                    out.append({"kind": kind, "path": str(f), "rel": str(f.relative_to(base)),
                                "size": st.st_size, "mtime": st.st_mtime})
        return {"files": out[-2000:], "total": len(out)}

    def restore(self, path: str) -> dict:
        cfg = self.cfg()
        settings = config.settings(cfg, self.config_path)
        f = Path(path).resolve()
        for root in (Path(settings.trash_dir).resolve(), Path(settings.archive_dir).resolve()):
            if root in f.parents:
                rel = f.relative_to(root)
                if root == Path(settings.archive_dir).resolve():
                    rel = Path(*rel.parts[1:])  # archivo/<idioma>/...
                break
        else:
            return {"ok": False, "error": "ruta fuera de la papelera"}
        lib_root = next((Path(r) for r in cfg["library"] if Path(r).name == rel.parts[0]), None)
        if lib_root is None:
            return {"ok": False, "error": f"la carpeta {rel.parts[0]} ya no está en la biblioteca"}
        dest = lib_root / Path(*rel.parts[1:])
        if dest.exists():
            dest = dest.with_name(dest.stem + ".restaurado" + dest.suffix)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(f), str(dest))
        log.info("recuperado %s -> %s", f, dest)
        return {"ok": True, "dest": str(dest)}

    def status(self) -> dict:
        cfg = self.cfg()
        counts = collections.Counter()
        reasons = []
        try:
            lib = self.library(cfg)
            media = lib.media()
            total = len(media)
            st = lib.statuses()
            for m in media:
                for lang in cfg["languages"]:
                    r = st.get(m.id, {}).get(lang)
                    counts[r["status"] if r else "pendiente"] += 1
                    reasons.append(diagnose.reason(r, m.label))
        except Exception:
            log.exception("estado")
            total = 0
        return {"version": __version__, "state": self.state, "enabled": cfg["enabled"],
                "problems": config.problems(cfg), "media": total, "counts": counts,
                "missing": diagnose.summary(reasons), "auto": {**self.auto, "settings": cfg["auto"]}}

    # --- formatos (qué reproduce Jellyfin directo) ----------------------------------
    def formats_scan(self) -> dict:
        if self.fmt["running"]:
            return {"ok": True}
        self.fmt.update(running=True, message="empezando", error=None, stop=False, done=0, total=0)

        def work():
            try:
                cfg = self.cfg()
                lib = self.library(cfg)
                if not self.state["running"] and cfg["library"]:  # lista de videos al día (la pasada ya la hace)
                    lib.scan(cfg["library"], exclude=config.excluded(cfg), progress=lambda n: self.fmt.update(
                        message=f"buscando videos: {n} encontrados"))
                res = formats.Formats(lib).scan(
                    progress=lambda done, total, name: self.fmt.update(done=done, total=total, message=name),
                    stop=lambda: self.fmt["stop"])
                log.info("formatos: %d videos mirados de %d", res["mirados"], res["total"])
            except Exception as e:
                log.exception("formatos")
                self.fmt.update(error=str(e))
            finally:
                self.fmt.update(running=False, message="")

        threading.Thread(target=work, name="formatos", daemon=True).start()
        return {"ok": True}

    def formats_state(self) -> dict:
        cfg = self.cfg()
        lib = self.library(cfg)
        formats.Formats(lib)
        lib.db.executescript(convert.SCHEMA)
        failed = {r["path"]: r["error"] for r in lib.db.execute("SELECT path, error FROM convert_failed")}
        out = {**self.fmt, **formats.Formats(lib).summary(cfg["convert"]["allow_hevc"])}
        for i in out["items"]:
            if i["path"] in failed:
                i["failed"] = failed[i["path"]]
        return {**out, "conv": self.conv, "settings": cfg["convert"], "failed": len(failed),
                "converted": convert.converted_list(lib)}

    def convert_settings(self, b: dict) -> dict:
        with self.lock:
            cfg = self.cfg()
            c = cfg["convert"]
            for k in ("enabled", "allow_hevc", "night_only"):
                if k in b:
                    c[k] = bool(b[k])
            if "groups" in b:
                c["groups"] = [g for g in ("caja", "sonido", "video") if g in b["groups"]]
            for k in ("night_start", "night_end"):
                if k in b:
                    c[k] = max(0, min(23, int(b[k])))
            if "originals_dir" in b and b["originals_dir"].strip():
                d = Path(b["originals_dir"].strip()).resolve()
                root = Path(MEDIA_ROOT).resolve()
                if root != d and root not in d.parents:
                    return {"ok": False, "error": f"la carpeta de originales tiene que estar dentro de {MEDIA_ROOT}"}
                if any(d == Path(r).resolve() or Path(r).resolve() in d.parents for r in cfg["library"]):
                    return {"ok": False, "error": "la carpeta de originales no puede estar dentro de una carpeta de la biblioteca"}
                c["originals_dir"] = str(d)
            if "min_free_gb" in b:
                c["min_free_gb"] = max(5, int(b["min_free_gb"]))
            config.save(self.config_path, cfg)
        if not c["enabled"]:
            self.conv["stop"] = True  # para el que esté a medias (se borra lo a medio hacer, el original no se toca)
        log.info("conversión: %s", "encendida" if c["enabled"] else "apagada")
        self.conv_wake.set()
        return {"ok": True}

    def convert_retry(self) -> dict:
        lib = self.library()
        lib.db.executescript(convert.SCHEMA)
        lib.db.execute("DELETE FROM convert_failed")
        lib.db.commit()
        self.conv_wake.set()
        return {"ok": True}

    def convert_restore(self, row_id: int) -> dict:
        cfg = self.cfg()
        if self.conv["current"]:
            return {"ok": False, "error": "espera a que termine el que está convirtiendo, o apaga la conversión"}
        lib = self.library(cfg)
        try:
            dest = convert.restore(lib, row_id, cfg["convert"]["originals_dir"], MEDIA_ROOT, cfg["library"])
        except (ValueError, OSError) as e:
            return {"ok": False, "error": str(e)}
        # que no lo vuelva a convertir solo
        lib.db.execute("INSERT OR REPLACE INTO convert_failed VALUES (?,?,?)",
                       (dest, "lo devolviste tú: no se vuelve a convertir solo", time.time()))
        lib.db.commit()
        self._jellyfin_refresh(cfg)
        return {"ok": True, "dest": dest}

    def converter(self) -> None:
        """Hilo que convierte uno a uno lo que falta, mientras esté encendido."""
        last_check, cleaned, since_refresh = 0.0, False, 0
        while True:
            cfg = self.cfg()
            c = cfg["convert"]
            self.conv["stop"] = False
            wait = 0
            if not c["enabled"]:
                self.conv.update(message="apagado", current="")
                wait = 3600
            elif not cfg["library"]:
                self.conv.update(message="elige primero las carpetas de la biblioteca en Ajustes")
                wait = 300
            elif not convert.in_window(c):
                self.conv.update(message=f"esperando la noche (de {c['night_start']}:00 a {c['night_end']}:00)")
                wait = 300
            if wait:
                if since_refresh:
                    self._jellyfin_refresh(cfg)
                    since_refresh = 0
                self.conv_wake.wait(wait)
                self.conv_wake.clear()
                continue
            lib = self.library(cfg)
            lib.db.executescript(convert.SCHEMA)
            try:
                if not cleaned:
                    n = convert.leftovers(cfg["library"])
                    cleaned = True
                    if n:
                        log.info("conversión: quitados %d archivos a medio convertir de antes de un reinicio", n)
                if time.time() - last_check > 1800 and not self.fmt["running"]:  # videos nuevos (p. ej. de Sonarr)
                    self.conv.update(message="mirando si hay videos nuevos")
                    if not self.state["running"]:
                        lib.scan(cfg["library"], exclude=config.excluded(cfg))
                    formats.Formats(lib).scan(stop=lambda: self.conv["stop"])
                    last_check = time.time()
                item = self._next_to_convert(lib, c)
            except Exception as e:
                log.exception("conversión")
                self.conv.update(message=f"error: {e}")
                self.conv_wake.wait(600)
                self.conv_wake.clear()
                continue
            if not item:
                self.conv.update(message="al día: no queda nada por convertir", current="")
                if since_refresh:
                    self._jellyfin_refresh(cfg)
                    since_refresh = 0
                last_check = 0  # la próxima vez, mirar si llegó algo
                self.conv_wake.wait(1800)
                self.conv_wake.clear()
                continue
            self.conv.update(current=item["label"], what=item["grp"], progress=0.0, started=time.time(),
                             message="convirtiendo", path=item["path"])
            try:
                convert.convert(lib, item["path"], item["info"], cfg, cfg["library"], MEDIA_ROOT,
                                progress=lambda x: self.conv.update(progress=x), stop=lambda: self.conv["stop"])
                self.conv["done"] += 1
                since_refresh += 1
                if since_refresh >= 20:
                    self._jellyfin_refresh(cfg)
                    since_refresh = 0
            except convert.Stopped:
                log.info("conversión parada: %s queda como estaba", item["label"])
            except convert.NoSpace as e:
                log.warning("conversión en pausa: %s", e)
                self.conv.update(message=f"en pausa: {e}", current="")
                self.conv_wake.wait(1800)
                self.conv_wake.clear()
            except Exception as e:
                log.warning("no se pudo convertir %s: %s", item["path"], e)
                lib.db.execute("INSERT OR REPLACE INTO convert_failed VALUES (?,?,?)", (item["path"], str(e)[:300], time.time()))
                lib.db.commit()
            finally:
                self.conv.update(current="", what="", progress=0.0, path="")

    def _next_to_convert(self, lib, c: dict) -> dict | None:
        failed = {r[0] for r in lib.db.execute("SELECT path FROM convert_failed")}
        best = None
        for r in lib.db.execute("SELECT f.path, f.info, m.size, m.kind, m.title, m.season, m.episode, m.year "
                                "FROM formats f JOIN media m ON m.path = f.path"):
            if r["path"] in failed:
                continue
            info = json.loads(r["info"])
            grp = formats.group(info, c["allow_hevc"])
            if grp not in c["groups"] or grp not in ("caja", "sonido", "video") or "why_video" not in info:
                continue
            key = (("caja", "sonido", "video").index(grp), r["size"] or 0)  # lo rápido primero
            if best is None or key < best[0]:
                label = (f"{r['title']} S{r['season']:02d}E{r['episode']:02d}" if r["kind"] == "episode"
                         else f"{r['title']} ({r['year']})" if r["year"] else r["title"])
                best = (key, {"path": r["path"], "info": info, "grp": grp, "label": label})
        return best[1] if best else None


    # --- actualizarse sola (Watchtower) ----------------------------------------------
    def update_state(self) -> dict:
        cfg = self.cfg()
        u = self.upd
        return {**u, "elapsed": time.time() - u["asked"] if u["running"] and u["asked"] else None,
                "build": updater.BUILD, "built": updater.BUILT, "watchtower": updater.available(),
                "settings": cfg["update"], "now": time.strftime("%H:%M"), "timezone": cfg.get("timezone", "")}

    def update_now(self) -> dict:
        if not updater.available():
            return {"ok": False, "error": "falta Watchtower en el proyecto de Container Manager (mira INSTALAR.md)"}
        if self.upd["running"]:
            return {"ok": True}
        self.upd.update(running=True, phase="buscando", message="buscando versión nueva", asked=time.time(),
                        bytes=None, eta=None)

        def work():
            try:
                try:
                    found = updater.check()
                except Exception as e:  # sin respuesta de ghcr.io: que lo mire Watchtower, sin saber el tamaño
                    log.info("actualización: no se pudo mirar el tamaño en ghcr.io: %s", e)
                    found = {"new": True, "bytes": None}
                if not found["new"]:
                    self.upd.update(message="ya tienes la última versión")
                    return
                learned = self._auto_load().get("update_learned") or {}
                nbytes = found["bytes"]
                eta = updater.estimate(nbytes if nbytes is not None else 150_000_000, learned)
                self.upd.update(phase="descargando", bytes=nbytes, eta=eta, asked=time.time(),
                                message="descargando versión nueva")
                self._auto_save(update_pending={"asked": self.upd["asked"], "bytes": nbytes, "from": updater.BUILD})
                log.info("actualización: versión nueva, %s MB por bajar, unos %d s",
                         "?" if nbytes is None else round(nbytes / 1e6), eta)
                if updater.update():
                    self._auto_save(update_pending=None)  # Watchtower contestó: no había nada que reiniciar
                    self.upd.update(message="ya tienes la última versión")
                else:
                    self.upd.update(phase="reiniciando", message="reiniciando")
            except Exception as e:
                self._auto_save(update_pending=None)
                self.upd.update(message=f"no se pudo: {e}")
            finally:
                self.upd.update(running=False, phase=None)

        threading.Thread(target=work, name="actualizar", daemon=True).start()
        return {"ok": True}

    def update_finished(self) -> None:
        """Al arrancar: si venimos de una actualización, apunta cuánto tardó para calcular mejor la próxima."""
        st = self._auto_load()
        p = st.get("update_pending")
        if not p:
            return
        took = time.time() - p["asked"]
        if p.get("from") != updater.BUILD and 0 < took < 3600:
            learned = updater.learn(st.get("update_learned") or {}, p.get("bytes") or 0, took)
            self._auto_save(update_pending=None, update_learned=learned)
            self.upd.update(message=f"actualizado en {round(took)} s", asked=time.time())
            log.info("actualización: terminada en %d s", took)
        else:
            self._auto_save(update_pending=None)

    def update_settings(self, b: dict) -> dict:
        with self.lock:
            cfg = self.cfg()
            if "auto" in b:
                cfg["update"]["auto"] = bool(b["auto"])
            if "hour" in b:
                cfg["update"]["hour"] = max(0, min(23, int(b["hour"])))
            config.save(self.config_path, cfg)
        return {"ok": True, **self.update_state()}

    def updates(self) -> None:
        """Hilo: una vez al día, a la hora elegida, pide la versión nueva (si hay, se reinicia).
        Si a esa hora se está convirtiendo un video, espera a que acabe para no perderlo."""
        due = None
        while True:
            time.sleep(300)
            try:
                cfg = self.cfg()
                u = cfg["update"]
                today = time.strftime("%Y-%m-%d")
                if not u["auto"] or not updater.available() or self._auto_load().get("updated") == today:
                    continue
                if due is None and time.localtime().tm_hour == int(u["hour"]):
                    due = time.time()
                if due is None:
                    continue
                busy = (self.conv["current"] or self.names["running"] or self.dupes["running"] or self.state["running"]
                        or self.series["running"])
                if busy and time.time() - due < 20 * 3600:
                    continue
                self._auto_save(updated=today)
                due = None
                self.update_now()
            except Exception:
                log.exception("actualización automática")

    # --- Nombres y Duplicados solos -------------------------------------------------
    @property
    def auto_file(self) -> Path:
        return Path(self.config_path).parent / "automatico.json"

    def _auto_load(self) -> dict:
        try:
            return json.loads(self.auto_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return {}

    def _auto_save(self, **changes) -> dict:
        with self.auto_lock:
            st = self._auto_load()
            st.update(changes)
            try:
                self.auto_file.write_text(json.dumps(st, ensure_ascii=False), encoding="utf-8")
            except OSError as e:
                log.warning("no se pudo guardar %s: %s", self.auto_file, e)
            return st

    def _auto_keep(self, kind: str, path: str) -> None:
        """Lo que deshaces a mano no lo vuelve a tocar el trabajo automático."""
        with self.auto_lock:
            keep = self._auto_load().get(kind, [])
        if path not in keep:
            self._auto_save(**{kind: [*keep, path][-2000:]})

    def automatic(self) -> None:
        """Hilo que pasa Nombres y Duplicados solos: cuando llegan videos nuevos y/o una vez por noche."""
        self.auto["last"] = self._auto_load().get("last")
        while True:
            cfg = self.cfg()
            a = cfg["auto"]
            wait = 900
            if not (a["names"] or a["dupes"]):
                self.auto.update(message="apagado")
                wait = 3600
            elif not cfg["library"]:
                self.auto.update(message="elige primero las carpetas de la biblioteca en Ajustes")
            elif self.names["running"] or self.dupes["running"] or self.series["running"]:
                wait = 120  # ya hay una búsqueda en marcha (pulsada a mano): se espera a que acabe
            else:
                try:
                    self._auto_tick(cfg, a)
                except Exception as e:
                    log.exception("trabajo automático")
                    self.auto.update(message=f"error: {e}")
            self.auto_wake.wait(wait)
            self.auto_wake.clear()

    def _auto_tick(self, cfg: dict, a: dict) -> None:
        lib = self.library(cfg)
        if not self.state["running"]:
            lib.scan(cfg["library"], exclude=config.excluded(cfg))
        top = lib.db.execute("SELECT COALESCE(MAX(id), 0) FROM media").fetchone()[0]
        st = self._auto_load()
        if "seen" not in st:  # la primera vez lo que ya había no cuenta como «nuevo»
            st = self._auto_save(seen=top)
        start, end = int(a["night_start"]), int(a["night_end"])
        night = time.strftime("%Y-%m-%d", time.localtime(time.time() - start * 3600))  # la misma para toda la noche
        new = {m.id for m in lib.media("id > ?", (st["seen"],))}
        if a["nightly"] and convert.in_hours(start, end) and st.get("night") != night:
            why, only = "noche", None
        elif a["on_new"] and new:
            why, only = "nuevo", new
        else:
            self.auto.update(message=self._auto_waiting(a))
            return
        # se apunta antes de empezar, para no repetirlo si se reinicia a medias
        st_seen = st["seen"]
        st = self._auto_save(seen=top, **({"night": night} if why == "noche" else {}))
        log.info("trabajo automático: %s", "revisión de la noche" if only is None else f"{len(only)} videos nuevos")
        out = {"at": time.time(), "why": why, "new": len(only or ())}
        if a["names"]:
            keep = set(st.get("names_keep", []))
            movies = only if only is None else {m.id for m in lib.media("kind='movie' AND id > ?", (st_seen,))}
            if movies is None or movies:
                out["names"] = self._auto_names(cfg, movies, keep)
            eps = only if only is None else {m.id for m in lib.media("kind='episode' AND id > ?", (st_seen,))}
            if eps is None or eps:
                out["series"] = self._auto_series(cfg, eps, keep)
        if a["dupes"]:
            out["dupes"] = self._auto_dupes(cfg, only, set(st.get("dupes_keep", [])))
        if a.get("empty_dirs"):
            out["empty"] = self.empty_dirs()
        out["end"] = time.time()
        self.auto["last"] = out
        self._auto_save(last=out)
        self.auto.update(message=self._auto_waiting(a))

    def _auto_waiting(self, a: dict) -> str:
        parts = []
        if a["on_new"]:
            parts.append("mirando cada 15 minutos si llega algo nuevo")
        if a["nightly"]:
            parts.append(f"revisión completa cada noche entre las {a['night_start']}:00 y las {a['night_end']}:00")
        return "; ".join(parts) or "esperando: marca «al llegar algo nuevo» o «cada noche»"

    def _auto_names(self, cfg: dict, only: set | None, keep: set) -> dict:
        jf = cfg.get("jellyfin", {})
        if not (jf.get("url") and jf.get("api_key")):
            return {"error": "falta Jellyfin en Ajustes"}
        if not cfg.get("tmdb", {}).get("api_key"):
            return {"error": "falta la clave de TheMovieDB en Ajustes"}
        if self.names["running"]:
            return {"error": "ya se estaba identificando"}
        self.names.update(running=True, message="trabajo automático: empezando", error=None, stop=False, counts=None)
        self.auto.update(message="identificando películas")
        res = self._identify_all(only, skip=keep)
        if res is None:
            return {"error": self.names.get("error") or "falló"}
        return {"applied": len(res["applied"]), "left": len(res["todo"])}

    def _auto_series(self, cfg: dict, only: set | None, keep: set) -> dict:
        """Capítulos: sólo los de series sin dudas y que no deshiciste a mano; el resto queda en Nombres › Series."""
        if not cfg.get("tmdb", {}).get("api_key"):
            return {"error": "falta la clave de TheMovieDB en Ajustes"}
        if self.series["running"]:
            return {"error": "ya se estaban revisando"}
        self.series.update(running=True, message="trabajo automático: empezando", error=None)
        self.auto.update(message="ordenando capítulos de series")
        res = self._series_review(only)
        if res is None:
            return {"error": self.series.get("error") or "falló"}
        lib = self.library(cfg)
        applied = 0
        for g in res["series"]:
            ids = {e["id"] for e in g["episodes"] if e["ok"] and e["path"] not in keep}
            if g.get("why") or not ids:
                continue
            try:
                applied += series.apply(lib, g, ids, list(cfg["library"]), how="auto")["done"]
            except (ValueError, OSError) as e:
                log.warning("series automáticas: %s: %s", g["title"], e)
        self.series["result"]["series"] = [g for g in self.series["result"]["series"] if g["episodes"]]
        left = sum(len(g["episodes"]) for g in self.series["result"]["series"])
        self.series.update(auto={"applied": applied, "left": left, "at": self.series["at"]})
        if applied:
            self._jellyfin_refresh(cfg)
        return {"applied": applied, "left": left}

    def _auto_dupes(self, cfg: dict, only: set | None, keep: set) -> dict:
        if self.dupes["running"]:
            return {"error": "ya se estaban buscando"}
        self.dupes.update(running=True, message="trabajo automático: empezando", error=None)
        self.auto.update(message="buscando duplicados")
        groups = self._find_dupes()
        if groups is None:
            return {"error": self.dupes.get("error") or "falló"}
        mine = [g for g in groups if only is None or any(f["id"] in only for f in g["files"])]
        # dudosos, o con una copia que devolviste a mano: los decides tú
        clear = [g for g in mine if not g["doubtful"] and not any(f["path"] in keep for f in g["files"])]
        ids = [f["id"] for g in clear for f in g["files"] if f["move"]]
        r = self.dupes_move(ids) if ids else {"moved": 0, "error": ""}
        if r.get("error"):
            log.warning("duplicados automáticos: %s", r["error"])
        log.info("duplicados automáticos: %d copias apartadas, %d grupos dudosos para revisar",
                 r["moved"], len(mine) - len(clear))
        return {"moved": r["moved"], "doubtful": len(mine) - len(clear)}

    def auto_state(self) -> dict:
        return {**self.auto, "settings": self.cfg()["auto"]}

    def auto_settings(self, b: dict) -> dict:
        with self.lock:
            cfg = self.cfg()
            a = cfg["auto"]
            for k in ("names", "dupes", "empty_dirs", "on_new", "nightly"):
                if k in b:
                    a[k] = bool(b[k])
            for k in ("night_start", "night_end"):
                if k in b:
                    a[k] = max(0, min(23, int(b[k])))
            config.save(self.config_path, cfg)
        log.info("trabajo automático: nombres %s, duplicados %s", "sí" if a["names"] else "no", "sí" if a["dupes"] else "no")
        self.auto_wake.set()
        return {"ok": True, **self.auto_state()}

    def empty_dirs(self) -> dict:
        cfg = self.cfg()
        keep = [cfg.get("duplicates_dir"), cfg.get("discard_dir"), cfg.get("trash_dir"),
                cfg.get("convert", {}).get("originals_dir"), MEDIA_ROOT]
        try:
            gone = duplicates.empty_dirs(cfg["library"], [k for k in keep if isinstance(k, str)])
        except OSError as e:
            return {"ok": False, "error": str(e), "removed": 0}
        log.info("carpetas vacías: %d borradas", len(gone))
        return {"ok": True, "removed": len(gone), "paths": gone[:200]}

    # --- duplicados ----------------------------------------------------------------
    def dupes_scan(self) -> dict:
        if self.dupes["running"]:
            return {"ok": True}
        self.dupes.update(running=True, message="empezando", error=None)
        threading.Thread(target=self._find_dupes, name="duplicados", daemon=True).start()
        return {"ok": True}

    def _find_dupes(self) -> list | None:
        """Busca duplicados (con self.dupes["running"] ya puesto)."""
        try:
            cfg = self.cfg()
            lib = self.library(cfg)
            if not self.state["running"] and cfg["library"]:  # lista de videos al día (la pasada ya la hace)
                lib.scan(cfg["library"], exclude=config.excluded(cfg), progress=lambda n: self.dupes.update(
                    message=f"buscando videos: {n} encontrados"))
            groups = duplicates.find(lib, cfg, lambda msg: self.dupes.update(message=msg))
            self.dupes.update(groups=groups, at=time.time(), message="")
            log.info("duplicados: %d grupos", len(groups))
            return groups
        except Exception as e:
            log.exception("duplicados")
            self.dupes.update(message="", error=str(e))
            return None
        finally:
            self.dupes.update(running=False)

    def dupes_state(self) -> dict:
        cfg = self.cfg()
        return {**self.dupes, "dest": cfg["duplicates_dir"], "moved": duplicates.moved_list(self.library(cfg)),
                "jellyfin": bool(cfg.get("jellyfin", {}).get("url") and cfg.get("jellyfin", {}).get("api_key"))}

    def dupes_move(self, ids: list[int], everything: bool = False) -> dict:
        """everything: también se pueden apartar todas las copias de una película (lo confirmó con un segundo toque)."""
        cfg = self.cfg()
        dest = cfg["duplicates_dir"]
        root = Path(MEDIA_ROOT).resolve()
        d = Path(dest).resolve()
        if root != d and root not in d.parents:
            return {"ok": False, "error": f"la carpeta de duplicados tiene que estar dentro de {MEDIA_ROOT}"}
        if any(d == Path(r).resolve() or Path(r).resolve() in d.parents for r in cfg["library"]):
            return {"ok": False, "error": "la carpeta de duplicados no puede estar dentro de una carpeta de la biblioteca"}
        want = set(ids)
        lib = self.library(cfg)
        moved, errors = set(), []
        for g in self.dupes["groups"] or []:
            pick = [f for f in g["files"] if f["id"] in want]
            if not pick:
                continue
            if len(pick) >= len(g["files"]) and not everything:
                errors.append(f"{g['title']}: hay que dejar al menos una copia")
                continue
            for f in pick:
                m = lib.media("id=?", (f["id"],))
                if not m or m[0].path != f["path"] or not Path(f["path"]).exists():
                    errors.append(f"{f['name']}: ya no está donde estaba")
                    continue
                try:
                    duplicates.move(lib, m[0], dest, MEDIA_ROOT, cfg["library"], g["title"])
                    moved.add(f["id"])
                except OSError as e:
                    errors.append(f"{f['name']}: {e}")
            g["files"] = [f for f in g["files"] if f["id"] not in moved]
        self.dupes["groups"] = [g for g in self.dupes["groups"] or [] if len(g["files"]) > 1]
        jf = cfg.get("jellyfin", {})
        if moved and jf.get("url") and jf.get("api_key"):
            try:
                jellyfin.refresh(jf["url"], jf["api_key"])
            except Exception as e:
                log.warning("Jellyfin: no se pudo avisar: %s", e)
        return {"ok": not errors, "moved": len(moved), "error": "; ".join(errors)}

    # --- identificar y ordenar en carpetas ------------------------------------------
    def names_scan(self) -> dict:
        if self.names["running"]:
            return {"ok": True}
        if self.cfg().get("tmdb", {}).get("api_key"):
            return self.names_auto()  # con TheMovieDB, revisar ya es identificar: que no te deje trabajo que puede hacer solo
        self.names.update(running=True, message="empezando", error=None, counts=None)

        def work():
            try:
                cfg = self.cfg()
                lib = self.library(cfg)
                if not self.state["running"] and cfg["library"]:
                    lib.scan(cfg["library"], exclude=config.excluded(cfg), progress=lambda n: self.names.update(
                        message=f"buscando videos: {n} encontrados"))
                res = identify.review(lib, cfg, lambda msg: self.names.update(message=msg))
                res = autoid.enrich(lib, cfg, res, lambda msg: self.names.update(message=msg))
                self.names.update(result=res, at=time.time(), message="")
                log.info("nombres: %d por identificar, %d sin carpeta propia", len(res["todo"]), len(res["loose"]))
            except Exception as e:
                log.exception("nombres")
                self.names.update(message="", error=str(e))
            finally:
                self.names.update(running=False)

        threading.Thread(target=work, name="nombres", daemon=True).start()
        return {"ok": True}

    def names_auto(self) -> dict:
        if self.names["running"]:
            return {"ok": True}
        cfg = self.cfg()
        if not cfg.get("tmdb", {}).get("api_key"):
            return {"ok": False, "error": "pon la clave de TheMovieDB en Ajustes"}
        self.names.update(running=True, message="empezando", error=None, stop=False, counts=None)
        threading.Thread(target=self._identify_all, name="identificar", daemon=True).start()
        return {"ok": True}

    def _identify_all(self, only: set | None = None, skip: set = frozenset()) -> dict | None:
        """«Identificar todo» (con self.names["running"] ya puesto). only: sólo esos videos (los recién llegados);
        skip: rutas que no se tocan solas (las deshiciste a mano)."""
        try:
            cfg = self.cfg()
            lib = self.library(cfg)
            if not self.state["running"] and cfg["library"]:
                lib.scan(cfg["library"], exclude=config.excluded(cfg), progress=lambda n: self.names.update(
                    message=f"buscando videos: {n} encontrados"))
            rev = identify.review(lib, cfg, lambda msg: self.names.update(message=msg), search=False)
            if only is None:  # lo ya ordenado que no cuadra vuelve a identificarse
                rev["todo"] += autoid.verify(lib, cfg, lambda msg: self.names.update(message=msg), skip,
                                             stop=lambda: self.names["stop"])
            held = []
            if only is not None or skip:
                held = [e for k in ("todo", "loose") for e in rev.get(k, []) if e["path"] in skip
                        and (only is None or e["id"] in only)]
                rev = {k: [e for e in rev.get(k, []) if (only is None or e["id"] in only) and e["path"] not in skip]
                       for k in ("todo", "loose")}
            res = autoid.run(lib, cfg, rev, lambda msg: self.names.update(message=msg),
                             stop=lambda: self.names["stop"], counts=lambda c: self.names.update(counts=c),
                             frames_dir=self.frames_dir)
            now = time.time()
            for e in held:
                e["why"] = "la deshiciste a mano: no se toca sola"
                e.setdefault("options", [])
            res["todo"] += held
            if only is not None and self.names["result"]:  # se suman a la lista que ya había
                old = self.names["result"]
                todo = [e for e in old.get("todo", []) if e["id"] not in only] + res["todo"]
                self.names.update(result={"todo": todo, "loose": [e for e in old.get("loose", []) if e["id"] not in only]})
            else:
                self.names.update(result={"todo": res["todo"], "loose": []})
            self.names.update(at=now, message="",
                              auto={"applied": len(res["applied"]),
                                    "ai": sum(a["how"] == "ia" for a in res["applied"]), "copies": sum(bool(a.get("copy")) for a in res["applied"]),
                                    "left": len(res["todo"]), "at": now})
            if res["applied"]:
                self._jellyfin_refresh(cfg)
            return res
        except Exception as e:
            log.exception("identificar todo")
            self.names.update(message="", error=str(e))
            return None
        finally:
            self.names.update(running=False)

    @property
    def frames_dir(self) -> Path:
        return Path(self.config_path).parent / "cache" / "fotos"

    def names_frame(self, media_id: int, n: int) -> bytes | None:
        """Una foto del video para enseñarla en Nombres (se saca la primera vez que se pide)."""
        if not 0 <= n < len(peek.FRAMES):
            return None
        m = self.library(self.cfg()).media("id=?", (media_id,))
        if not m or not os.path.exists(m[0].path):
            return None
        key = f"{m[0].id}_{int(os.stat(m[0].path).st_mtime)}"
        jpg = self.frames_dir / f"{key}_{n}.jpg"
        if not jpg.exists():
            jpg = peek.frame(m[0].path, self.frames_dir, peek.info(m[0].path)["minutes"], key, n)
        return jpg.read_bytes() if jpg else None

    def series_frame(self, media_id: int, n: int) -> bytes | None:
        """Una foto del principio de un capítulo (para elegir la serie a ojo)."""
        if not 0 <= n < len(peek.OPENING):
            return None
        m = self.library(self.cfg()).media("id=?", (media_id,))
        if not m or not os.path.exists(m[0].path):
            return None
        key = f"{m[0].id}_{int(os.stat(m[0].path).st_mtime)}"
        jpg = self.frames_dir / f"{key}_p{n}.jpg"
        if not jpg.exists():
            peek.opening(m[0].path, self.frames_dir, peek.info(m[0].path)["minutes"], key)
        return jpg.read_bytes() if jpg.exists() and jpg.stat().st_size else None

    def names_stop(self) -> dict:
        self.names["stop"] = True
        return {"ok": True}

    def names_state(self) -> dict:
        cfg = self.cfg()
        jf = cfg.get("jellyfin", {})
        return {**self.names, "history": identify.history(self.library(cfg)),
                "jellyfin": bool(jf.get("url") and jf.get("api_key")),
                "tmdb": bool(cfg.get("tmdb", {}).get("api_key")), "gemini": bool(cfg.get("gemini", {}).get("api_key"))}

    def _names_entry(self, media_id: int):
        res = self.names["result"] or {}
        return next((e for e in [*res.get("todo", []), *res.get("loose", [])] if e["id"] == media_id), None)

    def _names_done(self, ids: set) -> None:
        res = self.names["result"] or {}
        for k in ("todo", "loose"):
            res[k] = [e for e in res.get(k, []) if e["id"] not in ids]

    def _names_apply(self, lib, cfg, media_id: int, title: str, year, tmdb, imdb) -> None:
        entry = self._names_entry(media_id)
        if not entry:
            raise ValueError("vuelve a pulsar «Revisar nombres»")
        m = lib.media("id=?", (media_id,))
        if not m or m[0].path != entry["path"]:
            raise ValueError(f"{entry['name']}: ya no está donde estaba")
        if self.state["running"] and self.state["current"] == m[0].label:
            raise ValueError(f"{entry['name']}: se está procesando ahora mismo; prueba en un momento")
        identify.apply(lib, cfg, m[0], title.strip(), int(year) if year else None, tmdb or None, imdb or None)
        autoid.learn(lib, m[0], tmdb)

    def _jellyfin_refresh(self, cfg: dict) -> None:
        jf = cfg.get("jellyfin", {})
        if jf.get("url") and jf.get("api_key"):
            try:
                jellyfin.refresh(jf["url"], jf["api_key"])
            except Exception as e:
                log.warning("Jellyfin: no se pudo avisar: %s", e)

    def names_apply(self, b: dict) -> dict:
        cfg = self.cfg()
        lib = self.library(cfg)
        try:
            self._names_apply(lib, cfg, int(b["id"]), b["name"], b.get("year"), b.get("tmdb"), b.get("imdb"))
        except (ValueError, OSError) as e:
            return {"ok": False, "error": str(e)}
        self._names_done({int(b["id"])})
        self._jellyfin_refresh(cfg)
        return {"ok": True}

    def names_organize(self, ids: list[int]) -> dict:
        if self.names["running"]:
            return {"ok": False, "done": 0, "error": "espera a que termine lo que está haciendo Nombres"}
        cfg = self.cfg()
        lib = self.library(cfg)
        done, gone, errors = set(), set(), []
        loose = {e["id"]: e for e in (self.names["result"] or {}).get("loose", [])}
        for i in ids:
            e = loose.get(i)
            m = lib.media("id=?", (i,))
            if not e or not m or m[0].path != e["path"]:  # ya la movió otra cosa (p. ej. «Identificar todo»)
                gone.add(i)
                continue
            if not e["jellyfin"]:
                continue
            jf = e["jellyfin"]
            try:
                self._names_apply(lib, cfg, i, jf["name"], jf["year"], jf["tmdb"], jf["imdb"])
                done.add(i)
            except (ValueError, OSError) as ex:
                errors.append(str(ex))
        self._names_done(done | gone)
        if done:
            self._jellyfin_refresh(cfg)
        return {"ok": not errors, "done": len(done), "gone": len(gone), "error": "; ".join(errors[:5])}

    def names_search(self, media_id: int, query: str) -> dict:
        cfg = self.cfg()
        m = self.library(cfg).media("id=?", (media_id,))
        if not m:
            return {"ok": False, "error": "no existe"}
        entry = self._names_entry(media_id)
        if cfg.get("tmdb", {}).get("api_key"):  # con TheMovieDB: también duración y saga
            opts = autoid.search(cfg, m[0], query, (entry or {}).get("minutes"))
        else:
            opts = identify.search(cfg, m[0], query)
        if entry is not None:
            entry["options"] = opts
        return {"ok": True, "options": opts}

    def names_undo(self, row_id: int) -> dict:
        cfg = self.cfg()
        lib = self.library(cfg)
        r = lib.db.execute("SELECT video_from, extra FROM renamed WHERE id=?", (row_id,)).fetchone()
        try:
            title = identify.undo(lib, row_id)
        except (ValueError, OSError) as e:
            return {"ok": False, "error": str(e)}
        if r and r["extra"]:  # una serie: ninguno de sus capítulos se vuelve a tocar solo
            for old, _ in json.loads(r["extra"]).get("videos", []):
                self._auto_keep("names_keep", old)
        elif r and r["video_from"]:
            self._auto_keep("names_keep", r["video_from"])
        self._jellyfin_refresh(cfg)
        return {"ok": True, "title": title}

    # --- series: capítulos con su nombre ----------------------------------------------
    def series_scan(self) -> dict:
        if self.series["running"]:
            return {"ok": True}
        if not self.cfg().get("tmdb", {}).get("api_key"):
            return {"ok": False, "error": "pon la clave de TheMovieDB en Ajustes"}
        self.series.update(running=True, message="empezando", error=None)
        threading.Thread(target=self._series_review, name="series", daemon=True).start()
        return {"ok": True}

    def _series_review(self, only: set | None = None) -> dict | None:
        """Revisa los capítulos (con self.series["running"] ya puesto). only: sólo esos (los recién llegados)."""
        try:
            cfg = self.cfg()
            lib = self.library(cfg)
            if not self.state["running"] and cfg["library"] and only is None:
                lib.scan(cfg["library"], exclude=config.excluded(cfg), progress=lambda n: self.series.update(
                    message=f"buscando videos: {n} encontrados"))
            res = series.review(lib, cfg, lambda msg: self.series.update(message=msg), only, self.frames_dir)
            if only is not None and self.series["result"]:  # se suman a la lista que ya había
                keys = {g["key"] for g in res["series"]}
                res = {"series": [g for g in self.series["result"]["series"] if g["key"] not in keys] + res["series"]}
            self.series.update(result=res, at=time.time(), message="")
            log.info("series: %d con capítulos por renombrar", len(res["series"]))
            return res
        except Exception as e:
            log.exception("series")
            self.series.update(message="", error=str(e))
            return None
        finally:
            self.series.update(running=False)

    def series_state(self) -> dict:
        return {**self.series, "tmdb": bool(self.cfg().get("tmdb", {}).get("api_key"))}

    def _series_group(self, key: str) -> dict:
        g = next((g for g in (self.series["result"] or {}).get("series", []) if g["key"] == key), None)
        if not g:
            raise ValueError("vuelve a pulsar «Revisar series»")
        return g

    def series_search(self, key: str, query: str) -> dict:
        try:
            self._series_group(key)
            return {"ok": True, "options": series.search(self.cfg(), query)}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def series_pick(self, key: str, tmdb_id: str) -> dict:
        try:
            g = series.pick(self.cfg(), self._series_group(key), tmdb_id)
        except Exception as e:
            return {"ok": False, "error": str(e)}
        return {"ok": True, "group": g}

    def series_renumber(self, key: str, changes: list) -> dict:
        try:
            g = self._series_group(key)
            if not g.get("show"):
                raise ValueError("elige primero qué serie es")
            g = series.renumber(self.cfg(), g, {int(c["id"]): (int(c["season"]), int(c["episode"])) for c in changes})
        except Exception as e:
            return {"ok": False, "error": str(e)}
        return {"ok": True, "group": g}

    def series_apply(self, key: str, ids: list[int]) -> dict:
        if self.series["running"]:
            return {"ok": False, "error": "espera a que termine de revisar las series"}
        cfg = self.cfg()
        lib = self.library(cfg)
        try:
            g = self._series_group(key)
            for e in g["episodes"]:
                if e["id"] in ids:
                    m = lib.media("id=?", (e["id"],))
                    if not m or m[0].path != e["path"]:
                        raise ValueError(f"{e['name']}: ya no está donde estaba; vuelve a pulsar «Revisar series»")
                    if self.state["running"] and self.state["current"] == m[0].label:
                        raise ValueError(f"{e['name']}: se está procesando ahora mismo; prueba en un momento")
            r = series.apply(lib, g, set(ids), list(cfg["library"]))
        except (ValueError, OSError) as e:
            return {"ok": False, "error": str(e)}
        if not g["episodes"]:
            self.series["result"]["series"] = [x for x in self.series["result"]["series"] if x is not g]
        self._jellyfin_refresh(cfg)
        return {"ok": True, **r}

    def dupes_restore(self, row_id: int) -> dict:
        cfg = self.cfg()
        try:
            dest = duplicates.restore(self.library(cfg), row_id)
        except (ValueError, OSError) as e:
            return {"ok": False, "error": str(e)}
        self._auto_keep("dupes_keep", dest)
        self.wake.set()  # que vuelva a aparecer en la biblioteca
        return {"ok": True, "dest": dest}

    # --- No se sabe qué es: papelera de videos -----------------------------------------
    def unknown_state(self) -> dict:
        cfg = self.cfg()
        lib = self.library(cfg)
        rows = lib.db.execute("SELECT u.path, u.minutes, u.at, m.id, m.size FROM unknown u "
                              "JOIN media m ON m.path = u.path ORDER BY u.at DESC").fetchall()
        items = [{"id": r["id"], "path": r["path"], "name": Path(r["path"]).name, "folder": Path(r["path"]).parent.name,
                  "minutes": r["minutes"], "size": r["size"], "at": r["at"]} for r in rows if os.path.exists(r["path"])]
        return {"items": items, "dest": cfg["discard_dir"], "discarded": duplicates.moved_list(lib, "discarded")}

    def _discard_dir_problem(self, cfg: dict) -> str | None:
        d = Path(cfg["discard_dir"]).resolve()
        root = Path(MEDIA_ROOT).resolve()
        if root != d and root not in d.parents:
            return f"la papelera de videos tiene que estar dentro de {MEDIA_ROOT}"
        if any(d == Path(r).resolve() or Path(r).resolve() in d.parents for r in cfg["library"]):
            return "la papelera de videos no puede estar dentro de una carpeta de la biblioteca"
        return None

    def unknown_discard(self, ids: list[int]) -> dict:
        cfg = self.cfg()
        bad = self._discard_dir_problem(cfg)
        if bad:
            return {"ok": False, "error": bad}
        lib = self.library(cfg)
        done, errors = 0, []
        for media_id in ids:
            m = lib.media("id=?", (media_id,))
            if not m or not Path(m[0].path).exists():
                errors.append(f"{media_id}: ya no está")
                continue
            try:
                duplicates.move(lib, m[0], cfg["discard_dir"], MEDIA_ROOT, cfg["library"], Path(m[0].path).name, "discarded")
                lib.db.execute("DELETE FROM unknown WHERE path=?", (m[0].path,))
                lib.db.commit()
                done += 1
            except OSError as e:
                errors.append(f"{Path(m[0].path).name}: {e}")
        res = self.names["result"] or {}
        for k in ("todo", "loose"):
            res[k] = [e for e in res.get(k, []) if e["id"] not in set(ids)]
        if done:
            self._jellyfin_refresh(cfg)
        return {"ok": not errors, "done": done, "error": "; ".join(errors)}

    def unknown_restore(self, row_id: int) -> dict:
        try:
            dest = duplicates.restore(self.library(), row_id, "discarded")
        except (ValueError, OSError) as e:
            return {"ok": False, "error": str(e)}
        self.wake.set()
        return {"ok": True, "dest": dest}

    def unknown_retry(self, media_id: int) -> dict:
        lib = self.library()
        lib.db.execute("DELETE FROM unknown WHERE path IN (SELECT path FROM media WHERE id=?)", (media_id,))
        lib.db.commit()
        e = self._names_entry(media_id)
        if e:  # vuelve a la lista de Nombres para buscarla a mano
            e["unknown"] = False
            e["why"] = "la reconoces tú: escribe su nombre"
        return {"ok": True}

    def unknown_empty(self) -> dict:
        """Borra de verdad lo que está en la papelera de videos (sólo lo que se mandó desde aquí)."""
        cfg = self.cfg()
        lib = self.library(cfg)
        trash = Path(cfg["discard_dir"]).resolve()
        n, freed, errors = 0, 0, []
        for r in lib.db.execute("SELECT * FROM discarded").fetchall():
            dest = Path(r["dest"])
            if trash not in dest.resolve().parents:
                errors.append(f"{dest}: no está en la papelera de videos, no se toca")
                continue
            try:
                if dest.is_dir():
                    freed += sum(f.stat().st_size for f in dest.rglob("*") if f.is_file())
                    shutil.rmtree(dest)
                elif dest.exists():
                    freed += dest.stat().st_size
                    dest.unlink()
                lib.db.execute("DELETE FROM discarded WHERE id=?", (r["id"],))
                lib.db.commit()
                n += 1
                log.info("papelera de videos: borrado %s", dest)
            except OSError as e:
                errors.append(f"{dest.name}: {e}")
        for dirpath, _, _ in sorted(os.walk(trash), key=lambda w: -len(w[0])):  # carpetas que quedaron vacías
            if Path(dirpath) != trash:
                try:
                    os.rmdir(dirpath)
                except OSError:
                    pass
        return {"ok": not errors, "done": n, "freed": freed, "error": "; ".join(errors)}

    # --- Descargas (Phono por Telegram) -------------------------------------------------
    @property
    def tg_session(self) -> Path:
        return Path(self.config_path).parent / "telegram.session"

    def _telegram(self, cfg: dict | None = None):
        """El cliente de Telegram (o None si faltan el API ID y el hash)."""
        t = (cfg or self.cfg()).get("telegram", {})
        key = (str(t.get("api_id") or "").strip(), (t.get("api_hash") or "").strip())
        if not all(key):
            return None
        with self.tg_lock:
            if self.tg is None or self.tg_key != key:
                from . import telegram
                if self.tg is not None:
                    self.tg.close()
                self.tg = telegram.Telegram(self.tg_session, *key)
                self.tg_key = key
            return self.tg

    def _tg_user(self, cfg: dict | None = None, fresh: bool = False) -> dict | None:
        """Con quién está conectado Telegram (se recuerda un minuto: la página pregunta a menudo)."""
        hit = getattr(self, "_tg_me", None)
        if hit and not fresh and time.time() - hit[0] < 60:
            return hit[1]
        try:
            tg = self._telegram(cfg)
            user = tg.me() if tg else None
        except Exception as e:
            log.info("Telegram: %s", e)
            user = None
        self._tg_me = (time.time(), user)
        return user

    def downloads_state(self) -> dict:
        cfg = self.cfg()
        t = cfg.get("telegram", {})
        rows = downloads.Downloads(self.library(cfg), cfg).rows()
        return {"rows": rows, "state": {k: v for k, v in self.dl.items() if k != "stop"},
                "settings": cfg["downloads"], "telegram": {"api_id": str(t.get("api_id") or ""),
                                                           "api_hash": MASK if t.get("api_hash") else "",
                                                           "user": self._tg_user(cfg),
                                                           "bot": t.get("bot_name") or ""}}

    def downloads_settings(self, b: dict) -> dict:
        with self.lock:
            cfg = self.cfg()
            d = cfg["downloads"]
            for k in ("enabled", "auto", "remind"):
                if k in b:
                    d[k] = bool(b[k])
            for k in ("movies_dir", "series_dir", "tmp_dir", "realitys_dir", "es_movies_dir", "es_series_dir"):
                if k in b:
                    d[k] = (b[k] or "").strip()
            for k, lo, hi in (("gap_minutes", 1, 1440), ("daily", 1, 200), ("min_free_gb", 0, 10000), ("summary_hour", -1, 23)):
                if k in b:
                    d[k] = max(lo, min(hi, int(b[k])))
            if "audio" in b:
                d["audio"] = [x for x in b["audio"] if x in phono.LANG] or ["es", "en"]
            config.save(self.config_path, cfg)
        self.dl_wake.set()
        return {"ok": True, **self.downloads_state()}

    def telegram_code(self, b: dict) -> dict:
        api_id = re.sub(r"\D", "", str(b.get("api_id") or ""))
        api_hash = (b.get("api_hash") or "").strip()
        with self.lock:
            cfg = self.cfg()
            t = cfg.setdefault("telegram", {})
            if api_hash == MASK:
                api_hash = t.get("api_hash", "")
            if not api_id or not api_hash:
                return {"ok": False, "error": "faltan el API ID y el API hash (de my.telegram.org)"}
            t.update(api_id=api_id, api_hash=api_hash)
            config.save(self.config_path, cfg)
        if not (b.get("phone") or "").strip():
            return {"ok": False, "error": "falta tu número de teléfono, con el +34, +52…"}
        try:
            self._telegram(cfg).send_code(b["phone"])
        except Exception as e:
            return {"ok": False, "error": str(e)}
        return {"ok": True}

    def telegram_login(self, b: dict) -> dict:
        try:
            tg = self._telegram()
            if not tg:
                return {"ok": False, "error": "pide primero el código"}
            res = tg.sign_in(b.get("code", ""), b.get("password", ""))
        except Exception as e:
            return {"ok": False, "error": str(e)}
        if res == "password":
            return {"ok": True, "password": True}
        log.info("Telegram: conectado")
        self.dl_wake.set()
        return {"ok": True, "user": self._tg_user(fresh=True)}

    def telegram_logout(self) -> dict:
        try:
            tg = self._telegram()
            if tg:
                tg.log_out()
        except Exception as e:
            return {"ok": False, "error": str(e)}
        self._tg_user(fresh=True)
        return {"ok": True}

    def telegram_bot(self, b: dict) -> dict:
        """Conecta (o quita, con el token vacío) tu bot de avisos y manda un mensaje de prueba."""
        cfg = self.cfg()
        token = (b.get("token") or "").strip()
        if token == MASK:
            token = cfg["telegram"].get("bot_token") or ""
        if not token:
            with self.lock:
                cfg = self.cfg()
                cfg["telegram"].update(bot_token="", bot_name="", chat_id="")
                config.save(self.config_path, cfg)
            return {"ok": True}
        try:
            name = notify.bot_name(token)
        except Exception as e:
            return {"ok": False, "error": f"ese token no vale: {e}"}
        user = self._tg_user(cfg, fresh=True)
        if not user:
            return {"ok": False, "error": "conecta primero tu Telegram (arriba)"}
        try:
            self._telegram(cfg).say(name, "/start")  # para que el bot pueda escribirte
            notify.send(token, user["id"], "✅ Listo: por aquí te avisaré cuando te toque pulsar el anuncio. "
                        "Deja este chat con sonido.")
        except Exception as e:
            return {"ok": False, "error": f"no se pudo mandar el mensaje de prueba: {e}"}
        with self.lock:
            cfg = self.cfg()
            cfg["telegram"].update(bot_token=token, bot_name=name, chat_id=str(user["id"]))
            config.save(self.config_path, cfg)
        return {"ok": True, "bot": name}

    @staticmethod
    def _dl_label(row: dict) -> str:
        """«🎬 Película: Título (2026)» o «📺 Serie: Título (2026) · temporada 1», para ver de un vistazo qué es."""
        return bot.label(row["kind"], row["title"], row.get("year"), row.get("season"))

    def _notify(self, cfg: dict, text: str, link: tuple[str, str] | None = None,
                buttons: list | None = None) -> bool:
        """Aviso por tu bot (en otro hilo, para no frenar la descarga). False si no hay bot puesto."""
        t = cfg.get("telegram", {})
        if not (t.get("bot_token") and t.get("chat_id")) or not cfg["downloads"].get("remind", True):
            return False

        def go():
            try:
                notify.send(t["bot_token"], t["chat_id"], text, link=link, buttons=buttons)
            except Exception as e:
                log.warning("bot de avisos: %s", e)
        threading.Thread(target=go, name="aviso", daemon=True).start()
        return True

    @staticmethod
    def _dl_progress(row: dict, label: str, name: str, done: int, total: int, speed: float, ready: int) -> str:
        """El texto del mensaje que se va editando mientras baja: qué capítulo, barra, velocidad y lo que ya está."""
        m = re.search(r"[Ss](\d{1,2})[ ._-]?[Ee](\d{1,3})", name)
        what = (f"Temporada {int(m.group(1))} · capítulo {int(m.group(2))}" if m and row["kind"] == "tv"
                else "Película" if row["kind"] == "movie" else name)
        frac = done / total if total else 0
        line = f"{done / 2**30:.2f} de {total / 2**30:.2f} GB"
        if speed:
            left = (total - done) / speed
            line += f" · {speed / 2**20:.1f} MB/s · " + ("menos de 1 min" if left < 60 else f"faltan {int(left // 60) + 1} min")
        extra = f"\n\n✅ {ready} capítulo{'s' if ready != 1 else ''} ya en la biblioteca" if ready and row["kind"] == "tv" else ""
        return f"⬇️ Descargando\n\n{label}\n\n{what}\n{bot.bar(frac)} {frac * 100:.0f} %\n{line}{extra}"

    def _nag(self, cfg: dict, rid: int, label: str, bot_name: str, status, every: float = 20 * 60, times: int = 3) -> None:
        """Si a los 20 minutos sigue esperando el anuncio, te lo recuerda (hasta 3 veces). Los recordatorios
        se borran solos en cuanto empieza a bajar."""
        def go():
            for k in range(times):
                time.sleep(every)
                r = downloads.Downloads(self.library(cfg), cfg).get(rid)
                if not r or r["state"] != "manual":
                    return
                status.nag(f"⏰ Sigue esperando el anuncio ({(k + 1) * int(every // 60)} min)\n\n{label}",
                           link=(f"👉 Ir a Watch Ads (@{bot_name})", f"https://t.me/{bot_name}"))
        threading.Thread(target=go, name="recordatorio", daemon=True).start()

    def downloads_add(self, b: dict) -> dict:
        cfg = self.cfg()
        dl = downloads.Downloads(self.library(cfg), cfg)
        tid, title, year = int(b["tmdb"]), b.get("title") or "", b.get("year")
        if b["kind"] == "tv" and b.get("seasons"):  # sólo las temporadas que marcaste
            have = dl.seasons_in(tid)
            new = sorted({int(n) for n in b["seasons"]} - have)
            if not new:
                return {"ok": False, "error": "esas temporadas ya están en Descargas"}
            for n in new:
                dl.add("tv", tid, title, year, "manual", season=n)
            self.dl_wake.set()
            return {"ok": True, "done": len(new)}
        if dl.has(b["kind"], tid):
            return {"ok": False, "error": "ya está en Descargas"}
        dl.add(b["kind"], tid, title, year, "manual")
        self.dl_wake.set()
        return {"ok": True}

    def downloads_seasons(self, b: dict) -> dict:
        """Las temporadas que tiene Phono de una serie, para elegir cuáles pedir."""
        cfg = self.cfg()
        if not cfg.get("tmdb", {}).get("api_key"):
            return {"ok": False, "error": "falta la clave de TheMovieDB"}
        dl = downloads.Downloads(self.library(cfg), cfg)
        row = {"kind": "tv", "tmdb": int(b["tmdb"]), "title": b.get("title") or "", "year": b.get("year")}
        db = tmdb.TMDb(cfg["tmdb"]["api_key"], cfg["tmdb"].get("language") or "es-MX")
        api = phono.Phono()
        card = dl.find(row, api, db)
        if not card:
            return {"ok": False, "error": "Phono aún no tiene esta serie"}
        seasons = phono.seasons(api.buttons(card["content_id"]))
        return {"ok": True, "seasons": sorted(seasons), "have": sorted(dl.seasons_in(row["tmdb"]))}

    def downloads_cancel(self, row_id: int) -> dict:
        cfg = self.cfg()
        dl = downloads.Downloads(self.library(cfg), cfg)
        r = dl.get(row_id)
        if not r:
            return {"ok": False, "error": "no existe"}
        if r["state"] == "listo":
            dl.db.execute("DELETE FROM downloads WHERE id=?", (row_id,))  # quitarla de la lista
            dl.db.commit()
            return {"ok": True}
        if self.dl["current"] == row_id:
            self.dl["stop"] = row_id
        dl.set(row_id, state="cancelado", detail="cancelada")
        return {"ok": True}

    def downloads_clear(self) -> dict:
        """Vaciar la cola: cancela lo que espera turno; lo que se está bajando o espera tu toque sigue."""
        cfg = self.cfg()
        dl = downloads.Downloads(self.library(cfg), cfg)
        n = 0
        for r in dl.rows():
            if r["state"] in ("cola", "no-esta", "error") and r["id"] != self.dl["current"]:
                dl.set(r["id"], state="cancelado", detail="cancelada")
                n += 1
        return {"ok": True, "done": n}

    def downloads_retry(self, row_id: int) -> dict:
        cfg = self.cfg()
        downloads.Downloads(self.library(cfg), cfg).set(row_id, state="cola", detail="", retry=None)
        self.dl_wake.set()
        return {"ok": True}

    def downloader(self) -> None:
        """Hilo: de uno en uno, con una pausa entre pedido y pedido y un máximo al día."""
        cleaned = False
        while True:
            wait = 600
            try:
                wait = self._dl_tick(cleaned)
                cleaned = True
            except Exception as e:
                log.exception("descargas")
                self.dl.update(message=f"error: {e}")
            self.dl_wake.wait(wait)
            self.dl_wake.clear()

    def _dl_tick(self, cleaned: bool) -> float:
        cfg = self.cfg()
        d = cfg["downloads"]
        if not d["enabled"]:
            self.dl.update(message="apagado")
            return 3600
        why = ("conecta tu Telegram" if not self._tg_user(cfg) else
               "falta la clave de TheMovieDB" if not cfg.get("tmdb", {}).get("api_key") else
               "elige las carpetas de películas y de series" if not (d["movies_dir"] and d["series_dir"]) else "")
        if why:
            self.dl.update(message=why)
            return 600
        dl = downloads.Downloads(self.library(cfg), cfg)
        if not cleaned:
            downloads.cleanup(Path(d["tmp_dir"]))
            for r in dl.rows():  # se reinició a medias: vuelve a la cola
                if r["state"] in downloads.ACTIVE:
                    dl.set(r["id"], state="cola", detail="")
        if d["auto"]:
            self._dl_enqueue(cfg, dl)
        row = dl.next()
        if not row:
            self.dl.update(message="nada pendiente")
            return 600
        left = self.dl["last"] + d["gap_minutes"] * 60 - time.time()
        if left > 0:
            self.dl.update(message=f"siguiente pedido en {int(left // 60) + 1} min")
            return left
        if dl.today() >= d["daily"]:
            self.dl.update(message=f"ya van {d['daily']} pedidos hoy; sigue mañana")
            return 1800
        if downloads.free_gb(d["tmp_dir"]) < d["min_free_gb"]:
            self.dl.update(message=f"queda poco espacio (menos de {d['min_free_gb']} GB)")
            if time.time() - self.dl.get("space_warned", 0) > 12 * 3600:  # como mucho dos veces al día
                self.dl["space_warned"] = time.time()
                self._notify(cfg, f"⚠️ Queda poco espacio en el NAS\n\nMenos de {d['min_free_gb']} GB libres: "
                                  "las descargas esperan hasta que hagas sitio.")
            return 1800
        before = row["state"]
        self._dl_one(cfg, dl, row)
        after = dl.get(row["id"]) or {}
        if after.get("state") == "no-esta" and after["state"] != before:  # sólo la primera vez, no a diario
            self._notify(cfg, f"❌ {self._dl_label(after)}\n\n{after.get('detail') or ''}\nLo vuelvo a mirar mañana.")
            self._tell_friends_missing(after)
        return 5

    def _tell_friends_missing(self, row: dict) -> None:
        """A quien la pidió: que por ahora no está (una vez); si aparece, le llega el «Ya está» de siempre."""
        try:
            who = self._friends().requesters(row["kind"], row["tmdb"])
            if who:
                self._notices().send(who, f"😕 {row.get('title') or 'Lo que pediste'} no está disponible por ahora",
                                     (row.get("detail") or "Phono aún no la tiene") + ". Se sigue buscando cada día.",
                                     "/pedir")
        except Exception as e:
            log.warning("avisos: no se pudo avisar a los amigos (%s)", e)

    def _dl_enqueue(self, cfg: dict, dl) -> None:
        # lo que cancelaste no vuelve solo a la cola; con «Descargar de Phono» sí se puede volver a pedir
        wanted = [w for w in dl.wanted() if not dl.has(w["kind"], w["tmdb"], cancelled=True)]
        if not wanted:
            return
        dsc = discover.Discover(self.library(cfg), cfg)
        idx = dsc.owned_index()
        for w in wanted:
            if not dsc.in_library(w, idx):
                dl.add(w["kind"], w["tmdb"], w["title"], w["year"], "auto")
                log.info("descargas: %s en la cola", w["title"])

    def _dl_one(self, cfg: dict, dl, row: dict) -> None:
        d = cfg["downloads"]
        rid = row["id"]
        self.dl.update(current=rid, what=row["title"], done=0, total=0, speed=0.0, stop=None, message=f"{row['title']}: buscando en Phono")
        label = self._dl_label(row)
        status = bot.Status(cfg)  # el mensaje que se va editando en tu bot
        placed: list[str] = []
        try:
            dl.set(rid, state="buscando", detail="buscando en Phono")
            api = phono.Phono()
            db = tmdb.TMDb(cfg["tmdb"]["api_key"], cfg["tmdb"].get("language") or "es-MX")
            card = dl.find(row, api, db)
            if not card:
                dl.set(rid, state="no-esta", detail="Phono aún no la tiene; se vuelve a mirar mañana",
                       retry=time.time() + downloads.RETRY_MISSING)
                return
            cid = card["content_id"]
            if row["kind"] == "movie":
                f = phono.pick(api.files(cid), d["audio"])
                link = phono.bot_link(f["download_url"]) if f else None
                if not link:
                    dl.set(rid, state="no-esta", detail="Phono no tiene una versión en español ni en inglés",
                           retry=time.time() + downloads.RETRY_MISSING)
                    return
                dl.set(rid, file=f["file_name"], size=f.get("file_size_bytes"))
                what = f["file_name"]
            else:
                seasons = phono.seasons(api.buttons(cid))
                if not seasons:
                    dl.set(rid, state="no-esta", detail="Phono no tiene temporadas para descargar",
                           retry=time.time() + downloads.RETRY_MISSING)
                    return
                if row["season"] is None:  # una fila por temporada
                    first, *rest = sorted(seasons)
                    for n in rest:
                        dl.add("tv", row["tmdb"], row["title"], row["year"], row["how"], season=n)
                    dl.set(rid, season=first)
                    row["season"] = first
                s = seasons.get(row["season"])
                if not s:
                    dl.set(rid, state="no-esta", detail=f"Phono no tiene la temporada {row['season']}",
                           retry=time.time() + downloads.RETRY_MISSING)
                    return
                link = (s["bot"], s["code"])
                what = f"temporada {row['season']}"
                dl.set(rid, file=s["text"])
            dl.set(rid, state="pidiendo", detail=f"pidiendo {what} al bot")
            self.dl.update(message=f"{row['title']}: pidiendo al bot", last=time.time())
            tmp = Path(d["tmp_dir"]) / str(rid)

            def note(**kw):  # el cliente de Telegram llama desde su hilo: conexión a la base aparte
                downloads.Downloads(self.library(cfg), cfg).set(rid, **kw)

            # lo ya recibido (antes de un reinicio o un error) no se vuelve a bajar
            got_names = json.loads(row.get("got") or "[]")
            placed = json.loads(row.get("dest") or "[]")

            def put(path: Path, _info=None):
                """Cada video pasa a la biblioteca en cuanto termina, sin esperar al resto de la temporada."""
                final = downloads.place(path, dl.target(row, path, db))
                downloads.extract_subs(final, cfg.get("languages") or ["es"])
                got_names.append(path.name)
                placed.append(str(final))
                note(got=json.dumps(got_names, ensure_ascii=False), dest=json.dumps(placed, ensure_ascii=False))
                if row["kind"] == "tv":
                    status.show(f"⬇️ Descargando\n\n{label}\n\n✅ {len(placed)} capítulo{'s' if len(placed) != 1 else ''} ya en la biblioteca\n"
                                "⏳ esperando el siguiente…")
                log.info("descargas: %s -> %s", path.name, final)
                self.wake.set()  # que busque y compruebe los subtítulos ya
                self._jellyfin_refresh(cfg)

            if tmp.is_dir():  # lo que quedó en la carpeta de espera de una versión anterior
                for p in sorted(tmp.iterdir()):
                    if p.suffix == ".part":
                        p.unlink(missing_ok=True)
                    elif p.is_file() and downloads.videos([p]) and p.name not in got_names:
                        put(p)

            started = set()

            rate = {"name": None, "t": 0.0, "done": 0}

            def progress(name, done, total):
                now = time.time()
                if name != rate["name"] or done < rate["done"]:
                    rate.update(name=name, t=now, done=done)
                    self.dl["speed"] = 0.0
                elif now - rate["t"] >= 2:  # velocidad media de los últimos segundos, suavizada
                    v = (done - rate["done"]) / (now - rate["t"])
                    old = self.dl.get("speed") or 0.0
                    self.dl["speed"] = v if not old else 0.6 * old + 0.4 * v
                    rate.update(t=now, done=done)
                self.dl.update(done=done, total=total, message=f"{row['title']}: {name}")
                if total:
                    status.show(self._dl_progress(row, label, name, done, total, self.dl.get("speed") or 0, len(placed)))
                if total and name not in started:
                    started.add(name)
                    note(state="descargando", detail=f"descargando {name}", size=total)

            by_bot = bool(cfg["telegram"].get("bot_token") and cfg["telegram"].get("chat_id"))

            def manual(name):
                note(state="manual", bot=name, detail=f"pulsa tú «Watch Ads» y «I have clicked ads» en el chat de @{name}")
                status.show(f"🚨 Te toca pulsar el anuncio\n\n{label}\n\nPulsa el botón, luego «Watch Ads» y después "
                            "«I have clicked ads». Este mensaje irá cambiando con lo que se descarga.",
                            buttons=[[{"text": f"👉 Ir a Watch Ads (@{name})", "url": f"https://t.me/{name}"}]])
                self.dl.update(message=f"{row['title']}: esperando que pulses el anuncio en Telegram")
                self._nag(cfg, rid, label, name, status)

            self._telegram(cfg).fetch(link[1], tmp, bot=link[0], progress=progress,
                                      stop=lambda: self.dl["stop"] == rid, on_manual=manual,
                                      skip=lambda name, size: name in got_names, on_file=put,
                                      remind=label if d.get("remind", True) and not by_bot else "")
            if self.dl["stop"] == rid:
                shutil.rmtree(tmp, ignore_errors=True)
                status.show(f"✖️ Cancelada\n\n{label}", final=True)
                return
            for p in tmp.glob("*") if tmp.exists() else []:  # lo que no es video (avisos, fotos)
                if p.is_file():
                    p.unlink()
            if tmp.exists():
                tmp.rmdir()
            if not placed:
                raise ValueError("el bot no mandó ningún video")
            dl.set(rid, state="listo", detail=f"{len(placed)} video(s) en la biblioteca", dest=json.dumps(placed, ensure_ascii=False))
            n = f"\n{len(placed)} capítulos" if row["kind"] == "tv" and len(placed) != 1 else ""
            status.show(f"✅ Finalizado: ya está en tu biblioteca\n\n{label}{n}", final=True)
            self.dl.update(message=f"{row['title']}: lista", done=0, total=0, speed=0.0)
            dl.db.execute("UPDATE discover SET have=1 WHERE kind=? AND tmdb=? AND have=0", (row["kind"], row["tmdb"]))
            dl.db.commit()
        except Exception as e:
            if self.dl["stop"] == rid:  # la cancelaste a medias
                shutil.rmtree(Path(d["tmp_dir"]) / str(rid), ignore_errors=True)
                status.show(f"✖️ Cancelada\n\n{label}", final=True)
                return
            log.exception("descargas: %s", row["title"])
            dl.set(rid, state="error", detail=str(e)[:300], retry=time.time() + 6 * 3600)
            done_n = f"\n✅ {len(placed)} capítulo(s) ya están en la biblioteca; el resto se reintenta." if row["kind"] == "tv" and placed else ""
            status.show(f"⚠️ Se cortó la descarga\n\n{label}\n\n{str(e)[:300]}\nLo reintento en unas horas.{done_n}", final=True)
        finally:
            status.close()
            self.dl.update(current=None, what="", stop=None)

    # --- Descubrir -------------------------------------------------------------------
    def _discover(self, mine: bool = False):
        """Descubrir con tus gustos o, si la página la usa un amigo, con los suyos (``mine`` = siempre los tuyos)."""
        cfg = self.cfg()
        if not cfg.get("tmdb", {}).get("api_key"):
            raise ValueError("pon la clave de TheMovieDB en Ajustes")
        friend = None if mine else getattr(self.who, "friend", None)
        lib = friends.FriendLib(self.config_path, cfg, friend["id"]) if friend else self.library(cfg)
        return discover.Discover(lib, cfg)

    def discover_list(self, section: str, page: int) -> dict:
        try:
            return {"ok": True, "items": self._discover().section(section, page)}
        except ValueError as e:
            return {"ok": False, "error": str(e)}

    def discover_search(self, query: str) -> dict:
        try:
            return {"ok": True, "items": self._discover().search(query)}
        except ValueError as e:
            return {"ok": False, "error": str(e)}

    def discover_mark(self, b: dict) -> dict:
        kw = {k: b[k] for k in ("rating", "have") if k in b}
        try:
            self._discover().mark(b["item"], **kw)
        except ValueError as e:
            return {"ok": False, "error": str(e)}
        friend = getattr(self.who, "friend", None)
        if friend and b.get("have") == 0:  # «Agregar a mi lista» de un amigo = te lo pide
            self.friends_ask(friend, {"item": b["item"]})
        return {"ok": True}

    def _dsc(self, fn) -> dict:
        try:
            return {"ok": True, **fn(self._discover())}
        except ValueError as e:
            return {"ok": False, "error": str(e)}

    def discover_taste(self) -> dict:
        return self._dsc(lambda d: {"profile": d.profile(), "genres": d.genre_list(), "stats": d.stats(),
                                     "learned": d.learned_summary()})

    # --- Peticiones de los amigos (página /pedir) ------------------------------------------
    def _friends(self) -> friends.Friends:
        return friends.Friends(self._discover(mine=True))  # las peticiones viven en tu base

    def friends_ask(self, user: dict, b: dict) -> dict:
        f = self._friends()
        new = f"{b['item']['kind']}{b['item']['tmdb']}" not in f.asked(user)
        f.ask(user, b["item"])
        if new:
            it = b["item"]
            what = "📺 Serie" if it["kind"] == "tv" else "🎬 Película"
            year = f" ({it['year']})" if it.get("year") else ""
            self._notify(self.cfg(), f"🙋 {user['name']} pide {what}: {it.get('title', '')}{year}\n\n"
                                     "Míralo en Videotecario › Descubrir › Peticiones.")
        return {"ok": True}

    # --- Avisos para los amigos (campanita y notificaciones al móvil) -----------------------------
    def _notices(self) -> notices.Notices:
        return notices.Notices(self.library().db, Path(self.config_path).parent / "push-key.pem")

    def notice_watch(self) -> None:
        """Cada pocos minutos mira lo nuevo en Jellyfin (llegue por Phono o copiado a mano) y avisa a quien toque."""
        time.sleep(60)
        while True:
            try:
                n = self._notices().check_jellyfin(self.cfg())
                if n:
                    log.info("avisos: %d avisos de cosas nuevas en Jellyfin", n)
            except Exception as e:
                log.warning("avisos: no se pudo mirar lo nuevo en Jellyfin (%s)", e)
            time.sleep(180)

    def friend_notices(self, user: dict) -> dict:
        n = self._notices()
        return {"ok": True, "items": n.list(user["id"]), "unread": n.unread(user["id"]), "following": n.following(user["id"]),
                "key": notices.public_key(n.key_path)}

    def notices_admin(self) -> dict:
        n = self._notices()
        return {"ok": True, "users": n.users(), "follows": n.all_follows()}

    def notices_send(self, b: dict) -> dict:
        n = self._notices()
        text = (b.get("text") or "").strip()
        if not text:
            return {"ok": False, "error": "escribe el aviso"}
        users = [u["id"] for u in n.users()] if b.get("users") == "all" else list(b.get("users") or [])
        if not users:
            return {"ok": False, "error": "elige a quién mandarlo"}
        title, _, body = text.partition("\n")
        phones = n.send(users, "📣 " + title.strip(), body.strip())
        return {"ok": True, "people": len(users), "phones": phones}

    def friends_decide(self, b: dict) -> dict:
        item = self._friends().decide(b["kind"], int(b["tmdb"]), bool(b.get("accept")))
        return {"ok": bool(item), **({"item": item} if item else {"error": "ya no existe"})}


def make_handler(app: App):
    routes_get = {
        "/api/status": lambda q: app.status(),
        "/api/config": lambda q: app.api_config(),
        "/api/folders": lambda q: app.folders(q.get("path", [""])[0]),
        "/api/library": lambda q: app.library_list(),
        "/api/item": lambda q: app.item(int(q["id"][0])),
        "/api/log": lambda q: {"lines": list(app.logs.lines)},
        "/api/trash": lambda q: app.trash(),
        "/api/dupes": lambda q: app.dupes_state(),
        "/api/formats": lambda q: app.formats_state(),
        "/api/names": lambda q: app.names_state(),
        "/api/series": lambda q: app.series_state(),
        "/api/auto": lambda q: app.auto_state(),
        "/api/unknown": lambda q: app.unknown_state(),
        "/api/update": lambda q: app.update_state(),
        "/api/update/news": lambda q: updater.news(force=bool(q.get("force"))),
        "/api/discover": lambda q: app.discover_list(q.get("section", ["novedades"])[0], int(q.get("page", ["1"])[0])),
        "/api/discover/search": lambda q: app.discover_search(q.get("q", [""])[0]),
        "/api/discover/detail": lambda q: app._dsc(lambda d: {"item": d.detail(q["kind"][0], int(q["tmdb"][0]))}),
        "/api/discover/taste": lambda q: app.discover_taste(),
        "/api/discover/people": lambda q: app._dsc(lambda d: {"people": d.people(q.get("q", [""])[0])}),
        "/api/discover/popular-people": lambda q: app._dsc(lambda d: {"people": d.popular_people(int(q.get("page", ["1"])[0]))}),
        "/api/discover/mood": lambda q: app._dsc(lambda d: {"rows": d.mood(q.get("m", [""])[0], q.get("kind", [""])[0])}),
        "/api/discover/deck": lambda q: app._dsc(lambda d: {"items": d.deck()}),
        "/api/downloads": lambda q: app.downloads_state(),
        "/api/discover/platforms": lambda q: app._dsc(lambda d: {"platforms": d.platforms_summary()}),
        "/api/discover/platform": lambda q: app._dsc(lambda d: {"items": d.platform(q.get("p", [""])[0], q.get("kind", ["movie"])[0],
                                                                                   q.get("new", ["1"])[0] == "1", int(q.get("page", ["1"])[0]))}),
        "/api/discover/releases/summary": lambda q: app._dsc(lambda d: {"summary": d.releases_summary()}),
        "/api/avisos": lambda q: app.notices_admin(),
        "/api/requests": lambda q: app._dsc(lambda d: {"items": friends.Friends(d).all()}),
        "/api/discover/releases": lambda q: app._dsc(lambda d: {"items": d.releases(q.get("kind", ["movie"])[0], int(q.get("page", ["1"])[0]))}),
    }
    routes_post = {
        "/api/config": lambda b: app.save_config(b),
        "/api/test-opensubtitles": lambda b: app.test_opensubtitles(b),
        "/api/test-jellyfin": lambda b: app.test_jellyfin(b),
        "/api/run": lambda b: (app.wake.set(), {"ok": True})[1],
        "/api/retry": lambda b: app.retry(int(b["id"])),
        "/api/accept": lambda b: app.accept(int(b["id"]), b["lang"]),
        "/api/reject": lambda b: app.reject(int(b["id"]), b["lang"]),
        "/api/restore": lambda b: app.restore(b["path"]),
        "/api/dupes/scan": lambda b: app.dupes_scan(),
        "/api/formats/scan": lambda b: app.formats_scan(),
        "/api/formats/stop": lambda b: (app.fmt.update(stop=True), {"ok": True})[1],
        "/api/convert/settings": lambda b: app.convert_settings(b),
        "/api/convert/retry": lambda b: app.convert_retry(),
        "/api/convert/restore": lambda b: app.convert_restore(int(b["id"])),
        "/api/dupes/empty-dirs": lambda b: app.empty_dirs(),
        "/api/dupes/move": lambda b: app.dupes_move([int(x) for x in b["ids"]], bool(b.get("all"))),
        "/api/dupes/restore": lambda b: app.dupes_restore(int(b["id"])),
        "/api/names/scan": lambda b: app.names_scan(),
        "/api/names/apply": lambda b: app.names_apply(b),
        "/api/names/organize": lambda b: app.names_organize([int(x) for x in b["ids"]]),
        "/api/names/search": lambda b: app.names_search(int(b["id"]), b["query"]),
        "/api/names/undo": lambda b: app.names_undo(int(b["id"])),
        "/api/names/auto": lambda b: app.names_auto(),
        "/api/series/scan": lambda b: app.series_scan(),
        "/api/series/search": lambda b: app.series_search(b["key"], b["query"]),
        "/api/series/pick": lambda b: app.series_pick(b["key"], str(b["tmdb"])),
        "/api/series/renumber": lambda b: app.series_renumber(b["key"], b["changes"]),
        "/api/series/apply": lambda b: app.series_apply(b["key"], [int(x) for x in b["ids"]]),
        "/api/auto/settings": lambda b: app.auto_settings(b),
        "/api/unknown/discard": lambda b: app.unknown_discard([int(x) for x in b["ids"]]),
        "/api/unknown/restore": lambda b: app.unknown_restore(int(b["id"])),
        "/api/unknown/retry": lambda b: app.unknown_retry(int(b["id"])),
        "/api/unknown/empty": lambda b: app.unknown_empty(),
        "/api/update/now": lambda b: app.update_now(),
        "/api/update/settings": lambda b: app.update_settings(b),
        "/api/names/stop": lambda b: app.names_stop(),
        "/api/test-tmdb": lambda b: app.test_tmdb(b),
        "/api/test-gemini": lambda b: app.test_gemini(b),
        "/api/discover/mark": lambda b: app.discover_mark(b),
        "/api/requests/decide": lambda b: app.friends_decide(b),
        "/api/avisos/send": lambda b: app.notices_send(b),
        "/api/avisos/followers": lambda b: (app._notices().set_followers(b["item"], list(b.get("users") or [])), {"ok": True})[1],
        "/api/requests/forget": lambda b: app._dsc(lambda d: (friends.Friends(d).forget(b["kind"], int(b["tmdb"])), {})[1]),
        "/api/downloads/add": lambda b: app.downloads_add(b),
        "/api/downloads/cancel": lambda b: app.downloads_cancel(int(b["id"])),
        "/api/downloads/retry": lambda b: app.downloads_retry(int(b["id"])),
        "/api/downloads/clear": lambda b: app.downloads_clear(),
        "/api/downloads/seasons": lambda b: app.downloads_seasons(b),
        "/api/downloads/settings": lambda b: app.downloads_settings(b),
        "/api/telegram/code": lambda b: app.telegram_code(b),
        "/api/telegram/login": lambda b: app.telegram_login(b),
        "/api/telegram/logout": lambda b: app.telegram_logout(),
        "/api/telegram/bot": lambda b: app.telegram_bot(b),
        "/api/discover/taste": lambda b: app._dsc(lambda d: {"profile": d.save_profile(b)}),
        "/api/discover/taste/reset": lambda b: app._dsc(lambda d: (d.reset_profile(), {})[1]),
    }

    # la página de los amigos: cada ruta recibe el usuario de Jellyfin que entró
    friend_get = {
        "/api/pedir/notices": lambda u, q: app.friend_notices(u),
        "/api/pedir/browse": lambda u, q: app._dsc(lambda d: {"items": app._friends().browse(
            u, q.get("section", ["novedades"])[0], int(q.get("page", ["1"])[0]), q.get("q", [""])[0].strip())}),
        "/api/pedir/mine": lambda u, q: app._dsc(lambda d: {"items": app._friends().mine(u)}),
    }
    friend_post = {
        "/api/pedir/notices/read": lambda u, b: (app._notices().read_all(u["id"]), {"ok": True})[1],
        "/api/pedir/push": lambda u, b: (app._notices().subscribe(u["id"], b["sub"]), {"ok": True})[1],
        "/api/pedir/push/off": lambda u, b: (app._notices().unsubscribe(u["id"], b.get("endpoint", "")), {"ok": True})[1],
        "/api/pedir/follow": lambda u, b: (app._notices().follow(u["id"], b["item"], bool(b.get("on", True))), {"ok": True})[1],
        "/api/pedir/ask": lambda u, b: app.friends_ask(u, b),
        "/api/pedir/cancel": lambda u, b: app._dsc(lambda d: (app._friends().cancel(u, b["kind"], int(b["tmdb"])), {})[1]),
    }

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):  # sin ruido en el registro
            pass

        def _send(self, code: int, body: bytes, ctype: str, headers: dict | None = None) -> None:
            self.send_response(code)
            headers = {"Cache-Control": "no-store", **(headers or {})}
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            for k, v in headers.items():
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def _json(self, fn, arg, headers: dict | None = None) -> None:
            try:
                data, code = fn(arg), 200
            except Exception as e:
                log.error("API %s: %s", self.path, traceback.format_exc(limit=3))
                data, code, headers = {"ok": False, "error": str(e)}, 500, None
            self._send(code, json.dumps(data, ensure_ascii=False, default=str).encode(), "application/json", headers)

        def _authed(self) -> bool:
            cfg = app.cfg()
            if not auth.enabled(cfg):  # sin contraseña puesta sólo se entra desde casa
                return not friends.from_internet(self.headers)
            return auth.token_ok(cfg, auth.token_from(self.headers.get("Cookie")))

        def _addr(self) -> str:
            return friends.client_ip(self.headers, self.client_address[0]) if friends.from_internet(self.headers) \
                else self.client_address[0]

        def _friend(self) -> dict | None:
            cfg = app.cfg()
            token = self.headers.get("X-Emby-Token")  # la app de la tele (Everflix) entra con su usuario de Jellyfin
            if token:
                return friends.jellyfin_user(cfg, token)
            return friends.user_from(cfg, self.headers.get("Cookie"))

        def _locked(self) -> None:
            """Desde internet sin tu contraseña: a la página de pedir (o una explicación si es la API)."""
            if urlparse(self.path).path.startswith("/api/"):
                return self._send(401, b'{"ok": false, "error": "hay que entrar"}', "application/json")
            if auth.enabled(app.cfg()):
                return self._static("login.html", "text/html; charset=utf-8")
            self._send(302, b"", "text/plain", {"Location": "/pedir"})

        def _as_friend(self, user: dict, fn, arg) -> None:
            """Una ruta de Descubrir hecha por un amigo: con sus gustos, no los tuyos."""
            def run(a):
                app.who.friend = user
                try:
                    return fn(a)
                finally:
                    app.who.friend = None
            self._json(run, arg)

        def _friend_page(self, user: dict) -> None:
            """La página de siempre, sólo con Descubrir (y «Pedir» en vez de descargar)."""
            try:
                app._notices().touch(user)  # para que salga en tu lista de «a quién avisar»
            except Exception as e:
                log.warning("avisos: %s", e)
            flag = json.dumps({"name": user["name"]}, ensure_ascii=False).replace("</", "<\\/")
            html = (STATIC / "index.html").read_text(encoding="utf-8").replace(
                "<body>", f"<body>\n<script>window.FRIEND = {flag};</script>", 1)
            self._send(200, html.encode(), "text/html; charset=utf-8")

        def _static(self, name: str, ctype: str, cache: bool = False) -> None:
            self._send(200, (STATIC / name).read_bytes(), ctype, {"Cache-Control": "max-age=86400"} if cache else None)

        def do_GET(self):
            u = urlparse(self.path)
            if u.path in PUBLIC:
                return self._static(*PUBLIC[u.path], cache=u.path != "/sw.js")
            if u.path == "/health":  # para el HEALTHCHECK de Docker (no dice nada de la biblioteca)
                return self._send(200, b"ok", "text/plain")
            if u.path in ("/pedir", "/pedir/"):
                return self._static("pedir.html", "text/html; charset=utf-8")
            if u.path == "/api/pedir/me":
                user = self._friend()
                return self._send(200, json.dumps({"ok": bool(user), "user": user and user["name"]},
                                                  ensure_ascii=False).encode(), "application/json")
            if u.path in friend_get:
                user = self._friend()
                if not user:
                    return self._send(401, b'{"ok": false, "error": "hay que entrar"}', "application/json")
                return self._json(lambda q: friend_get[u.path](user, q), parse_qs(u.query))
            if u.path == "/entrar" and not self._authed():
                return self._static("login.html", "text/html; charset=utf-8")
            if not self._authed():
                user = self._friend()
                if user and u.path in ("/", "/index.html"):
                    cfg = app.cfg()
                    if auth.enabled(cfg) and friends.is_admin(cfg, user["id"]):  # eres tú: todo Videotecario
                        return self._send(200, (STATIC / "index.html").read_bytes(), "text/html; charset=utf-8",
                                          {"Set-Cookie": auth.cookie(auth.make_token(cfg))})
                    return self._friend_page(user)
                if user and u.path.startswith("/api/discover"):
                    if u.path in routes_get:
                        return self._as_friend(user, routes_get[u.path], parse_qs(u.query))
                if friends.from_internet(self.headers) and u.path in ("/", "/index.html"):
                    return self._send(302, b"", "text/plain", {"Location": "/pedir"})
                return self._locked()
            if u.path == "/entrar":
                return self._send(302, b"", "text/plain", {"Location": "/"})
            if u.path in routes_get:
                return self._json(routes_get[u.path], parse_qs(u.query))
            if u.path in ("/api/names/frame", "/api/series/frame"):
                q = parse_qs(u.query)
                get = app.names_frame if u.path == "/api/names/frame" else app.series_frame
                try:
                    img = get(int(q["id"][0]), int(q["n"][0]))
                except (KeyError, ValueError):
                    img = None
                if not img:
                    return self._send(404, b"", "text/plain")
                self.send_response(200)
                self.send_header("Content-Type", "image/jpeg")
                self.send_header("Content-Length", str(len(img)))
                self.send_header("Cache-Control", "max-age=86400")
                self.end_headers()
                return self.wfile.write(img)
            if u.path == "/api/names/video":  # ver la película en la página para decidir qué es (con saltos: Range)
                try:
                    m = app.library(app.cfg()).media("id=?", (int(parse_qs(u.query)["id"][0]),))
                except (KeyError, ValueError):
                    m = []
                if not m or not os.path.exists(m[0].path):
                    return self._send(404, b"", "text/plain")
                return self._send_video(m[0].path)
            if u.path in ("/", "/index.html"):
                return self._send(200, (STATIC / "index.html").read_bytes(), "text/html; charset=utf-8")
            self._send(404, b"no encontrado", "text/plain")

        def _send_video(self, path: str):
            size = os.path.getsize(path)
            start, end = 0, size - 1
            rng = re.match(r"bytes=(\d*)-(\d*)", self.headers.get("Range") or "")
            if rng and (rng.group(1) or rng.group(2)):
                if rng.group(1):
                    start = int(rng.group(1))
                    end = min(int(rng.group(2)), size - 1) if rng.group(2) else size - 1
                else:
                    start = max(0, size - int(rng.group(2)))
                if start >= size:
                    self.send_response(416)
                    self.send_header("Content-Range", f"bytes */{size}")
                    return self.end_headers()
                end = min(end, start + 8 * 1024 * 1024 - 1)  # trozos de 8 MB: el navegador pide lo siguiente
                self.send_response(206)
                self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
            else:
                self.send_response(200)
            ext = Path(path).suffix.lower()
            self.send_header("Content-Type", {".mp4": "video/mp4", ".m4v": "video/mp4", ".webm": "video/webm",
                                              ".mkv": "video/webm", ".mov": "video/quicktime"}.get(ext, "video/mp4"))
            self.send_header("Accept-Ranges", "bytes")
            self.send_header("Content-Length", str(end - start + 1))
            self.end_headers()
            try:
                with open(path, "rb") as f:
                    f.seek(start)
                    left = end - start + 1
                    while left > 0:
                        chunk = f.read(min(1024 * 1024, left))
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        left -= len(chunk)
            except (BrokenPipeError, ConnectionResetError):
                pass  # el navegador cortó (saltó a otro punto o cerró)

        def do_POST(self):
            u = urlparse(self.path)
            n = int(self.headers.get("Content-Length") or 0)
            if n > 5_000_000:
                return self._send(413, b"demasiado grande", "text/plain")
            raw = self.rfile.read(n)
            if u.path == "/api/login":
                return self._login(json.loads(raw or b"{}"))
            if u.path == "/api/logout":
                return self._send(200, b'{"ok": true}', "application/json", {"Set-Cookie": auth.cookie("", 0)})
            if u.path == "/api/pedir/login":
                return self._friend_login(json.loads(raw or b"{}"))
            if u.path == "/api/pedir/logout":
                return self._send(200, b'{"ok": true}', "application/json", {"Set-Cookie": friends.cookie("", 0)})
            if u.path in friend_post:
                user = self._friend()
                if not user:
                    return self._send(401, b'{"ok": false, "error": "hay que entrar"}', "application/json")
                return self._json(lambda b: friend_post[u.path](user, b), json.loads(raw or b"{}"))
            if not self._authed():
                user = self._friend()
                if user and u.path in FRIEND_POST:
                    return self._as_friend(user, routes_post[u.path], json.loads(raw or b"{}"))
                return self._locked()
            if u.path not in routes_post:
                return self._send(404, b"no encontrado", "text/plain")
            body = json.loads(raw or b"{}")
            headers = None
            if u.path == "/api/config" and "access" in body:
                # quien acaba de poner (o cambiar) la contraseña sigue dentro sin tener que entrar otra vez
                headers = {"Set-Cookie": auth.cookie("", 0)}

                def save(b):
                    out = app.save_config(b)
                    cfg = app.cfg()
                    if auth.enabled(cfg):
                        headers["Set-Cookie"] = auth.cookie(auth.make_token(cfg))
                    return out
                return self._json(save, body, headers)
            self._json(routes_post[u.path], body)

        def _login(self, b: dict) -> None:
            cfg, addr = app.cfg(), self._addr()
            if not auth.enabled(cfg):
                if friends.from_internet(self.headers):
                    return self._send(403, json.dumps({"ok": False, "error": "Desde internet sólo se entra con usuario y "
                                                       "contraseña: ponlos en Ajustes desde casa."}, ensure_ascii=False).encode(),
                                      "application/json")
                return self._send(200, b'{"ok": true}', "application/json")
            if auth.blocked(addr):
                return self._send(429, json.dumps({"ok": False, "error": "Demasiados intentos. Espera unos minutos."},
                                                  ensure_ascii=False).encode(), "application/json")
            token = auth.login(cfg, addr, b.get("user", ""), b.get("password", ""))
            if not token and cfg.get("jellyfin", {}).get("url"):
                # ¿la olvidaste? un administrador de Jellyfin también entra (y se pone otra en Ajustes)
                try:
                    jf = friends.jellyfin_login(cfg, (b.get("user") or "").strip(), b.get("password") or "")
                except Exception as e:
                    log.info("entrar: Jellyfin no respondió (%s)", e)
                    jf = None
                if jf and jf["admin"]:
                    auth.note(addr, True)
                    token = auth.make_token(cfg)
                    log.info("entrada con el administrador de Jellyfin %s desde %s", jf["name"], addr)
            if not token:
                log.warning("intento de entrar con usuario o contraseña incorrectos desde %s", addr)
                return self._send(401, json.dumps({"ok": False, "error": "Usuario o contraseña incorrectos"},
                                                  ensure_ascii=False).encode(), "application/json")
            log.info("entrada a la página desde %s", addr)
            self._send(200, b'{"ok": true}', "application/json", {"Set-Cookie": auth.cookie(token)})

        def _friend_login(self, b: dict) -> None:
            """Los amigos entran con su usuario y contraseña de Jellyfin."""
            cfg, addr = app.cfg(), self._addr()
            if auth.blocked(addr):
                return self._send(429, json.dumps({"ok": False, "error": "Demasiados intentos. Espera unos minutos."},
                                                  ensure_ascii=False).encode(), "application/json")
            try:
                user = friends.jellyfin_login(cfg, (b.get("user") or "").strip(), b.get("password") or "")
            except Exception as e:
                log.warning("pedir: Jellyfin no respondió al entrar (%s)", e)
                return self._send(502, json.dumps({"ok": False, "error": "No se pudo hablar con Jellyfin. Prueba en un rato."},
                                                  ensure_ascii=False).encode(), "application/json")
            auth.note(addr, bool(user))
            if not user:
                time.sleep(1)
                log.warning("pedir: usuario o contraseña de Jellyfin incorrectos desde %s", addr)
                return self._send(401, json.dumps({"ok": False, "error": "Usuario o contraseña incorrectos"},
                                                  ensure_ascii=False).encode(), "application/json")
            log.info("pedir: entró %s desde %s", user["name"], addr)
            if user["admin"] and auth.enabled(cfg):  # tú: Videotecario completo
                return self._send(200, json.dumps({"ok": True, "user": user["name"], "admin": True}, ensure_ascii=False).encode(),
                                  "application/json", {"Set-Cookie": auth.cookie(auth.make_token(cfg))})
            self._send(200, json.dumps({"ok": True, "user": user["name"]}, ensure_ascii=False).encode(), "application/json",
                       {"Set-Cookie": friends.cookie(friends.make_token(cfg, user))})

    return Handler


def serve(config_path: str, port: int | None = None) -> None:
    app = App(config_path)
    cfg = app.cfg()
    updater.apply_timezone(cfg.get("timezone", ""))
    port = port or cfg["web_port"]
    threading.Thread(target=app.worker, name="trabajador", daemon=True).start()
    threading.Thread(target=app.converter, name="conversión", daemon=True).start()
    threading.Thread(target=app.automatic, name="automático", daemon=True).start()
    app.update_finished()
    threading.Thread(target=app.updates, name="actualizaciones", daemon=True).start()
    threading.Thread(target=app.downloader, name="descargas", daemon=True).start()
    threading.Thread(target=app.notice_watch, name="avisos", daemon=True).start()
    threading.Thread(target=bot.Bot(app).run, name="bot", daemon=True).start()
    httpd = ThreadingHTTPServer(("0.0.0.0", port), make_handler(app))
    log.info("Videotecario en http://<ip-del-nas>:%d%s", port,
             " (pide usuario y contraseña)" if auth.enabled(cfg) else "")
    httpd.serve_forever()
