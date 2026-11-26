"""Saltar intro y créditos: Videotecario busca dónde empiezan y acaban en cada capítulo.

Pistas, de más a menos fiable:
1. Lo que corregiste a mano.
2. Las marcas de capítulo del archivo («Opening», «Intro», «Credits», «Ending»…).
3. El audio que se repite entre capítulos de la misma temporada (la canción de la intro y la de los
   créditos): huella del sonido cada 0,128 s y se busca el tramo común más largo, tolerando ruido.
Lo guardado se da a Everflix (web, Roku y Google TV) para el botón «Saltar».
"""
from __future__ import annotations

import json
import logging
import re
import subprocess
import time
from pathlib import Path

import numpy as np

log = logging.getLogger("subman.skipseg")

SR = 8000
HOP = 512               # 0,064 s por huella
STEP = HOP / SR
HEAD = 12 * 60          # la intro se busca en los primeros 12 min
TAIL = 10 * 60          # los créditos, en los últimos 10 min
INTRO_LEN = (12, 150)   # segundos
CRED_LEN = (15, 360)

SCHEMA = """
CREATE TABLE IF NOT EXISTS skipseg (
    path TEXT PRIMARY KEY, duration REAL,
    intro_start REAL, intro_end REAL, credits_start REAL, credits_end REAL,
    source TEXT, manual INTEGER DEFAULT 0, updated REAL
);
"""

_INTRO = re.compile(r"\b(intro|opening|op|apertura|cabecera|introducci[oó]n)\b", re.I)
_CRED = re.compile(r"\b(credits?|ending|ed|end ?credits|cr[eé]ditos|outro)\b", re.I)


def duration(path: Path) -> float:
    out = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", str(path)],
                         capture_output=True, text=True).stdout.strip()
    return float(out or 0)


def chapters(path: Path) -> dict:
    """Intro/créditos según las marcas de capítulo del archivo, si las tiene con nombre."""
    try:
        out = subprocess.run(["ffprobe", "-v", "error", "-print_format", "json", "-show_chapters", str(path)],
                             capture_output=True, text=True, timeout=60).stdout
        chs = json.loads(out or "{}").get("chapters") or []
    except Exception:
        return {}
    res = {}
    for c in chs:
        title = (c.get("tags") or {}).get("title") or ""
        s, e = float(c.get("start_time") or 0), float(c.get("end_time") or 0)
        if "intro" not in res and _INTRO.search(title) and INTRO_LEN[0] <= e - s <= INTRO_LEN[1] + 30:
            res["intro"] = (s, e)
        elif "credits" not in res and _CRED.search(title) and e - s >= CRED_LEN[0]:
            res["credits"] = (s, e)
    return res


def _pcm(path: Path, start: float, length: float) -> np.ndarray:
    cmd = ["nice", "-n", "19", "ffmpeg", "-nostdin", "-v", "error", "-ss", f"{max(0, start):.2f}", "-t", f"{length:.2f}",
           "-i", str(path), "-map", "0:a:0", "-ac", "1", "-ar", str(SR), "-f", "s16le", "-"]
    raw = subprocess.run(cmd, capture_output=True, timeout=600).stdout
    return np.frombuffer(raw, dtype=np.int16).astype(np.float32) / 32768.0


def fingerprint(x: np.ndarray) -> np.ndarray:
    """Por cada trozo de 0,064 s, las 2 bandas de frecuencia más fuertes (de 48): (n, 2) enteros."""
    n = 2048
    if len(x) < n + HOP:
        return np.zeros((0, 2), dtype=np.int16)
    frames = np.lib.stride_tricks.sliding_window_view(x, n)[::HOP] * np.hanning(n)
    spec = np.abs(np.fft.rfft(frames, axis=1)) ** 2
    freqs = np.fft.rfftfreq(n, 1 / SR)
    edges = np.geomspace(200, 3800, 49)
    bands = np.stack([spec[:, (freqs >= a) & (freqs < b)].sum(axis=1) for a, b in zip(edges[:-1], edges[1:])], axis=1)
    return np.argsort(-bands, axis=1)[:, :2].astype(np.int16)


def _hashes(fp: np.ndarray) -> np.ndarray:
    """Huellas tipo «constelación» por posición: (combinaciones, n). -1 donde no hay."""
    m = len(fp)
    rows = []
    for dt in (2, 4, 7):
        for p in range(2):
            for q in range(2):
                h = np.full(m, -1, dtype=np.int64)
                h[:m - dt] = (fp[:m - dt, p].astype(np.int64) * 48 + fp[dt:, q]) * 8 + dt
                rows.append(h)
    return np.stack(rows)


def common(a: np.ndarray, b: np.ndarray, min_len: float, max_len: float) -> tuple[float, float, float, float] | None:
    """El tramo de sonido más largo que está en los dos: (inicio en a, fin en a, inicio en b, fin en b), en segundos."""
    if len(a) < 50 or len(b) < 50:
        return None
    ha, hb = _hashes(a), _hashes(b)
    # desplazamientos candidatos: votos de huellas iguales (las muy repetidas pesan poco)
    idx: dict[int, list[int]] = {}
    for j, v in enumerate(hb.T.tolist()):
        for x in v:
            if x >= 0:
                idx.setdefault(x, []).append(j)
    votes: dict[int, float] = {}
    for i, v in enumerate(ha.T.tolist()):
        for x in v:
            js = idx.get(x)
            if js and len(js) < 300:
                w = 1 / len(js)
                for j in js:
                    votes[i - j] = votes.get(i - j, 0) + w
    best = None
    gap = int(1.5 / STEP)
    tried = []
    for off, _ in sorted(votes.items(), key=lambda kv: -kv[1]):
        if len(tried) >= 6:
            break
        if any(abs(off - t) < 3 for t in tried):
            continue
        tried.append(off)
        i0, j0 = max(0, off), max(0, -off)
        n = min(len(a) - i0, len(b) - j0)
        if n <= 0:
            continue
        hit = np.zeros(n, dtype=bool)
        for d in (-1, 0, 1):  # medio trozo de desajuste
            lo, hi = max(0, -d), min(n, len(b) - j0 - d)
            if hi > lo:
                hit[lo:hi] |= ((ha[:, i0 + lo:i0 + hi] == hb[:, j0 + lo + d:j0 + hi + d]) & (ha[:, i0 + lo:i0 + hi] >= 0)).sum(axis=0) >= 4
        pos = np.flatnonzero(hit)
        run_s = last = None
        cur = None
        for i in pos.tolist():
            if run_s is None or i - last > gap:
                run_s = i
            last = i
            if cur is None or last - run_s > cur[1] - cur[0]:
                cur = (run_s, last + 1)
        if not cur:
            continue
        length = (cur[1] - cur[0]) * STEP
        dens = hit[cur[0]:cur[1]].mean()
        if min_len <= length <= max_len and dens > 0.3 and (not best or length > best[1] - best[0]):
            best = ((i0 + cur[0]) * STEP, (i0 + cur[1]) * STEP, (j0 + cur[0]) * STEP, (j0 + cur[1]) * STEP)
    return best


class Skips:
    def __init__(self, db):
        self.db = db
        self.db.executescript(SCHEMA)

    def get(self, path: str) -> dict | None:
        r = self.db.execute("SELECT * FROM skipseg WHERE path=?", (path,)).fetchone()
        return dict(r) if r else None

    def put(self, path: str, **kw) -> None:
        kw["updated"] = time.time()
        old = self.get(path)
        if old and old.get("manual") and not kw.get("manual"):
            return  # lo que corregiste a mano no se pisa
        cols = ["path", *kw]
        upd = ", ".join(f"{k}=excluded.{k}" for k in kw)
        self.db.execute(f"INSERT INTO skipseg ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))}) "
                        f"ON CONFLICT(path) DO UPDATE SET {upd}", (path, *kw.values()))
        self.db.commit()

    def analyze_season(self, files: list[Path], force: bool = False) -> int:
        """Busca intro y créditos en los capítulos de una temporada. Devuelve cuántos tienen algo."""
        files = sorted(files)
        todo = [f for f in files if force or not self.get(str(f))]
        if not todo:
            return 0
        info: dict[Path, dict] = {}
        for f in files:
            try:
                d = duration(f)
            except Exception:
                d = 0
            if not d:
                continue
            info[f] = {"dur": d, "ch": chapters(f)}
        need_audio = [f for f in info if not ("intro" in info[f]["ch"] and "credits" in info[f]["ch"])]
        if len(info) >= 2 and need_audio:
            for f in info:
                try:
                    info[f]["head"] = fingerprint(_pcm(f, 0, min(HEAD, info[f]["dur"] / 2)))
                    t0 = max(info[f]["dur"] - TAIL, info[f]["dur"] / 2)
                    info[f]["t0"] = t0
                    info[f]["tail"] = fingerprint(_pcm(f, t0, info[f]["dur"] - t0))
                except Exception as e:
                    log.info("saltar intro: %s: %s", f.name, e)
        found = {f: dict(info[f]["ch"]) for f in info}
        names = list(info)
        for i, f in enumerate(names):
            others = [names[k] for k in (i + 1, i - 1, i + 2, i - 2) if 0 <= k < len(names)]
            for part, key, lim in (("intro", "head", INTRO_LEN), ("credits", "tail", CRED_LEN)):
                if part in found[f] or key not in info[f]:
                    continue
                best = None
                for g in others:
                    if key not in info[g]:
                        continue
                    m = common(info[f][key], info[g][key], *lim)
                    if m and (not best or m[1] - m[0] > best[1] - best[0]):
                        best = m
                if best:
                    off = info[f].get("t0", 0) if part == "credits" else 0
                    s, e = best[0] + off, best[1] + off
                    if part == "credits" and info[f]["dur"] - e < 20:
                        e = info[f]["dur"]  # hasta el final
                    found[f][part] = (round(s, 1), round(e, 1))
        n = 0
        for f in info:
            if str(f) not in {str(x) for x in todo}:
                continue
            it, cr = found[f].get("intro"), found[f].get("credits")
            src = "capítulos" if info[f]["ch"] else "audio"
            self.put(str(f), duration=info[f]["dur"], intro_start=it[0] if it else None, intro_end=it[1] if it else None,
                     credits_start=cr[0] if cr else None, credits_end=cr[1] if cr else None, source=src)
            n += bool(it or cr)
        return n
