"""Convertir videos al formato que Jellyfin reproduce directo en todos los aparatos.

Por cada video, sólo lo que le falta (ver :mod:`formats`):

* imagen: se copia tal cual si ya vale; si no, H.264 8 bits (libx264, CRF 20) hasta 1080p;
* sonido: primera pista AAC estéreo (copiada si ya lo era); después las pistas originales,
  copiadas si son AAC/MP3/AC3/E-AC3, o en AC3 5.1 si son DTS, TrueHD, FLAC...;
* caja: MP4 (con ``faststart``); si trae subtítulos de imagen se queda en MKV para no perderlos;
* subtítulos de texto que vengan dentro: a un .srt al lado (si no hay ya uno de ese idioma).

Se escribe primero en ``Nombre.subman.part`` junto al original. Sólo si sale bien (misma duración,
con imagen y sonido) el original se mueve a ``originals_dir`` (misma estructura de carpetas) y el
nuevo ocupa su sitio. Nada se borra: «Devolver» pone el original otra vez y aparta el convertido.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import sqlite3
import subprocess
import time
from pathlib import Path

from . import audio, formats
from .duplicates import _relative, _unique
from .library import Library, opensubtitles_hash, sidecars

log = logging.getLogger(__name__)

COPY_AUDIO = {"aac", "mp3", "ac3", "eac3"}
TEXT_SUBS = {"subrip", "ass", "ssa", "mov_text", "webvtt", "text"}
PART = ".subman.part"

SCHEMA = """
CREATE TABLE IF NOT EXISTS converted (   -- videos convertidos (se pueden devolver)
    id INTEGER PRIMARY KEY,
    original TEXT NOT NULL,      -- dónde estaba el original
    kept TEXT NOT NULL,          -- dónde está ahora el original (carpeta de originales)
    new TEXT NOT NULL,           -- el archivo convertido
    what TEXT,                   -- caja | sonido | video
    old_size INTEGER,
    new_size INTEGER,
    seconds REAL,
    at REAL
);
CREATE TABLE IF NOT EXISTS convert_failed (  -- no se vuelven a intentar solos («Reintentar» los limpia)
    path TEXT PRIMARY KEY,
    error TEXT,
    at REAL
);
"""


class Stopped(Exception):
    pass


def plan(info: dict, ff: dict, allow_hevc: bool) -> dict:
    """Qué hacer con cada pista. ``info`` es lo guardado por formats; ``ff`` lo que dice ffprobe ahora."""
    streams = ff.get("streams", [])
    video = next(s for s in streams if s.get("codec_type") == "video"
                 and not s.get("disposition", {}).get("attached_pic"))
    auds = [s for s in streams if s.get("codec_type") == "audio"]
    subs = [s for s in streams if s.get("codec_type") == "subtitle"]
    image_subs = any(s.get("codec_name") in formats.IMAGE_SUBS for s in subs)
    reencode = bool(info["why_video"]) and not (allow_hevc and info.get("hevc"))
    interlaced = (video.get("field_order") or "progressive") not in ("progressive", "unknown")
    out = {"container": "matroska" if image_subs else "mp4", "reencode": reencode, "video": video["index"],
           "deinterlace": reencode and interlaced, "hevc_copy": not reencode and video.get("codec_name") == "hevc",
           "audio": [], "subs_inside": [], "subs_out": []}
    if auds:
        main = next((a for a in auds if a.get("disposition", {}).get("default")), auds[0])
        stereo_ok = main.get("codec_name") in formats.MAIN_AUDIO_OK and (main.get("channels") or 2) <= 2
        out["audio"].append({"index": main["index"], "codec": "copy" if stereo_ok else "aac", "channels": 2,
                             "lang": main.get("tags", {}).get("language")})
        for a in auds:
            if a is main and stereo_ok:
                continue
            ch = a.get("channels") or 2
            if a.get("codec_name") in COPY_AUDIO:
                if a is main and ch <= 2:  # el estéreo principal ya va primero, convertido a AAC
                    continue
                out["audio"].append({"index": a["index"], "codec": "copy", "lang": a.get("tags", {}).get("language")})
            elif ch > 2:
                out["audio"].append({"index": a["index"], "codec": "ac3", "channels": min(ch, 6),
                                     "lang": a.get("tags", {}).get("language")})
            elif a is not main:
                out["audio"].append({"index": a["index"], "codec": "aac", "channels": 2,
                                     "lang": a.get("tags", {}).get("language")})
    for s in subs:
        if out["container"] == "matroska":
            out["subs_inside"].append(s["index"])
        elif s.get("codec_name") in TEXT_SUBS:
            lang = (s.get("tags", {}).get("language") or "und").lower()
            forced = bool(s.get("disposition", {}).get("forced"))
            out["subs_out"].append({"index": s["index"], "lang": lang, "forced": forced})
    return out


def ffmpeg_args(src: str, dest: str, p: dict) -> list[str]:
    args = ["ffmpeg", "-nostdin", "-hide_banner", "-v", "error", "-y", "-i", src,
            "-map", f"0:{p['video']}", "-map_metadata", "0", "-map_chapters", "0"]
    for a in p["audio"]:
        args += ["-map", f"0:{a['index']}"]
    for i in p["subs_inside"]:
        args += ["-map", f"0:{i}"]
    if p["reencode"]:
        args += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-profile:v", "high", "-level:v", "4.1",
                 "-pix_fmt", "yuv420p"]
        if p["deinterlace"]:
            args += ["-vf", "yadif"]
    else:
        args += ["-c:v", "copy"]
        if p["hevc_copy"] and p["container"] == "mp4":
            args += ["-tag:v", "hvc1"]  # sin esto el iPhone y algunos Roku no lo reconocen en MP4
    for n, a in enumerate(p["audio"]):
        if a["codec"] == "copy":
            args += [f"-c:a:{n}", "copy"]
        elif a["codec"] == "aac":
            args += [f"-c:a:{n}", "aac", f"-ac:a:{n}", "2", f"-b:a:{n}", "192k"]
        else:
            args += [f"-c:a:{n}", "ac3", f"-ac:a:{n}", str(a["channels"]), f"-b:a:{n}", "640k"]
        args += [f"-disposition:a:{n}", "default" if n == 0 else "0"]
    if p["subs_inside"]:
        args += ["-c:s", "copy"]
    if p["container"] == "mp4":
        args += ["-movflags", "+faststart"]
    args += ["-max_muxing_queue_size", "4096", "-progress", "pipe:1", "-nostats", "-f", p["container"], dest]
    return args


def _nice(cmd: list[str]) -> list[str]:
    """Prioridad baja: Jellyfin y lo demás van primero."""
    pre = []
    if shutil.which("ionice"):
        pre += ["ionice", "-c", "3"]
    if shutil.which("nice"):
        pre += ["nice", "-n", "19"]
    return pre + cmd


def run_ffmpeg(cmd: list[str], duration: float, progress, stop) -> None:
    proc = subprocess.Popen(_nice(cmd), stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        for line in proc.stdout:
            if stop():
                proc.kill()
                proc.wait()
                raise Stopped()
            if line.startswith("out_time_us=") and duration:
                try:
                    progress(min(1.0, int(line.split("=")[1]) / 1e6 / duration))
                except ValueError:
                    pass
        err = proc.stderr.read()
        if proc.wait() != 0:
            raise RuntimeError(err.strip().splitlines()[-1] if err.strip() else f"ffmpeg salió con {proc.returncode}")
    finally:
        if proc.poll() is None:
            proc.kill()


def _extract_subs(src: str, video_stem: Path, subs: list[dict]) -> list[str]:
    """Saca los subtítulos de texto a .srt al lado del video (sin pisar ninguno que ya exista)."""
    made = []
    for s in subs:
        name = f"{video_stem.name}.{s['lang']}{'.forced' if s['forced'] else ''}.srt"
        dest = video_stem.parent / name
        if dest.exists():
            continue
        r = subprocess.run(["ffmpeg", "-nostdin", "-v", "error", "-i", src, "-map", f"0:{s['index']}",
                            "-c:s", "srt", "-f", "srt", str(dest)], capture_output=True)
        if r.returncode == 0 and dest.exists() and dest.stat().st_size > 0:
            made.append(str(dest))
        elif dest.exists():
            dest.unlink()
    return made


def check_output(path: str, duration: float, want_audio: bool) -> dict:
    ff = audio.probe(path)
    got = float(ff.get("format", {}).get("duration") or 0)
    if duration and abs(got - duration) > max(2.0, duration * 0.01):
        raise RuntimeError(f"el convertido dura {got:.0f} s y el original {duration:.0f} s")
    kinds = {s.get("codec_type") for s in ff.get("streams", [])}
    if "video" not in kinds or (want_audio and "audio" not in kinds):
        raise RuntimeError("al convertido le falta la imagen o el sonido")
    return ff


def convert(lib: Library, path: str, info: dict, cfg: dict, roots: list[str], media_root: str,
            progress=lambda x: None, stop=lambda: False) -> dict:
    """Convierte un video. Devuelve la fila guardada en ``converted``. Si algo falla, todo queda como estaba."""
    conv = cfg["convert"]
    src = Path(path)
    if not src.exists():
        raise RuntimeError("ya no está")
    ff = audio.probe(path)
    p = plan(info, ff, conv["allow_hevc"])
    ext = ".mkv" if p["container"] == "matroska" else ".mp4"
    final = src.with_suffix(ext)
    if final != src and final.exists():
        raise RuntimeError(f"ya existe {final.name}")
    size = src.stat().st_size
    free = shutil.disk_usage(src.parent).free
    need = size * 1.2 + conv["min_free_gb"] * 1e9
    if free < need:
        raise NoSpace(f"queda poco espacio libre ({free / 1e9:.0f} GB); hace falta dejar {conv['min_free_gb']} GB")
    tmp = src.with_name(src.stem + PART)
    duration = float(ff.get("format", {}).get("duration") or 0)
    what = formats.group(info, conv["allow_hevc"])
    t0 = time.time()
    try:
        run_ffmpeg(ffmpeg_args(path, str(tmp), p), duration, progress, stop)
        check_output(str(tmp), duration, bool(p["audio"]))
        subs = _extract_subs(path, final.with_suffix(""), p["subs_out"])
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    kept = _unique(Path(conv["originals_dir"]) / _relative(src, [media_root, *roots]))
    kept.parent.mkdir(parents=True, exist_ok=True)
    st = src.stat()
    shutil.move(str(src), str(kept))
    try:
        os.replace(tmp, final)
        try:
            os.chmod(final, st.st_mode & 0o7777)
            os.chown(final, st.st_uid, st.st_gid)
        except OSError:
            pass
    except OSError:
        shutil.move(str(kept), str(src))
        tmp.unlink(missing_ok=True)
        raise
    new_size = final.stat().st_size
    row = {"original": str(src), "kept": str(kept), "new": str(final), "what": what, "old_size": size,
           "new_size": new_size, "seconds": time.time() - t0, "at": time.time()}
    _commit(lib, row, final)
    log.info("convertido (%s, %.0f s): %s -> %s; original en %s%s", what, row["seconds"], src.name, final.name, kept,
             f"; subtítulos sacados: {len(subs)}" if subs else "")
    return row


class NoSpace(RuntimeError):
    pass


def _commit(lib: Library, row: dict, final: Path) -> None:
    for attempt in range(5):  # la web o la pasada de subtítulos pueden tener la base ocupada un momento
        try:
            lib.db.execute("INSERT INTO converted (original, kept, new, what, old_size, new_size, seconds, at) "
                           "VALUES (:original,:kept,:new,:what,:old_size,:new_size,:seconds,:at)", row)
            _repoint(lib, row["original"], final)
            lib.db.commit()
            return
        except sqlite3.OperationalError:
            lib.db.rollback()
            time.sleep(5)
    log.error("convertido pero no se pudo anotar en la base de datos: %s (original en %s)", final, row["kept"])


def _repoint(lib: Library, old: str, new: Path) -> None:
    """El video sigue siendo el mismo para subman (subtítulos, nombres): sólo cambia el archivo."""
    st = new.stat()
    dup = lib.db.execute("SELECT id FROM media WHERE path=?", (str(new),)).fetchone()
    if dup and str(new) != old:
        lib.db.execute("DELETE FROM media WHERE id=?", (dup["id"],))
    lib.db.execute("UPDATE media SET path=?, size=?, mtime=?, oshash=? WHERE path=?",
                   (str(new), st.st_size, st.st_mtime, opensubtitles_hash(new), old))
    lib.db.execute("DELETE FROM formats WHERE path IN (?, ?)", (old, str(new)))
    try:
        grp, info = formats.classify(audio.probe(str(new)))
        lib.db.execute("INSERT OR REPLACE INTO formats VALUES (?,?,?,?,?)",
                       (str(new), st.st_size, st.st_mtime, grp, json.dumps(info, ensure_ascii=False)))
    except Exception:
        pass  # se mirará en el próximo análisis


def converted_list(lib: Library, limit: int = 300) -> list[dict]:
    rows = lib.db.execute("SELECT * FROM converted ORDER BY at DESC LIMIT ?", (limit,)).fetchall()
    return [{**dict(r), "exists": os.path.exists(r["kept"])} for r in rows]


def restore(lib: Library, row_id: int, originals_dir: str, media_root: str, roots: list[str]) -> str:
    """Devuelve el original a su sitio y aparta el convertido a la carpeta de originales (no se borra)."""
    r = lib.db.execute("SELECT * FROM converted WHERE id=?", (row_id,)).fetchone()
    if not r:
        raise ValueError("no está en la lista de convertidos")
    original, kept, new = Path(r["original"]), Path(r["kept"]), Path(r["new"])
    if not kept.exists():
        raise ValueError(f"el original ya no está en {kept}")
    if original.exists() and original != new:
        raise ValueError(f"ya hay algo en {original}; muévelo a mano")
    aside = None
    if new.exists():
        rel = _relative(new, [media_root, *roots])
        aside = _unique(Path(originals_dir) / rel.with_name(f"{rel.stem} (convertido){rel.suffix}"))
        aside.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(new), str(aside))
    shutil.move(str(kept), str(original))
    try:
        lib.db.execute("DELETE FROM converted WHERE id=?", (row_id,))
        _repoint(lib, str(new), original)
        lib.db.commit()
    except sqlite3.Error:
        lib.db.rollback()
    log.info("convertido devuelto: %s vuelve a su sitio%s", original, f"; el convertido quedó en {aside}" if aside else "")
    return str(original)


def in_window(conv: dict, now: float | None = None) -> bool:
    if not conv.get("night_only"):
        return True
    return in_hours(int(conv["night_start"]), int(conv["night_end"]), now)


def in_hours(a: int, b: int, now: float | None = None) -> bool:
    """¿Estamos entre las a:00 y las b:00? (si a > b, el horario pasa por medianoche)."""
    h = time.localtime(now).tm_hour
    return a <= h < b if a < b else (h >= a or h < b)


def leftovers(roots: list[str]) -> int:
    """Borra los .subman.part que quedaron a medias (por un reinicio). Son copias incompletas nuestras."""
    n = 0
    for root in roots:
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames if not d.startswith(("@", "."))]
            for fn in filenames:
                if fn.endswith(PART):
                    try:
                        os.unlink(os.path.join(dirpath, fn))
                        n += 1
                    except OSError:
                        pass
    return n
