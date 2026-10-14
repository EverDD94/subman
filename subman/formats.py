"""Formatos: qué videos reproduce Jellyfin directo en todos los aparatos y cuáles no.

Lo que reproducen directo móvil, Roku, Fire TV, Google TV y el navegador:

* caja **MP4**,
* imagen **H.264** de 8 bits, hasta 1080p,
* sonido principal **AAC** (o MP3) estéreo; las demás pistas en AAC, AC3 o E-AC3,
* subtítulos de texto en un .srt al lado (los de imagen, PGS/DVD, obligan a convertir).

Cada video cae en un grupo, de lo más barato a lo más caro de arreglar:

* ``ok``     ya está bien, no se toca;
* ``caja``   sólo hay que cambiar la caja a MP4 (segundos, sin perder calidad);
* ``sonido`` hay que convertir el sonido (la imagen se copia tal cual; un par de minutos);
* ``video``  hay que convertir la imagen (lento: más o menos lo que dura el video);
* ``4k``     4K o HDR: no se toca (los aparatos que lo muestran ya entienden HEVC);
* ``error``  no se pudo leer.

Este módulo sólo mira (ffprobe) y cuenta: no cambia ningún archivo. El resultado se guarda
en la base de datos y sólo se vuelve a mirar un video si cambió su tamaño o su fecha.
"""
from __future__ import annotations

import json
import logging
import time
from pathlib import Path

from . import audio
from .library import Library

log = logging.getLogger(__name__)

VERSION = 2  # sube cuando cambia lo que se guarda de cada video (así se vuelven a mirar)
GROUPS = ("ok", "caja", "sonido", "video", "4k", "error")
MP4_FORMATS = {"mov", "mp4", "m4a", "3gp", "3g2", "mj2"}  # format_name de ffprobe para MP4: «mov,mp4,m4a,3gp,3g2,mj2»
MAIN_AUDIO_OK = {"aac", "mp3"}
OTHER_AUDIO_OK = {"aac", "mp3", "ac3", "eac3"}
IMAGE_SUBS = {"hdmv_pgs_subtitle", "dvd_subtitle", "dvb_subtitle", "xsub"}
H264_PROFILES_OK = {"constrained baseline", "baseline", "main", "high"}
HDR_TRANSFER = {"smpte2084", "arib-std-b67"}
MAX_LEVEL = 42  # H.264 4.2: lo que aguantan los Roku y Fire TV más sencillos a 1080p

# Cuánto tarda el NAS (Ryzen R1600, sin aceleración de video), en segundos de trabajo por segundo de video.
SPEED = {"video_1080": 1.1, "video_720": 0.5, "video_sd": 0.25, "sonido": 0.02}
COPY_MB_S = 80  # cambiar la caja = copiar el archivo

SCHEMA = """
CREATE TABLE IF NOT EXISTS formats (
    path TEXT PRIMARY KEY,
    size INTEGER,
    mtime REAL,
    grp TEXT NOT NULL,
    info TEXT NOT NULL      -- JSON: lo que se vio del archivo y por qué está en su grupo
);
"""

AUDIO_NAMES = {"aac": "AAC", "mp3": "MP3", "ac3": "AC3 (Dolby)", "eac3": "E-AC3 (Dolby+)", "dts": "DTS",
               "truehd": "TrueHD", "flac": "FLAC", "opus": "Opus", "vorbis": "Vorbis", "pcm_s16le": "PCM",
               "pcm_s24le": "PCM", "mp2": "MP2", "wmav2": "WMA"}
VIDEO_NAMES = {"h264": "H.264", "hevc": "HEVC (H.265)", "mpeg4": "MPEG-4 (XviD/DivX)", "av1": "AV1",
               "vp9": "VP9", "mpeg2video": "MPEG-2", "vc1": "VC-1", "wmv3": "WMV", "msmpeg4v3": "DivX 3"}


CONTAINER_NAMES = {"matroska": "MKV", "mov": "MP4", "avi": "AVI", "mpegts": "TS", "asf": "WMV", "flv": "FLV",
                   "mpeg": "MPG", "ogg": "OGM", "webm": "WebM"}


def _lang(s: dict) -> str:
    lang = s.get("tags", {}).get("language", "")
    return f" ({lang})" if lang and lang != "und" else ""


def _channels(n) -> str:
    return {1: "mono", 2: "estéreo", 6: "5.1", 8: "7.1"}.get(n or 0, f"{n} canales" if n else "")


def _bits(v: dict) -> int:
    raw = v.get("bits_per_raw_sample")
    if raw and str(raw).isdigit():
        return int(raw)
    pf = v.get("pix_fmt") or ""
    return 10 if "10" in pf else 12 if "12" in pf else 8


def classify(info: dict) -> tuple[str, dict]:
    """Grupo de un video a partir de lo que dice ffprobe, con las razones en palabras sencillas."""
    streams = info.get("streams", [])
    fmt = info.get("format", {})
    video = next((s for s in streams if s.get("codec_type") == "video"
                  and not s.get("disposition", {}).get("attached_pic")), None)
    auds = [s for s in streams if s.get("codec_type") == "audio"]
    subs = [s for s in streams if s.get("codec_type") == "subtitle"]
    duration = float(fmt.get("duration") or 0)
    first = (fmt.get("format_name") or "").split(",")[0]
    out = {"duration": duration, "container": CONTAINER_NAMES.get(first, first.upper()),
           "video": "", "height": 0, "audio": [], "subs": [], "why": [], "notes": []}
    if not video:
        out.update(why=["no tiene imagen"], grp="error", v=VERSION)
        return "error", out

    codec = video.get("codec_name", "")
    w, h = int(video.get("width") or 0), int(video.get("height") or 0)
    bits = _bits(video)
    profile = (video.get("profile") or "").lower()
    level = int(video.get("level") or 0)
    hdr = (video.get("color_transfer") in HDR_TRANSFER
           or any("dovi" in (sd.get("side_data_type") or "").lower() or "dolby vision" in (sd.get("side_data_type") or "").lower()
                  for sd in video.get("side_data_list", [])))
    out.update(video=VIDEO_NAMES.get(codec, codec.upper()) + (f" {bits} bits" if bits != 8 else ""), height=h,
               width=w, hdr=hdr)
    out["audio"] = [f"{AUDIO_NAMES.get(a.get('codec_name'), (a.get('codec_name') or '?').upper())} "
                    f"{_channels(a.get('channels'))}".strip()
                    + _lang(a) for a in auds]
    out["subs"] = [("imagen" if s.get("codec_name") in IMAGE_SUBS else "texto")
                   + _lang(s) for s in subs]

    if w > 1920 or h > 1088 or hdr:
        out["why"].append("4K" if (w > 1920 or h > 1088) else "HDR")
        out.update(grp="4k", v=VERSION)
        return "4k", out

    why_video = []
    if codec != "h264":
        why_video.append(f"la imagen es {VIDEO_NAMES.get(codec, codec.upper())}")
    else:
        if bits > 8:
            why_video.append(f"H.264 de {bits} bits")
        if profile and profile not in H264_PROFILES_OK:
            why_video.append(f"H.264 perfil «{video.get('profile')}»")
        if level > MAX_LEVEL:
            why_video.append(f"H.264 nivel {level / 10:g}")
    interlaced = (video.get("field_order") or "progressive") not in ("progressive", "unknown")
    if interlaced:
        why_video.append("imagen entrelazada")
    # HEVC normal (8 o 10 bits) lo leen Roku, Fire TV, Google TV, Android e iPhone; sólo algún navegador no.
    out["hevc"] = codec == "hevc" and profile in ("main", "main 10") and not interlaced

    why_audio = []
    if not auds:
        out["notes"].append("no tiene sonido")
    else:
        main = next((a for a in auds if a.get("disposition", {}).get("default")), auds[0])
        if main.get("codec_name") not in MAIN_AUDIO_OK:
            why_audio.append(f"el sonido principal es {AUDIO_NAMES.get(main.get('codec_name'), main.get('codec_name'))}")
        elif (main.get("channels") or 2) > 2:
            why_audio.append(f"el sonido principal es AAC {_channels(main.get('channels'))}, falta uno estéreo")
        bad = sorted({AUDIO_NAMES.get(a.get("codec_name"), a.get("codec_name") or "?") for a in auds
                      if a is not main and a.get("codec_name") not in OTHER_AUDIO_OK})
        if bad:
            why_audio.append("otra pista en " + ", ".join(bad))

    container = set((fmt.get("format_name") or "").split(","))
    why_box = [] if container & MP4_FORMATS else [f"la caja es {out['container'] or '?'}"]
    out["image_subs"] = any(s.get("codec_name") in IMAGE_SUBS for s in subs)
    if out["image_subs"]:
        if why_box:  # MKV lo leen todos los aparatos de la tele; en MP4 se perderían esos subtítulos
            out["notes"].append("trae subtítulos de imagen dentro: se queda en MKV para no perderlos")
            why_box = []
    elif subs and why_box:
        out["notes"].append("trae subtítulos de texto dentro: se sacan a un .srt al lado")

    out.update(why_video=why_video, why_audio=why_audio, why_box=why_box, v=VERSION)
    return group(out, allow_hevc=False), out


def group(info: dict, allow_hevc: bool) -> str:
    """Grupo de un video ya analizado. Con allow_hevc, el HEVC normal cuenta como imagen válida."""
    if "why_video" not in info:  # análisis antiguo o error
        return info.get("grp", "error")
    if info.get("height", 0) > 1088 or info.get("width", 0) > 1920 or info.get("hdr"):
        return "4k"
    if info["why_video"] and not (allow_hevc and info.get("hevc")):
        return "video"
    if info["why_audio"]:
        return "sonido"
    if info["why_box"]:
        return "caja"
    return "ok"


def reasons(info: dict, allow_hevc: bool) -> list[str]:
    if "why_video" not in info:
        return info.get("why", [])
    vid = [] if allow_hevc and info.get("hevc") else info["why_video"]
    return vid + info["why_audio"] + info["why_box"]


def estimate(grp: str, info: dict, size: int) -> float:
    """Segundos aproximados que tardaría el NAS en arreglarlo."""
    d = info.get("duration") or 0
    copy = (size or 0) / 1e6 / COPY_MB_S
    if grp == "video":
        h = info.get("height") or 0
        return d * SPEED["video_1080" if h > 720 else "video_720" if h > 576 else "video_sd"]
    if grp == "sonido":
        return d * SPEED["sonido"] + copy
    if grp == "caja":
        return copy
    return 0.0


class Formats:
    def __init__(self, lib: Library):
        self.lib = lib
        lib.db.executescript(SCHEMA)

    def scan(self, progress=lambda done, total, name: None, stop=lambda: False) -> dict:
        """Mira con ffprobe los videos nuevos o cambiados; los demás salen de lo guardado."""
        media = self.lib.media()
        known = {r["path"]: r for r in self.lib.db.execute(
            "SELECT path, size, mtime FROM formats WHERE json_extract(info, '$.v') = ?", (VERSION,))}
        mtimes = {r["path"]: r["mtime"] for r in self.lib.db.execute("SELECT path, mtime FROM media")}
        looked = 0
        for n, m in enumerate(media):
            if stop():
                break
            k = known.get(m.path)
            if k and k["size"] == m.size and k["mtime"] == mtimes.get(m.path):
                continue
            progress(n, len(media), Path(m.path).name)
            try:
                grp, info = classify(audio.probe(m.path))
            except Exception as e:
                log.warning("formatos: no se pudo leer %s: %s", m.path, getattr(e, "stderr", b"") or e)
                grp, info = "error", {"why": ["no se pudo leer (¿archivo dañado o a medio copiar?)"], "duration": 0,
                                      "grp": "error", "v": VERSION}
            self.lib.db.execute("INSERT OR REPLACE INTO formats VALUES (?,?,?,?,?)",
                                (m.path, m.size, mtimes.get(m.path), grp, json.dumps(info, ensure_ascii=False)))
            looked += 1
            if looked % 20 == 0:
                self.lib.db.commit()
        self.lib.db.execute("DELETE FROM formats WHERE path NOT IN (SELECT path FROM media)")
        self.lib.db.commit()
        return {"mirados": looked, "total": len(media)}

    def summary(self, allow_hevc: bool = True) -> dict:
        rows = self.lib.db.execute(
            "SELECT f.path, f.grp, f.info, m.id, m.size, m.kind, m.title, m.season, m.episode, m.year "
            "FROM formats f JOIN media m ON m.path = f.path ORDER BY m.title, m.season, m.episode").fetchall()
        groups = {g: {"count": 0, "size": 0, "seconds": 0.0, "duration": 0.0} for g in GROUPS}
        items = []
        for r in rows:
            info = json.loads(r["info"])
            info.setdefault("grp", r["grp"])
            grp = group(info, allow_hevc)
            secs = estimate(grp, info, r["size"])
            g = groups[grp]
            g["count"] += 1
            g["size"] += r["size"] or 0
            g["seconds"] += secs
            g["duration"] += info.get("duration") or 0
            label = (f"{r['title']} S{r['season']:02d}E{r['episode']:02d}" if r["kind"] == "episode"
                     else f"{r['title']} ({r['year']})" if r["year"] else r["title"])
            items.append({"id": r["id"], "label": label, "name": Path(r["path"]).name, "path": r["path"],
                          "grp": grp, "size": r["size"], "seconds": secs, "video": info.get("video", ""),
                          "height": info.get("height", 0), "audio": info.get("audio", []), "subs": info.get("subs", []),
                          "container": info.get("container", ""), "why": reasons(info, allow_hevc),
                          "notes": info.get("notes", []), "duration": info.get("duration", 0)})
        pending = self.lib.db.execute(
            "SELECT COUNT(*) FROM media WHERE path NOT IN (SELECT path FROM formats WHERE json_extract(info, '$.v') = ?)",
            (VERSION,)).fetchone()[0]
        return {"groups": groups, "items": items, "pending": pending, "analyzed": len(rows), "at": time.time()}
