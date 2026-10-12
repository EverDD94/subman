"""Flujo completo para un video: buscar candidatos, sincronizar cada uno, verificar y
quedarse con el mejor. Sólo se escribe junto al video lo que pasa la verificación."""
from __future__ import annotations

import json
import logging
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

from . import subtitles, sync, verify
from .library import Library, Media, sidecar_for
from .providers import Provider

log = logging.getLogger(__name__)
_RANK = {verify.OK: 2, verify.REVIEW: 1, verify.FAIL: 0}


@dataclass
class Settings:
    languages: list[str]
    candidates: int = 5          # cuántos subtítulos probar como máximo por video e idioma
    keep_review: bool = True     # guardar también los dudosos (marcados como 'revisar')
    sync_existing: bool = True   # sincronizar subtítulos que ya estaban junto al video
    reference: str = "auto"
    retry_hours: float = 24      # cada cuánto reintentar lo que salió mal o no tenía candidatos


def _quality(rep: verify.Report) -> tuple:
    return (_RANK[rep.verdict], rep.identity, rep.on_time_after)


def _write(media: Media, lang: str, cues, local: Path | None = None) -> Path:
    """Si ya había un .srt de ese idioma junto al video se sustituye (para que Jellyfin no
    muestre el viejo desfasado al lado del bueno); si no, se crea <video>.<idioma>.srt."""
    video = Path(media.path)
    out = local if local is not None and local.suffix.lower() == ".srt" else video.with_name(f"{video.stem}.{lang}.srt")
    backup = out.with_name(out.name + ".orig")  # Jellyfin ignora .orig
    if out.exists() and not backup.exists():
        shutil.copy2(out, backup)
    subtitles.save(cues, out)
    return out


def process(media: Media, lib: Library, providers: list[Provider], cfg: Settings) -> dict[str, str]:
    results = {}
    cache = {}

    def reference():  # el análisis de audio sólo se hace si hay algo que sincronizar
        if "ref" not in cache:
            log.info("%s: analizando audio", media.label)
            cache["ref"] = sync.build_reference(media.path, cfg.reference)
        return cache["ref"]

    for lang in cfg.languages:
        row = lib.subtitle_status(media.id, lang)
        if row and (row["status"] == verify.OK or time.time() - row["updated"] < cfg.retry_hours * 3600):
            continue

        tried: list[tuple[tuple, str, list, verify.Report, Path | None]] = []
        local = sidecar_for(Path(media.path), lang)
        if local and cfg.sync_existing and local.suffix.lower() in (".srt", ".vtt"):
            cues = subtitles.load(local)
            if cues:
                synced, rep = sync.sync(cues, reference())
                tried.append((_quality(rep), f"local:{local.name}", synced, rep, local))

        if not tried or tried[0][3].verdict != verify.OK:
            for prov in providers:
                try:
                    cands = prov.search(media, [lang])
                except Exception as e:  # un proveedor caído no debe parar la biblioteca
                    log.warning("%s: fallo buscando en %s: %s", media.label, prov.name, e)
                    continue
                for cand in cands[: cfg.candidates]:
                    try:
                        cues = subtitles.parse(subtitles.decode(prov.download(cand)))
                    except Exception as e:
                        log.warning("%s: fallo descargando %s: %s", media.label, cand.id, e)
                        continue
                    if not cues:
                        continue
                    synced, rep = sync.sync(cues, reference())
                    log.info("%s [%s] %s -> %s", media.label, lang, cand.release, rep.verdict)
                    tried.append((_quality(rep), f"{prov.name}:{cand.release}", synced, rep, None))
                    if rep.verdict == verify.OK:
                        break
                if tried and max(tried, key=lambda t: t[0])[3].verdict == verify.OK:
                    break

        if not tried:
            lib.set_subtitle(media.id, lang, None, "", "pendiente", "sin candidatos")
            results[lang] = "sin candidatos"
            continue
        _, source, synced, rep, _ = max(tried, key=lambda t: t[0])
        detail = json.dumps({"source": source, **rep.to_dict(),
                             "tried": [(t[1], t[3].verdict) for t in tried]}, ensure_ascii=False)
        if rep.verdict == verify.OK or (rep.verdict == verify.REVIEW and cfg.keep_review):
            path = _write(media, lang, synced, local)
            lib.set_subtitle(media.id, lang, str(path), source, rep.verdict, detail)
        else:
            lib.set_subtitle(media.id, lang, None, source, rep.verdict, detail)
        results[lang] = rep.verdict
    return results
