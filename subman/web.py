"""Interfaz web + trabajador en segundo plano.

Un solo proceso: un hilo recorre la biblioteca cada ``interval_minutes`` (o cuando se pulsa
"Procesar ahora") y un servidor HTTP sirve la página y una pequeña API JSON. Pensado para la
red de casa: no tiene usuarios ni contraseña.
"""
from __future__ import annotations

import collections
import json
import logging
import logging.handlers
import os
import shutil
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from . import __version__, autoid, config, convert, diagnose, discover, duplicates, formats, gemini, identify, jellyfin, manager, peek, providers, tmdb
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
                      "last_start": None, "last_end": None, "next_run": None, "message": "", "scan": None}
        self.dupes = {"running": False, "message": "", "groups": None, "at": None}
        self.names = {"running": False, "message": "", "result": None, "at": None, "auto": None, "stop": False}
        self.fmt = {"running": False, "message": "", "done": 0, "total": 0, "error": None, "stop": False}
        self.conv = {"current": "", "what": "", "progress": 0.0, "message": "apagado", "started": None,
                     "done": 0, "stop": False}
        self.conv_wake = threading.Event()
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
                     pub.get("gemini", {})]:
            for f in SECRET_FIELDS:
                if f in prov:
                    prov[f] = MASK if prov.get(f) else ""
        return {"config": pub, "problems": config.problems(cfg), "media_root": MEDIA_ROOT}

    def save_config(self, new: dict) -> dict:
        with self.lock:
            cfg = self.cfg()
            for k in ("enabled", "library", "languages", "candidates", "keep_review", "sync_existing",
                      "retry_hours", "archive_languages", "dry_run", "interval_minutes", "duplicates_dir"):
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
                "missing": diagnose.summary(reasons)}

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


    # --- duplicados ----------------------------------------------------------------
    def dupes_scan(self) -> dict:
        if self.dupes["running"]:
            return {"ok": True}
        self.dupes.update(running=True, message="empezando", error=None)

        def work():
            try:
                cfg = self.cfg()
                lib = self.library(cfg)
                if not self.state["running"] and cfg["library"]:  # lista de videos al día (la pasada ya la hace)
                    lib.scan(cfg["library"], exclude=config.excluded(cfg), progress=lambda n: self.dupes.update(
                        message=f"buscando videos: {n} encontrados"))
                groups = duplicates.find(lib, cfg, lambda msg: self.dupes.update(message=msg))
                self.dupes.update(groups=groups, at=time.time(), message="")
                log.info("duplicados: %d grupos", len(groups))
            except Exception as e:
                log.exception("duplicados")
                self.dupes.update(message="", error=str(e))
            finally:
                self.dupes.update(running=False)

        threading.Thread(target=work, name="duplicados", daemon=True).start()
        return {"ok": True}

    def dupes_state(self) -> dict:
        cfg = self.cfg()
        return {**self.dupes, "dest": cfg["duplicates_dir"], "moved": duplicates.moved_list(self.library(cfg)),
                "jellyfin": bool(cfg.get("jellyfin", {}).get("url") and cfg.get("jellyfin", {}).get("api_key"))}

    def dupes_move(self, ids: list[int]) -> dict:
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
            if len(pick) >= len(g["files"]):
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

        def work():
            try:
                cfg = self.cfg()
                lib = self.library(cfg)
                if not self.state["running"] and cfg["library"]:
                    lib.scan(cfg["library"], exclude=config.excluded(cfg), progress=lambda n: self.names.update(
                        message=f"buscando videos: {n} encontrados"))
                rev = identify.review(lib, cfg, lambda msg: self.names.update(message=msg), search=False)
                res = autoid.run(lib, cfg, rev, lambda msg: self.names.update(message=msg),
                                 stop=lambda: self.names["stop"], counts=lambda c: self.names.update(counts=c),
                                 frames_dir=self.frames_dir)
                now = time.time()
                self.names.update(result={"todo": res["todo"], "loose": []}, at=now, message="",
                                  auto={"applied": len(res["applied"]),
                                        "ai": sum(a["how"] == "ia" for a in res["applied"]), "copies": sum(bool(a.get("copy")) for a in res["applied"]),
                                        "left": len(res["todo"]), "at": now})
                if res["applied"]:
                    self._jellyfin_refresh(cfg)
            except Exception as e:
                log.exception("identificar todo")
                self.names.update(message="", error=str(e))
            finally:
                self.names.update(running=False)

        threading.Thread(target=work, name="identificar", daemon=True).start()
        return {"ok": True}

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
        try:
            title = identify.undo(self.library(cfg), row_id)
        except (ValueError, OSError) as e:
            return {"ok": False, "error": str(e)}
        self._jellyfin_refresh(cfg)
        return {"ok": True, "title": title}

    def dupes_restore(self, row_id: int) -> dict:
        cfg = self.cfg()
        try:
            dest = duplicates.restore(self.library(cfg), row_id)
        except (ValueError, OSError) as e:
            return {"ok": False, "error": str(e)}
        self.wake.set()  # que vuelva a aparecer en la biblioteca
        return {"ok": True, "dest": dest}

    # --- Descubrir -------------------------------------------------------------------
    def _discover(self):
        cfg = self.cfg()
        if not cfg.get("tmdb", {}).get("api_key"):
            raise ValueError("pon la clave de TheMovieDB en Ajustes")
        return discover.Discover(self.library(cfg), cfg)

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
        return {"ok": True}

    def _dsc(self, fn) -> dict:
        try:
            return {"ok": True, **fn(self._discover())}
        except ValueError as e:
            return {"ok": False, "error": str(e)}

    def discover_taste(self) -> dict:
        return self._dsc(lambda d: {"profile": d.profile(), "genres": d.genre_list(), "stats": d.stats()})


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
        "/api/discover": lambda q: app.discover_list(q.get("section", ["novedades"])[0], int(q.get("page", ["1"])[0])),
        "/api/discover/search": lambda q: app.discover_search(q.get("q", [""])[0]),
        "/api/discover/detail": lambda q: app._dsc(lambda d: {"item": d.detail(q["kind"][0], int(q["tmdb"][0]))}),
        "/api/discover/taste": lambda q: app.discover_taste(),
        "/api/discover/people": lambda q: app._dsc(lambda d: {"people": d.people(q.get("q", [""])[0])}),
        "/api/discover/popular-people": lambda q: app._dsc(lambda d: {"people": d.popular_people(int(q.get("page", ["1"])[0]))}),
        "/api/discover/deck": lambda q: app._dsc(lambda d: {"items": d.deck()}),
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
        "/api/dupes/move": lambda b: app.dupes_move([int(x) for x in b["ids"]]),
        "/api/dupes/restore": lambda b: app.dupes_restore(int(b["id"])),
        "/api/names/scan": lambda b: app.names_scan(),
        "/api/names/apply": lambda b: app.names_apply(b),
        "/api/names/organize": lambda b: app.names_organize([int(x) for x in b["ids"]]),
        "/api/names/search": lambda b: app.names_search(int(b["id"]), b["query"]),
        "/api/names/undo": lambda b: app.names_undo(int(b["id"])),
        "/api/names/auto": lambda b: app.names_auto(),
        "/api/names/stop": lambda b: app.names_stop(),
        "/api/test-tmdb": lambda b: app.test_tmdb(b),
        "/api/test-gemini": lambda b: app.test_gemini(b),
        "/api/discover/mark": lambda b: app.discover_mark(b),
        "/api/discover/taste": lambda b: app._dsc(lambda d: {"profile": d.save_profile(b)}),
        "/api/discover/taste/reset": lambda b: app._dsc(lambda d: (d.reset_profile(), {})[1]),
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
            if u.path == "/api/names/frame":
                q = parse_qs(u.query)
                try:
                    img = app.names_frame(int(q["id"][0]), int(q["n"][0]))
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
    threading.Thread(target=app.converter, name="conversión", daemon=True).start()
    httpd = ThreadingHTTPServer(("0.0.0.0", port), make_handler(app))
    log.info("interfaz web en http://<ip-del-nas>:%d", port)
    httpd.serve_forever()
