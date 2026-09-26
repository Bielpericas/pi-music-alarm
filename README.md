# pi-music-alarm

Despertador ligero pensado para una **Raspberry Pi Zero 2 W**. Interfaz web (pensada para móvil) hecha con Flask y SQLite.

Estado actual: gestión de alarmas (crear, listar, activar/desactivar, borrar, probar) y un
scheduler que las dispara a su hora. Al dispararse, la alarma reproduce un WAV local y escribe
`ALARMA ACTIVADA: <nombre>` en la consola y en `instance\alarms.log`.
También se puede vincular una cuenta de Spotify y controlar sus dispositivos (todavía sin relación con las alarmas).

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

### Tests

```powershell
python -m unittest discover tests -v
```

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
  `create_player()` elige la implementación según `AUDIO_BACKEND` (`local` o `none`). Aquí se
  enchufará un futuro `SpotifyAudioPlayer`.

### Probar una alarma programada

1. Arranca con `python app.py` y mira la hora actual del PC.
2. Crea una alarma para 1–2 minutos después, con el día de hoy marcado (o sin días = una vez).
3. Espera: al llegar el minuto (en el segundo 0) sonará el WAV y verás en la consola
   `ALARMA ACTIVADA: <nombre>`, y la misma línea en `instance\alarms.log`.
4. Recarga la página: la alarma muestra "Última vez: …".

La hora que se usa es la del sistema. En la Pi, configura la zona horaria con `sudo raspi-config`.

## Spotify (fase 1: vincular cuenta y controlar dispositivos)

De momento Spotify **no** está conectado con las alarmas. La sección **Spotify** (enlace arriba a la
derecha) permite vincular tu cuenta, ver tus dispositivos Spotify Connect, elegir uno y probar
Transferir / Play / Pause.

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

## Estructura

```
app.py              # create_app(), rutas y validación de formularios
db.py               # conexión SQLite y consultas (sin ORM)
scheduler.py        # APScheduler: revisa alarmas cada minuto y las dispara
audio_player.py     # AudioPlayer / LocalAudioPlayer (winsound o aplay)
spotify_client.py   # todo el HTTP con Spotify: OAuth, refresh, errores
spotify_views.py    # rutas /spotify/...
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
`enabled` y `last_triggered`.

## Próximos pasos

- `SpotifyAudioPlayer`: usar Spotify como sonido de alarma (con el WAV local de respaldo).
- Volumen progresivo y botón para parar o posponer la alarma.
- En la Pi: servir con un servidor de producción ligero (p. ej. `waitress`) y un servicio `systemd`.
- Protección CSRF y un `SECRET_KEY` real (variable de entorno `SECRET_KEY`) si la app se expone fuera de la red local.
