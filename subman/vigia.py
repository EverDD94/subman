"""Vigía: deja rastro de por qué se apaga Videotecario y avisa si se reinicia una y otra vez.

- Cada 15 s apunta en /config/vigia.json la hora y la memoria que usa (latido).
- Si Docker/el NAS lo para con SIGTERM, lo apunta en el registro antes de salir.
- Si un fallo interno grave (de Python o de una librería en C) lo tumba, el detalle va a /config/subman-fallo.log.
- Al arrancar mira el último latido: si la vez anterior se cortó sin despedirse, lo dice en el registro
  (con la memoria que tenía y si el sistema lo mató por falta de memoria).
- Si ha arrancado 3 veces o más en 10 minutos, avisa por el bot de Telegram (como mucho una vez por hora).
"""
from __future__ import annotations

import faulthandler
import json
import logging
import os
import signal
import sys
import threading
import time
from pathlib import Path

log = logging.getLogger("vigia")

BEAT = 15
WINDOW = 600
TOO_MANY = 3


def _rss_mb() -> float:
    try:
        for line in Path("/proc/self/status").read_text().splitlines():
            if line.startswith("VmRSS:"):
                return int(line.split()[1]) / 1024
    except OSError:
        pass
    return 0.0


def _oom_kills() -> int | None:
    """Veces que el sistema ha matado algo de este contenedor por falta de memoria (cgroup v2 o v1)."""
    for p in ("/sys/fs/cgroup/memory.events", "/sys/fs/cgroup/memory/memory.oom_control"):
        try:
            for line in Path(p).read_text().splitlines():
                k, _, v = line.partition(" ")
                if k == "oom_kill":
                    return int(v)
        except (OSError, ValueError):
            continue
    return None


class Vigia:
    def __init__(self, config_dir: Path, notify=None):
        self.path = config_dir / "vigia.json"
        self.crash = config_dir / "subman-fallo.log"
        self.notify = notify  # función(texto) para avisar por el bot
        self.state = self._load()

    def _load(self) -> dict:
        try:
            return json.loads(self.path.read_text())
        except (OSError, ValueError):
            return {}

    def _save(self) -> None:
        try:
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.state))
            os.replace(tmp, self.path)
        except OSError as e:
            log.debug("vigía: no se pudo guardar (%s)", e)

    def start(self) -> None:
        now = time.time()
        prev = self.state.get("beat")
        if prev and not self.state.get("clean"):
            when = time.strftime("%H:%M:%S", time.localtime(prev["t"]))
            why = ""
            oom = _oom_kills()
            if oom:
                why = f"; el sistema ha matado {oom} vez/veces algo por falta de memoria"
            log.warning("vigía: la vez anterior se cortó sin avisar (último latido %s, usaba %.0f MB, "
                        "llevaba %.0f s encendido%s)", when, prev.get("mb", 0), prev.get("up", 0), why)
        starts = [t for t in self.state.get("starts", []) if now - t < WINDOW] + [now]
        self.state.update(starts=starts, clean=False, began=now)
        self._save()
        if len(starts) >= TOO_MANY and now - self.state.get("warned", 0) > 3600 and self.notify:
            self.state["warned"] = now
            self._save()
            try:
                self.notify(f"⚠️ Videotecario se ha reiniciado {len(starts)} veces en 10 minutos. "
                            "Mira subman.log en docker/subman para ver por qué.")
            except Exception as e:
                log.warning("vigía: no se pudo avisar (%s)", e)

        try:  # fallos graves (también de librerías en C) con su detalle
            self._crash_fh = open(self.crash, "a", encoding="utf-8")
            self._crash_fh.write(f"\n--- arranque {time.strftime('%Y-%m-%d %H:%M:%S')} ---\n")
            self._crash_fh.flush()
            faulthandler.enable(self._crash_fh, all_threads=True)
        except OSError:
            faulthandler.enable()

        def excepthook(t, v, tb):
            log.critical("vigía: fallo sin controlar, se apaga", exc_info=(t, v, tb))
        sys.excepthook = excepthook
        threading.excepthook = lambda a: log.error("vigía: el hilo «%s» se cayó", a.thread.name if a.thread else "?",
                                                   exc_info=(a.exc_type, a.exc_value, a.exc_traceback))

        def bye(sig, _frame):
            log.warning("vigía: me han pedido apagarme (%s, lo manda Docker o el NAS) tras %.0f s encendido",
                        signal.Signals(sig).name, time.time() - self.state.get("began", time.time()))
            self.state["clean"] = True
            self._save()
            logging.shutdown()
            os._exit(0)
        for s in (signal.SIGTERM, signal.SIGINT):
            try:
                signal.signal(s, bye)
            except ValueError:  # sólo desde el hilo principal
                pass
        threading.Thread(target=self._beat, name="vigía", daemon=True).start()

    def _beat(self) -> None:
        peak = 0.0
        while True:
            mb = _rss_mb()
            if mb > peak + 100:
                peak = mb
                log.info("vigía: usando %.0f MB de memoria", mb)
            self.state["beat"] = {"t": time.time(), "mb": round(mb), "up": round(time.time() - self.state["began"])}
            self._save()
            time.sleep(BEAT)
