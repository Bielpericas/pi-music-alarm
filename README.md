# pi-music-alarm

Despertador ligero pensado para una **Raspberry Pi Zero 2 W**. Interfaz web (pensada para móvil) hecha con Flask y SQLite.

Estado actual: gestión de alarmas (crear, listar, activar/desactivar, borrar, probar) y un
scheduler que las dispara a su hora. Al dispararse, la alarma reproduce un WAV local y escribe
`ALARMA ACTIVADA: <nombre>` en la consola y en `instance\alarms.log`.
Cada alarma puede sonar con el WAV local o con Spotify (canción, álbum o playlist); si Spotify falla,
suena el WAV local como respaldo.

## Requisitos

- Python 3.10 o superior
- Nada más: SQLite viene incluido con Python.

## Ejecutar en Windows (PowerShell)

Desde la carpeta del proyecto:

```powershell
# 1. Crear el entorno virtual (solo la primera vez)
py -m venv .venv

# 2. Activarlo
.venv\Scripts\Activate.ps1
```

Si PowerShell dice que la ejecución de scripts está deshabilitada, ejecuta esto una vez y vuelve a activar:

```powershell
Set-ExecutionPolicy -Scope CurrentUser RemoteSigned
```

```powershell
# 3. Instalar dependencias
pip install -r requirements.txt

# 4. Arrancar la app en modo desarrollo
python app.py
```

(Equivalente: `flask --app app run --debug`.)

Abre http://127.0.0.1:5000 en el navegador.

La base de datos se crea sola en `instance\alarms.db` la primera vez que arrancas. Para borrar todas las alarmas, basta con borrar ese fichero.

### Sonido de la alarma

La alarma reproduce **`sounds\alarm.wav`**. Ese fichero no está en git (no subimos binarios); créalo una vez:

```powershell
python tools\make_test_sound.py
```

Esto genera tres pitidos de ~2 s. También puedes poner ahí tu propio sonido. Tiene que ser **WAV PCM**
(el WAV "normal" sin comprimir, por ejemplo 16 bits a 44.1 kHz). MP3, OGG o un WAV comprimido no
funcionan; conviértelos antes con Audacity ("Exportar como WAV").

Para usar otra ruta, define la variable de entorno `ALARM_SOUND` antes de arrancar:

```powershell
$env:ALARM_SOUND = "C:\ruta\a\mi_sonido.wav"
python app.py
```

Con `$env:AUDIO_BACKEND = "none"` la app funciona sin sonido.

Para probarlo, pulsa **Probar** en cualquier alarma de la lista: suena al momento. Si no suena, mira la
consola o `instance\alarms.log`, donde aparece el motivo (fichero inexistente, formato no válido…).

### Probar desde el móvil

Con el PC y el móvil en la misma red Wi-Fi:

```powershell
flask --app app run --host 0.0.0.0
```

Abre `http://<IP-del-PC>:5000` en el móvil (la IP la ves con `ipconfig`). Puede que el Firewall de Windows pida permiso la primera vez.

También puedes probar en Windows el modo producción que se usa en la Pi, con `python serve.py`
(waitress, escuchando en `0.0.0.0:5000`).

### Tests

```powershell
python -m unittest discover tests -v
```

## Instalación en Raspberry Pi OS Lite (Raspberry Pi Zero 2 W)

Probado con Raspberry Pi OS Lite de 64 bits (Bookworm, Python 3.11, o Trixie, Python 3.13).
La app es la misma que en Windows; en la Pi se ejecuta con **`serve.py`** (waitress), que escucha en la
red local, y como **servicio systemd** para que arranque sola.

En los comandos, `<usuario>` es el usuario que creaste al grabar la tarjeta con Raspberry Pi Imager.

### 0. Antes de empezar: audio en la Zero 2 W

La Zero 2 W **no tiene salida de audio analógica (jack)**. El sonido sale por el mini-HDMI (monitor o TV
con altavoces), por una tarjeta de sonido USB, por un DAC I2S (tipo HAT) o por Bluetooth. Lo más
sencillo es una tarjeta USB barata con un adaptador micro-USB OTG.

### 1. Paquetes del sistema

```bash
sudo apt update
sudo apt full-upgrade -y
sudo apt install -y git python3 python3-venv python3-pip alsa-utils ffmpeg
```

- `python3-venv`: crear el entorno virtual.
- `alsa-utils`: incluye `aplay`, que reproduce el WAV de emergencia.
- `ffmpeg`: reproduce la música local (MP3, OGG, WAV). Si ya tenías Groove instalado:
  `sudo apt install -y ffmpeg`.
- No hace falta compilar nada: todas las dependencias tienen versión en Python puro o wheel para ARM.

### 2. Clonar el repositorio

```bash
cd ~
git clone <URL-de-tu-repositorio> pi-music-alarm
cd pi-music-alarm
```

Puedes clonarlo en otra carpeta: ni el servicio ni los scripts dependen de la ruta.

### 3. Entorno virtual y dependencias

```bash
python3 -m venv .venv
.venv/bin/pip install --upgrade pip
.venv/bin/pip install -r requirements.txt
```

En la Zero 2 W tarda un par de minutos.

### 4. Configurar `.env`

`.env` no está en git: créalo en la Pi.

```bash
cp .env.example .env
nano .env
```

- `SECRET_KEY`: genérala con `python3 -c "import secrets; print(secrets.token_hex(32))"`.
- `SPOTIFY_CLIENT_ID` / `SPOTIFY_CLIENT_SECRET`: los mismos que en el PC.
- `SPOTIFY_REDIRECT_URI`: déjala en `http://127.0.0.1:5000/spotify/callback` (ver paso 9).
- `ALSA_DEVICE`: solo si el sonido no sale por la tarjeta buena (ver paso 6).
- `BLUETOOTH_SERVICE`: servicio del reproductor Bluetooth (por defecto `bluealsa-aplay`; `none` para
  no gestionarlo). Ver paso 10.
- `HOST` / `PORT`: por defecto `0.0.0.0` y `5000`, accesible desde la red local.

```bash
chmod 600 .env   # solo tu usuario puede leer los secretos
```

### 5. Zona horaria y hora

Las alarmas usan la hora del sistema. La Pi **no tiene reloj con pila**: al encenderse toma la hora
de Internet (NTP), así que necesita red.

```bash
sudo timedatectl set-timezone Europe/Madrid     # o la tuya: timedatectl list-timezones
timedatectl                                     # debe decir "System clock synchronized: yes"
```

Para que el servicio espere a tener la hora sincronizada antes de arrancar (recomendado):

```bash
sudo systemctl enable systemd-time-wait-sync
```

### 6. Probar el sonido con aplay

```bash
.venv/bin/python tools/make_test_sound.py      # crea sounds/alarm.wav
aplay -l                                        # lista las tarjetas de sonido
aplay sounds/alarm.wav                          # prueba con la tarjeta por defecto
```

Si no suena por donde quieres, busca tu tarjeta con `aplay -L` y pruébala:

```bash
aplay -D plughw:CARD=Device,DEV=0 sounds/alarm.wav
```

Usa `plughw:` y no `hw:`: convierte el formato del WAV al que admita la tarjeta. Cuando suene, pon
ese valor en `.env`:

```
ALSA_DEVICE=plughw:CARD=Device,DEV=0
```

El volumen se ajusta con `alsamixer`: F6 elige la tarjeta y la tecla M quita el silencio.

### 7. Ejecutar la app a mano (prueba)

```bash
.venv/bin/python serve.py
```

- Desde el móvil o el PC, abre `http://<IP-de-la-Pi>:5000`. La IP la ves con `hostname -I`; también
  suele funcionar `http://<nombre-de-la-pi>.local:5000`.
- Crea una alarma y pulsa **Probar**: debe sonar el WAV.
- Para parar, pulsa Ctrl+C.

No uses `python app.py` en la Pi: es el modo desarrollo (debug, solo `127.0.0.1`).

### 8. Arranque automático (systemd)

```bash
bash deploy/install-service.sh
```

El script:
1. Rellena `deploy/pi-music-alarm.service.template` con **tu usuario y la carpeta real** del
   repositorio (no hay rutas fijas).
2. Lo instala como `/etc/systemd/system/pi-music-alarm.service` (pide la contraseña de sudo).
3. Lo activa y lo arranca.

Para usar otro usuario: `SERVICE_USER=otro bash deploy/install-service.sh`.

Gestión del servicio:

| Acción | Comando |
|---|---|
| Ver estado | `sudo systemctl status pi-music-alarm` |
| Iniciar | `sudo systemctl start pi-music-alarm` |
| Detener | `sudo systemctl stop pi-music-alarm` |
| Reiniciar (p. ej. tras cambiar `.env` o hacer `git pull`) | `sudo systemctl restart pi-music-alarm` |
| Logs en directo | `journalctl -u pi-music-alarm -f` |
| Logs desde el último arranque | `journalctl -u pi-music-alarm -b` |
| No arrancar al encender | `sudo systemctl disable pi-music-alarm` |
| Desinstalar | `sudo systemctl disable --now pi-music-alarm && sudo rm /etc/systemd/system/pi-music-alarm.service && sudo systemctl daemon-reload` |

Las alarmas también quedan en `instance/alarms.log`.

Para actualizar:

```bash
git pull
.venv/bin/pip install -r requirements.txt
sudo systemctl restart pi-music-alarm
```

### 9. Vincular Spotify desde la Pi

Spotify solo acepta redirect URIs `http://` con `127.0.0.1`, no con la IP de la Pi en la red. Por eso
el paso **Conectar Spotify** se hace con un túnel SSH desde tu PC, **una sola vez**: los tokens se
guardan en la Pi y se renuevan solos.

En el PC (PowerShell), con la app corriendo en la Pi:

```powershell
ssh -L 5000:127.0.0.1:5000 <usuario>@<IP-de-la-Pi>
```

Deja esa ventana abierta y, en el navegador del PC, abre **http://127.0.0.1:5000/spotify/**, pulsa
**Conectar Spotify** y acepta. Si en el PC tienes la app de desarrollo usando el puerto 5000, párala
antes. Luego ya puedes cerrar el túnel y usar la Pi con su IP normal.

Recuerda que una alarma Spotify suena en un **dispositivo Spotify Connect** (móvil, altavoz, PC con
Spotify abierto). La Pi todavía no es uno de ellos: eso llegará con librespot. Si no hay
dispositivo disponible, suena el WAV por la tarjeta de la Pi.

### 10. Bluetooth (BlueALSA) y alarmas

Si usas la Pi también como **altavoz Bluetooth**, `bluealsa-aplay` reproduce lo que llega del móvil
por la **misma tarjeta USB** que el WAV de las alarmas y Raspotify. Una tarjeta USB con `plughw:`
solo la puede abrir un programa a la vez, así que Groove **para `bluealsa-aplay` antes de cada alarma**
y lo vuelve a arrancar después (ver [Bluetooth y alarmas](#bluetooth-y-alarmas)).

**1. Paquetes** (si aún no los tienes; en Bookworm/Trixie):

```bash
sudo apt install -y bluez bluez-alsa-utils polkitd
```

`bluez-alsa-utils` trae `bluealsa` y `bluealsa-aplay` con sus servicios systemd. El emparejamiento
del móvil se hace una vez con `bluetoothctl` (Groove no lo toca).

**2. Hacer que `bluealsa-aplay` use la tarjeta USB.** Crea un *override* del servicio:

```bash
sudo systemctl edit bluealsa-aplay
```

y escribe (la primera línea `ExecStart=` vacía borra la original):

```ini
[Service]
ExecStart=
ExecStart=/usr/bin/bluealsa-aplay -S -D plughw:CARD=Device,DEV=0 --single-audio
```

- `-D plughw:CARD=Device,DEV=0`: la misma tarjeta que `ALSA_DEVICE` (mírala con `aplay -L`).
- `-S`: registra en syslog/journal.
- `--single-audio`: solo un dispositivo Bluetooth suena a la vez.

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now bluealsa bluealsa-aplay
systemctl status bluealsa-aplay        # active (running)
```

**3. Permiso mínimo para Groove (sin sudo).** El servicio `pi-music-alarm` corre con tu usuario y con
`NoNewPrivileges=true`, así que no puede (ni debe) usar `sudo`. En su lugar, una regla de **polkit**
permite a tu usuario **solo `start` y `stop` de `bluealsa-aplay.service`**: nada de otras unidades, ni
`enable`/`disable`, ni BlueZ.

```bash
bash deploy/install-bluetooth-permission.sh
```

El script rellena `deploy/pi-music-alarm-bluetooth.rules.template` con tu usuario y la unidad
(`BLUETOOTH_SERVICE` si la cambias: `BLUETOOTH_SERVICE=otro bash deploy/...`) y la instala en
`/etc/polkit-1/rules.d/50-pi-music-alarm-bluetooth.rules`. Necesita polkit con reglas JavaScript
(`pkaction --version` ≥ 0.106; Bookworm trae la 122). Compruébalo **sin sudo** (no debe pedir
contraseña):

```bash
systemctl --no-ask-password stop bluealsa-aplay && systemctl --no-ask-password start bluealsa-aplay
```

Y que la regla no concede nada más (esto **debe fallar** con "Access denied"):

```bash
systemctl --no-ask-password restart bluealsa        # otra unidad: denegado
systemctl --no-ask-password disable bluealsa-aplay  # otro verbo: denegado
```

**No uses** una regla de `sudoers` ni añadas tu usuario a grupos amplios: no hace falta.

Para quitar el permiso: `sudo rm /etc/polkit-1/rules.d/50-pi-music-alarm-bluetooth.rules`.

## Cómo funciona el scheduler

- Al arrancar la app se inicia un `BackgroundScheduler` de APScheduler (un hilo, sin servicios externos).
- Tiene **un solo job** que se ejecuta cada minuto, en el segundo 0. Busca en SQLite las alarmas activas
  con esa hora `HH:MM` y comprueba el día de la semana. Las alarmas "una vez" (sin días) se disparan
  una vez y se desactivan solas.
- La base de datos es la única fuente de verdad: crear, borrar o desactivar una alarma no requiere
  tocar el scheduler, y tras reiniciar la app todo sigue funcionando.
- Para no disparar dos veces en el mismo minuto, antes de disparar se guarda `last_triggered`
  (`"YYYY-MM-DD HH:MM"`) con un `UPDATE` atómico que solo tiene éxito si ese minuto no estaba ya marcado.
- Si la app está apagada a la hora de una alarma, esa alarma **no** se recupera después.
- El botón **Probar** ejecuta la misma acción al momento, sin tocar `last_triggered`.
- Además hay jobs de **pre-flight** (`preflight:<id>`) y uno que los revisa cada minuto
  (`preflight-sync`); solo observan y nunca cambian cuándo suena una alarma. Ver
  [Diagnóstico y pre-flight](#diagnóstico-y-pre-flight).

## Cómo funciona el audio

- `audio_player.py` define la interfaz `AudioPlayer` (`play()` / `stop()`), con dos reglas:
  `play()` **no bloquea** y **nunca lanza excepciones** (los errores van al log).
- `LocalAudioPlayer` reproduce el WAV configurado:
  - **Windows:** `winsound` de la librería estándar, en modo asíncrono.
  - **Raspberry Pi OS / Linux:** `aplay` (paquete `alsa-utils`, ya viene en Raspberry Pi OS) en un
    subproceso. Un hilo ligero espera a que termine para registrar errores.
- Antes de reproducir se valida la cabecera del WAV. Si el fichero no existe o no es válido,
  se registra el error y la alarma sigue su curso.
- El scheduler y el botón **Probar** solo llaman a `player.play()`: no saben nada de audio.
  `create_player()` elige la implementación según `AUDIO_BACKEND` (`local` o `none`).

### Música local (biblioteca)

Las alarmas locales, y las de Spotify cuando Spotify falla, suenan con música de la **biblioteca local**:
la carpeta `instance/music/` de la Pi (se crea sola). Formatos: **MP3, OGG y WAV** (se ignora todo lo
demás, subcarpetas y ficheros ocultos incluidos).

**Añadir música**, de dos formas:

- Desde la web: sección **Música** (navegación inferior) → elegir archivo → **Subir**. Desde ahí también
  se ven (nombre, tipo, tamaño y qué alarmas la usan) y se **eliminan** (con confirmación).
- Por SCP desde el PC:

  ```bash
  scp cancion.mp3 pericasbiel@Groove:~/pi-music-alarm/instance/music/
  ```

Lo subido o copiado aparece al momento en los formularios de alarma, sin reiniciar Groove.

Subidas desde la web:

- Máximo `LOCAL_MUSIC_MAX_UPLOAD_MB` por archivo (50 MB por defecto, en `.env`).
- El nombre se sanea (sin rutas, sin `../`, solo letras, números, `-`, `_` y `.`; los acentos se
  quitan: `canción.mp3` → `cancion.mp3`) y siempre se guarda dentro de `instance/music/`.
- Se comprueba que el contenido corresponde a la extensión (cabecera MP3/OGG/WAV).
- **Nunca se sobrescribe**: si ya existe un archivo con ese nombre, se avisa.

En cada alarma, **Música local** (o **Música de respaldo** si es de Spotify) puede ser una pista concreta o
**Aleatorio**, que elige una pista cada vez que la alarma empieza a sonar (también al volver de +10 MIN;
puede repetirse). En la base de datos solo se guarda el nombre del fichero (`local_track`; vacío =
aleatorio).

**Orden de respaldo: Spotify → música local → WAV de emergencia.** El WAV de emergencia es el de siempre
(`sounds/alarm.wav` o `ALARM_SOUND`, con `aplay`) y suena solo si la música local no puede sonar:

- la biblioteca está vacía;
- la pista elegida ya no existe (se borró; la alarma no se toca y en su formulario sale «no disponible»);
- ffmpeg no está instalado, o termina con error al empezar (archivo corrupto, tarjeta ocupada...).

Todo queda en el log (`Música local para «…»: sunrise.mp3 (al azar)`, `La pista «…» ya no está en la
biblioteca`, `Sin música local para «…»: suena el WAV de emergencia`, `ffmpeg no está instalado`...).

Reproducción: un proceso `ffmpeg` por alarma, sin shell, con la pista **en bucle** hasta STOP, +10 MIN o
el auto-stop:

```
ffmpeg -hide_banner -nostdin -loglevel error -stream_loop -1 -i <pista> -f alsa <ALSA_DEVICE>
```

La salida es `ALSA_DEVICE` (o `plughw:CARD=Device,DEV=0` si está vacío). Para pararlo (STOP, +10 MIN,
auto-stop, borrar o sustituir la alarma) se le envía SIGTERM y, si no termina en 2 s, SIGKILL: no quedan
procesos huérfanos. En Windows (desarrollo) no se usa ffmpeg: suena el WAV de emergencia.

### Probar una alarma programada

1. Arranca con `python app.py` y mira la hora actual del PC.
2. Crea una alarma para 1–2 minutos después, con el día de hoy marcado (o sin días = una vez).
3. Espera: al llegar el minuto (en el segundo 0) sonará el WAV y verás en la consola
   `ALARMA ACTIVADA: <nombre>`, y la misma línea en `instance\alarms.log`.
4. Recarga la página: la alarma muestra "Última vez: …".

La hora que se usa es la del sistema. En la Pi, configura la zona horaria con `sudo raspi-config`.

## Spotify

La sección **Spotify** (enlace arriba a la derecha) permite vincular tu cuenta, ver tus dispositivos
Spotify Connect, elegir uno y probar Transferir / Play / Pause. Cada alarma puede usar Spotify como
sonido (ver [Alarmas con Spotify](#alarmas-con-spotify)).

Requisitos de Spotify (desde febrero de 2026): el dueño de la app de desarrollo necesita **Spotify
Premium**, y los endpoints de control de reproducción solo funcionan con cuentas Premium.

### 1. Crear la app en Spotify Developer Dashboard

1. Entra en https://developer.spotify.com/dashboard con tu cuenta de Spotify y pulsa **Create app**.
2. Rellena **App name** y **App description** con lo que quieras.
3. En **Redirect URIs** añade exactamente `http://127.0.0.1:5000/spotify/callback`
   (con `127.0.0.1`, no `localhost`, que Spotify no admite; sin barra final) y pulsa **Add**.
4. En **Which API/SDKs are you planning to use?** marca **Web API**.
5. Acepta los términos y pulsa **Save**.
6. En **Settings** copia el **Client ID** y, con **View client secret**, el **Client secret**.
7. En **User Management** añade el email de tu cuenta de Spotify, si no es la misma con la que creaste la app.

### 2. Configurar `.env`

```powershell
Copy-Item .env.example .env
notepad .env
```

Rellena `SECRET_KEY`, `SPOTIFY_CLIENT_ID`, `SPOTIFY_CLIENT_SECRET` y deja
`SPOTIFY_REDIRECT_URI=http://127.0.0.1:5000/spotify/callback`. `.env` está en `.gitignore`, así que nunca
se sube. Los tokens se guardan en `instance\alarms.db`, que tampoco se sube.

### 3. Probar

1. Reinicia la app (`python app.py`) para que lea `.env`.
2. Abre **http://127.0.0.1:5000/spotify/**. Usa `127.0.0.1` y no `localhost`, o el callback fallará.
3. Pulsa **Conectar Spotify**, inicia sesión y acepta los permisos. Volverás a la página con el estado "Conectado".
4. Abre Spotify en el PC o en el móvil (si no, no aparece como dispositivo) y pulsa **Actualizar**.
5. Pulsa **Seleccionar** en un dispositivo, luego **Transferir**, **▶ Play** y **⏸ Pause**.

Si algo falla, la página muestra el motivo: 403 = sin Premium o usuario fuera de User Management;
404 = no hay dispositivo activo; 429 = demasiadas peticiones (se respeta `Retry-After`).

### Cómo está hecho

- `spotify_client.py` concentra **todo** el HTTP con Spotify (con `urllib` de la librería estándar, sin SDK):
  - Authorization Code Flow.
  - Refresh automático del token, 60 s antes de caducar o tras un 401.
  - Errores tipados (`SpotifyAuthError`, `SpotifyForbiddenError`, `SpotifyNotFoundError`,
    `SpotifyRateLimitError`, `SpotifyConnectionError`).
  - Ante un 429 espera el `Retry-After` si es corto (≤ 5 s) y reintenta; si es largo, no vuelve a
    llamar a Spotify hasta que pase ese tiempo.
- `spotify_views.py` contiene las rutas `/spotify/...`. Solo usa `SpotifyClient` y traduce los
  errores a mensajes para la interfaz.
- Scopes pedidos: `user-read-playback-state` y `user-modify-playback-state`.

### Alarmas con Spotify

Al crear o editar una alarma, en **Sonido** elige **Sonido local** o **Spotify**. Con Spotify, en
**Contenido de Spotify** eliges qué suena. Se guarda siempre como URI.

Hay dos formas igual de válidas, y las dos acaban en el mismo URI:

1. **Pegar un enlace** (siempre visible). En Spotify: Compartir → Copiar enlace, y pegarlo en
   *Pega un enlace de Spotify (playlist, álbum o canción)*. Funciona sin Spotify vinculado, sin
   buscador y sin JavaScript: es el método de siempre.
2. **Buscar en Spotify** (debajo, tras un separador «o»). Una comodidad que depende de que la API
   de Spotify permita la búsqueda; nunca hace falta para crear una alarma.

#### Enlace manual

Acepta exactamente los mismos formatos que antes, para canciones, álbumes y playlists:

- `https://open.spotify.com/playlist/...`, `.../album/...`, `.../track/...` (con o sin `?si=...`,
  con o sin `intl-xx/`);
- `spotify:playlist:...`, `spotify:album:...`, `spotify:track:...`.

Cualquier otro tipo (artista, episodio, podcast...) o texto se rechaza al guardar.

#### Buscador integrado

Con Spotify vinculado (página **Spotify** → **Conectar Spotify**), escribe en *Buscar en Spotify*.
Groove busca a la vez **canciones, álbumes y playlists** (5 de cada, `GET /v1/search`) y los muestra
agrupados, con portada si Spotify la da y un enlace para abrir cada resultado en Spotify. Al tocar
uno queda seleccionado en una tarjeta (p. ej. *✓ Discovery · Álbum · Daft Punk*) y su enlace se
escribe en el campo de arriba; no hace falta copiar ninguna URI.

Enlace y buscador comparten la selección: **el último que cambias manda**. Si eliges un resultado y
luego pegas otro enlace, el nombre del resultado anterior se descarta (en el navegador y, de nuevo,
en el servidor, que solo guarda un nombre si corresponde a ese mismo URI).

- Solo busca a partir de 2 caracteres y espera ~400 ms a que dejes de escribir; las búsquedas
  antiguas se cancelan y las repetidas salen de una pequeña caché del navegador.
- Si Spotify responde 429, el buscador se pausa el tiempo que pida (`Retry-After`) sin reintentar;
  eso **no** frena a las alarmas. Timeout de 6 s.
- Sin Spotify vinculado se ve *Conecta Spotify para usar el buscador. También puedes pegar un enlace
  directamente.*
- El buscador usa el mismo OAuth y refresh de tokens que el resto de Groove (no hay otro login).

**Si la búsqueda no está permitida (403).** Spotify puede negar `/v1/search` a una app o cuenta
aunque el resto funcione (p. ej. apps en modo desarrollo cuyo propietario no tiene Premium, o
usuarios fuera de *User Management*). Groove distingue:

| Caso | Mensaje |
|---|---|
| Faltan scopes (Spotify lo dice, o la autorización guardada no tiene los que pide Groove) | *Vuelve a vincular Spotify para activar la búsqueda.* + enlace a Spotify |
| Sesión caducada o revocada (401 tras renovar una vez) | igual que el anterior |
| La app/cuenta no tiene acceso al endpoint, u otro 403 | *La búsqueda de Spotify no está disponible. Puedes pegar un enlace de Spotify arriba.* |

En todos los casos el buscador se desactiva en esa página (no insiste ni entra en bucles de
refresh) y el enlace manual sigue funcionando. El motivo que da Spotify (`error.message`, sin
tokens) queda en el log de Groove para diagnosticarlo.

Por dentro, el navegador llama a dos endpoints JSON internos (solo lectura):
`GET /spotify/search?q=...` y `GET /spotify/lookup?uri=...` (metadata de un URI ya guardado).
Devuelven cada elemento normalizado así (sin tokens ni el JSON de Spotify):

```json
{"uri": "spotify:album:…", "type": "album", "name": "Discovery", "subtitle": "Daft Punk",
 "external_url": "https://open.spotify.com/album/…", "image_url": "https://i.scdn.co/…"}
```

`subtitle` son los artistas (canción y álbum) o el propietario (playlist); `image_url` puede ser
`null`. Las portadas se cargan directamente desde Spotify (no se descargan ni se cachean en la Pi).
Los errores llevan `search_available: false` cuando reintentar no sirve.

#### Nombre guardado y alarmas antiguas

Al elegir un resultado se guarda, junto al URI, una pequeña copia legible (`spotify_name` y
`spotify_subtitle`) para verla al editar y en la lista (*Spotify · Discovery*). El tipo sale siempre
del URI. El servidor **valida el URI con el mismo parser de siempre** (solo track, album y playlist),
limpia y recorta los textos y los descarta si no corresponden a ese URI. La reproducción usa
**solo el URI**: manipular el nombre desde el navegador no cambia lo que suena.

Las alarmas creadas antes (solo URI) siguen funcionando igual y no hace falta volver a guardarlas.
Al editarlas se muestra el URI y su tipo, y Groove intenta obtener el nombre desde Spotify; si lo consigue, se guarda al pulsar Guardar. Si
Spotify no está disponible, se queda el URI tal cual: la alarma nunca se invalida ni se modifica por
no poder leer su nombre.

Cuando se dispara, o al pulsar **Probar**, que ejecuta exactamente el mismo flujo:

1. Se lee el dispositivo seleccionado en la sección Spotify.
2. Se transfiere la reproducción a ese dispositivo.
3. Se reproduce el contenido: una canción va en `uris`; un álbum o playlist, como `context_uri`.

**Inicio aleatorio**: con un álbum o una playlist, cada vez que suena la alarma (también tras un
snooze) Groove pregunta cuántas pistas tiene y empieza directamente en una al azar (`offset.position`),
sin reproducir antes la primera. Una canción suelta suena tal cual. Si no se puede saber el número de
pistas, se empieza por la primera como antes. Pasa con playlists que no son tuyas ni colaborativas
(Spotify no da su contenido desde febrero de 2026) o si hay un error de red. Los reintentos de Groove
"en frío" usan la misma pista elegida.

Si algo falla (Spotify sin configurar o sin vincular, ningún dispositivo seleccionado, 403, 404,
429, sin red…), el motivo queda en `instancelarms.log` y **suena el WAV local**.

Código: `spotify_player.py` (`SpotifyAlarmPlayer`) hace ese flujo usando `spotify_client.py`.
`scheduler.fire_alarm()` solo elige entre el WAV y `SpotifyAlarmPlayer`, y aplica el respaldo; no hace HTTP.

## Dispositivo de las alarmas Spotify (Groove)

Al pulsar **Seleccionar** en la página Spotify se guardan el **ID y el nombre** del dispositivo (p. ej.
«Groove», el de Raspotify). El ID de un dispositivo Spotify Connect puede cambiar (por ejemplo al
reiniciar Raspotify), así que cada vez que una alarma Spotify empieza a sonar, también tras un snooze:

1. Se pide la lista actual de dispositivos.
2. Si el **ID guardado** está en la lista, se usa.
3. Si no, se busca el dispositivo cuyo **nombre** coincide (sin distinguir mayúsculas). Si aparece, se
   usa y se **guarda su ID nuevo**.
4. Si no aparece, se **reintenta**: inmediato, +2 s, +4 s y +6 s (12 s como máximo). Sirve para
   Raspotify recién reiniciado o aún no anunciado y para fallos de red puntuales. Un 404 al transferir
   también se reintenta.
5. Si tras los intentos no aparece, se registra el motivo y suena el **WAV local**.

Después: transferir la reproducción a ese dispositivo → volumen inicial → reproducir → fade-in. Nunca se
usa "el dispositivo que estaba sonando": si por la noche Spotify sonaba en una tablet, la alarma se
lleva la música a Groove.

- **Errores sin reintentos**: autenticación inválida, permisos (403, p. ej. sin Premium),
  configuración o un 429 con espera larga. Van directamente al WAV.
- **Dos dispositivos con el mismo nombre**: si uno es el del ID guardado, se usa ese. Si no, no se
  elige al azar: se registra el conflicto y suena el WAV.
- Mientras se busca el dispositivo, la página ya muestra la alarma ("Conectando con Spotify…") y
  **STOP** / **+10 MIN** cortan la búsqueda al momento, sin que suene el WAV.
- Instalaciones antiguas: no hace falta hacer nada. Si solo había un ID guardado, el nombre se completa
  solo la primera vez que se encuentra el dispositivo. Opcionalmente, `SPOTIFY_DEVICE_NAME=Groove` en
  `.env` sirve de nombre si nunca se ha seleccionado ninguno.

**Groove "en frío"** (tras reiniciar Raspotify o la Pi): Groove sale en la lista de dispositivos, pero
la primera orden puede fallar con **403 "Player command failed: Restriction violated"** (reason
`UNKNOWN`) hasta que se activa. Para eso:

- Ese 403 concreto se considera **temporal** y se reintenta con el mismo backoff (2, 4 y 6 s). Cada
  reintento repite el ciclo completo: resolver Groove (ID/nombre), transferir y reproducir.
- Tras transferir, se consulta la lista otra vez y se espera **hasta ~2 s** (comprobaciones inmediata,
  +1 s y +1 s) a que Groove figure **activo y no restringido** antes de reproducir. Si no llega a
  estarlo, se intenta reproducir igualmente. Si ya estaba activo, no se espera.
- Los demás 403 (Premium, usuario no registrado, otros motivos) siguen **sin** reintentarse.
- STOP / +10 MIN cortan cualquier espera al momento y no suena el WAV. Si se agotan los intentos, suena
  el WAV como siempre.

Código: `spotify_player.py` (`choose_device`, `is_cold_start_restriction` y reintentos en
`SpotifyAlarmPlayer`).

## Volumen y fade-in (alarmas Spotify)

Cada alarma Spotify tiene **volumen inicial**, **volumen final** y **duración del fade-in**. Por defecto
son 20 % → 60 % en 5 min, y en la lista se ve como `Volumen: 20 → 60 % · 5 min`. Se configuran en
crear/editar alarma, dentro del bloque de Spotify, con dos deslizadores y un desplegable (0–30 min;
0 = directo al volumen final).

Al sonar:

1. Se transfiere la reproducción al dispositivo, se fija el **volumen inicial** y empieza la música.
2. Cada **15 s** sube un poco el volumen (`PUT /v1/me/player/volume`) hasta llegar **exactamente** al
   volumen final al acabar el fade. Con 20 → 60 % en 5 min son 20 peticiones, muy lejos de los
   límites de la API.
3. **STOP** y **+10 MIN** cancelan el fade al momento. Al volver a sonar tras un snooze empieza un
   fade nuevo desde el volumen inicial. Si otra alarma sustituye a la actual, se cancela su fade.
4. Si ajustar el volumen falla (403, sin red…), se registra, el fade se detiene y **la música sigue**
   con el último volumen conseguido. La alarma no se para.

Las alarmas locales guardan estos valores pero todavía no los usan (el WAV suena igual que antes).

**Raspotify / librespot**: para que Spotify pueda cambiar el volumen, en `/etc/raspotify/conf` **no**
uses `LIBRESPOT_VOLUME_CTRL=fixed` (con `fixed` el volumen no cambia). Con el valor por defecto (`log`)
el volumen es logarítmico: 20 % suena bastante bajo, lo cual va bien para despertar suave. Tras
cambiar la configuración: `sudo systemctl restart raspotify`.

Código: `fade.py` (`fade_plan` calcula los pasos; `VolumeFade` los aplica en un único hilo que se
cancela con un `Event`, sin timers huérfanos). `AlarmPlaybackManager` crea y cancela el fade;
`SpotifyAlarmPlayer.set_volume()` hace la llamada vía `spotify_client.py`.

## Alarma sonando: STOP y +10 MIN

Cuando una alarma suena (a su hora o con **Probar**), la página principal muestra arriba un recuadro con
la hora, el nombre, cómo suena (Spotify, sonido local o sonido local como respaldo) y dos botones grandes:

- **STOP**: para el sonido (pausa Spotify o para el WAV) y da la alarma por terminada. No cambia la
  programación: una alarma recurrente seguirá sonando los próximos días y una puntual sigue desactivada.
  Pulsarlo varias veces no pasa nada.
- **+10 MIN**: para el sonido y vuelve a disparar **la misma alarma** (misma fuente y mismo contenido de
  Spotify) dentro de 10 minutos, sin cambiar su hora ni crear una alarma nueva en la lista. Se puede
  posponer las veces que quieras. Mientras tanto aparece "vuelve a sonar a las HH:MM" con un botón
  **Cancelar**.

La página consulta el estado cada 10 s (`/playback/state`) y se recarga sola cuando empieza a sonar una
alarma, así que puedes dejarla abierta en el móvil.

Reglas:

- **Solo suena una alarma a la vez**: si se dispara otra mientras suena una, la anterior se para y suena
  la nueva.
- Si una alarma vuelve a sonar (o la borras) mientras estaba pospuesta, ese snooze se cancela.
- **Los snoozes solo viven en memoria**: si la app o la Raspberry se reinician durante un snooze, se
  pierde (mejor eso que sonar a una hora incorrecta). Las alarmas normales siguen guardadas en SQLite.
- La música local suena en bucle; el WAV de emergencia, una vez. El recuadro sigue visible hasta STOP,
  +10 MIN o el auto-stop.

### Duración máxima (auto-stop)

Cada alarma tiene una **Duración máxima** (formulario de crear/editar): **15, 30, 45 o 60 minutos**, o
**Sin límite**. Por defecto, 30 minutos (también para las alarmas que ya existían).

- Al empezar a sonar se programa el auto-stop (un job de APScheduler en memoria, como los snoozes).
  Al llegar el límite, la alarma se para **igual que con STOP**: se cancela el fade, se pausa Spotify o
  se para el WAV, desaparece el recuadro y Bluetooth vuelve a estar disponible.
- No es un snooze: no vuelve a sonar ni cambia la programación de la alarma.
- **STOP** antes del límite cancela el auto-stop. **+10 MIN** también, y al volver a sonar empieza un
  contador **completo** desde cero. Si otra alarma sustituye a la que sonaba, manda el límite de la nueva.
- **Sin límite** no programa nada: suena hasta STOP.
- Si Groove se reinicia mientras suena una alarma, el auto-stop se pierde (como los snoozes).
- En el log: `Auto-stop programado en 30 min (a las 08:00) para «…»`, `Auto-stop cancelado` y
  `ALARMA DETENIDA POR AUTO-STOP: … (duración máxima: 30 min)`.
- Para que un auto-stop antiguo nunca pare una alarma posterior, cada vez que empieza a sonar una alarma
  recibe un número de reproducción nuevo; el job solo para la alarma si ese número sigue siendo el de la
  que suena.

### Bluetooth y alarmas

Prioridad: **ALARMA > SPOTIFY > BLUETOOTH**. La alarma siempre gana a Bluetooth:

| Momento | Bluetooth (`bluealsa-aplay`) |
|---|---|
| Empieza a sonar una alarma (a su hora, **Probar** o al volver un snooze) | se **para** antes de reproducir (Spotify, WAV o WAV de respaldo) |
| Suena el WAV de respaldo porque Spotify falló | sigue parado |
| Otra alarma sustituye a la que sonaba | sigue parado (no se arranca entre medias) |
| **STOP** | se vuelve a **arrancar** |
| **+10 MIN** | se vuelve a arrancar durante los 10 minutos; al volver a sonar se para otra vez |
| Borrar la alarma que suena | como STOP |

- Solo se vuelve a arrancar si lo paró Groove: si lo tenías parado, sigue parado.
- Solo se para y arranca el **reproductor**: el móvil sigue emparejado y conectado; nunca se
  desconecta, desempareja ni cambia la visibilidad.
- Si `systemctl` falla (sin permiso, sin BlueALSA, tiempo agotado), se registra en el log y **la
  alarma suena igual**.
- En el log: `Bluetooth pausado por alarma (bluealsa-aplay.service detenido)` y `Bluetooth disponible
  de nuevo (bluealsa-aplay.service arrancado)`.
- Si Groove se reinicia **mientras suena** una alarma, no sabe que había parado Bluetooth: arráncalo a
  mano con `sudo systemctl start bluealsa-aplay` (o reinicia la Pi).
- En Windows (desarrollo) y en los tests no se ejecuta `systemctl`.

Código: `bluetooth_audio.py` es el único módulo que llama a `systemctl` (`pause()` / `resume()`);
`AlarmPlaybackManager` lo llama al empezar a sonar y en STOP.

Código: `playback.py` (`AlarmPlaybackManager`) guarda el estado y hace start / stop / snooze con un lock
(los hilos de waitress y de APScheduler no se pisan). El scheduler solo llama a `manager.start(alarm)`;
las vistas solo llaman a `stop()` / `snooze()`. El snooze es un job de APScheduler en memoria.

## Diagnóstico y pre-flight

### Página Diagnóstico

En la sección **Diagnóstico** (`/diagnostics/`) cada componente tiene una tarjeta con su estado
(● verde = ok, ● ámbar = aviso, ● rojo = problema), un resumen y detalles técnicos seguros. Se ve la hora
de la última comprobación y qué la lanzó (al abrir la página, **Comprobar ahora** o un pre-flight). El
último informe también está en `/diagnostics/report.json`.

| Tarjeta | Qué comprueba |
|---|---|
| **Audio USB** | Que la tarjeta de `ALSA_DEVICE` (o `plughw:CARD=Device,DEV=0`) existe y tiene salida, leyendo `/proc/asound`. No abre el dispositivo: no suena nada ni molesta a una alarma que esté sonando. Indica si está libre o en uso. |
| **Spotify** | Si hay cuenta vinculada: una llamada ligera (`GET /me/player/devices`, 5 s máx.) y si el dispositivo de las alarmas (p. ej. «Groove») aparece. Sin cuenta: aviso, no error. |
| **Raspotify** | `systemctl is-active raspotify`. |
| **Música local** | Que `instance/music/` existe y cuántas pistas válidas hay. Vacía = aviso (queda el WAV de emergencia). |
| **ffmpeg** | Que `FFMPEG_BINARY` existe y responde a `ffmpeg -version` (muestra la versión). |
| **WAV de emergencia** | Que el archivo existe, se puede leer y es un WAV válido. Si falta es un **problema**: es el último respaldo. |
| **Bluetooth** | `systemctl is-active` de `bluealsa` y `bluealsa-aplay` y, si se puede, cuántos dispositivos hay conectados (`bluetoothctl devices Connected`). Si el reproductor está parado porque suena una alarma, es normal. |
| **Scheduler** | Que APScheduler está en marcha con el job de las alarmas, la próxima alarma, cuántas hay activas y cuántos pre-flight hay programados. |

Estados:

- **ok**: funciona como se espera.
- **aviso**: funciona a medias, no está configurado o no se puede comprobar en este equipo (p. ej. en
  Windows), y hay alternativa: biblioteca vacía, Spotify sin vincular, «Groove» no aparece...
- **problema**: el componente debería funcionar y no funciona (servicio parado, archivo que falta, API
  caída), o no se ha podido comprobar (tiempo agotado, `systemctl` no responde, error inesperado).

Reglas: todos los checks son **de solo lectura**. No arrancan ni paran servicios, no tocan el
emparejamiento ni la visibilidad Bluetooth, no reproducen sonido, no escriben en la base de datos y no
tocan la reproducción de Spotify (lo único que puede pasar es la renovación normal del token). Cada
comando tiene un timeout de 3 s y el conjunto, 12 s; un check que falla sale como problema y los demás
siguen. La página nunca muestra tokens, el client secret, variables de entorno ni trazas de Python.

### Pre-flight antes de cada alarma

`ALARM_PREFLIGHT_MINUTES` (por defecto **5**) minutos antes de cada alarma activa, Groove ejecuta los
mismos health checks, guarda el resultado como "última comprobación" y escribe un resumen en
`instance/alarms.log`:

```
Pre-flight alarma 12: «Trabajo» 07:30 (spotify): audio=ok spotify=warning raspotify=ok local_music=ok ffmpeg=ok emergency=ok bluetooth=ok scheduler=ok
Pre-flight alarma 12: Spotify puede fallar; hay respaldo: música local (4 pistas).
Pre-flight alarma 12: problemas: spotify=warning (Conectado)
```

Si todo está bien, la última línea es `todo listo.`. Para alarmas locales no se registra Spotify.

**En esta versión el pre-flight solo observa y registra.** No intenta reparar nada: no reinicia
Raspotify ni BlueALSA, no reproduce audio y no cambia la alarma. Aunque todos los checks fallen (o el
propio pre-flight falle), la alarma suena a su hora: la dispara el job `check_alarms` de siempre, que
no depende del pre-flight.

Programación:

- Cada alarma activa tiene un job `preflight:<id>` para su **próxima** vez. Al crear, editar,
  activar/desactivar o borrar una alarma se recalculan al momento; un job `preflight-sync` los revisa
  además cada minuto (programa la siguiente vez de las recurrentes, quita el de las de "una vez" tras
  sonar y corrige cambios de la hora del sistema). No quedan jobs huérfanos.
- Tras reiniciar Groove se vuelven a crear desde la base de datos.
- Si a la alarma le quedan **menos de 5 minutos** (p. ej. la acabas de crear para dentro de 3), esa vez
  no hay pre-flight y la alarma suena con normalidad. Nunca se ejecuta un pre-flight con retraso.
- `ALARM_PREFLIGHT_MINUTES=0` desactiva el pre-flight.
- En el log: `Pre-flight programado para «Trabajo» (alarma 12): 29/09 07:25, antes de la alarma de las
  07:30` y `Pre-flight cancelado para la alarma 12: la alarma ya no está activa`.

## Interfaz y app en el móvil (PWA)

La interfaz está pensada primero para el móvil: oscura, con navegación inferior (**Alarmas** /
**Spotify** / **Música** / **Diagnóstico**) y, en pantallas anchas, navegación arriba y un ancho máximo de lectura.

- **Pantalla principal**: arriba la **próxima alarma** (hora grande y cuánto falta; el sol del
  horizonte sube según se acerca). Debajo, la lista con un interruptor para activar o desactivar cada
  alarma y un menú **⋯** con Editar / Probar / Borrar. El botón **Nueva alarma** está siempre a mano.
- **Alarma sonando**: ocupa la parte superior con un **STOP** enorme y **+10 MIN** como opción
  secundaria. Si está pospuesta: "Pospuesta hasta HH:MM" con **Cancelar**.
- **Formulario**: días como chips **L M X J V S D**, selector **Sonido local / Spotify** (los campos de
  Spotify solo aparecen si eliges Spotify) y deslizadores de volumen con su porcentaje.
- Sin JavaScript todo sigue funcionando con formularios normales. El JS solo mejora (mostrar/ocultar
  campos, porcentajes, menús, comprobar la conexión cada 10 s).
- Sin recursos externos: tipografía del sistema, iconos SVG propios, sin CDN ni Google Fonts.

**Instalar como app**: en el móvil, abre Groove en el navegador y usa **Añadir a pantalla de inicio**
(Safari en iOS, o el menú de Chrome en Android). Se abre a pantalla completa con su icono.

- El *service worker* solo guarda la "carcasa" estática (CSS, JS, iconos). **Nunca** guarda alarmas,
  estado de reproducción, Spotify ni formularios. Si la Raspberry no responde, Groove lo dice ("No hay
  conexión con Groove"); no hay modo offline de mentira.
- Los navegadores solo activan el *service worker* en HTTPS o en `127.0.0.1`/`localhost`. Accediendo
  por `http://<IP-de-la-Pi>:5000` la app funciona igual y se puede añadir a la pantalla de inicio, pero
  sin *service worker* (Android Chrome puede ofrecerlo solo como acceso directo).
- Los iconos se generan con `python tools/make_icons.py` (sin dependencias).

## Estructura

```
app.py              # create_app(), rutas y validación de formularios
db.py               # conexión SQLite y consultas (sin ORM)
scheduler.py        # APScheduler: revisa alarmas cada minuto y las dispara
audio_player.py     # LocalAudioPlayer (WAV de emergencia) y FfmpegPlayer (música local)
music_library.py    # biblioteca instance/music/: pistas, validación, subida, borrado, selección
music_views.py      # rutas /music/... (ver, subir, eliminar)
spotify_client.py   # todo el HTTP con Spotify: OAuth, refresh, errores, búsqueda y metadata
spotify_views.py    # rutas /spotify/... (incluye /spotify/search y /spotify/lookup en JSON)
spotify_player.py   # flujo de alarma Spotify: dispositivo -> transferir -> reproducir
playback.py         # alarma sonando: estado, STOP y snooze
bluetooth_audio.py  # para / arranca bluealsa-aplay alrededor de las alarmas (systemctl)
fade.py             # fade-in de volumen (Spotify)
health.py           # health checks de solo lectura (Diagnóstico y pre-flight)
preflight.py        # jobs preflight:<id> antes de cada alarma y su resumen en el log
diagnostics_views.py # rutas /diagnostics/... (página, Comprobar ahora, report.json)
serve.py            # arranque de producción (waitress, red local)
deploy/             # plantilla systemd + install-service.sh; regla polkit de Bluetooth
.env.example        # plantilla de configuración (copiar a .env)
schema.sql          # tablas "alarms", "spotify_auth" y "settings"
sounds/             # alarm.wav, el WAV de emergencia (no se sube a git)
tools/              # make_test_sound.py (WAV de prueba), make_icons.py (iconos PWA)
ui.py               # ayudas de presentación (próxima alarma, textos)
templates/          # HTML (Jinja2)
static/             # CSS y JS vanilla (confirmar borrado, buscador de Spotify...)
tests/              # tests con unittest
instance/           # base de datos, log y music/ (no se suben a git)
```

Cada alarma guarda `name`, `time` (`HH:MM`), `days` (p. ej. `"0,2,4"`, donde 0 = lunes y vacío = una vez),
`enabled`, `last_triggered`, `source` (`local` o `spotify`), `spotify_uri`,
`spotify_name` y `spotify_subtitle` (nombre legible del contenido, solo para mostrar; vacío en
alarmas antiguas), `volume_start`, `volume_end`, `fade_minutes` `max_duration_minutes` (auto-stop; 0 = sin límite) y `local_track` (música local; vacío = aleatoria).

## Próximos pasos

- Volumen progresivo y botón para parar o posponer la alarma.
- librespot para que la propia Pi sea un dispositivo Spotify Connect.
- Protección CSRF y un `SECRET_KEY` real (variable de entorno `SECRET_KEY`) si la app se expone fuera de la red local.
