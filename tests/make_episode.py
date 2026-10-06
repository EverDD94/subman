"""Genera un "episodio" sintético con voz real (espeak-ng), intro musical, efectos y música
de fondo, más su subtítulo perfecto y versiones desfasadas como las que bajan de internet.

Uso: python tests/make_episode.py OUT_DIR
"""
from __future__ import annotations

import io
import json
import random
import subprocess
import sys
import wave
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from subman import subtitles  # noqa: E402

SR = 16000
WORDS = ("we need to leave before the storm gets here and nobody knows where the keys are "
         "I told you this would happen if you kept lying to everyone in this town "
         "the doctor said she will be fine but we should wait until morning "
         "why did you come back after all these years tell me the truth "
         "listen to me carefully there is a car waiting outside for you right now").split()


def tts(text: str, voice: str, speed: int) -> np.ndarray:
    wav = subprocess.run(["espeak-ng", "-v", voice, "-s", str(speed), "--stdout", text],
                         check=True, capture_output=True).stdout
    with wave.open(io.BytesIO(wav)) as w:
        rate = w.getframerate()
        data = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768
    t = np.arange(int(len(data) * SR / rate)) / SR
    data = np.interp(t, np.arange(len(data)) / rate, data)
    nz = np.nonzero(np.abs(data) > 0.01)[0]
    return data[nz[0]:nz[-1]] if len(nz) else data


def music(seconds: float, rng: np.random.Generator, level: float) -> np.ndarray:
    t = np.arange(int(seconds * SR)) / SR
    out = np.zeros_like(t)
    chords = [(220, 277, 330), (196, 247, 294), (175, 220, 262), (165, 208, 247)]
    for n, start in enumerate(np.arange(0, seconds, 2.0)):
        m = (t >= start) & (t < start + 2.0)
        for f in chords[n % 4]:
            for h, a in ((1, 1), (2, 0.4), (3, 0.2)):
                out[m] += a * np.sin(2 * np.pi * f * h * t[m])
    for beat in np.arange(0, seconds, 0.5):  # bombo + charles
        i = int(beat * SR)
        k = np.exp(-np.arange(3000) / 400) * np.sin(2 * np.pi * 60 * np.arange(3000) / SR)
        out[i:i + 3000] += 3 * k[: len(out[i:i + 3000])]
        hh = rng.normal(0, 1, 800) * np.exp(-np.arange(800) / 150)
        j = i + SR // 4
        out[j:j + 800] += hh[: len(out[j:j + 800])]
    return level * out / np.abs(out).max()


def effects(seconds: float, rng: np.random.Generator) -> np.ndarray:
    n = int(seconds * SR)
    noise = rng.normal(0, 1, n)
    env = np.convolve(rng.random(n) ** 8, np.ones(2000) / 2000, mode="same")
    return 0.3 * noise * env / max(env.max(), 1e-9)


def build(out: Path, minutes: float = 10, seed: int = 1):
    rng = np.random.default_rng(seed)
    pyrng = random.Random(seed)
    total = minutes * 60
    track = rng.normal(0, 0.003, int(total * SR))  # ruido de sala
    cues: list[subtitles.Cue] = []
    intro = (95.0, 140.0)  # cabecera musical de 45 s tras el "cold open"
    track[int(intro[0] * SR): int(intro[1] * SR)] += music(intro[1] - intro[0], rng, 0.5)
    # música de fondo bajo el diálogo y escenas de acción sin diálogo
    for a, b in ((200, 260), (400, 430)):
        track[a * SR: b * SR] += music(b - a, rng, 0.12)
    for a, b in ((300, 330), (500, 515)):
        track[a * SR: b * SR] += effects(b - a, rng)

    t = 3.0
    voices = ["en", "en+f3", "en+m3", "en-us", "en+f2"]
    while t < total - 8:
        if intro[0] - 3 < t < intro[1]:
            t = intro[1] + 2
        if 300 < t < 330 or 500 < t < 515:
            t += 15
            continue
        words = pyrng.randint(3, 12)
        i = pyrng.randrange(len(WORDS) - words)
        text = " ".join(WORDS[i:i + words])
        speech = tts(text, pyrng.choice(voices), pyrng.randint(140, 190))
        a = int(t * SR)
        seg = track[a: a + len(speech)]
        seg += 0.6 * speech[: len(seg)]
        dur = len(speech) / SR
        cues.append(subtitles.Cue(round(t, 3), round(t + dur + 0.35, 3), text.capitalize() + "."))
        t += dur + pyrng.choice([0.4, 0.8, 1.2, 2.0, 3.5, 6.0])

    out.mkdir(parents=True, exist_ok=True)
    pcm = (np.clip(track, -1, 1) * 32767).astype(np.int16)
    wav = out / "audio.wav"
    with wave.open(str(wav), "wb") as w:
        w.setnchannels(1), w.setsampwidth(2), w.setframerate(SR), w.writeframes(pcm.tobytes())
    video = out / "Serie Prueba (2024) - S01E01.mkv"
    subprocess.run(["ffmpeg", "-y", "-v", "error", "-f", "lavfi", "-i", f"color=black:s=320x180:r=24000/1001",
                    "-i", str(wav), "-shortest", "-c:v", "libx264", "-preset", "ultrafast", "-c:a", "aac",
                    "-b:a", "96k", str(video)], check=True)
    wav.unlink()
    subtitles.save(cues, out / "truth.srt")

    def distort(name, fn, keep=lambda c: True):
        kept = [c for c in cues if keep(c)]
        subtitles.save([c.shifted(*fn(c.start, c.end)) for c in kept], out / f"{name}.srt")
        subtitles.save(kept, out / f"{name}.truth.srt")

    # 1) desfase constante (lo único que arreglan casi todas las herramientas)
    distort("const", lambda s, e: (s + 2.7, e + 2.7))
    # 2) release PAL a 25 fps con la intro 30 s más corta: deriva + salto a mitad
    r = 23.976 / 25

    def pal(s, e):
        d = 1.7 if s < intro[1] else 1.7 - 30.0
        return ((s + d) * r, (e + d) * r)
    distort("pal_intro", pal)

    # 3) subtítulo de una versión con 'previously on' de 20 s al principio y a la que le
    #    falta una escena de 15 s que nuestro video sí tiene (a los 6 min)
    def recap(s, e):
        d = 20.0 - (15.0 if s > 375 else 0.0)
        return (s + d, e + d)
    distort("recap_cut", recap, keep=lambda c: not (360 <= c.start <= 375))
    # 4) tiempos "humanos": cada línea entra/sale con imprecisión, más deriva e intro distinta
    jr = random.Random(seed + 7)

    def human(s, e):
        s2, e2 = pal(s, e)
        return (s2 + jr.gauss(0, 0.15), e2 + max(-0.3, jr.gauss(0.3, 0.25)))
    distort("human_pal_intro", human)
    # La verdad "ideal" es el subtítulo humano bien colocado (su imprecisión es irreducible).
    ideal = []
    for c, t0 in zip(subtitles.load(out / "human_pal_intro.srt"), cues):
        d = 1.7 if t0.start < intro[1] else 1.7 - 30.0
        ideal.append(c.shifted(c.start / r - d, c.end / r - d))
    subtitles.save(ideal, out / "human_pal_intro.truth.srt")
    (out / "meta.json").write_text(json.dumps({"lines": len(cues), "minutes": minutes}))
    return video


if __name__ == "__main__":
    print(build(Path(sys.argv[1])))
