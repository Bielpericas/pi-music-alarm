# Arquitectura de Groove

Este documento explica **cómo está construido Groove y por qué**: el hardware, lo que hay instalado en
la Raspberry, cómo se organiza el código y las decisiones de diseño que hay detrás de cada parte.

No es una guía de instalación ni de uso: para eso está el [README](../README.md). Aquí se cuenta lo
que no se ve desde fuera: qué piezas hay, cómo hablan entre sí y qué problema resuelve cada decisión.

Documentos relacionados:

- [README](../README.md): instalación, configuración y uso, con todo el detalle operativo.
- [docs/https-spotify-oauth.md](https-spotify-oauth.md): acceso por red, vinculación de Spotify por
  túnel SSH y el HTTPS pendiente.
- [docs/spotify-guest-mode.md](spotify-guest-mode.md): instalación, prueba y reversión del modo
  invitados de Spotify.

Estado descrito: octubre de 2026 (commit `161e105`).

---

## Índice

1. [Qué es Groove](#1-qué-es-groove)
2. [Hardware](#2-hardware)
3. [Sistema operativo y software instalado](#3-sistema-operativo-y-software-instalado)
4. [Vista general de la arquitectura](#4-vista-general-de-la-arquitectura)
5. [Recorrido por el código, módulo a módulo](#5-recorrido-por-el-código-módulo-a-módulo)
6. [Flujos principales](#6-flujos-principales)
7. [Base de datos](#7-base-de-datos)
8. [Concurrencia y estado en memoria](#8-concurrencia-y-estado-en-memoria)
9. [Seguridad y permisos](#9-seguridad-y-permisos)
10. [Interfaz web y PWA](#10-interfaz-web-y-pwa)
11. [Decisiones de diseño (resumen razonado)](#11-decisiones-de-diseño-resumen-razonado)
12. [Tests](#12-tests)
13. [Despliegue y mantenimiento](#13-despliegue-y-mantenimiento)
14. [Fallos conocidos, incidencias y lecciones aprendidas](#14-fallos-conocidos-incidencias-y-lecciones-aprendidas)
15. [Trabajo pendiente](#15-trabajo-pendiente)
16. [Historia del proyecto](#16-historia-del-proyecto)

---

## 1. Qué es Groove

Groove (el repositorio se llama `pi-music-alarm`) es un **despertador musical** que funciona en una
Raspberry Pi Zero 2 W y se controla desde el móvil con una web. Además del despertador, la Pi hace de
**altavoz de la casa**:

| Función | Cómo |
|---|---|
| Despertador | Alarmas programadas que suenan con Spotify, con música local o con un WAV de emergencia. |
| Altavoz Spotify Connect | Raspotify (librespot): la Pi aparece en Spotify como el dispositivo «Groove». |
| Altavoz Bluetooth | BlueZ + BlueALSA: el móvil se empareja y reproduce por la Pi. |
| Temporizador de sueño | Para la música puesta a mano (Spotify o Bluetooth) al cabo de 15–60 min. |
| Spotify invitados | Permite que otras cuentas de Spotify de la red local usen Groove. |
| Diagnóstico | Página de salud de todos los componentes y un *pre-flight* antes de cada alarma. |

El principio que guía todo el diseño es una sola frase:

> **Una alarma tiene que sonar siempre.** Si algo falla, debe sonar *otra cosa*; nunca silencio.

Casi todas las decisiones de este documento se explican por esa regla: la cadena de respaldo, que el
pre-flight no pueda impedir una alarma, que los fallos de Bluetooth o de systemd nunca bloqueen el
sonido, que la alarma tenga prioridad sobre todo lo demás, etc.

---

## 2. Hardware

### 2.1 Componentes

| Componente | Detalle | Por qué |
|---|---|---|
| **Raspberry Pi Zero 2 W** | 4 núcleos ARM Cortex-A53, **512 MB de RAM**, Wi-Fi y Bluetooth integrados (chip Broadcom). | Pequeña, barata, bajo consumo, siempre encendida. Tiene Wi-Fi y Bluetooth sin añadir nada. |
| **Tarjeta de sonido USB** | Se ve en ALSA como `CARD=Device` (`plughw:CARD=Device,DEV=0`), conectada con un adaptador micro-USB OTG. | La Zero 2 W **no tiene salida jack**. Las alternativas eran HDMI (necesita pantalla/TV), un DAC I2S (HAT) o Bluetooth. La USB es la más barata y simple. |
| **Altavoces** | Conectados a la tarjeta USB. El volumen físico se ajusta con el control ALSA `Speaker` (`amixer sget Speaker`). | El volumen físico es común a Spotify, Bluetooth y alarmas. |
| **Tarjeta microSD** | Raspberry Pi OS Lite. | — |
| **Sin reloj con pila (RTC)** | La hora llega por NTP al arrancar. | Limitación del hardware: obliga a esperar a la sincronización de la hora (ver §3.3). |

### 2.2 Consecuencias del hardware en el diseño

El hardware condiciona mucho el código:

- **512 MB de RAM** → dependencias mínimas: solo 4 paquetes de Python, sin ORM, sin SDK de Spotify
  (HTTP con `urllib` de la librería estándar), sin frameworks de frontend (JS *vanilla*), un único
  proceso y un log rotativo pequeño (256 KB × 3).
- **Una sola tarjeta de sonido USB que solo puede abrir un programa a la vez** (con `plughw:` no hay
  mezcla por software). Tres programas quieren usarla: `aplay`/`ffmpeg` (alarmas), `librespot`
  (Spotify) y `bluealsa-aplay` (Bluetooth). De aquí sale la **prioridad ALARMA > SPOTIFY > BLUETOOTH**
  y que Groove pare `bluealsa-aplay` antes de cada alarma (ver §6.4).
- **Sin RTC** → las alarmas usan la hora del sistema, el servicio espera a `time-sync.target` y el
  pre-flight se recalcula cada minuto por si la hora cambia de golpe al sincronizar.
- **Sin pantalla ni teclado** → el emparejamiento Bluetooth necesita un agente `NoInputNoOutput` que
  acepte solo, y la ventana de emparejamiento se controla desde la web (ver §6.6).
- **Chip Bluetooth integrado** → puede colgarse (ver §14.1); el diseño separa el adaptador BlueZ del
  reproductor de audio para que un fallo de uno no afecte al otro.

### 2.3 Red

- La Pi está en la red local con **IP fija por reserva DHCP**: `192.168.0.21`.
- Groove se sirve por **HTTP** en el puerto 5000: `http://192.168.0.21:5000`.
- Acceso por SSH con el alias `Groove` (definido en el `~/.ssh/config` del equipo del desarrollador).
- No se expone a Internet. Esto es una decisión consciente: no hay login ni CSRF general porque la
  red local se considera de confianza (ver §9 y §15).

---

## 3. Sistema operativo y software instalado

### 3.1 Sistema operativo

**Raspberry Pi OS Lite de 64 bits** (Bookworm con Python 3.11, o Trixie con Python 3.13). Versión
*Lite*, sin escritorio: con 512 MB de RAM no sobra nada.

### 3.2 Paquetes del sistema

| Paquete | Para qué lo usa Groove |
|---|---|
| `python3`, `python3-venv`, `python3-pip` | Ejecutar la app en un entorno virtual (`.venv`). |
| `git` | Clonar y actualizar el repositorio (`git pull`). |
| `alsa-utils` | `aplay` (WAV de emergencia), `aplay -l/-L` (tarjetas), `alsamixer`/`amixer` (volumen físico). |
| `ffmpeg` | Reproducir la música local (MP3, OGG, WAV) directamente a ALSA. |
| `bluez` | Pila Bluetooth (`bluetoothd`) y `bluetoothctl`, con el que Groove gestiona dispositivos. |
| `bluez-alsa-utils` | `bluealsa` (puente BlueZ ↔ ALSA) y `bluealsa-aplay` (reproduce el audio del móvil). |
| `polkitd` | Reglas de permisos mínimos para que Groove pare/arranque servicios **sin sudo**. |
| **Raspotify** | Empaqueta `librespot` (cliente Spotify Connect abierto) como servicio systemd `raspotify`. |

### 3.3 Servicios systemd

```
                    arranque de la Pi
                           │
        ┌──────────────────┼──────────────────────────┐
        ▼                  ▼                          ▼
 systemd-time-wait-sync   bluetooth (bluetoothd)   groove-spotify-boot   (oneshot, root)
        │                  │                          │  aplica modo PRIVADO a Raspotify
        │                  ▼                          ▼
        │             bluealsa ──► bluealsa-aplay   raspotify (librespot, «Groove»)
        │                                              │
        └──────────────► pi-music-alarm ◄──────────────┘   (Groove arranca después)
                         (Flask + waitress + APScheduler, usuario normal)
```

| Unidad | Quién la instala | Qué hace |
|---|---|---|
| `systemd-time-wait-sync.service` | systemd (activado a mano) | Hace que `time-sync.target` espere a que NTP haya puesto la hora. |
| `pi-music-alarm.service` | `deploy/install-service.sh` | La app. Usuario normal (nunca root), `Restart=on-failure`, espera red y hora. |
| `raspotify.service` | paquete Raspotify | `librespot`: la Pi como dispositivo Spotify Connect «Groove». |
| `bluetooth.service` | paquete `bluez` | `bluetoothd`. |
| `bluealsa.service` | `bluez-alsa-utils` | Expone los dispositivos Bluetooth como PCM de ALSA. |
| `bluealsa-aplay.service` | `bluez-alsa-utils` + *override* | Reproduce lo que llega por Bluetooth en la tarjeta USB (`-D plughw:CARD=Device,DEV=0 --single-audio`). |
| `groove-spotify-boot.service` | `install-spotify-guest-mode.sh` | Antes de Raspotify, fuerza el modo privado aunque Groove no arranque. |
| `groove-spotify-mode@{guest,private,status}.service` | `install-spotify-guest-mode.sh` | Operaciones fijas del helper root del modo invitados. |

Además, *drop-ins* de systemd:

- `raspotify.service.d/80-groove-spotify.conf`: Raspotify **requiere** el guard de arranque.
- `pi-music-alarm.service.d/80-groove-spotify.conf`: Groove arranca **después** de Raspotify.
- Runtime: `/run/systemd/system/raspotify.service.d/90-groove-spotify-mode.conf` (el guard que aísla
  credenciales según el modo, ver §6.8).

### 3.4 Configuración de Raspotify (librespot)

En `/etc/raspotify/conf`:

- El nombre del dispositivo Spotify Connect es **«Groove»**: es el que Groove busca para las alarmas.
- `LIBRESPOT_VOLUME_CTRL=cubic`: curva de volumen cúbica. **Decisión**: con `linear` el 50 % solo
  baja 6 dB y Spotify directo suena mucho más fuerte que el mismo porcentaje por Bluetooth; con `log`
  (60 dB de rango) el 50 % baja 30 dB y queda demasiado bajo. Con `cubic` (30 % ≈ −31 dB, 50 % ≈
  −18 dB, 80 % ≈ −6 dB) un mismo porcentaje suena parecido por las dos vías. Por eso el volumen final
  típico de una alarma está entre el 70 y el 90 %.
- `LIBRESPOT_ENABLE_VOLUME_NORMALISATION=` (activada): igual que hace la app de Spotify por defecto.
- **Nunca** `LIBRESPOT_VOLUME_CTRL=fixed`: con él no funcionaría el fade-in.
- En modo privado: usuario fijo, credenciales cacheadas en `/var/cache/raspotify` y *discovery*
  (Zeroconf) desactivado. En modo invitados cambian estas opciones (ver §6.8).

### 3.5 Python: dependencias

`requirements.txt` tiene **solo cuatro** dependencias, todas en Python puro o con *wheel* para ARM (no
hay que compilar nada en la Pi):

| Paquete | Uso |
|---|---|
| `Flask` 3.1 | Web: rutas, plantillas Jinja2, sesiones. |
| `APScheduler` 3.11 | Scheduler en un hilo: alarmas, snoozes, auto-stop, pre-flight, temporizador de sueño. |
| `python-dotenv` | Leer `.env`. |
| `waitress` | Servidor WSGI de producción, multihilo y en Python puro. |

Todo lo demás es librería estándar: `sqlite3`, `urllib`, `subprocess`, `threading`, `wave`, `pty`…

### 3.6 Ficheros que no están en git

| Ruta | Contenido |
|---|---|
| `.env` | Secretos y configuración (`SECRET_KEY`, credenciales de la app de Spotify, `ALSA_DEVICE`…). `chmod 600`. |
| `instance/alarms.db` | SQLite: alarmas, tokens de Spotify y ajustes. |
| `instance/alarms.log` | Log de alarmas (rotativo). |
| `instance/music/` | Biblioteca de música local. |
| `sounds/alarm.wav` | WAV de emergencia (se genera con `tools/make_test_sound.py`). |

---

## 4. Vista general de la arquitectura

### 4.1 Diagrama de capas

```
 Navegador (móvil / PC)
   │  HTTP :5000  (formularios + un poco de JS; JSON para estado y buscador)
   ▼
┌────────────────────────────────────────────────────────────────────────────┐
│  serve.py  → waitress (4 hilos)  → app.create_app()                        │
│                                                                            │
│  VISTAS (blueprints)                                                       │
│   app.py (alarmas, playback)  spotify_views  music_views  bluetooth_views  │
│   diagnostics_views  sleep_timer_views                                     │
│                                                                            │
│  NÚCLEO                                                                    │
│   scheduler.py ──► playback.AlarmPlaybackManager ◄── STOP / +10 / Probar   │
│        │                 │  (único punto por el que empieza una alarma)    │
│        │                 ├─► spotify_player ─► spotify_client ─► Spotify API│
│        │                 ├─► music_library.LocalMusic ─► FfmpegPlayer      │
│        │                 ├─► audio_player.LocalAudioPlayer (WAV)           │
│        │                 ├─► fade.VolumeFade                               │
│        │                 ├─► bluetooth_audio (para/arranca bluealsa-aplay) │
│        │                 └─► sleep_timer (se anula al empezar una alarma)  │
│        ├─► preflight ─► health (solo lectura)                              │
│        └─► jobs en memoria: snooze, auto-stop, sleep timer                 │
│                                                                            │
│   bluetooth_manager ─► bluetoothctl (BlueZ)                                │
│   spotify_guest ─► systemctl start groove-spotify-mode@… (helper root)     │
│   db.py ─► SQLite (instance/alarms.db)                                     │
└────────────────────────────────────────────────────────────────────────────┘
   │ subprocess (sin shell)                     │ HTTPS
   ▼                                            ▼
 aplay · ffmpeg · systemctl · bluetoothctl    api.spotify.com / accounts.spotify.com
```

### 4.2 Principios de arquitectura

1. **Un solo proceso.** `serve.py` arranca waitress con un único proceso. El scheduler vive dentro y
   arranca **una sola vez**. En desarrollo (`python app.py`), el recargador de Flask crea dos procesos,
   y `create_app` comprueba `is_running_from_reloader()` para no arrancar dos schedulers.
2. **La base de datos es la única fuente de verdad** de las alarmas. El scheduler no guarda una copia:
   cada minuto pregunta a SQLite qué toca. Crear, editar o borrar alarmas no requiere sincronizar nada.
3. **Un único punto de entrada para que suene una alarma**: `AlarmPlaybackManager.start()`. El
   scheduler, el botón Probar y la vuelta de un snooze pasan todos por ahí. Eso permite garantizar
   reglas globales (parar Bluetooth, anular el temporizador de sueño, una sola alarma a la vez…) en un
   único sitio.
4. **Un módulo por cada sistema externo.** Cada uno habla con *una* cosa y nadie más lo hace:

   | Sistema externo | Único módulo que lo toca |
   |---|---|
   | API web de Spotify | `spotify_client.py` |
   | `bluealsa-aplay` (systemctl start/stop) | `bluetooth_audio.py` |
   | BlueZ (`bluetoothctl`) | `bluetooth_manager.py` |
   | Carpeta `instance/music/` | `music_library.py` |
   | SQLite | `db.py` (y `SqliteTokenStore` en `spotify_client.py` para los tokens) |
   | Configuración de Raspotify | `deploy/spotify-mode-helper.py` (root, fuera del checkout) |
   | Reinicio de Raspotify | `preflight.py` (solo `restart`, solo en un caso) |

5. **Nada lanza excepciones hacia el núcleo.** Los reproductores (`play()`), Bluetooth, el fade, el
   temporizador de sueño y el pre-flight registran los fallos y devuelven `False` o un estado. Un error
   en una pieza secundaria nunca puede impedir que suene una alarma.
6. **Inyección de dependencias + objetos nulos.** `create_app(player=…, spotify=…, bluetooth=…,
   music=…, bluetooth_manager=…, spotify_guest=…)` acepta dobles para los tests. En Windows o en tests
   se usan implementaciones vacías (`NullAudioPlayer`, `NoBluetooth`, `NoBluetoothManager`,
   `NoSpotifyGuest`) en lugar de llenar el código de `if`.
7. **Funciona igual en Windows y en la Pi.** En Windows (desarrollo) el WAV suena con `winsound`, no se
   usa ffmpeg y no se toca systemd ni BlueZ. Así se puede desarrollar y probar todo en el PC.

---

## 5. Recorrido por el código, módulo a módulo

Unas 6.200 líneas de Python, más ~2.500 de plantillas, CSS y JS.

### 5.1 Arranque y web

| Módulo | Responsabilidad |
|---|---|
| `serve.py` | Arranque de producción: carga `.env` con ruta explícita (con systemd el directorio de trabajo podría ser otro), valida `HOST`/`PORT`/`THREADS` y lanza waitress. Avisa si falta `SECRET_KEY`. |
| `app.py` | `create_app()`: lee la configuración del entorno, construye todos los componentes, los conecta entre sí, registra blueprints y define las rutas de alarmas (crear/editar/activar/borrar/probar) y de reproducción (STOP, snooze, `/playback/state`). Contiene la validación del formulario de alarma. |
| `ui.py` | Solo presentación: próxima alarma, cuenta atrás, «Hoy/Mañana/El jueves», altura del sol, tamaños legibles, etiquetas. No toca BD ni scheduler. |
| `*_views.py` | Un blueprint por sección (`/spotify`, `/music`, `/bluetooth`, `/diagnostics`, `/sleep-timer`). Solo traducen HTTP ↔ llamadas al módulo correspondiente y errores ↔ mensajes. |

**Orden de construcción en `create_app`** (importa):

1. Modo invitados (`spotify_guest`) y **base de datos**. Si la BD falla, antes de abortar se intenta
   dejar Spotify en modo privado.
2. Logging, reproductor WAV, biblioteca de música, `FfmpegPlayer`, cliente de Spotify.
3. `guests.reconcile()`: aplicar el modo invitados guardado **antes** de programar alarmas.
4. Bluetooth (`bluetooth_audio` y `bluetooth_manager`) y el **fail-safe** que deja BlueZ en privado (en
   segundo plano, para no retrasar el arranque).
5. `SpotifyAlarmPlayer` (con `before_play=guests.before_alarm`), `AlarmPlaybackManager`,
   `SleepTimerManager` (enganchado a `playback.on_alarm_start`), `HealthChecker`, `PreflightScheduler`.
6. El scheduler, solo si procede (no en tests, no en el proceso vigilante del recargador).

### 5.2 Programación de alarmas

| Módulo | Responsabilidad |
|---|---|
| `scheduler.py` | `BackgroundScheduler` de APScheduler. Un job `check_alarms` cada minuto en el segundo 0. Configura el log (`instance/alarms.log`, rotativo). Ofrece `date_job_scheduler()`, la fábrica de jobs de una sola vez para snoozes, auto-stop y temporizador. |
| `db.py` | SQLite sin ORM. Migraciones por `ALTER TABLE ADD COLUMN`. `claim_trigger()` atómico para no disparar dos veces. |
| `preflight.py` | Jobs `preflight:<id>` antes de cada alarma y `preflight-sync` cada minuto (segundo 30). Recuperación de Raspotify. |

### 5.3 Reproducción

| Módulo | Responsabilidad |
|---|---|
| `playback.py` | `AlarmPlaybackManager`: la alarma que suena. `start`, `stop`, `snooze`, `cancel_snooze`, `forget`, auto-stop, estado (`connecting/playing/failed/stop_pending/finished/cancelled`). `play_alarm_sound()` implementa la cadena de respaldo. |
| `spotify_player.py` | `SpotifyAlarmPlayer`: resolver dispositivo → transferir → volumen inicial → reproducir, con reintentos, arranque «en frío» y pista inicial aleatoria. `stop()` pausa el dispositivo donde empezó. |
| `audio_player.py` | `LocalAudioPlayer` (WAV de emergencia con `aplay`/`winsound`, valida la cabecera WAV antes) y `FfmpegPlayer` (pistas en bucle con `ffmpeg … -f alsa`). Vigilan el proceso y avisan si termina. |
| `music_library.py` | `MusicLibrary` (carpeta plana `instance/music/`: listar, validar, subir, borrar) y `LocalMusic` (elige la pista de la alarma o una al azar y la reproduce). |
| `fade.py` | `fade_plan()` calcula los pasos (cada 15 s); `VolumeFade` los aplica en un hilo cancelable con un `Event`. |
| `bluetooth_audio.py` | `pause()`/`resume()` de `bluealsa-aplay` con `systemctl --no-ask-password`. Solo rearranca si lo paró Groove. |
| `sleep_timer.py` | `SleepTimerManager`: un temporizador ligado a una fuente concreta (ID de dispositivo Spotify o MAC Bluetooth). |

### 5.4 Spotify

| Módulo | Responsabilidad |
|---|---|
| `spotify_client.py` | **Todo** el HTTP con Spotify, con `urllib`. OAuth (Authorization Code Flow), refresco del token 60 s antes de caducar o tras un 401, errores tipados, 429 con `Retry-After`, búsqueda, metadata, validación de URIs/URLs (`parse_spotify_uri`). |
| `spotify_views.py` | Página Spotify: vincular/desvincular, dispositivos, transferir/play/pause, buscador JSON (`/spotify/search`, `/spotify/lookup`) y el interruptor de invitados (`/spotify/guest`, con CSRF). |
| `spotify_guest.py` | Lado sin privilegios del modo invitados: guarda un booleano en SQLite, lanza `systemctl start groove-spotify-mode@<modo>.service` y lee `/run/groove-spotify/status.json`. No toca credenciales. |
| `deploy/spotify-mode-helper.py` | Lado root del modo invitados (ver §6.8). |

### 5.5 Bluetooth

| Módulo | Responsabilidad |
|---|---|
| `bluetooth_manager.py` | Estado del adaptador, dispositivos (emparejados/de confianza/conectados), conectar/desconectar/confiar/olvidar y la **ventana de emparejamiento** de 2 minutos con un agente `bluetoothctl` en un pseudo-terminal. |
| `bluetooth_views.py` | Rutas `/bluetooth/…`; valida MAC y que el dispositivo sea conocido. |

Separación deliberada:

```
BluetoothManager ──► BlueZ (bluetoothctl)          ← dispositivos, visibilidad, emparejamiento
BluetoothAudio   ──► bluealsa-aplay (systemctl)    ← solo el reproductor de audio
```

Parar el reproductor para una alarma **nunca** desconecta ni desempareja el móvil, y gestionar
dispositivos nunca toca el reproductor.

### 5.6 Diagnóstico

| Módulo | Responsabilidad |
|---|---|
| `health.py` | `HealthChecker`: 8 checks **de solo lectura** en paralelo (audio USB, Spotify, Raspotify, música local, ffmpeg, WAV de emergencia, Bluetooth, scheduler) con timeouts (3 s por comando, 5 s Spotify, 12 s en total). Algunos resultados llevan un `code` legible por máquina (p. ej. `device_missing`). |
| `diagnostics_views.py` | Página, botón «Comprobar ahora» y `/diagnostics/report.json`. |

### 5.7 Frontend y utilidades

| Ruta | Contenido |
|---|---|
| `templates/` | Jinja2: `base.html` (estructura y navegación), `index.html` (próxima alarma, lista, alarma sonando), `alarm_form.html`, `spotify.html`, `music.html`, `bluetooth.html`, `diagnostics.html`, `_sleep_timer.html` (tarjeta común), `_icons.html` (iconos SVG). |
| `static/app.js` | JS *vanilla*: mostrar/ocultar campos, porcentajes de los deslizadores, menús, buscador de Spotify, cuenta atrás, polling de estado. |
| `static/style.css` | Estilos propios, tema oscuro, *mobile first*. |
| `static/sw.js`, `manifest.webmanifest`, `offline.html`, `icons/` | PWA. |
| `tools/make_test_sound.py` | Genera `sounds/alarm.wav` (tres pitidos) sin dependencias. |
| `tools/make_icons.py` | Genera los PNG de los iconos de la PWA sin dependencias. |

---

## 6. Flujos principales

### 6.1 Disparo de una alarma programada

```
APScheduler, cada minuto en el segundo 0
  └─ check_alarms(db)
       ├─ SELECT alarmas activas con time = "HH:MM"
       ├─ ¿hoy es uno de sus días? (vacío = una vez)
       ├─ claim_trigger(): UPDATE … SET last_triggered = "YYYY-MM-DD HH:MM"
       │      WHERE last_triggered <> ese minuto     ← atómico: solo gana una vez
       │      (y si es «una vez», enabled = 0)
       └─ AlarmPlaybackManager.start(alarm)
```

Decisiones:

- **Un solo job cada minuto, no un job por alarma.** Con un job por alarma habría que mantener
  sincronizados SQLite y APScheduler al crear, editar, borrar o reiniciar. Con un único job que lee la
  BD, eso no existe: la BD es la verdad y el scheduler no tiene estado.
- **`claim_trigger` atómico** evita disparar dos veces el mismo minuto aunque el job se ejecute dos
  veces o haya dos procesos (p. ej. en desarrollo).
- **`misfire_grace_time=30`, `coalesce=True`, `max_instances=1`**: si el job llega con algo de retraso
  aún se ejecuta, pero no se acumulan ejecuciones.
- **Las alarmas perdidas no se recuperan.** Si Groove estaba apagado a esa hora, no suena después.
  Sonar a una hora que no es la programada se considera peor que no sonar.
- El job corre en el pool de hilos de APScheduler: si Spotify tarda, no bloquea al scheduler.

### 6.2 `AlarmPlaybackManager.start()`: qué pasa cuando empieza a sonar

Todo dentro de un único lock (`RLock`):

1. Si hay una **parada pendiente** (un STOP que falló), no se empieza otra alarma: `"stop_pending"`.
2. Sube el **token/generación** (identifica esta reproducción).
3. Avisa a `on_alarm_start` → **el temporizador de sueño se anula** antes de hacer ningún ruido.
4. Cancela el fade y el auto-stop de la alarma anterior, si la había, y su snooze pendiente.
5. Si sonaba otra alarma en local, la para («la última gana»).
6. Publica el estado `connecting` (la página ya muestra «Conectando con Spotify…» y STOP funciona).
7. **Para `bluealsa-aplay`** para liberar la tarjeta USB.
8. Ejecuta la **cadena de respaldo** (§6.3) con el volumen inicial.
9. Si la anterior sonaba en Spotify y la nueva no, pausa Spotify.
10. Arranca el **fade-in** (solo Spotify) y programa el **auto-stop**.

### 6.3 Cadena de respaldo

```
           fuente = spotify                        fuente = local
                 │                                       │
                 ▼                                       │
   SpotifyAlarmPlayer.play(uri, volume)                  │
     ├─ ok ──────────────────────► SUENA SPOTIFY         │
     └─ falla / no configurado                           │
           │ (¿STOP pulsado mientras? → "cancelled")     │
           ▼                                             ▼
   Música local: pista elegida o aleatoria (ffmpeg, en bucle)
     ├─ ok ──────────────────────► SUENA MÚSICA LOCAL ("local" o "fallback")
     └─ biblioteca vacía / pista borrada / ffmpeg falla al arrancar
           ▼
   WAV de emergencia (aplay sounds/alarm.wav)
     ├─ ok ──────────────────────► SUENA EL WAV
     └─ falla ───────────────────► "failed" (se muestra «fallo de sonido»)
```

- **El WAV de emergencia** es la última garantía: no depende de red, de Spotify ni de ffmpeg; solo de
  `aplay` (que viene con el sistema) y de un fichero que se valida.
- `FfmpegPlayer` espera **0,5 s** tras lanzar ffmpeg para detectar fallos de arranque (fichero corrupto,
  tarjeta ocupada) y pasar al WAV en vez de dar por buena una reproducción que no existe.
- Si ffmpeg muere **a mitad** de la alarma, se intenta el WAV sin reiniciar el plazo del auto-stop.
- La pista aleatoria se elige **cada vez** que la alarma empieza a sonar (también tras un snooze).
- En la BD solo se guarda el nombre del fichero (`local_track`); si se borra, la alarma no se rompe.

### 6.4 Prioridad de audio: ALARMA > SPOTIFY > BLUETOOTH

La tarjeta USB solo admite un programa a la vez. Groove resuelve el conflicto así:

| Momento | `bluealsa-aplay` |
|---|---|
| Empieza a sonar una alarma (programada, Probar o vuelta de snooze) | **se para** antes de reproducir |
| La alarma cae al respaldo local | sigue parado |
| Otra alarma sustituye a la que sonaba | sigue parado (no se arranca entre medias) |
| STOP / auto-stop / borrar la alarma | se **vuelve a arrancar** |
| +10 MIN | se arranca durante el snooze y se para al volver a sonar |

- Solo se rearranca **si lo paró Groove**: si estaba parado a propósito, sigue parado.
- Solo se toca el reproductor, nunca la conexión Bluetooth: el móvil sigue emparejado y conectado.
- Si `systemctl` falla, se registra y **la alarma suena igual**.
- Spotify (librespot) no necesita que se le pare: cuando la alarma es de Spotify, la propia alarma usa
  librespot; cuando es local, Spotify estará en pausa o sonando en otro dispositivo.

### 6.5 Alarma Spotify

```
SpotifyAlarmPlayer.play(uri, volume)
  ├─ before_play(): si el modo invitados está ON → volver a privado y guardar OFF
  ├─ intento 1..4  (esperas 0, 2, 4, 6 s → máx. 12 s)
  │    ├─ GET /me/player/devices
  │    ├─ choose_device():  ID guardado → si no, nombre ("Groove", sin mayúsculas)
  │    │      → guarda el ID nuevo; dos con el mismo nombre y ninguno el guardado → conflicto
  │    ├─ PUT /me/player   (transferir, sin reproducir)
  │    ├─ esperar hasta ~2 s a que esté activo y sin restricciones (0, +1, +1 s)
  │    ├─ PUT /me/player/volume  (volumen inicial)
  │    └─ PUT /me/player/play    (track → uris; álbum/playlist → context_uri + offset aleatorio)
  └─ errores sin reintento: auth, 403 (salvo el «frío»), sin configurar, 429 largo
```

Decisiones:

- **Nunca se usa «el dispositivo que estuviera sonando».** Si por la noche Spotify sonaba en una tablet,
  la alarma se lleva la música a Groove. El objetivo es el dispositivo elegido, no el último activo.
- **ID + nombre.** El ID de un dispositivo Spotify Connect puede cambiar al reiniciar librespot. Se
  guarda también el nombre para reencontrarlo, y se actualiza el ID.
- **Reintentos cortos (12 s en total).** Cubren Raspotify recién reiniciado o un fallo de red puntual,
  sin retrasar demasiado el respaldo local.
- **Arranque «en frío».** Tras reiniciar Raspotify, Groove aparece en la lista pero la primera orden
  falla con `403 Player command failed: Restriction violated` (reason `UNKNOWN`). Ese 403 concreto se
  trata como temporal; los demás 403 (sin Premium, usuario no registrado) no se reintentan, porque
  esperar no los arregla.
- **Inicio aleatorio.** Con álbumes y playlists se pregunta cuántas pistas hay y se empieza en una al
  azar (`offset.position`), sin que suene antes la primera. Así no te despierta siempre la misma
  canción. Si no se puede saber (playlists ajenas: Spotify no da su contenido desde febrero de 2026),
  se empieza por la primera. Los reintentos usan la misma pista elegida.
- **STOP o +10 MIN durante la búsqueda** cortan la espera al momento (`interrupt()`), y en ese caso
  no suena el respaldo («cancelled»).
- **Fade-in en el servidor**: cada 15 s un `PUT /me/player/volume` hasta llegar *exactamente* al
  volumen final. 20 → 60 % en 5 min son 20 peticiones, muy lejos de los límites de la API. Si una falla,
  el fade se detiene y la música sigue con el último volumen conseguido: la alarma no se para. El hilo
  espera con un `Event`, así que `cancel()` lo despierta al momento y no quedan timers huérfanos.

### 6.6 Emparejamiento Bluetooth

Modo normal: **privado** (`Discoverable: no`, `Pairable: no`). El adaptador sigue encendido: los
dispositivos emparejados y de confianza se reconectan, pero nadie nuevo puede verlo.

```
«Emparejar nuevo dispositivo»
  ├─ lanzar `bluetoothctl --agent NoInputNoOutput` en un pty (agente vivo)
  ├─ default-agent
  ├─ órdenes una a una con bluetoothctl, comprobando cada respuesta:
  │     pairable on · discoverable on · discoverable-timeout 120
  ├─ verificar el estado real del adaptador
  ├─ vigilante: plazo cada 1 s, emparejados cada 2 s, eventos "[CHG] Device … Paired: yes"
  ├─ nuevo emparejado → trust <MAC> → cerrar al momento
  └─ cerrar (éxito, 120 s, cancelar o error):
        discoverable off · pairable off · fin del agente · (y otra vez por separado)
```

Decisiones:

- **El plazo lo controla el servidor**, no el navegador: cerrar la página no deja Groove visible.
- **Doble red de seguridad**: además del cierre de Groove, `discoverable-timeout 120` hace que **BlueZ
  oculte la Pi por sí mismo** aunque Groove se caiga a mitad.
- **Fail-safe al arrancar**: cada vez que Groove arranca (reinicio, caída, `systemctl restart`) aplica
  `discoverable off` y `pairable off` en segundo plano, con reintentos (0, 5, 20 s) por si `bluetoothd`
  aún no está listo.
- **Agente en un pseudo-terminal.** `bluetoothctl` se comporta distinto si no cree estar en una
  terminal; con un pty el agente queda registrado y contesta «yes» a las confirmaciones, que es lo que
  permite emparejar en una Pi sin pantalla.
- **Solo se confía automáticamente en lo emparejado durante la ventana** (lo que no estaba emparejado
  al abrirla, o lo que BlueZ anuncia como emparejado durante ella). Nunca en el resto.
- **Órdenes de visibilidad fuera del agente.** Al principio se enviaban por la terminal del agente,
  pero no siempre se procesaban varias líneas seguidas. Desde `ff9d1ba` se ejecutan una a una como
  comandos `bluetoothctl` separados y se verifica el resultado; el agente solo autoriza.
- **Una sola ventana a la vez**: pulsar dos veces devuelve la que ya está abierta.

### 6.7 Temporizador de sueño

- Ligado **al crearlo** a una fuente concreta: el ID del dispositivo Spotify activo, o la MAC de un
  dispositivo Bluetooth conectado. Al vencer no adivina qué suena: si la fuente ya no es la misma, no
  hace nada.
- Al vencer: Spotify → `pause` (sin transferir ni tocar el volumen). Bluetooth → `disconnect` (sigue
  emparejado, *bonded* y de confianza; nunca se olvida ni se toca el adaptador).
- **Nunca para una alarma**, con tres capas:
  1. `start()` llama a `invalidate_for_alarm()` antes de que suene nada.
  2. Cada temporizador recuerda la *generación* de alarmas al crearse; si ha empezado alguna desde
     entonces, o hay una sonando, no hace nada aunque la anulación no hubiera llegado.
  3. Vencimiento y anulación comparten lock: o la alarma anula primero, o la pausa termina antes de que
     la alarma empiece a sonar. Nunca después.
- Uno solo a la vez y solo en memoria.

### 6.8 Modo invitados de Spotify

Permite que otra cuenta de Spotify de la red local use Groove por Spotify Connect (Zeroconf), sin
tocar la cuenta principal ni el OAuth de las alarmas.

```
Navegador ──POST /spotify/guest (CSRF + booleano estricto)──► spotify_guest.py (usuario normal)
                                                                 │ systemctl start (polkit: solo 3 unidades)
                                                                 ▼
                                    groove-spotify-mode@guest|private|status.service (root, oneshot, sandbox)
                                                                 │
                                                  /usr/local/libexec/groove-spotify-mode
                                                   ├─ reescribe /etc/raspotify/conf (atómico)
                                                   ├─ guard runtime de systemd (aísla credenciales)
                                                   ├─ reinicia raspotify y lo observa 3 s
                                                   ├─ rollback si falla
                                                   └─ publica /run/groove-spotify/status.json (sin secretos)
```

| | Privado (OFF) | Invitados (ON) |
|---|---|---|
| Cuenta | La principal (usuario fijo) | La de quien se conecte por Zeroconf |
| Discovery | Desactivado | Activado |
| Caché | `/var/cache/raspotify` | `/run/groove-spotify/guest` (temporal, `0700`, en RAM) |
| Credenciales principales | Montadas **solo lectura** dentro de Raspotify | **Inaccesibles** para el proceso invitado |

Decisiones:

- **El código root vive fuera del checkout** (`/usr/local/libexec`, root:root), ejecutado con
  `python3 -I` (ignora `PYTHONPATH` y el directorio actual). El usuario de la app puede escribir en el
  repositorio, así que si el helper se ejecutara desde ahí, cualquier cambio en el repo se ejecutaría
  como root.
- **Sin sudoers.** Una regla polkit permite al usuario de Groove solo `start` de tres instancias fijas.
  El helper no acepta rutas, comandos ni nombres de servicio del navegador.
- **Transacciones con rollback.** Cada cambio exige un reinicio correcto y tres observaciones del
  proceso durante tres segundos (que systemd diga «arrancado» en un `Type=simple` no basta). Si falla,
  se restaura el modo anterior; si el rollback también falla, queda privado, detenido y sin caché
  invitada («estado sin confirmar»).
- **Nunca se leen credenciales.** El helper no abre, copia ni escribe `credentials.json`; los logs solo
  contienen códigos fijos.
- **Privado al arrancar.** `groove-spotify-boot.service` aplica privado antes de Raspotify en cada boot,
  aunque Groove o SQLite no arranquen. Luego Groove reactiva invitados solo si lee un `"true"` válido.
- **Instalación conservadora.** Antes de tocar nada, `check` verifica que la configuración instalada y
  el binario `librespot` son los esperados; si no, no modifica nada y devuelve un código. Se guarda un
  backup único de la configuración original (`/var/lib/groove-spotify/conf.original`, `0600`).
- **Compatibilidad de identidad.** `User=` vacío (root), root explícito, usuario con nombre o UID
  numérico son válidos; `DynamicUser=yes` se rechaza porque su UID puede cambiar y la caché quedaría
  asignada a un UID obsoleto (`10a8d6b`).
- **Las alarmas mandan.** Antes de una alarma Spotify se vuelve a privado y se guarda OFF; mientras suena
  una alarma Spotify se rechazan cambios del interruptor. ON no se restaura solo después.
- **SQLite solo guarda un booleano** (`spotify_guest_mode`), después de verificar el cambio. Un valor
  ausente o raro se interpreta como privado.

Detalle completo, instalación y pruebas: [spotify-guest-mode.md](spotify-guest-mode.md).

### 6.9 Pre-flight y recuperación de Raspotify

- Cada alarma activa tiene un job `preflight:<id>` **5 minutos antes** de su próxima vez
  (`ALARM_PREFLIGHT_MINUTES`). Ejecuta los health checks relevantes, escribe un resumen en el log y
  guarda el informe como «último diagnóstico».
- `sync()` deja los jobs exactamente como dicta la BD (crea, mueve, borra). Se llama al arrancar, tras
  cada cambio de alarma y cada minuto (`preflight-sync`, segundo 30), lo que también corrige saltos de
  la hora del sistema.
- **No puede impedir una alarma**: las alarmas las sigue disparando `check_alarms`, que no sabe nada
  del pre-flight. Si quedan menos de 5 minutos, esa vez no hay pre-flight; nunca se ejecuta tarde.
- **Única acción permitida**: librespot puede quedarse vivo (`active`) con la sesión caída
  (`Websocket peer does not respond`), y Groove desaparece de Spotify hasta que se reinicia. Si en una
  alarma **Spotify** Raspotify está activo, Spotify responde y el dispositivo **no aparece**
  (`code = device_missing`), se reinicia Raspotify **una sola vez**, se esperan 5 s y se repiten los
  checks. Nunca por otros errores (auth, red, 429, permisos, conflicto), ni si Raspotify está parado,
  ni en alarmas locales.
- `health.py` sigue siendo de solo lectura: devuelve un `code` y es `preflight.py` quien decide actuar.
  Así la página de Diagnóstico nunca puede reiniciar nada.

### 6.10 STOP, +10 MIN y auto-stop

- **STOP** para el sonido (pausa Spotify o mata ffmpeg/aplay), cancela fade y auto-stop y devuelve
  Bluetooth. Es idempotente. **Si no se puede parar** (p. ej. Spotify sin red), la alarma queda como
  `stop_pending`: no se devuelve Bluetooth, no se descarta el auto-stop, se puede reintentar STOP y no
  se empieza otra alarma hasta resolverla.
- **+10 MIN** para el sonido y programa un job de una sola vez que vuelve a llamar a `start()` con la
  misma alarma. No crea alarmas nuevas ni cambia la hora. Si no se puede parar el sonido, no se pospone.
- **Auto-stop** (15/30/45/60 min o sin límite; 30 por defecto): al empezar a sonar se programa un job
  que para la alarma por el mismo camino que STOP. Para que un auto-stop antiguo no pare una alarma
  posterior, el job lleva el **token** de su reproducción y solo actúa si sigue siendo el actual.
- Parar ffmpeg: `SIGTERM` y, si no termina en 2 s, `SIGKILL`. No quedan procesos huérfanos.

---

## 7. Base de datos

SQLite en `instance/alarms.db`, con la librería estándar (sin ORM, para ahorrar RAM). Tres tablas
(`schema.sql`):

### `alarms`

| Columna | Tipo | Significado |
|---|---|---|
| `id` | INTEGER PK | — |
| `name` | TEXT | Nombre (máx. 50). |
| `time` | TEXT | `"HH:MM"`. |
| `days` | TEXT | `"0,2,4"` (0 = lunes). Vacío = una vez (se desactiva sola al sonar). |
| `enabled` | INTEGER | 1/0. |
| `created_at` | TEXT | — |
| `last_triggered` | TEXT | `"YYYY-MM-DD HH:MM"` del último disparo programado (antiduplicados). |
| `source` | TEXT | `local` o `spotify`. |
| `spotify_uri` | TEXT | `spotify:<track\|album\|playlist>:<id>`. **Lo único que se usa para reproducir.** |
| `spotify_name`, `spotify_subtitle` | TEXT | Metadata legible, solo para mostrar. NULL en alarmas antiguas o enlaces pegados. |
| `volume_start`, `volume_end`, `fade_minutes` | INTEGER | 20 → 60 % en 5 min por defecto. Hoy solo los usa Spotify. |
| `max_duration_minutes` | INTEGER | Auto-stop; 0 = sin límite; 30 por defecto. |
| `local_track` | TEXT | Nombre de fichero en `instance/music/`; NULL = aleatoria. |

### `spotify_auth`

Una sola fila (`CHECK (id = 1)`): `access_token`, `refresh_token`, `expires_at` (epoch), `scope`.

### `settings`

Clave/valor: `spotify_device_id`, `spotify_device_name`, `spotify_guest_mode`…

### Migraciones

No hay herramienta de migraciones. `db.MIGRATIONS` es una lista ordenada de `(columna, definición)`;
al arrancar se compara con `PRAGMA table_info(alarms)` y se hace `ALTER TABLE ADD COLUMN` de lo que
falte. Las alarmas antiguas adoptan los valores por defecto. Es suficiente porque el esquema solo
crece (nunca se renombra ni borra una columna), y evita una dependencia más.

### Qué **no** está en la base de datos (a propósito)

Snoozes, auto-stop, temporizador de sueño, la alarma que suena, el último informe de diagnóstico y los
jobs de pre-flight viven **en memoria**. Si Groove se reinicia, se pierden (los pre-flight se rehacen
desde la BD). Es deliberado: **mejor perder un snooze que sonar a una hora incorrecta** tras un
reinicio en un momento cualquiera.

---

## 8. Concurrencia y estado en memoria

Hay varios hilos a la vez:

- 4 hilos de waitress (peticiones web).
- El pool de hilos de APScheduler (alarmas, pre-flight, snoozes, auto-stop, temporizador).
- Hilos daemon propios: fade-in, vigilantes de procesos (ffmpeg/aplay), vigilante de la ventana de
  emparejamiento, fail-safe de Bluetooth.

Reglas:

- **Un lock por gestor** (`AlarmPlaybackManager`, `SleepTimerManager`, `BluetoothManager`). Las acciones
  (`start`, `stop`, `snooze`…) se serializan: si Spotify tarda en arrancar y se pulsa STOP, STOP espera
  y luego para. Además, el `Event` `interrupted` permite que STOP corte la búsqueda del dispositivo.
- **Estado publicado como objetos inmutables** (`@dataclass(frozen=True)` `ActiveAlarm`,
  `PendingSnooze`). Las páginas leen `playback.active` **sin coger el lock**: nunca se quedan esperando
  aunque una alarma esté arrancando.
- **Tokens / generaciones** para que los jobs antiguos no actúen sobre reproducciones nuevas (auto-stop,
  temporizador de sueño, avisos de fin de ffmpeg).
- **Los callbacks de fin de proceso se llaman fuera del lock del reproductor**, para evitar bloqueos
  cruzados con el lock del gestor.
- **SQLite**: las peticiones web usan una conexión por petición (`flask.g`); el scheduler y el
  pre-flight abren su propia conexión, porque no corren dentro de una petición.

---

## 9. Seguridad y permisos

Groove es una app de red local sin login. Aun así, como ejecuta procesos del sistema y gestiona
Bluetooth y Spotify, el diseño aplica **mínimo privilegio**:

### 9.1 Proceso

- El servicio corre con **un usuario normal**; `install-service.sh` se niega a usar root.
- `NoNewPrivileges=true`, `PrivateTmp=true`, `ProtectSystem=full`. Con `NoNewPrivileges` ni siquiera
  podría usar `sudo`, y no debe.
- `SupplementaryGroups=audio` para acceder a la tarjeta de sonido.

### 9.2 Permisos concretos vía polkit (en lugar de sudo)

| Regla | Permite al usuario de Groove | Instalador |
|---|---|---|
| `50-pi-music-alarm-bluetooth.rules` | Solo `start` y `stop` de `bluealsa-aplay.service`. | `install-bluetooth-permission.sh` |
| `50-pi-music-alarm-raspotify.rules` | Solo `restart` de `raspotify.service`. | `install-raspotify-permission.sh` |
| `50-pi-music-alarm-spotify-guest.rules` | Solo `start` de `groove-spotify-mode@{guest,private,status}.service`. | `install-spotify-guest-mode.sh` |

Nada de `enable`/`disable`, ni otras unidades, ni sudoers, ni grupos amplios. BlueZ no necesita regla:
permite al usuario gestionar el adaptador por D-Bus (si no, basta con el grupo `bluetooth`).

### 9.3 Ejecución de comandos

- **Nunca con shell.** Todo `subprocess` recibe una lista de argumentos.
- Los argumentos son constantes o datos **validados**: MAC con `AA:BB:CC:DD:EE:FF`, nombre de pista
  dentro de la biblioteca.
- Los **nombres de dispositivos Bluetooth son texto no confiable**: nunca forman parte de un comando;
  se limpian (una línea, sin controles ni ANSI, recortados) y se muestran escapados.
- Al agente de `bluetoothctl` solo se le envían órdenes de una **lista cerrada** (`AGENT_COMMANDS`).
- Timeouts en todo: 3 s en los checks, 5 s en `bluetoothctl`, 8 s en `systemctl` de Bluetooth, 15 s en
  el reinicio de Raspotify, 10 s en la API de Spotify (6 s en el buscador).

### 9.4 Secretos

- `SPOTIFY_CLIENT_SECRET` solo en `.env` (`chmod 600`), nunca en el navegador (Authorization Code Flow
  con el secreto en el servidor).
- Tokens en SQLite dentro de `instance/`, fuera del repositorio.
- La página de Diagnóstico y los JSON no muestran tokens, el client secret, variables de entorno, rutas
  completas ni trazas de Python; las trazas van al log.

### 9.5 Entrada de usuario

- Formulario de alarma: hora con regex, días 0–6, volumen 0–100, duraciones de una lista cerrada.
- Spotify: `parse_spotify_uri` acepta solo track/álbum/playlist en formato URI o URL de
  `open.spotify.com`. La metadata que manda el navegador se limpia y **se descarta si no corresponde al
  URI**; manipularla no cambia lo que suena.
- Subidas: nombre saneado (sin rutas, sin `../`, sin acentos, solo `[A-Za-z0-9._-]`), **se comprueba la
  cabecera** del fichero (MP3/OGG/WAV), nunca se sobrescribe, límite de tamaño (50 MB por defecto) en la
  petición y al guardar, y se escribe primero a un temporal `.upload-*`.
- Todas las acciones que cambian algo son `POST`.
- CSRF: solo en el interruptor de invitados (la acción más sensible). En general no hay CSRF ni login
  porque la app no sale de la red local (pendiente si algún día se expone, ver §15).

---

## 10. Interfaz web y PWA

- **Mobile first**, tema oscuro, navegación inferior (Alarmas / Spotify / Música / Diagnóstico) y, en
  pantallas anchas, arriba.
- La pantalla principal muestra la **próxima alarma** con un sol que «sale» del horizonte según se
  acerca (`ui.sun_height`).
- **Funciona sin JavaScript**: todo son formularios normales. El JS solo mejora (mostrar/ocultar campos,
  porcentajes, menús, buscador, cuenta atrás).
- **Sin recursos externos**: tipografía del sistema, iconos SVG propios, sin CDN ni Google Fonts. La
  web no depende de Internet para cargarse (solo las portadas del buscador vienen de Spotify).
- **Estado en vivo por polling**, no WebSockets: `/playback/state` cada 10 s devuelve una «huella»
  (`playback_key`); si cambia, la página se recarga. El temporizador de sueño se descuenta en el
  navegador con los segundos que da el servidor (no depende de la hora del móvil) y se resincroniza cada
  15 s. Polling es suficiente, simple y funciona a través de cualquier cosa.
- **PWA conservadora**: el *service worker* solo cachea la carcasa estática (CSS, JS, iconos y
  `offline.html`). **Nunca** cachea alarmas, estado, Spotify ni formularios. Si la Pi no responde dice
  «No hay conexión con Groove»: no hay modo offline falso, porque en un despertador sería engañoso.
- El *service worker* se sirve desde la raíz (`/sw.js`) para que su alcance sea toda la app, y sin caché
  para que las actualizaciones lleguen enseguida. Solo se activa en HTTPS o `127.0.0.1`, así que por
  `http://192.168.0.21:5000` la app se instala como acceso directo pero sin *service worker* (ver §15).

### Buscador de Spotify: secundario a propósito

El campo **«Pega un enlace de Spotify»** es el método principal; el buscador es una comodidad que va
debajo. Motivo (`8c0407b`): la API de Spotify puede negar `/v1/search` a apps en modo desarrollo (403)
aunque el resto funcione. El enlace manual funciona siempre, sin JavaScript y sin Spotify vinculado. El
buscador está **aparcado**: si recibe un 403 o un 401 se desactiva en esa página sin insistir, y si
recibe un 429 se pausa el tiempo que pida Spotify sin afectar a las alarmas.

---

## 11. Decisiones de diseño (resumen razonado)

Una tabla para consultar rápido. Cada decisión está explicada con más detalle en su sección.

| # | Decisión | Por qué | Alternativa descartada |
|---|---|---|---|
| 1 | Flask + SQLite + APScheduler, 4 dependencias | 512 MB de RAM; nada que compilar en ARM | Django/ORM, Celery, Redis |
| 2 | HTTP con `urllib`, sin SDK de Spotify | Menos RAM y dependencias; control total de errores, 429 y timeouts | `spotipy` |
| 3 | Un único proceso (waitress) | El scheduler debe existir una sola vez | gunicorn con varios workers |
| 4 | Un job cada minuto que lee la BD | La BD es la única verdad; nada que sincronizar | Un job de APScheduler por alarma (persistente) |
| 5 | `claim_trigger` atómico | Nunca dos disparos en el mismo minuto | Bandera en memoria |
| 6 | No recuperar alarmas perdidas | Sonar a deshora es peor que no sonar | *Catch-up* al arrancar |
| 7 | Snoozes, auto-stop y temporizador en memoria | Tras un reinicio no deben sonar ni parar a deshora | Persistirlos en SQLite |
| 8 | Cadena Spotify → música local → WAV | Siempre suena algo | Solo Spotify |
| 9 | `play()` nunca lanza excepciones | Un fallo secundario no puede romper la alarma | Excepciones hasta el scheduler |
| 10 | `start()` como punto único | Reglas globales en un solo sitio | Lógica repartida en scheduler/vistas |
| 11 | «La última alarma gana» | Simple y predecible | Cola de alarmas |
| 12 | Estado `stop_pending` | Un STOP fallido no debe esconder que sigue sonando | Dar la alarma por parada igualmente |
| 13 | Tokens/generaciones en jobs | Un job antiguo nunca afecta a una alarma nueva | Cancelar y confiar en que no haya carreras |
| 14 | ALARMA > SPOTIFY > BLUETOOTH, parando `bluealsa-aplay` | Una sola tarjeta USB, un solo programa | dmix/PulseAudio (más RAM y complejidad) |
| 15 | Dispositivo Spotify por ID + nombre | El ID de librespot cambia al reiniciar | Solo ID; «el dispositivo activo» |
| 16 | Reintentar solo el 403 «en frío» | Es temporal; los otros 403 no se arreglan esperando | Reintentar todo / nada |
| 17 | Pista inicial aleatoria | No despertarse siempre con la misma canción | Modo aleatorio de Spotify (cambia el estado del usuario) |
| 18 | Fade-in desde el servidor cada 15 s | Funciona con cualquier dispositivo; pocas peticiones | Depender de la app de Spotify |
| 19 | Volumen `cubic` + normalización en librespot | Mismo % suena parecido por Spotify y por Bluetooth | `linear` (muy fuerte), `log` (muy bajo), `fixed` (sin fade) |
| 20 | URI como verdad; metadata solo para mostrar | Manipular texto no cambia lo que suena | Guardar el resultado del buscador |
| 21 | Enlace manual primero, buscador aparcado | `/v1/search` puede dar 403 en apps en desarrollo | Depender del buscador |
| 22 | Health checks de solo lectura | Diagnosticar nunca debe cambiar nada | Checks que «arreglan» |
| 23 | Pre-flight que no puede impedir la alarma | La alarma no depende del diagnóstico | Que el pre-flight dispare la alarma |
| 24 | Una sola acción en el pre-flight (reiniciar Raspotify una vez) | Fallo real y frecuente de librespot con arreglo seguro | Reinicios por cualquier error |
| 25 | Bluetooth privado por defecto, ventana de 2 min | Nadie desconocido puede emparejarse | Visible siempre |
| 26 | `discoverable-timeout` de BlueZ + fail-safe al arrancar | Aunque Groove muera, la Pi vuelve a ocultarse | Confiar solo en Groove |
| 27 | Separar BlueZ (`bluetooth_manager`) del reproductor (`bluetooth_audio`) | Parar audio nunca desconecta; gestionar nunca corta audio | Un módulo para todo |
| 28 | Temporizador de sueño ligado a una fuente | No pausar algo distinto de lo que se programó | «Parar lo que suene» |
| 29 | polkit con permisos por unidad y verbo | Mínimo privilegio, compatible con `NoNewPrivileges` | sudoers / correr como root |
| 30 | Helper root fuera del checkout, con rollback | Un repo escribible no debe ejecutarse como root | Script en el repo con sudo |
| 31 | Privado al arrancar (guard de boot) | Aunque Groove no arranque, nadie entra como invitado | Restaurar desde la app |
| 32 | Sin shell en `subprocess` | Sin inyección de comandos | `shell=True` |
| 33 | Funciona sin JS, sin recursos externos | Robusto en el móvil y sin Internet | SPA con framework |
| 34 | Polling en lugar de WebSockets | Simple y suficiente para un despertador | WebSockets/SSE |
| 35 | Service worker que no cachea datos | Un despertador no debe mostrar un estado falso | Modo offline |
| 36 | HTTP en LAN + túnel SSH para OAuth | Spotify solo admite `http://` en loopback | HTTPS con CA propia (pendiente) |
| 37 | Inyección de dependencias + objetos nulos | Tests sin red, sin audio y sin systemd; desarrollo en Windows | *Monkeypatching* global |
| 38 | Migraciones con `ALTER TABLE ADD COLUMN` | El esquema solo crece; sin dependencia extra | Alembic |

---

## 12. Tests

```bash
python -m unittest discover tests -v
```

**745 tests** con `unittest` (librería estándar), repartidos en 23 ficheros (20 se omiten en Windows,
sobre todo los del helper root, que necesitan Linux):

| Área | Ficheros |
|---|---|
| App y vistas | `test_app.py`, `test_spotify_views.py`, `test_ui.py` |
| Reproducción | `test_playback.py`, `test_playback_failures.py`, `test_auto_stop.py`, `test_audio_player.py`, `test_fade.py`, `test_music.py` |
| Spotify | `test_spotify_client.py`, `test_spotify_alarms.py`, `test_device_resolution.py`, `test_cold_start.py`, `test_random_start.py`, `test_spotify_search.py` |
| Modo invitados | `test_spotify_guest.py`, `test_spotify_mode_helper.py` |
| Bluetooth | `test_bluetooth.py`, `test_bluetooth_manager.py` |
| Otros | `test_sleep_timer.py`, `test_health.py`, `test_preflight.py`, `test_deploy.py` |

Principios:

- **Sin red, sin audio y sin systemd**: todo se inyecta (transporte HTTP falso, `run`/`popen` falsos,
  reloj controlable, `schedule_once` síncrono, reintentos con esperas a 0).
- En modo `testing`, `create_app` nunca lanza ffmpeg, systemctl ni bluetoothctl salvo que se inyecte un
  doble.
- Los tests del helper root usan un sistema de ficheros temporal, un `/proc` falso y systemd simulado;
  necesitan ejecutarse como root en Linux (`sudo /usr/bin/python3 -m unittest tests.test_spotify_mode_helper`).
  En Windows se omiten.
- `test_deploy.py` comprueba plantillas y scripts de `deploy/`.
- Lo que los tests **no** pueden probar (anuncio Zeroconf real, emparejamiento real, que el altavoz se
  oiga) tiene pruebas manuales documentadas en el README y en `spotify-guest-mode.md`.

---

## 13. Despliegue y mantenimiento

### 13.1 Contenido de `deploy/`

| Fichero | Qué es |
|---|---|
| `pi-music-alarm.service.template` | Unidad systemd de la app (con `@USER@`, `@GROUP@`, `@APP_DIR@`). |
| `install-service.sh` | Rellena la plantilla con el usuario y la carpeta reales e instala/arranca el servicio. Sin rutas fijas. |
| `pi-music-alarm-bluetooth.rules.template` + `install-bluetooth-permission.sh` | polkit: `start`/`stop` de `bluealsa-aplay`. |
| `pi-music-alarm-raspotify.rules.template` + `install-raspotify-permission.sh` | polkit: `restart` de `raspotify`. |
| `spotify-mode-helper.py` | Helper root del modo invitados (se instala en `/usr/local/libexec/groove-spotify-mode`). |
| `groove-spotify-mode@.service`, `groove-spotify-boot.service` | Unidades oneshot root con sandbox (`ProtectSystem=strict`, `ReadWritePaths` concretos, `UMask=0077`). |
| `raspotify-guest-mode.conf`, `pi-music-alarm-spotify-guest.conf` | *Drop-ins* de orden y dependencias. |
| `pi-music-alarm-spotify-guest.rules.template` + `install-spotify-guest-mode.sh` | polkit e instalador del modo invitados. |

Todos los instaladores se ejecutan **con el usuario normal, sin `sudo` delante** (piden sudo solo
para la parte del sistema) y rellenan plantillas con el usuario real.

### 13.2 Configuración (`.env`)

Ver `.env.example`. Las variables más relevantes: `SECRET_KEY`, `SPOTIFY_CLIENT_ID/SECRET`,
`SPOTIFY_REDIRECT_URI`, `SPOTIFY_DEVICE_NAME`, `ALSA_DEVICE`, `ALARM_SOUND`, `AUDIO_BACKEND`,
`FFMPEG_BINARY`, `LOCAL_MUSIC_MAX_UPLOAD_MB`, `BLUETOOTH_SERVICE`, `BLUEZ_MANAGEMENT`,
`ALARM_PREFLIGHT_MINUTES`, `RASPOTIFY_SERVICE`, `BLUEALSA_SERVICE`, `HOST`, `PORT`, `THREADS`.

Casi todas tienen un valor `none`/`off`/`0` que desactiva esa integración: así la misma app funciona en
Windows, en una Pi sin Bluetooth o sin Spotify.

### 13.3 Actualizar

```bash
git pull
.venv/bin/pip install -r requirements.txt
sudo systemctl restart pi-music-alarm
```

Si cambia el helper del modo invitados, hay que volver a ejecutar `install-spotify-guest-mode.sh`
(la copia root no se actualiza sola, a propósito).

### 13.4 Dónde mirar cuando algo falla

| Qué | Dónde |
|---|---|
| Alarmas, respaldo, pre-flight, Bluetooth, temporizador | `instance/alarms.log` |
| La app entera | `journalctl -u pi-music-alarm -f` |
| Spotify Connect | `journalctl -u raspotify` |
| Bluetooth | `journalctl -u bluetooth -u bluealsa -u bluealsa-aplay`; `journalctl -k \| grep -i hci` |
| Arranque anterior | `journalctl -b -1` (el journal es persistente desde el 2026-10-01) |
| Estado general | Página **Diagnóstico** o `/diagnostics/report.json` |

---

## 14. Fallos conocidos, incidencias y lecciones aprendidas

### 14.1 Cuelgue del chip Bluetooth (2026-10-01)

- **Síntomas**: los dispositivos guardados no conectaban y el emparejamiento «se interrumpía».
- **Diagnóstico**: el kernel registraba `hci0: command 0x0406 tx timeout` / `Opcode … failed: -110`;
  BlueZ, `Failed to set mode: Authentication Failed (0x05)` (en realidad un `ETIMEDOUT`).
- **Causa**: el chip Bluetooth integrado se quedó colgado. **No era un bug del código** (ni del modo
  invitados, ni del cambio de emparejamiento, ni del temporizador de sueño). Causa raíz desconocida.
- **Arreglo**: `sudo reboot`.
- **Lección**: ante fallos de Bluetooth, mirar primero `journalctl -k -b | grep -i "tx timeout"`. Ese
  día se activó el journal persistente para poder ver el arranque anterior. Si se repite a menudo,
  conviene añadir una detección y un aviso en Diagnóstico.

### 14.2 librespot vivo pero invisible

librespot puede seguir `active` con la sesión caída (`Websocket peer does not respond`). Solución: la
recuperación del pre-flight (§6.9). Lección: «el servicio está activo» no significa «funciona»; por eso
el check de Spotify mira si el dispositivo aparece de verdad.

### 14.3 Groove «en frío»

Tras reiniciar Raspotify, la primera orden falla con un 403 «Restriction violated». Solución: tratarlo
como temporal y esperar a que el dispositivo esté activo (§6.5).

### 14.4 Órdenes al agente de `bluetoothctl`

Enviar varias órdenes seguidas por la terminal del agente no era fiable. Solución (`ff9d1ba`):
ejecutarlas por separado y verificar el estado del adaptador.

### 14.5 Usuario de Raspotify

El instalador del modo invitados rechazaba un `User=` vacío (que en systemd significa root). Se corrigió
para aceptar root, usuarios con nombre y UID numéricos, y rechazar solo `DynamicUser=yes` (`10a8d6b`).

### 14.6 Fallos de reproducción silenciosos

Al principio, si ffmpeg moría o STOP fallaba, la interfaz no lo reflejaba. Desde `c2faa95` hay estados
explícitos (`failed`, `stop_pending`, `finished`), vigilancia de procesos y paso al WAV si la música
local muere a mitad.

---

## 15. Trabajo pendiente

- **HTTPS (Caddy)** con URL estable: permitiría vincular Spotify desde cualquier dispositivo sin túnel
  SSH y activar el *service worker*. Está empezado pero **no validado**; la decisión abierta es cómo dar
  un certificado en el que confíen los móviles sin instalar una CA a mano. Ver
  [https-spotify-oauth.md](https-spotify-oauth.md).
- **CSRF general y `SECRET_KEY` obligatoria** si algún día Groove sale de la red local.
- **Alarma durante una parada pendiente**: hoy se pierde (solo queda en el log). Decidir si reintentar la
  parada automáticamente o encolar la alarma. Si el auto-stop falla, no se reintenta solo.
- **Sustituir una alarma local** no comprueba si se pudo parar el sonido anterior (es menos estricto que
  STOP).
- **`FfmpegPlayer.stop()`** espera con el lock tomado: si ffmpeg no responde, STOP puede tardar unos
  segundos.
- **Volumen y fade en alarmas locales**: los valores se guardan pero todavía no se aplican.
- **Detección del cuelgue del chip Bluetooth** en Diagnóstico, si se repite.

---

## 16. Historia del proyecto

Todo el desarrollo se hizo entre el 26 de septiembre y el 1 de octubre de 2026:

| Fecha | Hito |
|---|---|
| 26/09 | Scheduler, audio local (WAV), OAuth y reproducción de Spotify con respaldo local, despliegue en la Pi, STOP/snooze, volumen y fade-in, selección de dispositivo resistente. |
| 27/09 | Rediseño de la UI y PWA, inicio aleatorio, prioridad de Bluetooth, auto-stop, biblioteca de música local, Diagnóstico y pre-flight, buscador de Spotify (y enlace manual como principal), gestión de dispositivos Bluetooth, temporizador de sueño. |
| 30/09 | Estados de fallo de reproducción y STOP reintentable; recuperación de Raspotify en el pre-flight. |
| 01/10 | Modo invitados de Spotify con caché aislada y rollback; compatibilidad con Raspotify como root; emparejamiento Bluetooth más fiable; curva de volumen `cubic`. |

El historial completo está en `git log`.
