"""Comprobación de que un subtítulo está sincronizado con el audio.

Mide, línea a línea, cuánta voz real hay debajo de cada subtítulo, y lo compara con lo que
saldría por azar (el mismo subtítulo desplazado a ciegas). Un subtítulo bien sincronizado
cae sobre voz casi siempre; uno desfasado, no más que el azar.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field

import numpy as np

from .subtitles import Cue
from .vad import FPS

OK, REVIEW, FAIL = "ok", "revisar", "falla"


@dataclass
class Fit:
    speech_on_cues: float   # % del tiempo de subtítulo que cae sobre voz (ponderado)
    cues_on_speech: float   # % de líneas con mayoría de voz debajo
    speech_covered: float   # % de la voz del video que tiene subtítulo encima


@dataclass
class Report:
    verdict: str
    before: Fit
    after: Fit
    chance: float           # % de líneas sobre voz que se logra alineando señuelos
    ratio: float = 1.0
    segments: list[dict] = field(default_factory=list)
    suspicious_lines: list[int] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


def _cue_means(cues: list[Cue], prob: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    prefix = np.concatenate([[0.0], np.cumsum(prob)])
    n = len(prob)
    s = np.clip(np.array([int(c.start * FPS) for c in cues]), 0, n)
    e = np.clip(np.array([int(c.end * FPS) for c in cues]), 0, n)
    dur = np.array([max(1, int(c.end * FPS) - int(c.start * FPS)) for c in cues])
    return (prefix[e] - prefix[s]) / dur, dur


def fit(cues: list[Cue], prob: np.ndarray) -> Fit:
    means, dur = _cue_means(cues, prob)
    covered = np.zeros(len(prob), dtype=bool)
    for c in cues:
        covered[max(0, int(c.start * FPS)): max(0, int(c.end * FPS))] = True
    speech = prob > 0.5
    return Fit(
        speech_on_cues=float((means * dur).sum() / dur.sum()),
        cues_on_speech=float((means > 0.5).mean()),
        speech_covered=float((speech & covered).sum() / max(1, speech.sum())),
    )


def decoys(cues: list[Cue], seed: int = 0) -> list[list[Cue]]:
    """Subtítulos "falsos" con el mismo ritmo de líneas pero sin relación con este audio.

    Se alinean igual que el real: lo bien que el alineador consigue encajar basura es el
    nivel de azar contra el que hay que comparar (así un tramo que encaja por casualidad
    no pasa por bueno).
    """
    end = cues[-1].end
    mirrored = [Cue(end - c.end, end - c.start, c.text) for c in reversed(cues)]
    rng = np.random.default_rng(seed)
    durs = np.array([c.end - c.start for c in cues])
    gaps = np.diff([c.start for c in cues], prepend=cues[0].start)
    order = rng.permutation(len(cues))
    t, shuffled = cues[0].start, []
    for i in order:
        shuffled.append(Cue(t, t + durs[i], cues[i].text))
        t += max(gaps[i], durs[i] + 0.05)
    return [mirrored, shuffled]


def report(original: list[Cue], synced: list[Cue], prob: np.ndarray, alignment=None,
           chance: float = 0.5) -> Report:
    """``chance``: % de líneas sobre voz que logra el alineador con subtítulos señuelo."""
    before, after = fit(original, prob), fit(synced, prob)
    means, _ = _cue_means(synced, prob)
    suspicious = [int(i) for i in np.nonzero(means < 0.2)[0]]

    margin = after.cues_on_speech - chance
    if after.cues_on_speech >= 0.80 and margin >= 0.20:
        verdict = OK
    elif after.cues_on_speech >= 0.60 and margin >= 0.10:
        verdict = REVIEW
    else:
        verdict = FAIL

    notes = []
    segments = []
    if alignment is not None:
        for s in alignment.segments:
            seg_means = means[s.first:s.last + 1]
            segments.append({
                "lines": [s.first + 1, s.last + 1],
                "from": round(s.start, 2), "to": round(s.end, 2),
                "offset": round(s.offset, 2),
                "on_speech": round(float((seg_means > 0.5).mean()), 3),
            })
        if alignment.ratio != 1.0:
            notes.append(f"corregida deriva de fps (x{alignment.ratio:.5f})")
        if len(alignment.segments) > 1:
            notes.append(f"{len(alignment.segments)} tramos con desfase distinto (intro/cortes)")
        weak = [g for g in segments if g["lines"][1] - g["lines"][0] >= 4 and g["on_speech"] < 0.6]
        if weak and verdict == OK:
            verdict = REVIEW
            notes.append(f"{len(weak)} tramo(s) encajan peor que el resto")
    if after.speech_covered < 0.6:
        notes.append("mucha voz sin subtítulo: ¿versión distinta o subtítulo incompleto?")
    return Report(verdict=verdict, before=before, after=after, chance=chance,
                  ratio=alignment.ratio if alignment else 1.0, segments=segments,
                  suspicious_lines=suspicious, notes=notes)
