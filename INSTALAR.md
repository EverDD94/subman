# Instalar Videotecario en el Synology (DS923+)

Videotecario es el nuevo nombre de subman. Por dentro todo sigue igual: la imagen sigue siendo
`ghcr.io/everdd94/subman` y la carpeta `docker/subman`, así que no hay que reinstalar nada.

## 1. Carpeta de configuración

En File Station, crea `docker/subman` dentro de `volume1`. Si ya tienes una carpeta `docker`
de otros contenedores, crea solo `subman` dentro de ella.

## 2. Proyecto en Container Manager

1. Abre Container Manager, ve a **Proyecto** y pulsa **Crear**.
2. Nombre: `subman`. Ruta: `/volume1/docker/subman`. Origen: **Crear docker-compose.yml**.
3. Pega el contenido de `docker-compose.yml` y cambia `/volume1/MEDIOS` por la carpeta
   compartida donde tienes `Series` (la misma que ve Jellyfin; en tu caso `/volume1/Plex Srver`).
4. Pulsa **Siguiente** y luego **Hecho**. Se descarga la imagen y arranca.

## 3. Abrir la interfaz

En el navegador: **http://192.168.1.121:8787** (la IP de tu Synology).

1. **Ajustes, Carpetas de la biblioteca:** marca las carpetas que quieres (por ejemplo
   `Series`, `Peliculas`, `Peliculas Animadas`). Con "abrir ›" entras en una carpeta para
   elegir solo una parte (por ejemplo solo `2 Broke Girls` para empezar).
2. **OpenSubtitles:** pon tu API key, usuario y contraseña y pulsa **Probar conexión**.
   La API key se crea en opensubtitles.com, en tu perfil, **API consumers**.
3. Opcional, **Jellyfin**: pon `http://192.168.1.121:8096` y una API key (en Jellyfin: Panel de
   control, **Claves de API**, **+**). Así Jellyfin ve los subtítulos nuevos al terminar cada pasada.
4. Opcional: activa **Modo ensayo** si primero quieres ver en el **Registro** qué haría sin
   que mueva nada.
5. Activa **Activar el trabajo automático** y pulsa **Guardar ajustes**. En **Inicio** se ve
   el progreso.

Los ajustes se guardan en `docker/subman/subman.toml`; no hace falta tocarlo a mano.

## Verlo desde el móvil, también fuera de casa (Tailscale)

Tailscale crea una red privada entre tus aparatos: el móvil ve el NAS como si estuviera en casa,
sin abrir puertos en el router y sin que nadie más de internet pueda llegar a la página.

1. **Pon usuario y contraseña** en Videotecario: *Ajustes*, tarjeta *Entrar con usuario y contraseña*,
   y pulsa *Guardar ajustes*. Desde entonces la página los pide (cada aparato se recuerda 90 días).
2. **En el NAS:** *Centro de paquetes*, busca **Tailscale**, *Instalar*. Al abrirlo te pide entrar con
   una cuenta (Google, Microsoft o Apple, gratis). Cuando termine, el NAS sale en la lista con un nombre,
   por ejemplo `ds923`.
3. **En el móvil:** instala la app **Tailscale** (App Store o Google Play) y entra con **la misma cuenta**.
   Deja el interruptor encendido.
4. Abre en el móvil `http://ds923:8787` (cambia `ds923` por el nombre que muestra la app de Tailscale;
   también vale la dirección `100.x.x.x` que sale al lado).
5. Para tenerlo como una app: en iPhone, Safari, botón *Compartir*, *Añadir a pantalla de inicio*;
   en Android, Chrome, menú, *Añadir a pantalla de inicio*.

Es el mismo Videotecario del NAS, no una copia: lo que hagas en el móvil se ve en el ordenador y al revés.

**Si olvidas la contraseña:** en File Station abre `docker/subman/subman.toml`, borra la línea que empieza
por `password_hash` (dentro de `[access]`), guarda y vuelve a abrir la página: entra sin contraseña y
puedes poner una nueva en *Ajustes*.

## Actualizar a una versión nueva

Con el bloque `subman-actualizar` (Watchtower) en el proyecto, Videotecario se actualiza solo:
cada día a la hora elegida en **Ajustes › Actualizaciones** o al pulsar **Actualizar ahora**.
Si hay versión nueva se reinicia en un minuto con todo igual; si no, no hace nada.

**Añadir Watchtower (una sola vez)**, si tu proyecto aún no lo tiene:

1. Container Manager, **Proyecto**, `subman`: **Detener**.
2. **Acción › Editar** (o la pestaña **YAML**) y pega al final, con la misma sangría que `subman:`,
   el bloque `subman-actualizar` de `docker-compose.yml`. Guarda.
3. **Acción › Limpiar**; en **Imagen** elimina `ghcr.io/everdd94/subman`; vuelve a **Proyecto** y
   **Acción › Crear** (o **Compilar**). Arranca con la versión nueva y ya con Watchtower.

Sin Watchtower, se actualiza a mano con el paso 3.

## Qué hace con tus archivos

Para cada capítulo:

- **Inglés:** los subtítulos en inglés se mueven a `docker/subman/archivo/en/`, con la misma
  estructura de carpetas. El idioma se reconoce por el texto, no solo por el nombre del archivo.
- **Español:** se prueban **todos** los subtítulos en español que haya, se llamen `.spa`,
  `.spa.0`, `.es` o no tengan etiqueta. El mejor que salga **ok** se guarda corregido como
  `capítulo.es.srt`.
- **Si ninguno pasa:** se prueban hasta 5 subtítulos de OpenSubtitles.
- **Los originales:** todos (los malos, las copias y el original del bueno) se mueven a
  `docker/subman/papelera/`, con la misma estructura. Nada se borra.
- **Lo que vuelva a aparecer:** si más adelante otro programa deja otro subtítulo en español
  junto a un capítulo que ya está bien, también se aparta a la papelera.
- **Ensayo:** para ver qué haría sin tocar nada, activa **Modo ensayo** en Ajustes y mira el
  Registro.
- **Revisar:** en **Biblioteca** puedes abrir cualquier capítulo, ver cómo se corrigió y
  rechazarlo si está mal (va a la papelera y se busca otro). En **Papelera** puedes
  recuperar cualquier archivo.

## Películas mal reconocidas o sin año

Necesita Jellyfin puesto en Ajustes. En la pestaña **Nombres**, pulsa **Revisar nombres**:

- **Por identificar:** las que Jellyfin no reconoce o reconoce mal (por ejemplo `after 1.mp4`
  convertida en otra película). Salen opciones con póster; elige la buena (o escribe otro
  nombre y pulsa Buscar) y **Aplicar**.
- **Sin carpeta propia:** Jellyfin ya las reconoce bien; **Ordenar las marcadas en carpetas**
  pone cada una en su carpeta.

**Identificar todo** lo hace solo. Necesita dos claves gratis en Ajustes:

- **TheMovieDB** (obligatoria): crea una cuenta en themoviedb.org, ve a tu perfil > Ajustes > API,
  pide una clave de uso personal y copia la *Clave de la API*. Con ella subman compara la duración
  de tu archivo con la de cada película y sabe el orden de las sagas («Fast 7» = la 7.ª).
- **Gemini** (opcional, IA): en aistudio.google.com/apikey, *Create API key*. Sólo se usa para las
  dudosas, y sólo se le envían nombres de archivo y duraciones.

Aplica sola las que no tienen dudas (salen en «Cambios hechos» como *automático* o *IA*, con
Deshacer) y deja el resto abajo con la mejor opción marcada.

Cada película queda como `After (2019) [tmdbid-338952]/After (2019).mp4`, con sus subtítulos al
lado. Ese número es el de TheMovieDB: con él Jellyfin ya no se equivoca. Abajo salen los
cambios hechos con **Deshacer**.

## Películas duplicadas

En la pestaña **Duplicados**, pulsa **Buscar duplicados**. Cada película repetida sale con
sus copias (calidad, duración, tamaño, subtítulos) y la peor ya marcada; si la duración no
coincide (por ejemplo, una versión extendida) sale como **dudoso** y no se marca nada.
**Apartar las marcadas** las mueve a `Duplicados` dentro de tu carpeta compartida (se cambia
en Ajustes), con la misma estructura de carpetas. Nada se borra: abajo salen las apartadas
con **Devolver**, y cuando las hayas revisado las borras tú desde File Station.
Si tienes Jellyfin puesto en Ajustes, lo usa para reconocer la película aunque el nombre
del archivo sea raro.

## Otros detalles

- Empieza por lo más reciente (lo que acaba de bajar Sonarr) y sigue hacia atrás.
- Si se acaban las descargas del día en OpenSubtitles, deja esos capítulos esperando y sigue al día
  siguiente, sin marcarlos como fallidos.
- El registro se guarda también en `docker/subman/subman.log`.

## Comandos útiles

Desde Container Manager, en **Contenedor**, `subman`, **Terminal**:

```
subman list          # estado de cada capítulo
subman sync "/media/Series/X/Season 1/X - S01E01.mkv" "/media/Series/X/Season 1/X - S01E01.spa.srt" -o /config/prueba.srt
```

## Si la imagen no se descarga ("unauthorized")

El paquete de GitHub tiene que ser público. En GitHub, ve a tu perfil, **Packages**, `subman`,
**Package settings** y en **Change visibility** elige **Public**.
