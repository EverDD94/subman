"""Sincroniza cada variante desfasada y la compara con la verdad conocida.

Uso: python tests/evaluate.py DIR_EPISODIO
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from subman import subtitles, sync  # noqa: E402


def errors(a, b):
    return np.abs(np.array([x.start for x in a]) - np.array([y.start for y in b]))


def main(d: Path) -> int:
    video = next(d.glob("*.mkv"))
    t0 = time.time()
    ref = sync.build_reference(video, "audio")
    print(f"huella de voz: {len(ref.prob) / 100:.0f} s de audio en {time.time() - t0:.1f} s")
    failed = 0
    for bad in sorted(d.glob("*.srt")):
        if bad.name.endswith(".truth.srt") or bad.name == "truth.srt" or ".synced" in bad.name:
            continue
        truth = subtitles.load(bad.with_name(bad.stem + ".truth.srt"))
        cues = subtitles.load(bad)
        t0 = time.time()
        synced, rep = sync.sync(cues, ref)
        subtitles.save(synced, bad.with_name(bad.stem + ".synced.srt"))
        print(f"\n== {bad.stem}  ({time.time() - t0:.1f} s)")
        if len(truth) != len(cues):  # subtítulo de otro episodio: sólo vale el veredicto
            failed += rep.verdict != "falla"
            print(f"  veredicto: {rep.verdict} | identidad {rep.identity:.0%} | " + "; ".join(rep.notes))
            continue
        e0, e1 = errors(cues, truth), errors(synced, truth)
        good = (e1 <= 0.2).mean()
        failed += good < 0.95
        print(f"  antes:   error mediano {np.median(e0):6.2f} s | ventanas a tiempo {rep.on_time_before:.0%}")
        print(f"  después: error mediano {np.median(e1):6.3f} s | <=0.2 s: {good:.0%} | máx {e1.max():.2f} s"
              f" | ventanas a tiempo {rep.on_time_after:.0%} | identidad {rep.identity:.0%}")
        print(f"  veredicto: {rep.verdict} | ratio {rep.ratio:.5f} | " + "; ".join(rep.notes))
        for s in rep.segments:
            print(f"    líneas {s['lines'][0]:>3}-{s['lines'][1]:<3} {s['from']:>7.1f}-{s['to']:<7.1f}"
                  f" desfase {s['offset']:+7.2f} s")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(Path(sys.argv[1])))
