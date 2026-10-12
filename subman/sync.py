"""Orquestación: video + subtítulo -> subtítulo sincronizado + informe."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from pathlib import Path

import numpy as np

from . import aligner, audio, subtitles, vad, verify

log = logging.getLogger(__name__)


@dataclass
class Reference:
    prob: np.ndarray
    source: str  # "audio" o "embedded:<idioma>"


def build_reference(video: str | Path, mode: str = "auto", audio_language: str | None = None,
                    min_cues: int = 0) -> Reference:
    """Huella temporal del video.

    * ``audio``: voz detectada en la pista de audio (funciona con cualquier idioma).
    * ``embedded``: un subtítulo de texto que ya traiga el propio archivo (precisión de fotograma).
    * ``auto``: embedded si hay uno razonablemente completo; si no, audio.
    """
    info = audio.probe(video)
    duration = float(info["format"]["duration"])
    if mode in ("auto", "embedded"):
        tracks = audio.embedded_text_subtitles(video, info)
        if tracks:
            stream, cues = max(tracks, key=lambda t: len(t[1]))
            if mode == "embedded" or len(cues) >= max(50, 0.5 * min_cues):
                lang = stream.get("tags", {}).get("language", "?")
                return Reference(vad.from_cues(cues, duration), f"embedded:{lang}")
        if mode == "embedded":
            raise ValueError("el video no trae subtítulos de texto incrustados")
    stream = audio.pick_audio_stream(info, audio_language)
    log.info("analizando voz en la pista de audio %d", stream)
    pcm = audio.load_audio(video, stream)
    return Reference(vad.speech_probability(pcm), "audio")


def sync(cues: list[subtitles.Cue], ref: Reference, params: aligner.Params | None = None):
    result = aligner.align(cues, ref.prob, params)
    rep = verify.report(cues, result, ref.prob)
    rep.notes.insert(0, f"referencia: {ref.source}")
    return result.cues, rep


def sync_file(video: str | Path, sub: str | Path, out: str | Path, ref_mode: str = "auto",
              audio_language: str | None = None, params: aligner.Params | None = None):
    cues = subtitles.load(sub)
    ref = build_reference(video, ref_mode, audio_language, min_cues=len(cues))
    synced, rep = sync(cues, ref, params)
    subtitles.save(synced, out)
    return rep
