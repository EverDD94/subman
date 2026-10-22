"""Configuración (subman.toml): valores por defecto, lectura y escritura."""
from __future__ import annotations

import copy
import tomllib
from pathlib import Path

from . import manager

DEFAULTS: dict = {
    "enabled": False,       # el trabajo automático sólo arranca cuando se activa (desde la web)
    "library": [],
    "languages": ["es"],
    "candidates": 5,
    "keep_review": True,
    "sync_existing": True,
    "reference": "auto",
    "retry_hours": 24,
    "archive_languages": ["en"],
    "dry_run": False,
    "interval_minutes": 60,
    "web_port": 8787,
    "duplicates_dir": "/media/Duplicados",  # adonde van las películas duplicadas que se apartan
    "discard_dir": "/media/Papelera de videos",  # las que no se sabe qué son y mandas a eliminar (hasta «Vaciar»)
    "providers": {"opensubtitles": {"api_key": "", "username": "", "password": ""}},
    "jellyfin": {"url": "", "api_key": ""},
    "tmdb": {"api_key": "", "language": "es-MX"},  # para identificar películas (duración y sagas)
    "gemini": {"api_key": ""},
    # usuario y contraseña de la página (vacío = sin contraseña). Olvidada: borra password_hash.
    "access": {"user": "", "password_hash": "", "secret": ""},
    # Nombres y Duplicados solos: al llegar videos nuevos y/o una vez cada noche dentro del horario
    "timezone": "",  # la del navegador; se pone sola al abrir Ajustes (para los horarios de noche)
    "update": {"auto": True, "hour": 5},  # actualizarse sola cada día a esa hora (con Watchtower)
    "auto": {"names": False, "dupes": False, "on_new": True, "nightly": True, "night_start": 1, "night_end": 8},
    # convertir videos al formato que Jellyfin reproduce directo (pestaña «Formatos»)
    "convert": {"enabled": False, "groups": ["caja", "sonido", "video"], "allow_hevc": True,
                "night_only": False, "night_start": 1, "night_end": 8,
                "originals_dir": "/media/Originales", "min_free_gb": 50},
    # descargar de Phono (por Telegram) lo que pones en «Por conseguir» (pestaña Descubrir)
    "telegram": {"api_id": "", "api_hash": "", "bot_token": "", "bot_name": "", "chat_id": ""},
    "downloads": {"enabled": False, "auto": True, "movies_dir": "", "series_dir": "",
                  "tmp_dir": "/media/Descargando", "gap_minutes": 2, "daily": 20, "audio": ["es", "en"],
                  "min_free_gb": 20, "remind": True},
}

HEADER = """# Configuración de Videotecario (antes subman). Se puede editar a mano o desde la web (puerto 8787).
# Rutas de la biblioteca tal como se ven DENTRO del contenedor (/media/...).
"""


def _merge(base: dict, over: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in over.items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def load(path: str | Path) -> dict:
    p = Path(path)
    if not p.exists():
        p.parent.mkdir(parents=True, exist_ok=True)
        save(p, DEFAULTS)
    with open(p, "rb") as f:
        cfg = _merge(DEFAULTS, tomllib.load(f))
    cfg.setdefault("database", str(p.parent / "subman.db"))
    return cfg


def _value(v) -> str:
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    if isinstance(v, list):
        return "[" + ", ".join(_value(x) for x in v) + "]"
    s = str(v).replace("\\", "\\\\").replace('"', '\\"').replace("\n", "\\n")
    return f'"{s}"'


def dumps(cfg: dict) -> str:
    lines = [HEADER]
    tables = []
    for k, v in cfg.items():
        if isinstance(v, dict):
            tables.append((k, v))
        else:
            lines.append(f"{k} = {_value(v)}")
    for name, table in tables:
        plain = {k: v for k, v in table.items() if not isinstance(v, dict)}
        if plain:
            lines.append(f"\n[{name}]")
            lines += [f"{k} = {_value(v)}" for k, v in plain.items()]
        for sub, values in table.items():
            if isinstance(values, dict):
                lines.append(f"\n[{name}.{sub}]")
                lines += [f"{k} = {_value(v)}" for k, v in values.items()]
    return "\n".join(lines) + "\n"


def save(path: str | Path, cfg: dict) -> None:
    p = Path(path)
    tmp = p.with_suffix(".tmp")
    tmp.write_text(dumps(cfg), encoding="utf-8")
    tmp.replace(p)


def excluded(cfg: dict) -> list[str]:
    """Carpetas que no son biblioteca aunque estén dentro: duplicados apartados, originales convertidos y descargas a medias."""
    return [d for d in (cfg.get("duplicates_dir"), cfg.get("discard_dir"), cfg.get("convert", {}).get("originals_dir"),
                        cfg.get("downloads", {}).get("tmp_dir")) if d]


def problems(cfg: dict) -> list[str]:
    """Lo que falta para poder trabajar (vacío = listo)."""
    out = []
    if not cfg.get("library"):
        out.append("elige al menos una carpeta de la biblioteca")
    else:
        missing = [d for d in cfg["library"] if not Path(d).is_dir()]
        if missing:
            out.append("no existen: " + ", ".join(missing))
    if not cfg.get("providers", {}).get("opensubtitles", {}).get("api_key"):
        out.append("falta la API key de OpenSubtitles (sin ella sólo se corrigen los subtítulos que ya tienes)")
    return out


def blocking(cfg: dict) -> list[str]:
    """Problemas que impiden trabajar del todo (la API key es opcional)."""
    return [p for p in problems(cfg) if not p.startswith("falta la API key")]


def settings(cfg: dict, config_path: str | Path, dry_run: bool = False) -> manager.Settings:
    base = Path(config_path).parent
    return manager.Settings(
        languages=cfg["languages"], candidates=cfg["candidates"], keep_review=cfg["keep_review"],
        sync_existing=cfg["sync_existing"], reference=cfg["reference"], retry_hours=cfg["retry_hours"],
        archive_languages=cfg["archive_languages"],
        trash_dir=cfg.get("trash_dir", str(base / "papelera")),
        archive_dir=cfg.get("archive_dir", str(base / "archivo")),
        roots=list(cfg["library"]), dry_run=dry_run or cfg["dry_run"])


def active_providers(cfg: dict) -> dict:
    """Sólo los proveedores con credenciales puestas."""
    return {"providers": {k: v for k, v in cfg.get("providers", {}).items() if v.get("api_key")}}
