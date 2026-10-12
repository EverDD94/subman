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
3. Opcional: activa **Modo ensayo** si primero quieres ver en el **Registro** qué haría sin
   que mueva nada.
4. Activa **Activar el trabajo automático** y pulsa **Guardar ajustes**. En **Inicio** se ve
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

## Comandos útiles

Desde Container Manager, en **Contenedor**, `subman`, **Terminal**:

```
subman list          # estado de cada capítulo
subman sync "/media/Series/X/Season 1/X - S01E01.mkv" "/media/Series/X/Season 1/X - S01E01.spa.srt" -o /config/prueba.srt
```

## Si la imagen no se descarga ("unauthorized")

El paquete de GitHub tiene que ser público. En GitHub, ve a tu perfil, **Packages**, `subman`,
**Package settings** y en **Change visibility** elige **Public**.
