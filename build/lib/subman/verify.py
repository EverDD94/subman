"""Comprobación de que un subtítulo es de este video y de que quedó sincronizado.

Se trocea el episodio en ventanas de 90 s y, en cada una, se busca por separado el desfase
con el que los arranques/finales de línea coinciden con los arranques/finales de voz. Esa
búsqueda local no sabe nada de la alineación global, así que sirve de testigo independiente:

* **Identidad**: con el subtítulo original (buscando hasta ±60 s), ¿las ventanas encuentran
  el mismo desfase que eligió la alineación? En un subtítulo de este episodio coinciden en
  una buena parte de las ventanas; en uno de otro episodio, casi nunca (los picos caen al azar).
* **Puntualidad**: con el subtítulo ya corregido (buscando ±3 s), ¿el mejor desfase de cada
  ventana con diálogo claro es 0? Las ventanas que no, se listan como tramos a revisar.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field

import numpy as np

from .aligner import Alignment, _edges
from .subtitles import Cue
from .vad import FPS

OK, REVIEW, FAIL = "ok", "revisar", "falla"

WINDOW, STEP = 90.0, 45.0
CLEAR_Z = 4.0          # cuánto debe destacar el pico de una ventana para fiarse de ella
ON_TIME = 0.3          # s de tolerancia para considerar una ventana "a tiempo"


@dataclass
class Window:
    start: float
    peak: float        # desfase (s) que mejor encaja en esta ventana
    z: float           # nitidez del pico
    lines: list[int]   # índices de las líneas de la ventana


@dataclass
class Report:
    verdict: str
    identity: float          # % de ventanas que confirman el desfase global (subtítulo original)
    on_time_before: float    # % de ventanas claras a tiempo antes de corregir
    on_time_after: float     # % de ventanas claras a tiempo después de corregir
    clear_windows: float     # % de ventanas con diálogo suficiente para juzgar
    ratio: float = 1.0
    segments: list[dict] = field(default_factory=list)
    review: list[list[float]] = field(default_factory=list)  # tramos (s) que no encajan
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def windows(cues: list[Cue], prob: np.ndarray, max_offset: float, step: float = 0.05) -> list[Window]:
    on, off = _edges(prob)
    n = len(on)
    s = np.array([int(c.start * FPS) for c in cues])
    e = np.array([int(c.end * FPS) for c in cues])
    k = int(round(max_offset / step))
    offs = np.arange(-k, k + 1) * int(round(step * FPS))
    out = []
    for w0 in np.arange(0.0, max(n / FPS - WINDOW / 2, 1.0), STEP):
        idx = np.nonzero((s >= w0 * FPS) & (s < (w0 + WINDOW) * FPS))[0]
        if len(idx) < 6:
            continue
        S = np.clip(s[idx][:, None] + offs, 0, n - 1)
        E = np.clip(e[idx][:, None] + offs, 0, n - 1)
        sc = (on[S] + 0.5 * off[E]).sum(axis=0)
        i = int(sc.argmax())
        med = np.median(sc)
        mad = np.median(np.abs(sc - med)) + 1e-6
        out.append(Window(float(w0), float(offs[i] / FPS), float((sc[i] - med) / mad), idx.tolist()))
    return out


def identity(original: list[Cue], alignment: Alignment, prob: np.ndarray) -> float:
    r = alignment.ratio
    scaled = [c.shifted(c.start * r, c.end * r) for c in original]
    ws = windows(scaled, prob, max_offset=60.0, step=0.1)
    if not ws:
        return 0.0
    hits = [abs(w.peak - float(np.median(alignment.offsets[w.lines]))) <= 0.5 for w in ws]
    return float(np.mean(hits))


def punctuality(cues: list[Cue], prob: np.ndarray) -> tuple[float, float, list[Window]]:
    """(% de ventanas claras a tiempo, % de ventanas claras, ventanas claras que no encajan)."""
    ws = windows(cues, prob, max_offset=3.0)
    clear = [w for w in ws if w.z >= CLEAR_Z]
    if not clear:
        return 0.0, 0.0, []
    late = [w for w in clear if abs(w.peak) > ON_TIME]
    return 1 - len(late) / len(clear), len(clear) / len(ws), late


def report(original: list[Cue], alignment: Alignment, prob: np.ndarray) -> Report:
    ident = identity(original, alignment, prob)
    before, _, _ = punctuality(original, prob)
    after, clear, late = punctuality(alignment.cues, prob)

    if ident >= 0.35 and after >= 0.75:
        verdict = OK
    elif ident >= 0.20 and after >= 0.55:
        verdict = REVIEW
    else:
        verdict = FAIL

    notes = []
    if ident < 0.20:
        notes.append("el subtítulo no parece de este video (otro episodio o versión muy distinta)")
    if alignment.ratio != 1.0:
        notes.append(f"corregida deriva de fps (x{alignment.ratio:.5f})")
    if len(alignment.segments) > 1:
        notes.append(f"{len(alignment.segments)} tramos con desfase distinto (intro/cortes)")
    if clear < 0.4:
        notes.append("poco diálogo claro para comprobar (mucha música o ruido)")

    segments = [{"lines": [s.first + 1, s.last + 1], "from": round(s.start, 2), "to": round(s.end, 2),
                 "offset": round(s.offset, 2)} for s in alignment.segments]
    review = [[round(w.start, 1), round(w.start + WINDOW, 1)] for w in late]
    return Report(verdict=verdict, identity=ident, on_time_before=before, on_time_after=after,
                  clear_windows=clear, ratio=alignment.ratio, segments=segments, review=review, notes=notes)
