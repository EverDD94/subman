"""Freno: que Videotecario no sature el NAS.

- Mientras alguien ve algo en Jellyfin, el trabajo pesado espera (y los ffmpeg en marcha se congelan).
- Tope de procesador (Ajustes › Rendimiento): si se pasa, descansa lo justo para quedarse por debajo.
- Todo con la prioridad más baja del sistema (nice 19): Jellyfin siempre va primero.

El trabajo pesado llama a `freno.wait()` entre trozo y trozo; los procesos hijos (ffmpeg) se apuntan
con `freno.child(proc)` para poder congelarlos con SIGSTOP y seguir con SIGCONT.
"""
from __future__ import annotations

import logging
import os
import signal
import threading
import time

import requests

log = logging.getLogger("freno")

_cfg = lambda: {}  # noqa: E731  (lo pone start())
_playing = False
_over = False
_children: set[int] = set()
_lock = threading.Lock()
_frozen = False
_started = False


def _cpu_seconds() -> float:
    """CPU usado por todo el contenedor (cgroup); si no, por este proceso y sus hijos."""
    try:
        for line in open("/sys/fs/cgroup/cpu.stat"):
            if line.startswith("usage_usec"):
                return int(line.split()[1]) / 1e6
    except OSError:
        pass
    try:
        return int(open("/sys/fs/cgroup/cpuacct/cpuacct.usage").read()) / 1e9
    except OSError:
        pass
    t = os.times()
    return t.user + t.system + t.children_user + t.children_system


def _settings() -> dict:
    try:
        return _cfg().get("performance", {}) or {}
    except Exception:
        return {}


def cap_percent() -> int:
    """Tope de procesador en % del NAS entero (100 = sin tope)."""
    try:
        return max(10, min(100, int(_settings().get("cpu_max", 50))))
    except (TypeError, ValueError):
        return 50


def pause_when_watching() -> bool:
    return bool(_settings().get("pause_when_watching", True))


def _someone_watching() -> bool:
    jf = _cfg().get("jellyfin", {}) or {}
    if not (jf.get("url") and jf.get("api_key")):
        return False
    try:
        r = requests.get(jf["url"].rstrip("/") + "/Sessions", timeout=10,
                         headers={"X-Emby-Token": jf["api_key"]}, params={"ActiveWithinSeconds": 120})
        r.raise_for_status()
        return any(s.get("NowPlayingItem") and not (s.get("PlayState") or {}).get("IsPaused") for s in r.json())
    except Exception as e:
        log.debug("freno: no se pudo mirar Jellyfin (%s)", e)
        return False


def _signal_children(sig) -> None:
    with _lock:
        for pid in list(_children):
            try:
                os.kill(pid, sig)
            except ProcessLookupError:
                _children.discard(pid)
            except OSError:
                pass


def _set_frozen(on: bool) -> None:
    global _frozen
    if on != _frozen:
        _frozen = on
        _signal_children(signal.SIGSTOP if on else signal.SIGCONT)


def _watch_jellyfin() -> None:
    global _playing
    while True:
        now = pause_when_watching() and _someone_watching()
        if now != _playing:
            _playing = now
            log.info("freno: %s", "alguien está viendo algo en Jellyfin, pauso el trabajo pesado" if now
                     else "nadie viendo nada en Jellyfin, sigo con el trabajo")
            _set_frozen(_playing or _over)
        time.sleep(20)


def _watch_cpu() -> None:
    """Mide el uso cada segundo; mientras la media pase del tope, congela los ffmpeg y para los hilos."""
    global _over
    ncpu = os.cpu_count() or 1
    last_t, last_c = time.monotonic(), _cpu_seconds()
    avg = 0.0
    while True:
        time.sleep(1)
        t, c = time.monotonic(), _cpu_seconds()
        used = 100.0 * (c - last_c) / max(1e-6, (t - last_t) * ncpu)
        last_t, last_c = t, c
        avg = 0.7 * avg + 0.3 * used
        cap = cap_percent()
        over = cap < 100 and avg > cap
        if over != _over:
            _over = over
            _set_frozen(_playing or _over)


def start(get_cfg) -> None:
    """Arranca el freno (una vez) y baja la prioridad de todo Videotecario y lo que lance."""
    global _cfg, _started
    _cfg = get_cfg
    if _started:
        return
    _started = True
    try:
        os.setpriority(os.PRIO_PROCESS, 0, 19)
    except (OSError, AttributeError):
        pass
    try:  # disco también en «sólo si nadie más lo usa» (ioprio_set, clase idle)
        import ctypes
        ctypes.CDLL(None, use_errno=True).syscall(251 if os.uname().machine == "x86_64" else 30, 1, 0, 3 << 13)
    except Exception:
        pass
    threading.Thread(target=_watch_jellyfin, name="freno-jellyfin", daemon=True).start()
    threading.Thread(target=_watch_cpu, name="freno-cpu", daemon=True).start()
    log.info("freno: prioridad baja, tope de procesador %d %%, %s", cap_percent(),
             "pausa si alguien ve algo en Jellyfin" if pause_when_watching() else "sin pausa por Jellyfin")


def child(proc) -> None:
    """Apunta un proceso hijo (ffmpeg…) para poder congelarlo."""
    with _lock:
        _children.add(proc.pid)
    if _frozen:
        try:
            os.kill(proc.pid, signal.SIGSTOP)
        except OSError:
            pass


def done(proc) -> None:
    with _lock:
        _children.discard(proc.pid)


def wait() -> None:
    """Punto de espera para el trabajo pesado: no sigue mientras haya que frenar."""
    if not _started:
        return
    while _playing or _over:
        time.sleep(1)


def run(cmd: list[str], **kw):
    """Como subprocess.run(capture_output=True), pero con el ffmpeg congelable."""
    import subprocess
    check = kw.pop("check", False)
    p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, **kw)
    child(p)
    try:
        out, err = p.communicate()
    finally:
        done(p)
    if check and p.returncode:
        raise subprocess.CalledProcessError(p.returncode, cmd, out, err)
    return subprocess.CompletedProcess(cmd, p.returncode, out, err)
