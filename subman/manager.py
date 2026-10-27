"""Flujo completo para un video.

1. Mira todos los subtítulos que ya hay junto al video. Los de los idiomas a archivar (inglés)
   se mueven a ``/config/archivo``. Los del idioma buscado (según su texto, no sólo su
   nombre) son candidatos.
2. Sincroniza y verifica cada candidato local. Si ninguno sale ``ok``, prueba los de los
   proveedores (OpenSubtitles).
3. El mejor se guarda como ``<video>.<idioma>.srt``. Todos los originales que había (también
   el del que salió el bueno) se mueven a ``/config/papelera``, con la misma estructura de
   carpetas, para poder recuperarlos.
"""
from __future__ import annotations

import dataclasses
import json
import logging
import shutil
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import audio, subtitles, sync, verify
from .library import Library, Media, sidecars
from .providers import Provider, QuotaExceeded

log = logging.getLogger(__name__)
_RANK = {verify.OK: 2, verify.REVIEW: 1, verify.FAIL: 0}
_READABLE = {".srt", ".vtt"}


@dataclass
class Settings:
    languages: list[str]
    candidates: int = 5          # cuántos subtítulos de proveedores probar como máximo por video e idioma
    keep_review: bool = True     # guardar también los dudosos (marcados como 'revisar')
    sync_existing: bool = True   # usar los subtítulos que ya había junto al video como candidatos
    reference: str = "auto"
    retry_hours: float = 24      # cada cuánto reintentar lo que salió mal o no tenía candidatos
    archive_languages: list[str] = field(default_factory=lambda: ["en"])  # se apartan a /config/archivo
    trash_dir: str = "/config/papelera"
    archive_dir: str = "/config/archivo"
    roots: list[str] = field(default_factory=list)  # carpetas de la biblioteca (para rutas relativas)
    dry_run: bool = False        # sólo contar lo que haría, sin tocar nada
    translator: object = None    # último recurso: función(textos, idioma) -> textos traducidos (Gemini)


# Reintentos escalonados: tras cada intento fallido se espera retry_hours por este factor.
BACKOFF = (1, 3, 7, 30)
GIVE_UP = len(BACKOFF)  # a partir de aquí se muestra «sin solución por ahora»
TR_MARK = "traducción probada"  # en el detalle: ya se intentó traducir uno en inglés


def retry_wait(cfg: Settings, attempts: int) -> float:
    """Segundos que hay que esperar antes de volver a intentarlo tras ``attempts`` fallos."""
    return cfg.retry_hours * 3600 * BACKOFF[min(max(attempts, 1), len(BACKOFF)) - 1]


def _quality(rep: verify.Report) -> tuple:
    return (_RANK[rep.verdict], rep.identity, rep.on_time_after)


def _relative(path: Path, roots: list[str]) -> Path:
    for r in roots:
        try:
            return Path(Path(r).name) / path.relative_to(r)
        except ValueError:
            continue
    return Path(path.name)


def _move(path: Path, dest_root: str, cfg: Settings, why: str) -> None:
    dest = Path(dest_root) / _relative(path, cfg.roots)
    if cfg.dry_run:
        log.info("[ensayo] movería %s -> %s (%s)", path, dest, why)
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    base, n = dest, 1
    while dest.exists():  # no pisar algo que ya estaba en la papelera
        dest = base.with_name(f"{base.name[:-len(base.suffix)]}.{n}{base.suffix}")
        n += 1
    shutil.move(str(path), str(dest))
    log.info("movido %s -> %s (%s)", path.name, dest, why)


def _classify(media: Media, cfg: Settings) -> tuple[dict[str, list[tuple[Path, list]]], list[tuple[str, Path]]]:
    """Reparte los subtítulos que hay junto al video: candidatos por idioma, y a archivar."""
    candidates: dict[str, list[tuple[Path, list]]] = {lang: [] for lang in cfg.languages}
    archive = []
    for tag, path in sidecars(Path(media.path)):
        cues = []
        detected = None
        if path.suffix.lower() in _READABLE:
            try:
                cues = subtitles.load(path)
            except Exception:
                cues = []
            detected = subtitles.detect_language(cues) if cues else None
        lang = detected or tag
        if lang in cfg.languages and cues:
            candidates[lang].append((path, cues))
        elif lang in cfg.archive_languages:
            archive.append((lang, path))
    return candidates, archive


def _target(media: Media, lang: str) -> Path:
    video = Path(media.path)
    return video.with_name(f"{video.stem}.{lang}.srt")


# Carpetas cuyo contenido ya está en español (Películas en Español, Series Españolas, Novelas...).
_NATIVE_WORDS = {"es": ("español", "espanol", "española", "espanola", "españolas", "espanolas", "españoles",
                        "castellano", "latino", "novelas")}
_NATIVE_TAGS = {"es": ("es", "spa", "esl", "esp")}
NATIVE_DETAIL = "el video ya está en este idioma"


def _native(media: Media, lang: str) -> str | None:
    """Motivo si el video ya está en ``lang`` (no hacen falta subtítulos), o None."""
    words = _NATIVE_WORDS.get(lang, ())
    for part in Path(media.path).parent.parts:
        if any(w in part.lower() for w in words):
            return f"{NATIVE_DETAIL} (carpeta «{part}»)"
    try:
        info = audio.probe(media.path)
        tag = audio.audio_streams(info)[audio.pick_audio_stream(info)].get("tags", {}).get("language", "").lower()
    except Exception:
        return None
    if tag and (tag in _NATIVE_TAGS.get(lang, (lang,)) or tag.startswith(lang + "-")):
        return f"{NATIVE_DETAIL} (audio «{tag}»)"
    return None


def _translated(media: Media, lang: str, providers: list[Provider], cfg: Settings, reference, rejected):
    """Busca un subtítulo en inglés que cuadre con el audio y lo traduce. Sólo si sale ``ok``."""
    for prov in providers:
        if getattr(prov, "exhausted", False):
            continue
        try:
            cands = prov.search(media, ["en"])
        except Exception as e:
            log.warning("%s: fallo buscando inglés en %s: %s", media.label, prov.name, e)
            continue
        for cand in [c for c in cands if f"traducido:{prov.name}:{c.release}" not in rejected][:2]:
            try:
                cues = subtitles.parse(subtitles.decode(prov.download(cand)))
            except QuotaExceeded:
                prov.exhausted = True
                break
            except Exception as e:
                log.warning("%s: fallo descargando %s: %s", media.label, cand.id, e)
                continue
            if not cues:
                continue
            synced, rep = sync.sync(cues, reference())
            if rep.verdict != verify.OK:
                continue
            try:
                texts = cfg.translator([c.text for c in synced], lang)
            except Exception as e:
                log.warning("%s: no se pudo traducir: %s", media.label, e)
                return None
            synced = [dataclasses.replace(c, text=t) for c, t in zip(synced, texts)]
            log.info("%s [%s] traducido de %s:%s", media.label, lang, prov.name, cand.release)
            return ((_RANK[verify.REVIEW], rep.identity, rep.on_time_after),
                    f"traducido:{prov.name}:{cand.release}", synced, rep)
    return None


def process(media: Media, lib: Library, providers: list[Provider], cfg: Settings) -> dict[str, str]:
    results = {}
    cache = {}

    def reference():  # el análisis de audio sólo se hace si hay algo que sincronizar
        if "ref" not in cache:
            log.info("%s: analizando audio", media.label)
            cache["ref"] = sync.build_reference(media.path, cfg.reference)
        return cache["ref"]

    candidates, archive = _classify(media, cfg)
    for lang, path in archive:
        _move(path, str(Path(cfg.archive_dir) / lang), cfg, f"idioma {lang}")

    for lang in cfg.languages:
        target = _target(media, lang)
        row = lib.subtitle_status(media.id, lang)
        if row and row["status"] == verify.OK and (row["detail"] or "").startswith(NATIVE_DETAIL):
            continue
        native = _native(media, lang)
        if native:
            if not cfg.dry_run:
                lib.set_subtitle(media.id, lang, row["path"] if row else None, "", verify.OK, native)
            log.info("%s [%s]: %s, no se buscan subtítulos", media.label, lang, native)
            results[lang] = "no hace falta"
            continue
        locals_ = candidates[lang]
        if row and row["status"] == verify.OK and row["path"] and Path(row["path"]).exists():
            # Ya está bien: sólo apartar lo que haya aparecido después (otro programa, Jellyfin...).
            for path, _ in locals_:
                if path != Path(row["path"]):
                    _move(path, cfg.trash_dir, cfg, "sobra: ya hay uno verificado")
            continue
        # Lo que ya se intentó (dudoso, falla, sin candidatos) se reintenta cada retry_hours, salvo
        # que haya aparecido un subtítulo nuevo junto al video (el propio .es.srt guardado no cuenta).
        new_locals = [l for l in locals_ if not (row and row["path"] and l[0] == Path(row["path"]))]
        # Si hay traductor y a este video aún no se le probó la traducción, no esperar al reintento.
        untranslated = (bool(cfg.translator) and row is not None and row["status"] != "error"
                        and TR_MARK not in (row["detail"] or ""))
        if (row and row["status"] != verify.OK and not new_locals and not untranslated
                and time.time() - row["updated"] < retry_wait(cfg, row["attempts"] or 1)):
            continue

        tried: list[tuple[tuple, str, list, verify.Report]] = []
        verdicts: dict[Path, str] = {}
        if cfg.sync_existing:
            seen: dict[tuple, str] = {}
            for path, cues in locals_:
                key = tuple((c.start, c.end, c.text) for c in cues)
                if key in seen:  # copia idéntica de otro (.spa y .spa.0): no hace falta repetir
                    verdicts[path] = seen[key]
                    continue
                synced, rep = sync.sync(cues, reference())
                log.info("%s [%s] local %s -> %s", media.label, lang, path.name, rep.verdict)
                tried.append((_quality(rep), f"local:{path.name}", synced, rep))
                verdicts[path] = seen[key] = rep.verdict

        def best():
            return max(tried, key=lambda t: t[0]) if tried else None

        rejected = lib.rejected(media.id, lang)
        quota_hit = False
        translate_tried = False
        if not best() or best()[3].verdict != verify.OK:
            for prov in providers:
                if getattr(prov, "exhausted", False):
                    quota_hit = True
                    continue
                try:
                    cands = prov.search(media, [lang])
                except Exception as e:  # un proveedor caído no debe parar la biblioteca
                    log.warning("%s: fallo buscando en %s: %s", media.label, prov.name, e)
                    continue
                cands = [c for c in cands if f"{prov.name}:{c.release}" not in rejected]
                for cand in cands[: cfg.candidates]:
                    if cfg.dry_run:
                        log.info("[ensayo] descargaría %s:%s", prov.name, cand.release)
                        break
                    try:
                        cues = subtitles.parse(subtitles.decode(prov.download(cand)))
                    except QuotaExceeded as e:
                        log.warning("%s: se acabaron las descargas de hoy (%s); se sigue mañana", prov.name, e)
                        prov.exhausted = True
                        quota_hit = True
                        break
                    except Exception as e:
                        log.warning("%s: fallo descargando %s: %s", media.label, cand.id, e)
                        continue
                    if not cues:
                        continue
                    synced, rep = sync.sync(cues, reference())
                    log.info("%s [%s] %s:%s -> %s", media.label, lang, prov.name, cand.release, rep.verdict)
                    tried.append((_quality(rep), f"{prov.name}:{cand.release}", synced, rep))
                    if rep.verdict == verify.OK:
                        break
                if best() and best()[3].verdict == verify.OK:
                    break

        if (not best() or best()[3].verdict != verify.OK) and cfg.translator and not cfg.dry_run:
            translate_tried = True  # aunque OpenSubtitles no tenga descargas, Addic7ed/Subdivx sí
            got = _translated(media, lang, providers, cfg, reference, rejected)
            if got:
                tried.append(got)

        if not tried:
            if quota_hit:  # no marcarlo: que lo intente en cuanto haya descargas
                results[lang] = "esperando descargas"
                continue
            if not cfg.dry_run:
                lib.set_subtitle(media.id, lang, None, "", "pendiente",
                                 "sin candidatos" + (f" ({TR_MARK})" if translate_tried else ""))
            results[lang] = "sin candidatos"
            continue

        _, source, synced, rep = best()
        keep = rep.verdict == verify.OK or (rep.verdict == verify.REVIEW and cfg.keep_review)
        detail = json.dumps({"source": source, **rep.to_dict(),
                             "tried": [(t[1], t[3].verdict) for t in tried],
                             **({"translation": TR_MARK} if translate_tried else {})}, ensure_ascii=False)
        # Todo lo que había se aparta: los malos porque fallan, y el original del bueno
        # porque se sustituye por su versión corregida.
        for path, _ in locals_:
            if keep and source == f"local:{path.name}":
                why = "original del elegido (se guarda corregido como .%s.srt)" % lang
            elif verdicts.get(path) == verify.FAIL:
                why = "falla la verificación"
            else:
                why = "sobra: hay uno mejor o es una copia"
            _move(path, cfg.trash_dir, cfg, why)
        if cfg.dry_run:
            log.info("[ensayo] %s [%s]: se quedaría %s (%s)", media.label, lang, source, rep.verdict)
        elif keep:
            subtitles.save(synced, target)
            lib.set_subtitle(media.id, lang, str(target), source, rep.verdict, detail)
        else:
            lib.set_subtitle(media.id, lang, None, source, rep.verdict, detail)
        results[lang] = rep.verdict
    return results
