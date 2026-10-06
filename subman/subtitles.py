"""Lectura y escritura de subtítulos (SRT y WebVTT)."""
from __future__ import annotations

import re
from dataclasses import dataclass, replace
from pathlib import Path

_TIME = r"(\d{1,2}):(\d{2}):(\d{2})[,.](\d{1,3})"
_TIMING = re.compile(_TIME + r"\s*-->\s*" + _TIME)


@dataclass(frozen=True)
class Cue:
    start: float  # segundos
    end: float
    text: str

    def shifted(self, start: float, end: float) -> "Cue":
        return replace(self, start=start, end=end)


def _secs(h, m, s, ms) -> float:
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms.ljust(3, "0")) / 1000


def decode(data: bytes) -> str:
    for enc in ("utf-8-sig", "utf-16"):
        try:
            text = data.decode(enc)
            if enc == "utf-16" and "-->" not in text:
                continue
            return text
        except UnicodeDecodeError:
            pass
    # Los .srt en español suelen venir en cp1252 cuando no son UTF-8.
    return data.decode("cp1252", errors="replace")


def parse(text: str) -> list[Cue]:
    cues: list[Cue] = []
    blocks = re.split(r"\n\s*\n", text.replace("\r\n", "\n").replace("\r", "\n"))
    for block in blocks:
        lines = block.strip("\n").split("\n")
        for i, line in enumerate(lines):
            m = _TIMING.search(line)
            if m:
                start = _secs(*m.groups()[:4])
                end = _secs(*m.groups()[4:])
                body = "\n".join(lines[i + 1:]).strip()
                if end > start and body:
                    cues.append(Cue(start, end, body))
                break
    cues.sort(key=lambda c: c.start)
    return cues


def load(path: str | Path) -> list[Cue]:
    return parse(decode(Path(path).read_bytes()))


def _fmt(t: float) -> str:
    ms = max(0, int(round(t * 1000)))
    h, ms = divmod(ms, 3_600_000)
    m, ms = divmod(ms, 60_000)
    s, ms = divmod(ms, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def dumps(cues: list[Cue]) -> str:
    out = []
    for n, c in enumerate(cues, 1):
        out.append(f"{n}\n{_fmt(c.start)} --> {_fmt(c.end)}\n{c.text}\n")
    return "\n".join(out)


def save(cues: list[Cue], path: str | Path) -> None:
    Path(path).write_text(dumps(cues), encoding="utf-8")
