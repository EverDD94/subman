# subman

Gestor de subtítulos para una biblioteca de Jellyfin: indexa series y películas, descarga
subtítulos de proveedores enchufables (OpenSubtitles para empezar), **los sincroniza contra
la voz del episodio completo** y **comprueba que la sincronización funcionó** antes de
dejarlos junto al video.

## Por qué fallan las herramientas que ya probaste

| Herramienta | Qué hace | Dónde se rompe |
|---|---|---|
| Plugin OpenSubtitles de Jellyfin, Bazarr | Descargan el subtítulo más votado | Si es de otra *release* (otra intro, 25 fps, escena de más) llega desfasado y nadie lo comprueba |
| ffsubsync | Busca **un único desfase** (+ fps) | Si hay una intro de distinta duración, arregla una mitad y rompe la otra |
| alass | Permite cortes, pero decide con heurísticas y su VAD trata la música como voz | Con intros/música de fondo se equivoca en tramos, y no te dice que se equivocó |
| Whisper / transcribir y traducir | Genera subtítulos nuevos | Tiempos imprecisos, frases cortadas y errores de traducción; muy lento en un NAS |

El problema de fondo es que ninguna **verifica** el resultado: te devuelven un archivo
y das por hecho que está bien hasta que lo ves.

## Cómo lo resuelve subman

1. **Huella de voz del episodio entero.** Se extrae el audio con ffmpeg y se pasa por
   [Silero VAD](https://github.com/snakers4/silero-vad), un detector de voz neuronal que
   distingue voz de música y efectos (las intros y la música de fondo no confunden).
   Si el video ya trae un subtítulo de texto incrustado, se puede usar como referencia exacta.
2. **Alineación global por tramos.** Un solo cálculo de programación dinámica sobre todas las
   líneas decide: el factor de fps (23.976 / 24 / 25), y un desfase por tramo. Cambiar de
   desfase entre dos líneas tiene un coste, así que sólo se acepta si el resto del
   episodio encaja claramente mejor (una intro distinta, un "previously on", una escena de
   más). Las líneas nunca se reordenan ni se pisan. Después cada tramo se afina a 10 ms.
3. **Verificación con nivel de azar.** Se mide qué porcentaje de líneas cae sobre voz real y
   se compara con lo que el mismo alineador consigue con subtítulos *señuelo* (el mismo
   subtítulo invertido o barajado). Así un subtítulo de otro episodio, que siempre "encaja
   un poco", no pasa por bueno. Veredicto: `ok`, `revisar` o `falla`, con el detalle por tramo.
4. **Varios candidatos.** Para cada episodio se prueban los mejores subtítulos del proveedor
   en orden; se para en el primero que sale `ok`. Si ninguno, se guarda el mejor `revisar`
   (configurable) o nada, y queda anotado para reintentar.

## Resultados en las pruebas

Episodios sintéticos (voz real con espeak-ng, intro musical de 45 s, música de fondo y
escenas de acción sin diálogo), de 10 y 45 minutos:

| Caso | Antes | Después |
|---|---|---|
| Desfase constante de 2.7 s | 2.7 s de error | 0.03 s, 100 % de líneas a ≤0.2 s, `ok` |
| Release a 25 fps + intro 30 s más corta | 41–83 s de error | 0.03 s, 100 %, `ok` (detecta x1.04271 y 2 tramos) |
| Lo mismo con tiempos "humanos" imprecisos | 41–83 s | 0.03–0.05 s, 96–100 %, `ok` |
| "Previously on" de 20 s + escena de 15 s que el subtítulo no tiene | 5–20 s | 0.03 s, 99.7–100 %, `ok` |
| Subtítulo de **otro episodio** | — | `falla` (encaja como un señuelo) |

Análisis de un episodio de 45 min: ~7 s de detección de voz + ~2 s de alineación en CPU.
En un Synology con Celeron/ARM espera unas 5–10 veces más.

Límite conocido: en el borde de una escena que sobra, una o dos líneas pueden caer en el
lado equivocado si ambas posiciones tienen voz (en el caso de 45 min, 2 de 603 líneas).

Reproducir: `python tests/make_episode.py /tmp/ep && python tests/evaluate.py /tmp/ep`
(necesita `espeak-ng`).

## Prueba con episodios reales (2 Broke Girls S01E01 y S01E02, 12/10/2026)

Subtítulos `.spa` descargados, audio en inglés. No tenían un desfase constante: entre +1 y
+3 s, con 5–6 saltos a lo largo del episodio (cortes de anuncios de otra versión).

| | Ventanas a tiempo antes | Después | ¿Es de este video? | Veredicto |
|---|---|---|---|---|
| E01 .spa | 0 % | 92 % | 59 % de ventanas lo confirman | ok |
| E02 .spa | 0 % | 82 % | 57 % | ok |
| E01 con el .spa del E02 (equivocado) | — | — | 7 % | falla |
| E02 con el .spa del E01 (equivocado) | — | — | 4 % | falla |

Lo que se aprendió y se cambió:
- En una sitcom (diálogo continuo y risas grabadas) "hay voz debajo de la línea" es cierto con
  casi cualquier desfase. Ahora el alineador se guía por **dónde arranca y se corta la voz**,
  que marca el tiempo con un pico nítido de ~0,5 s.
- La verificación antigua (comparar con subtítulos señuelo) no distinguía un episodio
  equivocado. Ahora se trocea el episodio en ventanas de 90 s, cada una busca su desfase por
  su cuenta y se comprueba que confirma el de la alineación global.
- Contraste independiente: el `.spa` corregido y el `.en` corregido quedan en el mismo sitio
  (diferencia ~0,1 s), aunque se alinearon por separado contra el audio.

## Uso

```bash
pip install -e .            # Python ≥3.11 y ffmpeg en el PATH

# Un archivo suelto (semiautomático)
subman sync "Serie - S01E02.mkv" "Serie - S01E02.es.srt" -o "Serie - S01E02.es.srt"
subman check "Serie - S01E02.mkv" "Serie - S01E02.es.srt"   # sólo verificar

# Biblioteca completa (automático)
subman scan -c subman.toml
subman run  -c subman.toml               # una pasada
subman serve -c subman.toml              # interfaz web en :8787 + trabajo automático
subman list -c subman.toml               # estado por episodio
```

Los subtítulos se escriben como `<video>.<idioma>.srt`, que Jellyfin detecta solo. Los
originales se apartan a `papelera/` y los de inglés a `archivo/en/` (junto a `subman.toml`).

## Interfaz web

`subman serve` (lo que arranca el contenedor) abre una página en el puerto 8787:

- **Inicio:** encender o pausar el trabajo automático, procesar ahora, progreso y recuento.
- **Biblioteca:** cada video con su estado; al abrirlo se ve de dónde salió el subtítulo,
  los tramos corregidos y la verificación, con botones para aceptarlo, rechazarlo (va a la
  papelera y se busca otro) o volver a procesarlo.
- **Ajustes:** carpetas de la biblioteca (navegando por `/media`), credenciales de
  OpenSubtitles con "Probar conexión", idiomas y demás opciones. Se guardan en `subman.toml`.
- **Registro** y **Papelera** (con "Recuperar").

No tiene contraseña: está pensada para la red de casa. Hasta que se activa en la web,
no toca ningún archivo.

## Synology (Container Manager)

`docker-compose.yml` monta `/volume1/docker/subman` como `/config` y la carpeta de medios de
Jellyfin como `/media`, y publica la interfaz web en el puerto 8787.

## Estructura

```
subman/
  audio.py       ffmpeg: audio a 16 kHz y subtítulos incrustados
  vad.py         Silero VAD (ONNX, incluido) -> probabilidad de voz cada 10 ms
  aligner.py     alineación global: fps + desfase por tramo (programación dinámica)
  verify.py      métricas, señuelos y veredicto
  sync.py        video + subtítulo -> subtítulo sincronizado + informe
  library.py     escaneo de la biblioteca, hash de OpenSubtitles, índice SQLite
  manager.py     buscar candidatos -> sincronizar -> verificar -> elegir y escribir
  providers/     proveedores enchufables (opensubtitles.py)
  config.py      subman.toml: valores por defecto, lectura y escritura
  web.py         interfaz web + trabajador en segundo plano
  static/        la página de la interfaz
  cli.py
tests/           generador de episodios sintéticos y evaluación
```

## Próximos pasos

- Probarlo con episodios reales que hayan fallado antes (lo más importante).
- Escuchar un trozo del video con el subtítulo desde la interfaz web antes de aceptar.
- Verificación extra por texto con Whisper cuando el subtítulo está en el idioma del audio.
- Más proveedores (Subdivx, Addic7ed, Podnapisi) y aviso a Jellyfin para refrescar.

El modelo Silero VAD (`subman/models/`) es MIT, © Silero Team.
