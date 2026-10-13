"""Gemini (Google) para los casos difíciles: elegir, entre las opciones de TheMovieDB, cuál es la
película de cada archivo. La clave es gratis en https://aistudio.google.com/apikey.

Sólo se envían nombres de archivo, duraciones y las opciones; nunca el video.
"""
from __future__ import annotations

import json
import logging
import time

import requests

log = logging.getLogger(__name__)
API = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
MODELS = ("gemini-2.5-flash", "gemini-flash-latest", "gemini-2.0-flash", "gemini-2.5-flash-lite")
PAUSE = 6.0  # la capa gratis admite unas 10 peticiones por minuto

PROMPT = """Eres un experto en cine. Para cada archivo de video de una biblioteca personal (casi siempre
con nombres en español de Latinoamérica o España, a veces mal escritos o con basura como «720p»,
«lat», «@canal», «24rf»), elige cuál de las opciones de TheMovieDB es esa película.
Usa el nombre, el número de la saga (p. ej. «Fast 7» es la 7.ª de su saga), el año si lo hay y
sobre todo la duración del archivo comparada con la de cada opción (debe parecerse; pocos minutos
de diferencia). Si ninguna opción encaja, responde null. No inventes números: elige sólo entre
las opciones dadas.

Responde sólo con JSON: una lista de objetos {"archivo": <número>, "tmdb": "<id elegido o null>",
"seguro": true|false}. «seguro» es true sólo si no tienes dudas.

Archivos:
"""


class Gemini:
    def __init__(self, key: str, timeout: float = 120):
        self.key = key
        self.timeout = timeout
        self.model = None
        self._last = 0.0

    def _post(self, model: str, body: dict) -> requests.Response:
        wait = PAUSE - (time.time() - self._last)
        if wait > 0:
            time.sleep(wait)
        self._last = time.time()
        return requests.post(API.format(model=model), params={"key": self.key}, json=body, timeout=self.timeout)

    def ask(self, text: str) -> str:
        body = {"contents": [{"parts": [{"text": text}]}],
                "generationConfig": {"temperature": 0, "responseMimeType": "application/json"}}
        models = [self.model] if self.model else list(MODELS)
        last: Exception | None = None
        for model in models:
            try:
                for attempt in range(3):
                    r = self._post(model, body)
                    if r.status_code == 429 and attempt < 2:  # límite por minuto: esperar y repetir
                        time.sleep(30)
                        continue
                    break
            except (requests.Timeout, requests.ConnectionError) as e:  # ese modelo va lento: probar otro
                log.warning("Gemini: %s no contesta: %s", model, e)
                last = RuntimeError(f"Gemini no contesta ({model}); prueba otra vez en un rato")
                continue
            if r.status_code == 404 or r.status_code >= 500:  # no existe o está saturado: el siguiente
                log.warning("Gemini: %s respondió %s", model, r.status_code)
                last = RuntimeError(f"Gemini respondió {r.status_code} ({model})")
                continue
            if r.status_code in (400, 403) and "API key" in r.text:
                raise RuntimeError("Google no acepta esa clave; cópiala otra vez de aistudio.google.com/apikey")
            r.raise_for_status()
            self.model = model
            parts = r.json()["candidates"][0]["content"]["parts"]
            return "".join(p.get("text", "") for p in parts)
        raise last or RuntimeError("Gemini no contesta")

    def test(self) -> dict:
        self.timeout, keep = 25, self.timeout  # para probar no hace falta esperar un minuto por modelo
        try:
            self.ask('Responde sólo con JSON: {"ok": true}')
        finally:
            self.timeout = keep
        return {"model": self.model}

    def choose(self, items: list[dict]) -> dict[int, tuple[str | None, bool]]:
        """items: [{"n", "file", "folder", "minutes", "options": [{"tmdb","name","original","year","runtime","saga"}]}]
        -> {n: (tmdb elegido o None, seguro)}"""
        lines = []
        for it in items:
            lines.append(f"\n{it['n']}. archivo «{it['file']}» (carpeta «{it['folder']}»), duración "
                         f"{it['minutes'] or '?'} min. Opciones:")
            for o in it["options"]:
                saga = f", saga «{o['saga']}» n.º {o['part']}" if o.get("saga") else ""
                lines.append(f"   - tmdb {o['tmdb']}: «{o['name']}» (original «{o['original']}», {o['year'] or '?'}, "
                             f"{o['runtime'] or '?'} min{saga})")
        raw = self.ask(PROMPT + "\n".join(lines))
        try:
            data = json.loads(raw)
        except ValueError:
            log.warning("Gemini: respuesta que no es JSON: %s", raw[:200])
            return {}
        out = {}
        valid = {it["n"]: {o["tmdb"] for o in it["options"]} for it in items}
        for d in data if isinstance(data, list) else []:
            try:
                n = int(d.get("archivo"))
            except (TypeError, ValueError):
                continue
            pick = str(d["tmdb"]) if d.get("tmdb") not in (None, "null", "") else None
            if n in valid and (pick is None or pick in valid[n]):
                out[n] = (pick, bool(d.get("seguro")))
        return out
