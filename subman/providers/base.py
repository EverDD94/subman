from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field

from ..library import Media


@dataclass
class Candidate:
    provider: str
    id: str                  # identificador para descargar
    lang: str
    release: str = ""
    hash_match: bool = False  # el proveedor dice que es para este archivo exacto
    downloads: int = 0
    trusted: bool = False
    machine_translated: bool = False
    hearing_impaired: bool = False
    fps: float | None = None
    extra: dict = field(default_factory=dict)

    def rank(self) -> tuple:
        """Orden de preferencia antes de sincronizar (después manda la verificación)."""
        return (self.hash_match, not self.machine_translated, self.trusted,
                not self.hearing_impaired, self.downloads)


class QuotaExceeded(Exception):
    """Se acabaron las descargas de hoy: no seguir pidiendo hasta mañana."""


class Provider(ABC):
    name: str

    @abstractmethod
    def search(self, media: Media, languages: list[str]) -> list[Candidate]: ...

    @abstractmethod
    def download(self, candidate: Candidate) -> bytes: ...
