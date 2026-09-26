# pi-music-alarm

Despertador ligero pensado para una **Raspberry Pi Zero 2 W**. Interfaz web (pensada para móvil) hecha con Flask y SQLite.

Estado actual: gestión de alarmas (crear, listar, activar/desactivar, borrar, probar) y un
scheduler que las dispara a su hora. Al dispararse, la alarma reproduce un WAV local y escribe
`ALARMA ACTIVADA: <nombre>` en la consola y en `instance\alarms.log`.
Spotify llegará más adelante.

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

## Estructura

```
app.py              # create_app(), rutas y validación de formularios
db.py               # conexión SQLite y consultas (sin ORM)
scheduler.py        # APScheduler: revisa alarmas cada minuto y las dispara
audio_player.py     # AudioPlayer / LocalAudioPlayer (winsound o aplay)
schema.sql          # tabla "alarms"
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

- `SpotifyAudioPlayer`.
- Volumen progresivo y botón para parar o posponer la alarma.
- En la Pi: servir con un servidor de producción ligero (p. ej. `waitress`) y un servicio `systemd`.
- Protección CSRF y un `SECRET_KEY` real (variable de entorno `SECRET_KEY`) si la app se expone fuera de la red local.
