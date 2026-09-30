# Spotify invitados

El interruptor está en **Spotify → Spotify invitados**, independientemente del
OAuth que Groove usa para controlar las alarmas. OFF usa la cuenta principal;
ON permite Spotify Connect/Zeroconf desde otras cuentas de la red local.
No hace falta vincular la segunda cuenta en Groove ni cambiar el OAuth existente.

## Instalación en la Raspberry

Con los archivos de esta versión ya copiados a `~/pi-music-alarm`, ejecuta como
el usuario habitual de `pi-music-alarm.service`, **sin anteponer sudo**:

```bash
cd ~/pi-music-alarm
bash deploy/install-spotify-guest-mode.sh
systemctl is-active raspotify.service pi-music-alarm.service
curl -fsS http://127.0.0.1:5000/spotify/guest/status
```

No hay dependencias Python nuevas. El instalador pide sudo para instalar la
parte del sistema. Si el servicio tiene otro usuario:

```bash
SERVICE_USER=nombre_del_usuario bash deploy/install-spotify-guest-mode.sh
```

Antes de tocar Raspotify comprueba la plantilla **instalada** en
`/etc/raspotify/conf`, `librespot --help`, el usuario estático no-root de la
unidad, su `ExecStart` y su único `EnvironmentFile`. Exige que la configuración
principal tenga `LIBRESPOT_SYSTEM_CACHE="/var/cache/raspotify"`, username,
discovery desactivado y el fichero principal de credenciales existente. No lo
abre. Rechaza un `ExecStart` con argumentos, fuentes de entorno adicionales,
`DynamicUser=yes`, autenticación por token/password/OAuth en conf o asignaciones
duplicadas/multilínea de las opciones gestionadas. Ante incompatibilidad no
modifica la configuración ni detiene la música: informa de un código fijo.

La correspondencia de opciones se comprueba tanto en el fichero instalado como
en el binario, siguiendo las fuentes oficiales de
[Raspotify](https://github.com/dtcooper/raspotify/blob/master/raspotify/etc/raspotify/conf)
y [librespot 0.8.0](https://github.com/librespot-org/librespot/blob/v0.8.0/src/main.rs).
Los flags se activan por su **presencia**; `=false` o `=off` no los desactivan.
Esta implementación los comenta/elimina cuando deben quedar desactivados.

El instalador detiene brevemente Groove/Raspotify, aplica privado y arranca
ambos. Groove reconcilia entonces el booleano persistido, por lo que una
instalación posterior respeta un ON previamente válido. Si la instalación falla
después de detenerlos, no la des por terminada: usa los comandos de recuperación
de esta guía. No instala sudoers ni cambia `NoNewPrivileges`.

## Archivos del sistema y permisos

| Archivo | Función |
| --- | --- |
| `/usr/local/libexec/groove-spotify-mode` | Copia root:root del helper, ajena al checkout escribible por el usuario. Python aislado `-I`, sin shell. |
| `/etc/systemd/system/groove-spotify-mode@.service` | Operaciones `guest`, `private`, `status`; oneshot root, sandbox y timeout. |
| `/etc/systemd/system/groove-spotify-boot.service` | Antes de Raspotify aplica privado, incluso si Groove/SQLite no arrancan. |
| `/etc/systemd/system/raspotify.service.d/80-groove-spotify.conf` | Dependencia obligatoria del guard de arranque y acceso a su directorio runtime. Conserva la unidad y parámetros de audio existentes. |
| `/etc/systemd/system/pi-music-alarm.service.d/80-groove-spotify.conf` | Arranca Groove después de Raspotify. |
| `/etc/polkit-1/rules.d/50-pi-music-alarm-spotify-guest.rules` | Un único usuario puede únicamente `start` de las tres instancias autorizadas. |
| `/etc/tmpfiles.d/groove-spotify.conf` | Crea los directorios root de `/run` antes de las unidades en cada boot. |
| `/var/lib/groove-spotify/conf.original` | Backup inicial completo, root:root `0600` en directorio `0700`. No se sobrescribe. Puede contener información privada; no copiar al repo ni a logs. |
| `/run/systemd/system/raspotify.service.d/90-groove-spotify-mode.conf` | Guard temporal: principales inaccesibles en invitados; `credentials.json` de solo lectura en privado. |
| `/run/groove-spotify/guest` | Caché de invitado `0700`, propiedad del usuario/grupo real de Raspotify. |
| `/run/groove-spotify/status.json` | Estado público sin username, credenciales, tokens ni contenido de conf. |

La regla anterior de reinicio de Raspotify para pre-flight puede permanecer
instalada; el nuevo control no depende de ampliar ese permiso. El helper tiene
rutas y servicio fijos: no acepta rutas, comandos ni nombres de servicio del
navegador. Una instalación con `RASPOTIFY_SERVICE` distinto de `raspotify` o
`raspotify.service` deja este control indisponible.

## Qué cambia al conmutar

Solo se gestionan caché, username, discovery y fuentes de autenticación. Nombre,
ALSA, backend, bitrate, normalización y demás ajustes de audio se conservan del
fichero actual. Al volver a privado se recuperan las opciones principales desde
el backup; ediciones posteriores de audio permanecen intactas. Si cambias la
cuenta principal, hay que retirar/reinstalar esta integración con un nuevo
backup privado; no reutilices un backup de otra cuenta.

En ON, tanto `LIBRESPOT_CACHE` como `LIBRESPOT_SYSTEM_CACHE` apuntan a
`/run/groove-spotify/guest`; se desactivan username, token, password, OAuth y
`LIBRESPOT_DISABLE_DISCOVERY`. Se activa
`LIBRESPOT_DISABLE_CREDENTIAL_CACHE=`. Un guard de systemd hace inaccesibles
`/var/cache/raspotify` y `/var/lib/raspotify` al proceso invitado. La caché es
temporal en `/run` y no se comparte con la principal.

En OFF, se detiene Raspotify, se borra toda la caché temporal, se recuperan
username/cache principal y `LIBRESPOT_DISABLE_DISCOVERY=`, se reinicia y se
comprueba el proceso. El fichero principal de credenciales se monta de solo
lectura dentro de Raspotify: el helper nunca lo abre, copia, borra ni escribe.
Si librespot intenta guardar credenciales de nuevo, puede registrar un aviso de
escritura denegada; la reproducción utiliza las credenciales cacheadas existentes.

Las escrituras de conf, guard y estado son atómicas. Un bloqueo root serializa
las operaciones incluso entre procesos. Repetir un modo ya confirmado no
reinicia el servicio ni borra la sesión invitada activa. Tras cada cambio se
exige un restart correcto y tres observaciones del proceso durante tres segundos;
la respuesta exitosa de un servicio `Type=simple` por sí sola no basta.

Si un cambio falla se restaura el modo anterior confirmado y se comprueba el
arranque. Si el rollback también falla, se deja configuración privada, servicio
detenido y caché invitada eliminada: la interfaz muestra **estado sin confirmar**.
Un fallo al desactivar puede devolver el interruptor a ON mediante rollback,
con una sesión invitada nueva, vacía; el invitado tendrá que reconectarse. Nunca
se guardan copias de sus credenciales para restaurar una sesión.

SQLite guarda exclusivamente `spotify_guest_mode = "true" / "false"` en la
tabla `settings` existente. Se guarda después de verificar el cambio. Si falla
esa escritura se deshace el cambio. Un valor ausente, inválido o ilegible se
interpreta como privado. Al reiniciar la Raspberry, el guard aplica privado antes
del primer arranque de Raspotify; Groove solo reactiva invitados cuando lee un
`"true"` válido. Si no puede hacerlo, intenta privado y guarda `"false"`.

La interfaz y `/spotify/guest/status` consultan el entorno del **proceso activo**
de Raspotify a través del helper root y validan configuración, guard y permisos
de caché. No deducen el estado de una variable JavaScript o solo del booleano de
SQLite. La página se actualiza si el modo cambia desde otro navegador. Las
acciones POST exigen un token CSRF y un booleano estricto.

**Alarmas**: antes de una alarma Spotify, Groove recupera privado y guarda OFF.
Así la alarma utiliza la cuenta principal y los reintentos existentes; un fallo
mantiene el respaldo de música local/WAV. Las alarmas locales no cambian este
modo. Mientras suena una alarma Spotify se rechazan los cambios del interruptor.
El invitado se interrumpe y ON no se restaura automáticamente después de la
alarma. Los temporizadores existentes y la configuración OAuth se conservan.

## Prueba manual con dos cuentas Premium

Usa la cuenta principal y una segunda cuenta Premium en dos teléfonos/equipos de
la misma Wi-Fi, sin aislamiento entre clientes. Mantén el OAuth principal de
Groove. Empieza con OFF y sin una alarma Spotify a punto de sonar.

1. Guarda **solo un hash**, sin leer ni imprimir credenciales:

   ```bash
   GROOVE_CHECK="$(mktemp)"
   sudo sha256sum /var/cache/raspotify/credentials.json > "$GROOVE_CHECK"
   systemctl is-active raspotify.service
   curl -fsS http://127.0.0.1:5000/spotify/guest/status
   ```

   Debe estar `active`, con `enabled:false`. La principal reproduce en Groove.
   Una cuenta nueva de la LAN no debe descubrirlo/conectarse por Zeroconf.

2. Activa **Spotify invitados** en la web. Espera a que vuelva la página con ON:

   ```bash
   curl -fsS http://127.0.0.1:5000/spotify/guest/status
   sudo stat -c '%a %U:%G' /run/groove-spotify/guest
   sudo sha256sum -c "$GROOVE_CHECK"
   sudo test ! -e /run/groove-spotify/guest/credentials.json
   ```

   `enabled:true`, `active:true`, permisos `700`, hash `OK` y sin credenciales
   invitadas cacheadas. En la segunda cuenta, abre Dispositivos disponibles,
   selecciona **Groove** y reproduce. Esto comprueba el anuncio Zeroconf real y
   la autenticación de la segunda cuenta, que los tests simulados no pueden probar.

3. Apaga el interruptor mientras reproduce el invitado. Se corta su sesión:

   ```bash
   systemctl is-active raspotify.service
   curl -fsS http://127.0.0.1:5000/spotify/guest/status
   sudo test ! -e /run/groove-spotify/guest
   sudo sha256sum -c "$GROOVE_CHECK"
   ```

   Debe volver a privado, activo, caché temporal ausente y hash `OK`. La principal
   vuelve a reproducir sin vincularse otra vez. El invitado no puede reconectarse
   por Zeroconf. Puede tardar en desaparecer de la lista de Spotify por la caché
   del cliente; comprueba que ya no puede abrir una sesión nueva.

4. Prueba persistencia en ambos estados. En OFF, reinicia Groove y después la Pi:

   ```bash
   sudo systemctl restart pi-music-alarm.service
   curl -fsS http://127.0.0.1:5000/spotify/guest/status
   sudo reboot
   ```

   Tras volver a conectarte, debe seguir OFF/activo. Repite con ON: debe volver
   ON tras el arranque de Groove, con una nueva caché temporal segura. La segunda
   cuenta necesita reconectarse después del reboot. Comprueba de nuevo el hash
   usando el fichero de comprobación, si sigue disponible, o uno guardado en tu
   directorio personal antes del reboot (`/tmp` puede limpiarse).

5. Con ON y el invitado reproduciendo, prueba una alarma Spotify dirigida a
   **Groove**. Debe volver a la principal, sonar y dejar el interruptor OFF.
   Comprueba también una alarma local, STOP/snooze y la reproducción Spotify
   normal en privado.

6. Comprueba `/spotify/guest/status`: solo hay estado y booleanos, sin identidad.
   Los logs propios del helper solo contienen códigos fijos, nunca conf ni
   credenciales. No pegues `conf.original`, `credentials.json`, entornos completos
   o tokens en incidencias. Elimina el fichero de hash al terminar:

   ```bash
   rm -- "$GROOVE_CHECK"
   ```

### Prueba opcional de rollback en la Pi

Empieza en privado. Este fallo artificial afecta solo al arranque con caché
invitada y permite que el rollback privado arranque. No altera el audio ni
credenciales. El directorio runtime de drop-ins ya lo crea el helper.

```bash
printf '%s\n' '[Service]' 'ExecStartPre=/usr/bin/test ! -d /run/groove-spotify/guest' \
  | sudo tee /run/systemd/system/raspotify.service.d/99-groove-test-failure.conf > /dev/null
sudo systemctl daemon-reload
```

Intenta ON en la web: debe informar de fallo y volver a OFF, con Raspotify activo,
hash intacto y sin caché invitada. **Retira el fallo** al terminar:

```bash
sudo rm -f /run/systemd/system/raspotify.service.d/99-groove-test-failure.conf
sudo systemctl daemon-reload
systemctl is-active raspotify.service
```

## Recuperación y reversión manual

Para volver a privado sin desinstalar, apaga el interruptor. Alternativa de
emergencia, con Groove detenido para que no reactive el booleano ON:

```bash
cd ~/pi-music-alarm
sudo systemctl stop pi-music-alarm.service
sudo systemctl start groove-spotify-mode@private.service
.venv/bin/python - <<'PY'
import sqlite3
with sqlite3.connect('instance/alarms.db') as connection:
    connection.execute("INSERT INTO settings(key,value) VALUES('spotify_guest_mode','false') "
                       "ON CONFLICT(key) DO UPDATE SET value=excluded.value")
PY
sudo systemctl start pi-music-alarm.service
systemctl is-active raspotify.service
```

Para **retirar toda la integración y restaurar la conf original**, usa el backup
creado en la primera instalación. Esto también revierte posteriores ediciones de
conf: conserva por separado tus ajustes de audio si los has cambiado. Las
credenciales principales no forman parte de esta restauración y no se tocan:

```bash
cd ~/pi-music-alarm
sudo systemctl stop pi-music-alarm.service raspotify.service
sudo systemctl stop groove-spotify-mode@guest.service groove-spotify-mode@private.service \
  groove-spotify-mode@status.service groove-spotify-boot.service
sudo install -o root -g root -m 600 /var/lib/groove-spotify/conf.original /etc/raspotify/conf
sudo rm -f /etc/polkit-1/rules.d/50-pi-music-alarm-spotify-guest.rules
sudo rm -f /etc/systemd/system/raspotify.service.d/80-groove-spotify.conf
sudo rm -f /etc/systemd/system/pi-music-alarm.service.d/80-groove-spotify.conf
sudo rm -f /etc/systemd/system/groove-spotify-mode@.service
sudo rm -f /etc/systemd/system/groove-spotify-boot.service
sudo rm -f /run/systemd/system/raspotify.service.d/90-groove-spotify-mode.conf
sudo rm -f /run/systemd/system/raspotify.service.d/99-groove-test-failure.conf
sudo rm -f /etc/tmpfiles.d/groove-spotify.conf
sudo rm -f /usr/local/libexec/groove-spotify-mode
sudo rm -rf -- /run/groove-spotify
.venv/bin/python - <<'PY'
import sqlite3
with sqlite3.connect('instance/alarms.db') as connection:
    connection.execute("INSERT INTO settings(key,value) VALUES('spotify_guest_mode','false') "
                       "ON CONFLICT(key) DO UPDATE SET value=excluded.value")
PY
sudo systemctl daemon-reload
sudo systemctl reset-failed raspotify.service
sudo systemctl start raspotify.service pi-music-alarm.service
systemctl is-active raspotify.service pi-music-alarm.service
```

Se conserva el backup privado `/var/lib/groove-spotify/conf.original` para
recuperación. No elimines `/var/cache/raspotify` ni `credentials.json`.

## Pruebas automatizadas

```bash
.venv/bin/python -m unittest discover -s tests -q
```

Los tests del gestor y de Flask usan dobles sin red/systemd. Las transacciones
del helper usan un sistema de archivos temporal POSIX, un falso `/proc` y
systemd simulado; requieren ejecutarse como root para reproducir las comprobaciones
de propietario/permisos. No acceden a servicios ni credenciales reales:

```bash
sudo /usr/bin/python3 -m unittest tests.test_spotify_mode_helper -v
```

En Windows se omiten las pruebas POSIX; pueden ejecutarse con WSL. Los tests
automatizados no demuestran que Spotify anuncie o autentique correctamente en
tu Wi-Fi: completa la prueba manual con ambas cuentas Premium.
