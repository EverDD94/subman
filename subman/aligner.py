"""Alineación de subtítulos contra la huella de voz del video completo.

A diferencia de un desfase único (ffsubsync) o de buscar cortes con una heurística local,
aquí se resuelve un problema global con programación dinámica:

    t_nuevo = t_original * ratio + desfase_del_tramo

* ``ratio`` corrige la deriva por diferencia de fotogramas (23.976 / 24 / 25 fps).
* El desfase puede cambiar entre dos líneas (una intro, un corte de anuncios, una escena
  de más o de menos), pero cada cambio cuesta ``split_penalty``, así que sólo se acepta
  si mejora claramente el encaje del resto de líneas.
* Las líneas nunca se reordenan ni se pisan entre sí al cambiar de tramo.

Se recorre el episodio entero de una vez, por lo que una intro a mitad no "rompe" la
segunda parte: la segunda parte simplemente pasa a ser otro tramo con su propio desfase.
"""
from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np

from .subtitles import Cue
from .vad import FPS

DEFAULT_RATIOS = (
    1.0,
    25 / 23.976, 23.976 / 25,
    25 / 24, 24 / 25,
    24 / 23.976, 23.976 / 24,
)


@dataclass
class Params:
    max_offset: float = 300.0     # s, desfase máximo buscado en cualquier tramo
    coarse_step: float = 0.1      # s, resolución de la búsqueda global
    split_penalty: float = 3.0    # cuántas líneas "bien encajadas" debe ganar un corte para aceptarse
    silence_weight: float = 0.5   # cuánto penaliza que una línea caiga sobre silencio/música
    onset_weight: float = 1.0     # peso de que la línea empiece/acabe donde empieza/acaba la voz
    overlap_weight: float = 0.5   # peso de que la línea caiga, en conjunto, sobre voz
    ratios: tuple = DEFAULT_RATIOS


@dataclass
class Segment:
    first: int          # índice de la primera línea del tramo
    last: int           # índice de la última línea (incluida)
    offset: float       # s
    start: float = 0.0  # tiempo (ya corregido) donde empieza el tramo
    end: float = 0.0


@dataclass
class Alignment:
    ratio: float
    segments: list[Segment]
    cues: list[Cue]
    offsets: np.ndarray = field(repr=False)  # desfase final por línea (s)
    score: float = 0.0


def _signal(prob: np.ndarray, silence_weight: float) -> np.ndarray:
    return prob - silence_weight * (1.0 - prob)


def _prefix(sig: np.ndarray, pad: int) -> np.ndarray:
    # Fuera del audio cuenta como silencio total, así nada se "escapa" por los bordes.
    padded = np.concatenate([np.full(pad, -1.0), sig, np.full(pad, -1.0)])
    return np.concatenate([[0.0], np.cumsum(padded)])


def _edges(prob: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """Dónde arranca y dónde se corta la voz, suavizado ±0.1 s.

    Con diálogo denso (sitcoms, risas grabadas) "hay voz debajo" es casi siempre cierto
    con cualquier desfase; los arranques de frase, en cambio, marcan el tiempo con precisión.
    Una arrancada limpia vale ~1.
    """
    q = np.convolve(prob, np.ones(5) / 5, mode="same")
    d = np.diff(q, prepend=q[:1])
    g = np.exp(-0.5 * (np.arange(-30, 31) / 10) ** 2)
    on = np.convolve(np.clip(d, 0, None), g, mode="same")
    off = np.convolve(np.clip(-d, 0, None), g, mode="same")
    return on, off


class _Scorer:
    """Puntuación de cada línea con cada desfase candidato."""

    def __init__(self, prob: np.ndarray, p: "Params", pad: int):
        self.p, self.pad = p, pad
        self.prefix = _prefix(_signal(prob, p.silence_weight), pad)
        on, off = _edges(prob)
        z = np.zeros(pad)
        self.on = np.concatenate([z, on, z, [0.0]])
        self.off = np.concatenate([z, off, z, [0.0]])

    def __call__(self, starts: np.ndarray, ends: np.ndarray, offsets: np.ndarray) -> np.ndarray:
        hi = len(self.prefix) - 1
        s = np.clip(starts[:, None] + offsets[None, :] + self.pad, 0, hi)
        e = np.clip(ends[:, None] + offsets[None, :] + self.pad, 0, hi)
        dur = np.maximum(ends - starts, 1)[:, None]
        overlap = (self.prefix[e] - self.prefix[s]) / dur
        edges = self.on[s] + 0.5 * self.off[e]
        return self.p.onset_weight * edges + self.p.overlap_weight * overlap


def _dp(scores: np.ndarray, gaps: np.ndarray, penalty: float):
    """Elige un desfase por línea maximizando encaje - penalización por cortes.

    gaps[i]: cuántos pasos de desfase puede *retroceder* la línea i respecto a la i-1
    sin pisarla (el hueco que las separa).
    """
    n, m = scores.shape
    idx = np.arange(m)
    jumped = np.zeros((n, m), dtype=bool)
    source = np.zeros((n, m), dtype=np.int32)
    dp = scores[0].copy()
    for i in range(1, n):
        runmax = np.maximum.accumulate(dp)
        argrun = np.maximum.accumulate(np.where(dp >= runmax, idx, 0))
        j = idx + gaps[i]
        jc = np.clip(j, 0, m - 1)
        jump_val = np.where(j >= 0, runmax[jc] - penalty, -np.inf)
        take = jump_val > dp
        dp = np.where(take, jump_val, dp) + scores[i]
        jumped[i] = take
        source[i] = argrun[jc]
    k = int(np.argmax(dp))
    best = float(dp[k])
    path = np.empty(n, dtype=np.int64)
    for i in range(n - 1, -1, -1):
        path[i] = k
        if i > 0 and jumped[i, k]:
            k = int(source[i, k])
    return best, path


def align(cues: list[Cue], prob: np.ndarray, params: Params | None = None) -> Alignment:
    p = params or Params()
    if not cues:
        raise ValueError("el subtítulo no tiene líneas")
    pad = int(p.max_offset * FPS) + 1
    scorer = _Scorer(prob, p, pad)
    step = max(1, int(round(p.coarse_step * FPS)))
    k = int(p.max_offset * FPS) // step
    coarse = np.arange(-k, k + 1) * step  # desfases candidatos, en tramas
    penalty = p.split_penalty

    orig_start = np.array([c.start for c in cues])
    orig_end = np.array([c.end for c in cues])

    best = None
    for ratio in p.ratios:
        starts = np.round(orig_start * ratio * FPS).astype(np.int64)
        ends = np.round(orig_end * ratio * FPS).astype(np.int64)
        scores = scorer(starts, ends, coarse)
        gap_frames = np.zeros(len(cues), dtype=np.int64)
        gap_frames[1:] = starts[1:] - ends[:-1]
        gaps = np.floor((gap_frames + 5) / step).astype(np.int64)  # 50 ms de tolerancia
        total, path = _dp(scores, gaps, penalty)
        if ratio != 1.0:
            total -= penalty  # a igualdad, preferir no reescalar
        if best is None or total > best[0]:
            best = (total, ratio, starts, ends, path)

    total, ratio, starts, ends, path = best

    # Agrupar líneas consecutivas con el mismo desfase y afinar cada tramo a 10 ms.
    segments: list[Segment] = []
    frame_offsets = np.empty(len(cues), dtype=np.int64)
    i = 0
    while i < len(cues):
        j = i
        while j + 1 < len(cues) and path[j + 1] == path[i]:
            j += 1
        center = coarse[path[i]]
        fine = np.arange(center - step, center + step + 1)
        seg_scores = scorer(starts[i:j + 1], ends[i:j + 1], fine).sum(axis=0)
        off = int(fine[int(np.argmax(seg_scores))])
        frame_offsets[i:j + 1] = off
        segments.append(Segment(first=i, last=j, offset=off / FPS))
        i = j + 1

    offsets = frame_offsets / FPS
    new_cues = [
        c.shifted(c.start * ratio + o, c.end * ratio + o)
        for c, o in zip(cues, offsets)
    ]
    # Por si el afinado hizo que dos tramos se toquen: nunca dejar líneas pisadas.
    for n in range(1, len(new_cues)):
        prev, cur = new_cues[n - 1], new_cues[n]
        if cur.start < prev.start:
            new_cues[n] = cur.shifted(prev.start, max(prev.start + 0.2, cur.end))
    for s in segments:
        s.start = new_cues[s.first].start
        s.end = new_cues[s.last].end
    return Alignment(ratio=ratio, segments=segments, cues=new_cues, offsets=offsets, score=total)
