"""Detección de voz (VAD) con Silero, que distingue voz de música y efectos.

Devuelve la probabilidad de voz en una rejilla de 10 ms, que es la "huella" del audio
contra la que se alinean los subtítulos.
"""
from __future__ import annotations

from pathlib import Path

import numpy as np

from .audio import SAMPLE_RATE

FPS = 100  # tramas por segundo de la rejilla de alineación (10 ms)
_MODEL = Path(__file__).parent / "models" / "silero_vad_16k_sequence.onnx"
_CHUNK = 512
_CONTEXT = 64
_BATCH = 4096


def speech_probability(audio: np.ndarray) -> np.ndarray:
    import onnxruntime as ort

    from . import freno
    opts = ort.SessionOptions()
    opts.intra_op_num_threads = 1  # un solo núcleo: el resto del NAS para Jellyfin
    opts.inter_op_num_threads = 1
    sess = ort.InferenceSession(str(_MODEL), sess_options=opts, providers=["CPUExecutionProvider"])
    n = len(audio) // _CHUNK
    if n == 0:
        return np.zeros(0, dtype=np.float32)
    chunks = audio[: n * _CHUNK].reshape(n, _CHUNK)  # vista, sin copiar

    h = np.zeros((1, 1, 128), dtype=np.float32)
    c = np.zeros((1, 1, 128), dtype=np.float32)
    probs = []
    for i in range(0, n, _BATCH):  # tramas con su contexto, de lote en lote (no todo de golpe)
        j = min(n, i + _BATCH)
        frames = np.empty((j - i, _CONTEXT + _CHUNK), dtype=np.float32)
        frames[:, _CONTEXT:] = chunks[i:j]
        frames[0, :_CONTEXT] = chunks[i - 1, -_CONTEXT:] if i else 0
        frames[1:, :_CONTEXT] = chunks[i:j - 1, -_CONTEXT:]
        p, h, c = sess.run(None, {"input": frames, "h": h, "c": c})
        probs.append(p)
        freno.wait()
    probs = np.concatenate(probs)

    # Remuestrear de tramas de 32 ms a la rejilla de 10 ms.
    centers = (np.arange(n) * _CHUNK + _CHUNK / 2) / SAMPLE_RATE
    grid = np.arange(int(len(audio) / SAMPLE_RATE * FPS)) / FPS
    return np.interp(grid, centers, probs).astype(np.float32)


def from_cues(cues, duration: float) -> np.ndarray:
    """Huella de referencia construida a partir de subtítulos ya sincronizados."""
    sig = np.zeros(int(duration * FPS) + 1, dtype=np.float32)
    for c in cues:
        sig[int(c.start * FPS): int(c.end * FPS)] = 1.0
    return sig
