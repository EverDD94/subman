"""YouTube sin anuncios para Everflix: yt-dlp busca y saca el vídeo; la tele lo reproduce como uno más.

- Buscar: «ytsearch» de yt-dlp (sin la API de Google, sin clave).
- Reproducir: si YouTube da HLS (vídeo y audio juntos, hasta 1080p) la tele lo abre directo.
  Si no, el NAS junta el vídeo H.264 y el audio AAC con ffmpeg SIN recodificar (-c copy, casi no gasta
  CPU) en una lista HLS que se sirve desde aquí. Último recurso: el MP4 de 360p de siempre.
- yt-dlp se actualiza solo (al arrancar y cada semana) en /config/yt-dlp, porque YouTube cambia a menudo.
- Historial por usuario en /config/youtube.json (lo último que vio cada uno, para la portada).
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import os
import secrets
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

log = logging.getLogger(__name__)

WEEK = 7 * 24 * 3600
MAX_H = 1080


class YouTube:
    def __init__(self, config_dir: Path):
        self.dir = Path(config_dir)
        self.lib = self.dir / "yt-dlp"  # yt-dlp actualizado (si no, el de la imagen)
        self.work = Path(os.environ.get("YT_TMP", "/tmp/everflix-yt"))
        self.state_file = self.dir / "youtube.json"
        self.lock = threading.Lock()
        self.cache: dict[str, tuple[float, object]] = {}
        self.mux: dict[str, dict] = {}  # id -> {"proc", "dir", "used"}
        self.state = self._load()
        if not self.state.get("secret"):
            self.state["secret"] = secrets.token_hex(16)
            self._save()

    # ---------- yt-dlp ----------
    def _env(self) -> dict:
        env = dict(os.environ)
        if (self.lib / "yt_dlp").is_dir():
            env["PYTHONPATH"] = str(self.lib) + os.pathsep + env.get("PYTHONPATH", "")
        return env

    @property
    def cookies(self) -> Path:
        return self.dir / "youtube-cookies.txt"

    def linked(self) -> bool:
        return self.cookies.is_file() and self.cookies.stat().st_size > 100

    def link(self, text: str) -> None:
        """Guarda la sesión de YouTube exportada del navegador (cookies.txt de Netscape)."""
        text = (text or "").replace("\r\n", "\n")
        if ".youtube.com" not in text or ("SAPISID" not in text and "__Secure-3PAPISID" not in text):
            raise ValueError("ese archivo no tiene la sesión de YouTube (exporta las cookies estando dentro de youtube.com)")
        if not text.startswith("# Netscape") and not text.startswith("# HTTP"):
            text = "# Netscape HTTP Cookie File\n" + text
        if self.cookies.is_file() and self.cookies.read_text(encoding="utf-8") == text:
            return
        self.cookies.write_text(text, encoding="utf-8")
        os.chmod(self.cookies, 0o600)
        self.state["pushed"] = time.time()
        self._save()
        self.cache = {k: v for k, v in self.cache.items() if not k.startswith("f:")}

    def mine(self, uid: str) -> bool:
        """¿Es esta persona la dueña de la cuenta ligada? (sólo a ella le sale)."""
        return bool(uid) and self.state.get("owner") == uid and self.linked()

    def pair_code(self, uid: str) -> str:
        """El código para la extensión. La primera persona (administradora) que lo pide queda como dueña."""
        if not self.state.get("owner"):
            self.state["owner"] = uid
            self._save()
        if self.state["owner"] != uid:
            return ""
        if not self.state.get("pair"):
            self.state["pair"] = secrets.token_hex(4).upper()
            self._save()
        return self.state["pair"]

    def paired(self, key: str) -> bool:
        return bool(self.state.get("pair")) and hmac.compare_digest(key.strip().upper(), self.state["pair"])

    def unlink(self) -> None:
        self.cookies.unlink(missing_ok=True)
        self.state.pop("owner", None)
        self.state.pop("pair", None)
        self._save()
        self.cache = {k: v for k, v in self.cache.items() if not k.startswith("f:")}

    def _ytdlp(self, args: list[str], timeout: int = 60, account: bool = False) -> dict:
        acct = ["--cookies", str(self.cookies)] if account and self.linked() else []
        cmd = [sys.executable, "-m", "yt_dlp", "--no-warnings", "--ignore-config", "-J", *acct, *args]
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, env=self._env())
        if r.returncode != 0 or not r.stdout.strip():
            err = (r.stderr or "").strip().splitlines()
            raise RuntimeError(err[-1] if err else "yt-dlp no respondió")
        return json.loads(r.stdout)

    def version(self) -> str:
        try:
            r = subprocess.run([sys.executable, "-m", "yt_dlp", "--version"], capture_output=True, text=True,
                               timeout=20, env=self._env())
            return r.stdout.strip()
        except Exception:
            return ""

    def update(self) -> None:
        """Baja la última versión de yt-dlp (con su parte de JavaScript) a /config/yt-dlp."""
        tmp = self.dir / "yt-dlp.nuevo"
        shutil.rmtree(tmp, ignore_errors=True)
        r = subprocess.run([sys.executable, "-m", "pip", "install", "-q", "--no-cache-dir", "--upgrade",
                            "--target", str(tmp), "yt-dlp[default]"], capture_output=True, text=True, timeout=600)
        if r.returncode != 0 or not (tmp / "yt_dlp").is_dir():
            shutil.rmtree(tmp, ignore_errors=True)
            raise RuntimeError((r.stderr or "pip falló").strip().splitlines()[-1])
        old = self.dir / "yt-dlp.viejo"
        shutil.rmtree(old, ignore_errors=True)
        if self.lib.exists():
            self.lib.rename(old)
        tmp.rename(self.lib)
        shutil.rmtree(old, ignore_errors=True)
        self.state["updated"] = time.time()
        self._save()
        log.info("YouTube: yt-dlp actualizado a %s", self.version())

    def updates(self) -> None:
        """Hilo: actualiza al arrancar (si hace más de un día) y luego cada semana."""
        time.sleep(90)
        while True:
            if not self.lib.exists() or time.time() - self.state.get("updated", 0) > WEEK:
                try:
                    self.update()
                except Exception as e:
                    log.warning("YouTube: no se pudo actualizar yt-dlp (%s)", e)
            self._clean_mux()
            time.sleep(3600)

    # ---------- buscar ----------
    def _cached(self, key: str, ttl: int, fn):
        hit = self.cache.get(key)
        if hit and hit[0] > time.time():
            return hit[1]
        val = fn()
        self.cache[key] = (time.time() + ttl, val)
        if len(self.cache) > 300:
            for k in sorted(self.cache, key=lambda k: self.cache[k][0])[:100]:
                self.cache.pop(k, None)
        return val

    @staticmethod
    def _item(e: dict) -> dict | None:
        vid = e.get("id")
        if not vid or len(vid) != 11 or e.get("live_status") in ("is_upcoming",):
            return None
        return {
            "id": vid,
            "title": e.get("title") or "",
            "channel": e.get("channel") or e.get("uploader") or "",
            "channel_id": e.get("channel_id") or "",
            "duration": int(e.get("duration") or 0),
            "views": int(e.get("view_count") or 0),
            "live": e.get("live_status") == "is_live",
            "thumb": f"https://i.ytimg.com/vi/{vid}/hqdefault.jpg",
        }

    def search(self, q: str, n: int = 30) -> list[dict]:
        q = q.strip()[:120]
        if not q:
            return []
        def go():
            data = self._ytdlp(["--flat-playlist", f"ytsearch{n}:{q}"])
            return [i for i in map(self._item, data.get("entries") or []) if i]
        return self._cached("s:" + q.lower(), 1800, go)

    FEEDS = {"inicio": ":ytrec", "suscripciones": ":ytsubs", "historial": ":ythis", "tarde": ":ytwatchlater"}

    def feed(self, tab: str, account: bool, uid: str = "", n: int = 48) -> list[dict]:
        """Una pestaña de tu cuenta. Sin cuenta ligada, Inicio son las tendencias e Historial lo visto en Everflix."""
        if not (account and self.mine(uid)):
            if tab == "historial":
                return self.history(uid)
            return self.search("tendencias", n) if tab == "inicio" else []
        url = self.FEEDS.get(tab)
        if not url:
            raise ValueError("pestaña desconocida")
        def go():
            data = self._ytdlp(["--flat-playlist", "--playlist-end", str(n), url], timeout=120, account=True)
            return [i for i in map(self._item, data.get("entries") or []) if i]
        return self._cached("f:" + tab, 600 if tab != "historial" else 120, go)

    def channel(self, cid: str, n: int = 40) -> dict:
        if not cid.startswith("UC") or len(cid) != 24 or not all(c.isalnum() or c in "-_" for c in cid):
            raise ValueError("canal no válido")
        def go():
            data = self._ytdlp(["--flat-playlist", "--playlist-end", str(n), f"https://www.youtube.com/channel/{cid}/videos"], timeout=90)
            return {"title": data.get("channel") or data.get("title") or "", "items": [i for i in map(self._item, data.get("entries") or []) if i]}
        return self._cached("c:" + cid, 3600, go)

    def mark_watched(self, vid: str) -> None:
        """Lo apunta en tu historial de YouTube (sólo con cuenta ligada), sin esperar."""
        if not self.linked():
            return
        _check(vid)
        def go():
            try:
                subprocess.run([sys.executable, "-m", "yt_dlp", "--ignore-config", "-q", "--simulate", "--mark-watched",
                                "--cookies", str(self.cookies), f"https://www.youtube.com/watch?v={vid}"],
                               capture_output=True, timeout=120, env=self._env())
            except Exception as e:
                log.info("YouTube: no se pudo marcar visto (%s)", e)
        threading.Thread(target=go, daemon=True).start()

    # ---------- reproducir ----------
    def info(self, vid: str) -> dict:
        _check(vid)
        return self._cached("i:" + vid, 1800, lambda: self._ytdlp(
            ["--no-playlist", "--extractor-args", "youtube:player_client=default,ios,tv",
             f"https://www.youtube.com/watch?v={vid}"], timeout=90))

    def play(self, vid: str, mode: str = "") -> dict:
        """Lo que la tele tiene que abrir. mode=nas obliga a pasar por el NAS (p. ej. desde fuera de casa)."""
        info = self.info(vid)
        fmts = info.get("formats") or []
        item = self._item(info) or {"id": vid}
        item["description"] = (info.get("description") or "")[:600]
        hls = [f for f in fmts if "m3u8" in (f.get("protocol") or "") and f.get("vcodec") not in (None, "none")
               and f.get("acodec") not in (None, "none") and (f.get("height") or 0) <= MAX_H
               and (f.get("vcodec") or "").startswith("avc")]
        hls.sort(key=lambda f: (f.get("height") or 0, f.get("tbr") or 0))
        if info.get("live_status") == "is_live" and info.get("manifest_url"):
            return {**item, "url": info["manifest_url"], "format": "hls", "height": 0}
        if hls and (hls[-1].get("height") or 0) >= 720 and mode != "nas":
            f = hls[-1]
            return {**item, "url": f["url"], "format": "hls", "height": f.get("height")}
        video = [f for f in fmts if f.get("acodec") == "none" and (f.get("vcodec") or "").startswith("avc")
                 and (f.get("height") or 0) <= MAX_H and f.get("protocol", "").startswith("http")]
        audio = [f for f in fmts if f.get("vcodec") == "none" and f.get("ext") == "m4a" and f.get("protocol", "").startswith("http")]
        video.sort(key=lambda f: (f.get("height") or 0, f.get("tbr") or 0))
        audio.sort(key=lambda f: f.get("abr") or f.get("tbr") or 0)
        if video and audio and shutil.which("ffmpeg"):
            v, a = video[-1], audio[-1]
            self._start_mux(vid, v["url"], a["url"])
            return {**item, "url": f"/yt/{self.sign(vid)}/{vid}/index.m3u8", "format": "hls", "height": v.get("height"), "nas": True}
        prog = [f for f in fmts if f.get("ext") == "mp4" and f.get("acodec") not in (None, "none")
                and f.get("vcodec") not in (None, "none") and f.get("protocol", "").startswith("http")]
        prog.sort(key=lambda f: f.get("height") or 0)
        if prog:
            return {**item, "url": prog[-1]["url"], "format": "mp4", "height": prog[-1].get("height")}
        raise RuntimeError("YouTube no dio un formato que la tele pueda abrir")

    def sign(self, vid: str) -> str:
        return hmac.new(self.state["secret"].encode(), vid.encode(), hashlib.sha256).hexdigest()[:20]

    def signed(self, sig: str, vid: str) -> bool:
        return hmac.compare_digest(sig, self.sign(vid))

    def _start_mux(self, vid: str, vurl: str, aurl: str) -> None:
        with self.lock:
            m = self.mux.get(vid)
            if m and m["proc"].poll() in (None, 0) and (m["dir"] / "index.m3u8").exists():
                m["used"] = time.time()
                return
            if m:
                _kill(m["proc"])
            out = self.work / vid
            shutil.rmtree(out, ignore_errors=True)
            out.mkdir(parents=True, exist_ok=True)
            def src(url):
                return (["-headers", "User-Agent: Mozilla/5.0\r\n"] if url.startswith("http") else []) + ["-i", url]
            cmd = ["ffmpeg", "-nostdin", "-loglevel", "error", *src(vurl), *src(aurl),
                   "-map", "0:v:0", "-map", "1:a:0", "-c", "copy",
                   "-f", "hls", "-hls_time", "6", "-hls_list_size", "0", "-hls_playlist_type", "event",
                   "-hls_segment_filename", str(out / "s%05d.ts"), str(out / "index.m3u8")]
            proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            self.mux[vid] = {"proc": proc, "dir": out, "used": time.time()}
            # sólo una o dos a la vez: no cargar el NAS
            for k in sorted(self.mux, key=lambda k: self.mux[k]["used"])[:-2]:
                _kill(self.mux[k]["proc"])
        for _ in range(60):  # espera a que exista la lista con un trozo
            if (out / "index.m3u8").exists() and any(out.glob("s*.ts")):
                return
            if proc.poll() not in (None, 0):
                raise RuntimeError("no se pudo juntar vídeo y audio")
            time.sleep(0.25)

    def mux_file(self, vid: str, name: str) -> tuple[bytes, str] | None:
        _check(vid)
        m = self.mux.get(vid)
        if not m or not (name == "index.m3u8" or (name.startswith("s") and name.endswith(".ts") and name[1:-3].isdigit())):
            return None
        p = m["dir"] / name
        for _ in range(40):  # el trozo puede estar escribiéndose todavía
            if p.exists():
                break
            time.sleep(0.25)
        if not p.exists():
            return None
        m["used"] = time.time()
        body = p.read_bytes()
        if name.endswith(".m3u8") and m["proc"].poll() is not None and b"#EXT-X-ENDLIST" not in body:
            body += b"#EXT-X-ENDLIST\n"
        return body, "application/vnd.apple.mpegurl" if name.endswith(".m3u8") else "video/mp2t"

    def _clean_mux(self) -> None:
        with self.lock:
            for k in [k for k, m in self.mux.items() if time.time() - m["used"] > 3 * 3600]:
                _kill(self.mux[k]["proc"])
                shutil.rmtree(self.mux.pop(k)["dir"], ignore_errors=True)

    # ---------- historial ----------
    def _load(self) -> dict:
        try:
            return json.loads(self.state_file.read_text(encoding="utf-8"))
        except Exception:
            return {}

    def _save(self) -> None:
        tmp = self.state_file.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.state, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.state_file)

    def watched(self, uid: str, item: dict) -> None:
        keep = {k: item.get(k) for k in ("id", "title", "channel", "channel_id", "duration", "thumb")}
        _check(keep["id"] or "")
        with self.lock:
            h = self.state.setdefault("history", {}).setdefault(uid, [])
            h[:] = [keep] + [x for x in h if x.get("id") != keep["id"]][:59]
            self._save()

    def history(self, uid: str) -> list[dict]:
        return list(self.state.get("history", {}).get(uid, []))

    def home(self, uid: str) -> list[dict]:
        """Filas para la portada de YouTube: seguir viendo y lo que más se mira ahora."""
        rows = []
        h = self.history(uid)
        if h:
            rows.append({"title": "Visto recientemente", "items": h[:20]})
            seen = []
            for x in h:  # más de los canales que ve
                if x.get("channel") and x["channel"] not in seen:
                    seen.append(x["channel"])
            for ch in seen[:2]:
                try:
                    rows.append({"title": f"Más de {ch}", "items": [i for i in self.search(ch, 15) if i["channel"] == ch][:15]})
                except Exception as e:
                    log.info("YouTube: %s", e)
        for title, q in (("Tendencias", "tendencias"), ("Música", "música novedades")):
            try:
                rows.append({"title": title, "items": self.search(q, 20)})
            except Exception as e:
                log.warning("YouTube: %s", e)
        return [r for r in rows if r["items"]]


def _check(vid: str) -> None:
    if len(vid) != 11 or not all(c.isalnum() or c in "-_" for c in vid):
        raise ValueError("vídeo no válido")


def _kill(proc) -> None:
    try:
        if proc.poll() is None:
            proc.kill()
    except Exception:
        pass
