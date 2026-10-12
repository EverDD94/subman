"""Línea de comandos de subman."""
from __future__ import annotations

import argparse
import json
import logging
import shutil
import sys
import time
import tomllib
from pathlib import Path

from . import aligner, manager, providers, subtitles, sync, verify
from .library import Library

DEFAULT_CONFIG = "/config/subman.toml"


def _print_report(rep: verify.Report) -> None:
    print(f"Veredicto: {rep.verdict.upper()}")
    print(f"  es de este video: {rep.identity:.0%} de las ventanas lo confirman")
    print(f"  a tiempo: antes {rep.on_time_before:.0%} -> después {rep.on_time_after:.0%}"
          f" de las ventanas con diálogo claro ({rep.clear_windows:.0%} del episodio)")
    for n in rep.notes:
        print(f"  - {n}")
    for s in rep.segments:
        print(f"    líneas {s['lines'][0]}-{s['lines'][1]}  {_hms(s['from'])}-{_hms(s['to'])}"
              f"  desfase {s['offset']:+.2f} s")
    if rep.review:
        print("  tramos a revisar: " + ", ".join(f"{_hms(a)}-{_hms(b)}" for a, b in rep.review))


def _hms(t: float) -> str:
    t = max(0, int(t))
    return f"{t // 3600}:{t // 60 % 60:02d}:{t % 60:02d}"


def cmd_sync(a) -> int:
    out = a.output or str(Path(a.subtitle).with_suffix(".synced.srt"))
    params = aligner.Params(max_offset=a.max_offset, split_penalty=a.split_penalty)
    rep = sync.sync_file(a.video, a.subtitle, out, a.ref, a.audio_lang, params)
    if a.json:
        print(json.dumps(rep.to_dict(), ensure_ascii=False, indent=2))
    else:
        _print_report(rep)
        print(f"Guardado: {out}")
    return 0 if rep.verdict == verify.OK else 2


def cmd_check(a) -> int:
    cues = subtitles.load(a.subtitle)
    ref = sync.build_reference(a.video, a.ref, a.audio_lang, len(cues))
    rep = verify.report(cues, aligner.align(cues, ref.prob), ref.prob)
    ok = rep.identity >= 0.35 and rep.on_time_before >= 0.75
    print(f"Ya sincronizado: {'SÍ' if ok else 'NO'}  (ventanas a tiempo: {rep.on_time_before:.0%};"
          f" tras corregir quedaría en {rep.on_time_after:.0%})")
    _print_report(rep)
    return 0 if ok else 2


EXAMPLE_CONFIG = Path("/app/subman.example.toml")


def _config(path: str, wait: bool = False) -> dict:
    """Lee la configuración. En Docker, si aún no existe, deja una plantilla y espera a que
    se rellene (así el contenedor no se reinicia en bucle el primer día)."""
    p = Path(path)
    while True:
        if not p.exists() and EXAMPLE_CONFIG.exists():
            p.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy(EXAMPLE_CONFIG, p)
            logging.warning("creada %s: rellénala (rutas y OpenSubtitles) y el programa arrancará solo", p)
        if p.exists():
            with open(p, "rb") as f:
                cfg = tomllib.load(f)
            opensub = cfg.get("providers", {}).get("opensubtitles", {})
            if not wait or opensub.get("api_key") or not opensub:
                return cfg
            logging.warning("falta api_key de OpenSubtitles en %s; esperando...", p)
        elif not wait:
            raise SystemExit(f"no existe la configuración {p}")
        time.sleep(60)


def _library(cfg: dict) -> Library:
    return Library(cfg.get("database", str(Path(DEFAULT_CONFIG).with_name("subman.db"))))


def cmd_scan(a) -> int:
    cfg = _config(a.config)
    print(_library(cfg).scan(cfg["library"]))
    return 0


def cmd_list(a) -> int:
    cfg = _config(a.config)
    lib = _library(cfg)
    for m in lib.media():
        states = []
        for lang in cfg.get("languages", ["es"]):
            row = lib.subtitle_status(m.id, lang)
            states.append(f"{lang}:{row['status'] if row else '-'}")
        print(f"{m.id:>5}  {' '.join(states):<20} {m.label}")
    return 0


def cmd_run(a) -> int:
    cfg = _config(a.config, wait=True)
    settings = manager.Settings(
        languages=cfg.get("languages", ["es"]), candidates=cfg.get("candidates", 5),
        keep_review=cfg.get("keep_review", True), sync_existing=cfg.get("sync_existing", True),
        reference=cfg.get("reference", "auto"), retry_hours=cfg.get("retry_hours", 24))
    provs = providers.load(cfg)
    while True:
        lib = _library(cfg)
        logging.info("escaneo: %s", lib.scan(cfg["library"]))
        items = lib.media("path LIKE ?", (f"%{a.only}%",)) if a.only else lib.media()
        for m in items:
            try:
                res = manager.process(m, lib, provs, settings)
                if res:
                    logging.info("%s: %s", m.label, res)
            except Exception:
                logging.exception("%s: error", m.label)
        if not a.watch:
            return 0
        time.sleep(a.watch * 60)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="subman", description="Gestor y sincronizador de subtítulos")
    ap.add_argument("-v", "--verbose", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)

    for name, fn, hlp in (("sync", cmd_sync, "sincroniza un subtítulo con un video"),
                          ("check", cmd_check, "sólo comprueba si un subtítulo ya está sincronizado")):
        p = sub.add_parser(name, help=hlp)
        p.add_argument("video")
        p.add_argument("subtitle")
        p.add_argument("--ref", choices=["auto", "audio", "embedded"], default="auto",
                       help="contra qué alinear: voz del audio o subtítulo incrustado en el video")
        p.add_argument("--audio-lang", help="idioma de la pista de audio a usar (si hay varias)")
        if name == "sync":
            p.add_argument("-o", "--output")
            p.add_argument("--max-offset", type=float, default=300.0)
            p.add_argument("--split-penalty", type=float, default=4.0)
            p.add_argument("--json", action="store_true")
        p.set_defaults(fn=fn)

    for name, fn, hlp in (("scan", cmd_scan, "indexa la biblioteca"),
                          ("list", cmd_list, "lista la biblioteca y el estado de sus subtítulos"),
                          ("run", cmd_run, "escanea, descarga, sincroniza y verifica lo que falte")):
        p = sub.add_parser(name, help=hlp)
        p.add_argument("-c", "--config", default=DEFAULT_CONFIG)
        if name == "run":
            p.add_argument("--watch", type=float, metavar="MIN", help="repetir cada MIN minutos")
            p.add_argument("--only", help="procesar sólo rutas que contengan este texto")
        p.set_defaults(fn=fn)

    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
