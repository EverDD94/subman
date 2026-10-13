# Instalar subman en el Synology (DS923+)

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

## Actualizar a una versión nueva

Container Manager, **Proyecto**, `subman`: **Detener**, luego **Acción**, **Compilar**
(descarga la imagen nueva) y **Iniciar**. Si tu compose aún no tiene la parte `ports`,
añádela antes (ver `docker-compose.yml`).

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
