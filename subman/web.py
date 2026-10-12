"""Interfaz web + trabajador en segundo plano.

Un solo proceso: un hilo recorre la biblioteca cada ``interval_minutes`` (o cuando se pulsa
"Procesar ahora") y un servidor HTTP sirve la página y una pequeña API JSON. Pensado para la
red de casa: no tiene usuarios ni contraseña.
"""
from __future__ import annotations

import collections
import json
import logging
import os
import shutil
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import __version__, config, manager, providers
from .library import Library

log = logging.getLogger(__name__)
STATIC = Path(__file__).parent / "static"
MEDIA_ROOT = os.environ.get("SUBMAN_MEDIA_ROOT", "/media")
SECRET_FIELDS = ("api_key", "password")
MASK = "••••••••"


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
        self.wake = threading.Event()
        self.lock = threading.Lock()
        self.state = {"running": False, "current": "", "done": 0, "total": 0,
                      "last_start": None, "last_end": None, "next_run": None, "message": ""}
        self.logs = LogBuffer()
        logging.getLogger().addHandler(self.logs)

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
                message=f"buscando videos: {n} encontrados"))
            log.info("escaneo: %s", stats)
            items = lib.media()
            self.state.update(total=len(items), message="trabajando")
            for n, m in enumerate(items):
                self.state.update(current=m.label, done=n)
                try:
                    res = manager.process(m, lib, provs, settings)
                    if res:
                        log.info("%s: %s", m.label, res)
                except Exception:
                    log.exception("%s: error", m.label)
                if not self.cfg()["enabled"]:
                    log.info("pausado desde la web")
                    break
            self.state.update(done=len(items), message="terminado")
        except Exception as e:
            log.exception("fallo en la pasada")
            self.state.update(message=f"error: {e}")
        finally:
            self.state.update(running=False, current="", last_end=time.time())

    # --- API -----------------------------------------------------------------------
    def api_config(self) -> dict:
        cfg = self.cfg()
        pub = json.loads(json.dumps(cfg))
        for prov in pub.get("providers", {}).values():
            for f in SECRET_FIELDS:
                prov[f] = MASK if prov.get(f) else ""
        return {"config": pub, "problems": config.problems(cfg), "media_root": MEDIA_ROOT}

    def save_config(self, new: dict) -> dict:
        with self.lock:
            cfg = self.cfg()
            for k in ("enabled", "library", "languages", "candidates", "keep_review", "sync_existing",
                      "retry_hours", "archive_languages", "dry_run", "interval_minutes"):
                if k in new:
                    cfg[k] = new[k]
            for name, values in new.get("providers", {}).items():
                cur = cfg.setdefault("providers", {}).setdefault(name, {})
                for k, v in values.items():
                    if k in SECRET_FIELDS and v == MASK:
                        continue  # sin cambios
                    cur[k] = v.strip() if isinstance(v, str) else v
            cfg["candidates"] = int(cfg["candidates"])
            cfg["interval_minutes"] = max(5, int(cfg["interval_minutes"]))
            cfg["retry_hours"] = float(cfg["retry_hours"])
            config.save(self.config_path, cfg)
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
                               "updated": r["updated"] if r else None}
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
        try:
            lib = self.library(cfg)
            total = len(lib.media())
            for langs in lib.statuses().values():
                for lang, r in langs.items():
                    if lang in cfg["languages"]:
                        counts[r["status"]] += 1
        except Exception:
            total = 0
        return {"version": __version__, "state": self.state, "enabled": cfg["enabled"],
                "problems": config.problems(cfg), "media": total, "counts": counts}


def make_handler(app: App):
    routes_get = {
        "/api/status": lambda q: app.status(),
        "/api/config": lambda q: app.api_config(),
        "/api/folders": lambda q: app.folders(q.get("path", [""])[0]),
        "/api/library": lambda q: app.library_list(),
        "/api/item": lambda q: app.item(int(q["id"][0])),
        "/api/log": lambda q: {"lines": list(app.logs.lines)},
        "/api/trash": lambda q: app.trash(),
    }
    routes_post = {
        "/api/config": lambda b: app.save_config(b),
        "/api/test-opensubtitles": lambda b: app.test_opensubtitles(b),
        "/api/run": lambda b: (app.wake.set(), {"ok": True})[1],
        "/api/retry": lambda b: app.retry(int(b["id"])),
        "/api/accept": lambda b: app.accept(int(b["id"]), b["lang"]),
        "/api/reject": lambda b: app.reject(int(b["id"]), b["lang"]),
        "/api/restore": lambda b: app.restore(b["path"]),
    }

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):  # sin ruido en el registro
            pass

        def _send(self, code: int, body: bytes, ctype: str) -> None:
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _json(self, fn, arg) -> None:
            try:
                data, code = fn(arg), 200
            except Exception as e:
                log.error("API %s: %s", self.path, traceback.format_exc(limit=3))
                data, code = {"ok": False, "error": str(e)}, 500
            self._send(code, json.dumps(data, ensure_ascii=False, default=str).encode(), "application/json")

        def do_GET(self):
            u = urlparse(self.path)
            if u.path in routes_get:
                return self._json(routes_get[u.path], parse_qs(u.query))
            if u.path in ("/", "/index.html"):
                return self._send(200, (STATIC / "index.html").read_bytes(), "text/html; charset=utf-8")
            self._send(404, b"no encontrado", "text/plain")

        def do_POST(self):
            u = urlparse(self.path)
            if u.path not in routes_post:
                return self._send(404, b"no encontrado", "text/plain")
            n = int(self.headers.get("Content-Length") or 0)
            body = json.loads(self.rfile.read(n) or b"{}")
            self._json(routes_post[u.path], body)

    return Handler


def serve(config_path: str, port: int | None = None) -> None:
    app = App(config_path)
    cfg = app.cfg()
    port = port or cfg["web_port"]
    threading.Thread(target=app.worker, name="trabajador", daemon=True).start()
    httpd = ThreadingHTTPServer(("0.0.0.0", port), make_handler(app))
    log.info("interfaz web en http://<ip-del-nas>:%d", port)
    httpd.serve_forever()
