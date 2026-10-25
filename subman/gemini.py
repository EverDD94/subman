"""Gemini (Google) para los casos difíciles: elegir, entre las opciones de TheMovieDB, cuál es la
película de cada archivo. La clave es gratis en https://aistudio.google.com/apikey.

Sólo se envían nombres de archivo, duraciones y las opciones; nunca el video.
"""
from __future__ import annotations

import base64
import json
import logging
import re
import time

import requests

log = logging.getLogger(__name__)
API = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
LIST = "https://generativelanguage.googleapis.com/v1beta/models"
MODELS = ("gemini-flash-lite-latest", "gemini-flash-latest", "gemini-2.5-flash-lite", "gemini-2.5-flash")  # si no se puede preguntar
_SKIP = re.compile(r"image|tts|audio|live|embed|vision|thinking|exp", re.I)
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


GUESS = """Eres un experto en cine. Estos son archivos de video de una biblioteca personal (nombres casi
siempre en español de Latinoamérica o España, a veces mal escritos o con basura como «720p», «lat»,
«@canal», «hls-2413»). Para cada uno di qué película crees que es, con su duración como pista.
Da hasta 3 posibilidades, la más probable primero, con el título como se conoce en español y el
título original, y el año de estreno. Si no tienes idea, deja la lista vacía.

Responde sólo con JSON: una lista de objetos {"archivo": <número>, "peliculas":
[{"titulo": "...", "original": "...", "año": 2001}]}.

Archivos:
"""


SEE = """Eres un experto en cine. Te paso fotos sacadas de un archivo de video de una biblioteca personal
(la primera, del principio, a veces muestra el título o los créditos). Dime qué película es: fíjate en
los actores, títulos en pantalla y escenas reconocibles. Ayudas: el archivo se llama «{file}» (puede
ser basura) y dura {minutes} min.{options}

Responde sólo con JSON: {{"peliculas": [{{"titulo": "...", "original": "...", "año": 2001}}],
"seguro": true|false}}, hasta 3 posibilidades, la más probable primero. «seguro» es true sólo si
reconoces la película sin dudas. Si no tienes idea, lista vacía.
"""


LISTEN = """Eres un experto en cine. No sé qué película es este archivo de video de mi biblioteca y
las fotos de escenas no bastaron. Te paso fotos de los primeros minutos (suelen salir el título, los
créditos o el logo del estudio) y un trozo de audio con diálogo. Fíjate en los nombres de los
créditos, en lo que dicen y en las voces. Ayudas: el archivo se llama «{file}» (puede ser basura) y
dura {minutes} min.{options}

Responde sólo con JSON: {{"peliculas": [{{"titulo": "...", "original": "...", "año": 2001}}],
"seguro": true|false}}, hasta 3 posibilidades, la más probable primero. «seguro» es true sólo si
reconoces la película sin dudas. Si no tienes idea, lista vacía.
"""


SHOW = """Eres un experto en series de televisión. Tengo capítulos de una serie en mi biblioteca y no sé
cuál de estas series de TheMovieDB es. Te paso fotos de los primeros minutos de un capítulo (suelen salir
el título, el logo de la cadena o los créditos) y, si hay, el audio del principio (la canción de la cabecera
y el diálogo delatan mucho la serie). Fíjate en los actores, el título en pantalla, la canción y la época.
Ayudas: carpeta «{folder}», archivo «{file}» (puede ser basura), dura {minutes} min{inner}.
Opciones:
{options}

Responde sólo con JSON: {{"tmdb": "número de la opción o null", "seguro": true|false}}. «seguro» es true
sólo si lo reconoces sin dudas.
"""


class GeminiError(RuntimeError):
    """kind: «quota» (sin cupo gratis), «down» (no contesta / saturada) o «key» (clave mala)."""

    def __init__(self, message: str, kind: str):
        super().__init__(message)
        self.kind = kind


QUOTA_MSG = ("la IA (Gemini) llegó a su límite gratis; suele volver en un minuto, o mañana si es el límite "
             "del día")
DOWN_MSG = "la IA (Gemini) no contesta ahora mismo (Google está saturado o no hay conexión)"
KEY_MSG = "Google no acepta esa clave de Gemini; cópiala otra vez de aistudio.google.com/apikey"


class Gemini:
    def __init__(self, key: str, timeout: float = 60):
        self.key = key
        self.timeout = timeout
        self.model = None
        self._models: list[str] = []
        self._last = 0.0

    def _post(self, model: str, body: dict) -> requests.Response:
        wait = PAUSE - (time.time() - self._last)
        if wait > 0:
            time.sleep(wait)
        self._last = time.time()
        return requests.post(API.format(model=model), params={"key": self.key}, json=body, timeout=self.timeout)

    def models(self) -> list[str]:
        """Los modelos «flash» que Google ofrece ahora a esta clave (cambian a menudo), los rápidos primero."""
        r = requests.get(LIST, params={"key": self.key, "pageSize": 1000}, timeout=20)
        if r.status_code in (400, 401, 403):
            raise GeminiError(KEY_MSG, "key")
        r.raise_for_status()
        names = [m["name"].removeprefix("models/") for m in r.json().get("models", [])
                 if "generateContent" in m.get("supportedGenerationMethods", [])]
        flash = [n for n in names if "flash" in n and not _SKIP.search(n)]

        def rank(n):
            version = [float(v) for v in re.findall(r"(\d+(?:\.\d+)?)", n)[:1]] or [0.0]
            return ("preview" in n, "lite" not in n, -version[0], n)
        return sorted(flash, key=rank)

    def ask(self, text: str, images: list[bytes] = (), audio: bytes | None = None) -> str:
        parts = [{"text": text}] + [{"inline_data": {"mime_type": "image/jpeg", "data": base64.b64encode(b).decode()}}
                                    for b in images]
        if audio:
            parts.append({"inline_data": {"mime_type": "audio/mpeg", "data": base64.b64encode(audio).decode()}})
        body = {"contents": [{"parts": parts}],
                "generationConfig": {"temperature": 0, "responseMimeType": "application/json"}}
        if not self._models:
            try:
                self._models = self.models()[:5] or list(MODELS)
            except requests.RequestException as e:
                log.warning("Gemini: no se pudo pedir la lista de modelos: %s", e)
                self._models = list(MODELS)
            log.info("Gemini: modelos a probar: %s", ", ".join(self._models))
        # el que ya funcionó primero; si se queda sin cupo, los demás (cada modelo tiene su propio límite)
        models = [self.model] + [m for m in self._models if m != self.model] if self.model else self._models
        errors: list[str] = []
        waited = False
        deadline = time.time() + max(90, self.timeout)  # no quedarse minutos probando modelos
        for model in models:
            if time.time() > deadline:
                errors.append(f"{model}: sin tiempo")
                continue
            try:
                r = self._post(model, body)
                if r.status_code == 429 and not waited and self.timeout > 30:  # límite por minuto: esperar una vez (no al probar)
                    waited = True
                    time.sleep(30)
                    r = self._post(model, body)
            except (requests.Timeout, requests.ConnectionError) as e:  # ese modelo va lento: probar otro
                log.warning("Gemini: %s no contesta: %s", model, e)
                errors.append(f"{model}: no contesta")
                continue
            if r.status_code in (404, 429) or r.status_code >= 500:  # no existe, sin cupo o saturado: el siguiente
                log.warning("Gemini: %s respondió %s", model, r.status_code)
                errors.append(f"{model}: {r.status_code}")
                continue
            if r.status_code in (400, 403) and "API key" in r.text:
                raise GeminiError(KEY_MSG, "key")
            r.raise_for_status()
            self.model = model
            parts = r.json()["candidates"][0]["content"]["parts"]
            return "".join(p.get("text", "") for p in parts)
        detail = " (" + "; ".join(errors) + ")"
        if errors and any(e.endswith(": 429") for e in errors) and all(e.endswith((": 429", ": sin tiempo")) for e in errors):
            raise GeminiError(QUOTA_MSG + detail, "quota")
        raise GeminiError(DOWN_MSG + detail, "down")

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

    def guess(self, items: list[dict]) -> dict[int, list[tuple[str, int | None]]]:
        """items: [{"n", "file", "folder", "minutes"}] -> {n: [(título, año), ...]} para buscarlos en TheMovieDB."""
        lines = [f"{it['n']}. archivo «{it['file']}» (carpeta «{it['folder']}»), duración {it['minutes'] or '?'} min."
                 for it in items]
        raw = self.ask(GUESS + "\n".join(lines))
        try:
            data = json.loads(raw)
        except ValueError:
            log.warning("Gemini: respuesta que no es JSON: %s", raw[:200])
            return {}
        out: dict[int, list] = {}
        for d in data if isinstance(data, list) else []:
            try:
                n = int(d.get("archivo"))
            except (TypeError, ValueError, AttributeError):
                continue
            names = []
            for p in (d.get("peliculas") or [])[:3]:
                if not isinstance(p, dict):
                    continue
                year = p.get("año") or p.get("ano") or p.get("year")
                year = int(year) if str(year).isdigit() else None
                for t in (p.get("titulo"), p.get("original")):
                    if isinstance(t, str) and t.strip() and (t.strip(), year) not in names:
                        names.append((t.strip(), year))
            out[n] = names
        return out

    def see(self, images: list[bytes], file: str, minutes, options: list[dict] = ()) -> tuple[list, bool]:
        """Qué película es, mirando fotos del video -> ([(título, año), ...], seguro)."""
        return self._movies(self.ask(SEE.format(file=file, minutes=minutes or "?", options=_options(options)), images))

    def listen(self, images: list[bytes], audio: bytes | None, file: str, minutes,
               options: list[dict] = ()) -> tuple[list, bool]:
        """Segundo intento: fotos de los primeros minutos (título, créditos) y un trozo de diálogo."""
        return self._movies(self.ask(LISTEN.format(file=file, minutes=minutes or "?", options=_options(options)),
                                     images, audio))

    def show(self, images: list[bytes], folder: str, file: str, minutes, inner: str | None,
             options: list[dict], audio: bytes | None = None) -> tuple[str | None, bool]:
        """Qué serie es entre las opciones, mirando el principio de un capítulo -> (tmdb o None, seguro)."""
        opts = "\n".join(f"- tmdb {o['tmdb']}: «{o['name']}» ({o['year'] or '?'}, {o.get('runtime') or '?'} min por "
                         f"capítulo, {len(o.get('seasons') or {})} temporadas)" for o in options)
        raw = self.ask(SHOW.format(folder=folder, file=file, minutes=minutes or "?", options=opts,
                                   inner=f", título guardado en el video «{inner}»" if inner else ""), images, audio)
        try:
            data = json.loads(raw)
        except ValueError:
            log.warning("Gemini: respuesta que no es JSON: %s", raw[:200])
            return None, False
        if isinstance(data, list):
            data = data[0] if data and isinstance(data[0], dict) else {}
        pick = str(data.get("tmdb") or "")
        if pick not in {o["tmdb"] for o in options}:
            return None, False
        return pick, bool(data.get("seguro"))

    @staticmethod
    def _movies(raw: str) -> tuple[list, bool]:
        try:
            data = json.loads(raw)
        except ValueError:
            log.warning("Gemini: respuesta que no es JSON: %s", raw[:200])
            return [], False
        if isinstance(data, list):
            data = data[0] if data and isinstance(data[0], dict) else {}
        names = []
        for p in (data.get("peliculas") or [])[:3]:
            if not isinstance(p, dict):
                continue
            year = p.get("año") or p.get("ano") or p.get("year")
            year = int(year) if str(year).isdigit() else None
            for t in (p.get("titulo"), p.get("original")):
                if isinstance(t, str) and t.strip() and (t.strip(), year) not in names:
                    names.append((t.strip(), year))
        return names, bool(data.get("seguro"))


def _options(options) -> str:
    if not options:
        return ""
    return "\nPuede que sea una de estas (de TheMovieDB): " + "; ".join(
        f"«{o['name']}» ({o['year'] or '?'}, {o['runtime'] or '?'} min)" for o in options[:6])
