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
sudo apt install -y git python3 python3-venv python3-pip alsa-utils
```

- `python3-venv`: crear el entorno virtual.
- `alsa-utils`: incluye `aplay`, que reproduce el WAV.
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

Al crear o editar una alarma, en **Sonido** elige **Local (WAV)** o **Spotify**. Con Spotify, pega la
URL (`https://open.spotify.com/playlist/...`, con o sin `?si=...`) o la URI (`spotify:playlist:...`)
de una canción, un álbum o una playlist. Se guarda siempre como URI.

Cuando se dispara, o al pulsar **Probar**, que ejecuta exactamente el mismo flujo:

1. Se lee el dispositivo seleccionado en la sección Spotify.
2. Se transfiere la reproducción a ese dispositivo.
3. Se reproduce el contenido: una canción va en `uris`; un álbum o playlist, como `context_uri`.

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
- El WAV local suena una vez (no se repite en bucle); el recuadro sigue visible hasta STOP o +10 MIN.

Código: `playback.py` (`AlarmPlaybackManager`) guarda el estado y hace start / stop / snooze con un lock
(los hilos de waitress y de APScheduler no se pisan). El scheduler solo llama a `manager.start(alarm)`;
las vistas solo llaman a `stop()` / `snooze()`. El snooze es un job de APScheduler en memoria.

## Estructura

```
app.py              # create_app(), rutas y validación de formularios
db.py               # conexión SQLite y consultas (sin ORM)
scheduler.py        # APScheduler: revisa alarmas cada minuto y las dispara
audio_player.py     # AudioPlayer / LocalAudioPlayer (winsound o aplay)
spotify_client.py   # todo el HTTP con Spotify: OAuth, refresh, errores
spotify_views.py    # rutas /spotify/...
spotify_player.py   # flujo de alarma Spotify: dispositivo -> transferir -> reproducir
playback.py         # alarma sonando: estado, STOP y snooze
fade.py             # fade-in de volumen (Spotify)
serve.py            # arranque de producción (waitress, red local)
deploy/             # plantilla systemd + install-service.sh
.env.example        # plantilla de configuración (copiar a .env)
schema.sql          # tablas "alarms", "spotify_auth" y "settings"
sounds/             # alarm.wav (no se sube a git)
tools/              # make_test_sound.py: genera un WAV de prueba
templates/          # HTML (Jinja2)
static/             # CSS y un poco de JS (confirmar borrado)
tests/              # tests con unittest
instance/           # base de datos y log local (no se suben a git)
```

Cada alarma guarda `name`, `time` (`HH:MM`), `days` (p. ej. `"0,2,4"`, donde 0 = lunes y vacío = una vez),
`enabled`, `last_triggered`, `source` (`local` o `spotify`), `spotify_uri`,
`volume_start`, `volume_end` y `fade_minutes`.

## Próximos pasos

- Buscar canciones o playlists desde la app, en vez de pegar la URL.
- Volumen progresivo y botón para parar o posponer la alarma.
- librespot para que la propia Pi sea un dispositivo Spotify Connect.
- Protección CSRF y un `SECRET_KEY` real (variable de entorno `SECRET_KEY`) si la app se expone fuera de la red local.
