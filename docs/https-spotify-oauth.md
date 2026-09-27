# Acceso a Groove, vinculación de Spotify y HTTPS/PWA

Este documento recoge el estado real (septiembre de 2026) del acceso a Groove en la red local, el
workaround necesario para vincular Spotify y el trabajo de HTTPS/PWA que quedó **a medias**.

## Estado actual (TL;DR)

| Qué | Cómo |
|---|---|
| **Groove normal** | `http://192.168.0.21:5000` |
| **Vincular Spotify** | `ssh -L 5000:127.0.0.1:5000 Groove` y luego abrir `http://127.0.0.1:5000/spotify/` en ese mismo equipo |
| **HTTPS / PWA** | **Pendiente**: configuración iniciada pero no terminada ni validada |

- El acceso oficial sigue siendo **HTTP** por IP local. No hay URL HTTPS oficial.
- El túnel SSH solo hace falta **para vincular Spotify** (una vez, o si hay que volver a vincular).

## Acceso a Groove

Groove funciona en la red local en:

```
http://192.168.0.21:5000
```

La Raspberry tiene una **reserva DHCP** en el router, así que `192.168.0.21` se considera
actualmente la IP estable de Groove.

Por SSH se accede con:

```bash
ssh Groove
```

`Groove` es un alias definido en la configuración SSH (`~/.ssh/config`) del equipo del
desarrollador; en otro equipo habrá que usar `ssh <usuario>@192.168.0.21` o crear un alias
equivalente.

## Por qué no se puede vincular Spotify desde el móvil

Spotify tiene registrado ahora mismo un redirect URI **loopback**:

```
http://127.0.0.1:5000/spotify/callback
```

y Groove usa ese mismo valor en `SPOTIFY_REDIRECT_URI`. Spotify solo admite `http://` para
direcciones loopback (`127.0.0.1`); para cualquier otra dirección exige HTTPS. Por eso no se puede
registrar simplemente `http://192.168.0.21:5000/spotify/callback`.

El problema:

- `127.0.0.1` **siempre es el propio dispositivo donde está abierto el navegador**.
- Si abres Groove desde el móvil en `http://192.168.0.21:5000` y pulsas **Conectar Spotify**, el
  login en Spotify va bien, pero al terminar Spotify redirige a `http://127.0.0.1:5000/spotify/callback`.
- En ese momento `127.0.0.1` es **el móvil**, no la Raspberry. En el móvil no hay nada escuchando en
  el puerto 5000, así que el callback falla y Groove nunca recibe el código de autorización.

Esto **no es un bug de Spotify ni de Flask**: es la consecuencia directa de usar un redirect URI
loopback desde un dispositivo distinto del que ejecuta Groove. Funciona únicamente cuando el
`127.0.0.1:5000` del navegador llega de algún modo a la Raspberry, que es lo que hace el túnel SSH.

## Workaround actual: túnel SSH desde el portátil

1. En el portátil, abre una terminal y crea el túnel:

   ```bash
   ssh -L 5000:127.0.0.1:5000 Groove
   ```

   **Mantén esa terminal abierta** mientras dure la vinculación. Si en el portátil tienes la app de
   desarrollo usando el puerto 5000, párala antes.

2. En el navegador **del portátil**, abre:

   ```
   http://127.0.0.1:5000/spotify/
   ```

   (con `127.0.0.1`, no `localhost`, para que coincida con el redirect URI registrado).

3. Pulsa **Conectar Spotify** (vincular), inicia sesión en Spotify y acepta los permisos. Volverás a
   la página Spotify de Groove con el estado "Conectado".

El recorrido completo:

```
navegador del portátil
    ↓
127.0.0.1:5000
    ↓ túnel SSH
Raspberry Groove:5000
    ↓
Spotify (login y permisos)
    ↓
http://127.0.0.1:5000/spotify/callback
    ↓ túnel SSH
Raspberry Groove  → guarda los tokens
```

4. Cuando Spotify quede vinculado, el túnel ya no hace falta: ciérralo con **Ctrl+C** en esa terminal.

5. A partir de ahí Groove se usa con normalidad desde cualquier dispositivo en
   `http://192.168.0.21:5000`.

Los tokens (y su renovación automática) se guardan **en Groove**, en la base de datos de la
Raspberry. No hay que mantener el túnel abierto para reproducir Spotify, para las alarmas ni para el
resto de la interfaz. Solo habrá que repetir el procedimiento si hay que volver a vincular (tokens
revocados, cambio de cuenta, scopes nuevos...).

## HTTPS / PWA — pendiente

> ⚠️ **Trabajo iniciado pero NO terminado ni validado.** Nada de esta sección está en uso. El acceso
> oficial sigue siendo `http://192.168.0.21:5000` y la vinculación de Spotify sigue necesitando el
> túnel SSH.

### Qué se buscaba

Se empezó a preparar HTTPS con **Caddy** como proxy inverso para resolver dos cosas:

1. Tener un callback de Spotify que funcione **directamente** desde móvil, tablet o PC, sin túnel SSH.
2. Dar a Groove un contexto HTTPS para completar la **PWA**: los navegadores solo activan el
   *service worker* en HTTPS (o en `127.0.0.1`/`localhost`), así que con `http://192.168.0.21:5000`
   la app se puede añadir a la pantalla de inicio pero sin *service worker*.

La arquitectura que se estaba probando:

```
https://192.168.0.21
       ↓
     Caddy  (TLS)
       ↓
http://127.0.0.1:5000
       ↓
     Groove
```

### Hasta dónde se llegó

- Se llegó a preparar Caddy con una configuración y un certificado local (CA interna de Caddy).
- El despliegue HTTPS **no se considera terminado ni validado**.

### Qué quedó pendiente

La decisión abierta es **cómo dar a los dispositivos cliente un certificado HTTPS en el que confíen**.

- Se estudió usar la **CA interna de Caddy** e instalar su certificado raíz en cada dispositivo.
- Se **aplazó** porque obliga a añadir manualmente esa CA como de confianza en cada móvil y PC.

### Lo que NO hay que dar por hecho mientras siga pendiente

- **No** asumir que los dispositivos confían en la CA local de Caddy (en general no lo hacen).
- **No** tratar `https://192.168.0.21` como URL oficial de Groove.
- **No** sustituir el acceso HTTP actual (`http://192.168.0.21:5000`).
- **No** cambiar todavía el redirect URI de Spotify (ni en el Dashboard ni en `SPOTIFY_REDIRECT_URI`).
- **No** eliminar el workaround del túnel SSH.

## Solución definitiva (futura)

Cuando se retome HTTPS, el objetivo es tener una **URL HTTPS estable** para Groove, con un
certificado en el que los dispositivos confíen sin pasos manuales:

```
https://<nombre-o-direccion-definitiva>
```

Y entonces cambiar el redirect URI de Spotify a:

```
https://<nombre-o-direccion-definitiva>/spotify/callback
```

El redirect URI debe **coincidir exactamente** (esquema, host, puerto, ruta, sin barra final) en los
dos sitios:

- en **Spotify Developer Dashboard** → *Redirect URIs*;
- en `SPOTIFY_REDIRECT_URI` del `.env` de Groove (reiniciando el servicio después).

Una vez validado HTTPS:

- se podrá eliminar el túnel SSH para OAuth: se vinculará Spotify desde cualquier dispositivo;
- se podrá completar y validar la PWA (*service worker*);
- el acceso habitual a Groove podrá pasar de HTTP a HTTPS.

**Todo esto sigue siendo trabajo pendiente.**

## Nota: el buscador de Spotify es otro tema

El problema del callback `127.0.0.1` es **independiente** del buscador integrado de Spotify.
No mezclarlos:

- **Pegar un enlace de Spotify** sigue siendo el método fiable y principal para elegir qué suena.
- Se aceptan **playlists, álbumes y canciones**.
- El **buscador integrado** puede estar limitado o bloqueado por la API de Spotify (p. ej. un 403 en
  `/v1/search`) y está **aparcado** por ahora.
- No intentar "arreglar" el buscador como parte del trabajo de HTTPS/OAuth: HTTPS no lo cambia.
