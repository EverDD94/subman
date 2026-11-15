"""Extracción de audio y pistas de subtítulos con ffmpeg."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import numpy as np

from . import subtitles

SAMPLE_RATE = 16000
TEXT_SUB_CODECS = {"subrip", "ass", "ssa", "mov_text", "webvtt", "text"}


def probe(path: str | Path) -> dict:
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-print_format", "json", "-show_streams", "-show_format", str(path)],
        check=True, capture_output=True,
    ).stdout
    return json.loads(out)


def audio_streams(info: dict) -> list[dict]:
    return [s for s in info["streams"] if s.get("codec_type") == "audio"]


def pick_audio_stream(info: dict, language: str | None = None) -> int:
    """Índice relativo (0:a:N) de la pista de audio a analizar.

    Por defecto la pista marcada como 'default'; si se pide un idioma, la primera en ese idioma.
    """
    streams = audio_streams(info)
    if not streams:
        raise ValueError("el archivo no tiene pistas de audio")
    if language:
        for n, s in enumerate(streams):
            if s.get("tags", {}).get("language", "").lower().startswith(language.lower()[:2]):
                return n
    for n, s in enumerate(streams):
        if s.get("disposition", {}).get("default"):
            return n
    return 0


def load_audio(path: str | Path, stream: int = 0, duration: float | None = None) -> np.ndarray:
    """Audio completo en mono, 16 kHz, float32 en [-1, 1].

    Se lee a trozos y se va guardando ya convertido (antes había 3 copias a la vez en memoria:
    unos 900 MB en una película de 2 h; así, unos 460 MB). El ffmpeg es congelable (freno)."""
    from . import freno
    cmd = ["nice", "-n", "19", "ffmpeg", "-nostdin", "-v", "error", "-i", str(path), "-map", f"0:a:{stream}",
           "-ac", "1", "-ar", str(SAMPLE_RATE), "-f", "s16le", "-"]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    freno.child(proc)
    size = int((duration or 0) * SAMPLE_RATE) + SAMPLE_RATE
    out = np.empty(size, dtype=np.float32)
    n = 0
    try:
        while True:
            buf = proc.stdout.read(1 << 20)  # 0,5 M muestras
            if not buf:
                break
            if len(buf) % 2:
                buf += proc.stdout.read(1)
            chunk = np.frombuffer(buf, dtype=np.int16)
            if n + len(chunk) > len(out):
                out = np.resize(out, max(len(out) * 2, n + len(chunk)))
            out[n:n + len(chunk)] = chunk
            n += len(chunk)
        err = proc.stderr.read()
        if proc.wait() != 0:
            raise subprocess.CalledProcessError(proc.returncode, cmd, None, err)
    finally:
        freno.done(proc)
        if proc.poll() is None:
            proc.kill()
    out = out[:n]
    out *= 1 / 32768.0
    return out


def embedded_text_subtitles(path: str | Path, info: dict | None = None) -> list[tuple[dict, list[subtitles.Cue]]]:
    """Pistas de subtítulos de texto que trae el propio video (ya sincronizadas con él)."""
    info = info or probe(path)
    result = []
    for s in info["streams"]:
        if s.get("codec_type") != "subtitle" or s.get("codec_name") not in TEXT_SUB_CODECS:
            continue
        proc = subprocess.run(
            ["ffmpeg", "-nostdin", "-v", "error", "-i", str(path), "-map", f"0:{s['index']}", "-f", "srt", "-"],
            capture_output=True,
        )
        if proc.returncode == 0:
            cues = subtitles.parse(subtitles.decode(proc.stdout))
            if cues:
                result.append((s, cues))
    return result
