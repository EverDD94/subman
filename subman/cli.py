"""Línea de comandos de subman."""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

from . import aligner, config, manager, providers, subtitles, sync, verify
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


def _config(path: str) -> dict:
    return config.load(path)  # si no existe, la crea con los valores por defecto


def _library(cfg: dict) -> Library:
    return Library(cfg["database"])


def cmd_scan(a) -> int:
    cfg = _config(a.config)
    print(_library(cfg).scan(cfg["library"]))
    return 0


def cmd_list(a) -> int:
    cfg = _config(a.config)
    lib = _library(cfg)
    for m in lib.media():
        states = []
        for lang in cfg["languages"]:
            row = lib.subtitle_status(m.id, lang)
            states.append(f"{lang}:{row['status'] if row else '-'}")
        print(f"{m.id:>5}  {' '.join(states):<20} {m.label}")
    return 0


def cmd_serve(a) -> int:
    from . import web
    web.serve(a.config, a.port)
    return 0


def cmd_run(a) -> int:
    if a.watch:  # modo contenedor: interfaz web + trabajo automático según los ajustes
        return cmd_serve(argparse.Namespace(config=a.config, port=None))
    cfg = _config(a.config)
    if config.blocking(cfg):
        raise SystemExit("; ".join(config.blocking(cfg)))
    settings = config.settings(cfg, a.config, a.dry_run)
    provs = providers.load(config.active_providers(cfg))
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
    return 0


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
                          ("run", cmd_run, "escanea, descarga, sincroniza y verifica lo que falte"),
                          ("serve", cmd_serve, "interfaz web (puerto 8787) + trabajo automático")):
        p = sub.add_parser(name, help=hlp)
        p.add_argument("-c", "--config", default=DEFAULT_CONFIG)
        if name == "run":
            p.add_argument("--watch", type=float, metavar="MIN",
                           help="modo contenedor: igual que 'serve' (el intervalo se elige en la web)")
            p.add_argument("--only", help="procesar sólo rutas que contengan este texto")
            p.add_argument("--dry-run", "--ensayo", action="store_true",
                           help="no toca nada: sólo dice qué haría con cada archivo")
        if name == "serve":
            p.add_argument("--port", type=int, help="puerto (por defecto 8787 o web_port de la configuración)")
        p.set_defaults(fn=fn)

    a = ap.parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if a.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
