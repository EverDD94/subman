"""Proveedores de subtítulos enchufables.

Para añadir uno nuevo: crear una clase que herede de ``Provider`` y registrarla en ``REGISTRY``.
"""
from .base import Candidate, Provider, QuotaExceeded
from .opensubtitles import OpenSubtitles

REGISTRY: dict[str, type[Provider]] = {
    OpenSubtitles.name: OpenSubtitles,
}


def load(config: dict) -> list[Provider]:
    """Instancia los proveedores activos de la sección [providers.*] de la configuración."""
    out = []
    for name, opts in config.get("providers", {}).items():
        if opts.get("enabled", True) and name in REGISTRY:
            out.append(REGISTRY[name](**{k: v for k, v in opts.items() if k != "enabled"}))
    return out


__all__ = ["Candidate", "Provider", "QuotaExceeded", "REGISTRY", "load"]
