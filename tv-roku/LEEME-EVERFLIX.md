# Everflix para Roku

Basado en [jellyfin-roku](https://github.com/jellyfin/jellyfin-roku) (GPL-2.0) con la marca Everflix:
nombre, logo E, colores azules e intro de la E al abrir (`videos/intro.mp4`).

## Compilar

    npm ci && npm run build      # crea out/tv-roku.zip

## Instalar en la Roku (modo desarrollador)

1. En el mando: Casa ×3, Arriba ×2, Derecha, Izquierda, Derecha, Izquierda, Derecha.
2. Apunta la IP que sale, activa el instalador y pon una contraseña.
3. En el PC abre `http://IP-DE-LA-ROKU`, usuario `rokudev` y esa contraseña.
4. «Upload» → elige el .zip → «Install with zip».

## Everflix en el navegador

Hay una tercera versión de Everflix: la página web que sirve Videotecario en `/everflix`
(`subman/static/everflix.html`, en casa `http://IP-DEL-NAS:8787/everflix` y desde fuera por el puerto 8921).
**Todo cambio que se haga en Everflix de Roku o de Google TV hay que hacerlo también en esa página**, para que las tres vayan a la par.
