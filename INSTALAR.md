# Instalar subman en el Synology (DS923+)

## 1. Carpeta de configuración

En File Station, crea `docker/subman` dentro de `volume1`. Si ya tienes una carpeta `docker`
de otros contenedores, crea solo `subman` dentro de ella.

## 2. Proyecto en Container Manager

1. Abre Container Manager, ve a **Proyecto** y pulsa **Crear**.
2. Nombre: `subman`. Ruta: `/volume1/docker/subman`. Origen: **Crear docker-compose.yml**.
3. Pega el contenido de `docker-compose.yml` y cambia `/volume1/MEDIOS` por la carpeta
   compartida donde tienes `Series` (la misma que ve Jellyfin).
4. Pulsa **Siguiente** y luego **Hecho**. Se descarga la imagen y arranca.

La primera vez el contenedor crea `/volume1/docker/subman/subman.toml` y se queda esperando.

## 3. Rellenar `subman.toml`

Ábrelo con el Editor de texto de DSM, o desde Windows en `\\192.168.1.121\docker\subman`.

```toml
library = ["/media/Series", "/media/Peliculas"]   # rutas DENTRO del contenedor
languages = ["es"]

[providers.opensubtitles]
api_key = "..."     # opensubtitles.com, sección API consumers
username = "..."
password = "..."
```

- `library`: con el compose de arriba, `S:\Series` se ve como `/media/Series`. Ajusta el
  nombre de la carpeta de películas al tuyo.
- Al guardar, en menos de un minuto empieza a trabajar. El progreso se ve en Container
  Manager, en **Contenedor**, `subman`, **Registro**.

## Qué hace con tus archivos

- Si ya hay un `.spa.srt` o `.es.srt` junto al capítulo, lo sincroniza y lo **sustituye**.
  El original queda como `.srt.orig`, que Jellyfin ignora.
- Si no hay ninguno o el que hay no pasa la verificación, prueba hasta 5 de OpenSubtitles y
  se queda con el primero que salga **ok**.
- Lo que sale **falla** no se toca. Lo que sale **revisar** se guarda igualmente; si no lo
  quieres, cambia `keep_review = false`.
- Cuando todo está **ok**, no vuelve a tocarlo. Lo que falla lo reintenta cada 24 horas.

## Comandos útiles

Desde Container Manager, en **Contenedor**, `subman`, **Terminal**:

```
subman list          # estado de cada capítulo
subman sync "/media/Series/X/Season 1/X - S01E01.mkv" "/media/Series/X/Season 1/X - S01E01.spa.srt" -o /config/prueba.srt
```

## Si la imagen no se descarga ("unauthorized")

El paquete de GitHub tiene que ser público. En GitHub, ve a tu perfil, **Packages**, `subman`,
**Package settings** y en **Change visibility** elige **Public**.
