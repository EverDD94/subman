"""Por qué un video no tiene todavía un subtítulo bueno, y qué se puede hacer.

Se deduce de lo que guardó la última pasada (estado + informe de sincronización), sin volver a
analizar nada.
"""
from __future__ import annotations

import json
import re

FORCED_LINES = 150  # una película entera tiene muchas más; menos suele ser un subtítulo «forzado»
_SPANISH_AUDIO = re.compile(r"\b(lat|latino|latam|castellano|espa[nñ]ol)\b", re.I)

# código -> (título, qué pasa, qué hacer)
REASONS: dict[str, tuple[str, str, str]] = {
    "sin_procesar": (
        "Aún no se han mirado",
        "subman todavía no ha llegado a estos videos.",
        "No hace falta hacer nada: se irán haciendo en las próximas pasadas."),
    "sin_subtitulo": (
        "No se encontró ningún subtítulo",
        "No había subtítulo en español junto al video y OpenSubtitles no encontró ninguno con ese nombre.",
        "Casi siempre es por el nombre del archivo o la carpeta (en español, con letras cambiadas o "
        "con basura como «26rf» o «@canal»). Renómbralo como «Título original (Año)», o pon tú un "
        ".srt junto al video y subman lo sincroniza."),
    "forzado": (
        "Subtítulo incompleto o «forzado»",
        "El subtítulo tiene muy pocas líneas: suele ser uno «forzado», que sólo traduce carteles o "
        "frases en otro idioma, no la película entera.",
        "Si el audio ya está en español, no lo necesitas. Si no, abre el video y pulsa «Está mal, "
        "buscar otro»."),
    "otro_video": (
        "El subtítulo es de otra versión",
        "El texto no coincide con lo que se oye: es de otro episodio, de otra película o de un "
        "montaje distinto (versión extendida, cortes de TV...).",
        "Abre el video y pulsa «Está mal, buscar otro» para que pruebe con OpenSubtitles."),
    "poco_dialogo": (
        "Poco diálogo para comprobar",
        "Hay mucha música o ruido y subman no consigue confirmar si está bien.",
        "Mira un trozo: si va bien, «Está bien, aceptarlo»."),
    "desfasado": (
        "No se consiguió sincronizar",
        "El subtítulo parece de este video, pero hay tramos que no se pudieron cuadrar con la voz.",
        "Pulsa «Está mal, buscar otro». Si no hay otro mejor, míralo: a veces sólo falla un tramo."),
    "revisar": (
        "Casi bien, falta que lo confirmes",
        "Está guardado y sincronizado, pero algunos tramos no se pudieron confirmar del todo.",
        "Ponlo un momento: si va bien, «Está bien, aceptarlo»; si no, «Está mal, buscar otro»."),
    "error": (
        "Error al leer el video",
        "No se pudo abrir el audio o el archivo (dañado, incompleto o un formato raro).",
        "Abre el video para ver el error; también sale en el Registro."),
}
ORDER = list(REASONS)


def reason(row, label: str = "") -> dict:
    """``row`` es la fila de la tabla subtitle (o None). Devuelve código y pistas extra."""
    extra = {"audio_es": bool(_SPANISH_AUDIO.search(label))}
    if row is None:
        return {"code": "sin_procesar", **extra}
    status = row["status"]
    if status == "ok":
        return {"code": "ok", **extra}
    if status == "error":
        return {"code": "error", **extra}
    if status == "pendiente":
        return {"code": "sin_subtitulo", **extra}
    try:
        d = json.loads(row["detail"]) if row["detail"] and row["detail"].startswith("{") else {}
    except ValueError:
        d = {}
    tried = d.get("tried") or []
    extra["only_local"] = bool(tried) and all(str(t[0]).startswith("local:") for t in tried)
    segs = d.get("segments") or []
    lines = max((s["lines"][1] for s in segs), default=0)
    if status == "revisar":
        return {"code": "revisar", **extra}
    if lines and lines < FORCED_LINES:
        return {"code": "forzado", **extra}
    if d.get("identity", 1) < 0.20:
        return {"code": "otro_video", **extra}
    if d.get("clear_windows", 1) < 0.4:
        return {"code": "poco_dialogo", **extra}
    return {"code": "desfasado", **extra}


def summary(items: list[dict]) -> list[dict]:
    """``items``: [{"code", "audio_es", "only_local"}] -> resumen por motivo, en orden."""
    out = {}
    for it in items:
        if it["code"] == "ok":
            continue
        g = out.setdefault(it["code"], {"code": it["code"], "count": 0, "audio_es": 0, "only_local": 0})
        g["count"] += 1
        g["audio_es"] += it.get("audio_es", False)
        g["only_local"] += it.get("only_local", False)
    res = []
    for code in ORDER:
        if code in out:
            title, what, todo = REASONS[code]
            res.append({**out[code], "title": title, "what": what, "todo": todo})
    return res
