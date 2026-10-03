# Arranque Spotify y recuperación tras un router tardío

Implementado localmente el 03/10/2026. Falta comprobarlo con el router y la Raspberry reales.

## Plazo del intento de alarma

`ALARM_SPOTIFY_START_TIMEOUT_SECONDS=20` es el valor por defecto, aunque no se añada a `.env`. Un único plazo monotónico comprende el bloqueo de mantenimiento, las comprobaciones y cambios de invitados, SQLite, el token, DNS, HTTPS, metadata, búsqueda de dispositivo, transferencia, volumen, reproducción y esperas entre reintentos. No hace falta instalar dependencias nuevas.

Si Spotify no arranca dentro del plazo, se usa la música local o el WAV existente. Los 20 segundos corresponden al intento Spotify: la pausa Bluetooth anterior y el arranque del reproductor local tienen sus propios tiempos. Hay que medir el sonido físico en la Pi.

STOP cancela las esperas y corta el socket HTTP en curso. Solo la resolución DNS puede continuar en un hilo, compartido por destino, sin capacidad para enviar una orden de reproducción. Durante estos intentos se usa HTTPS directo con verificación TLS a los dos endpoints oficiales de Spotify; no se usan proxies de entorno. El cliente de la interfaz conserva su transporte habitual.

Una orden de reproducción ya enviada puede haberse ejecutado aunque se pierda su respuesta. En ese caso se intenta confirmar una pausa durante **hasta 2 segundos adicionales**. Si no se confirma, la alarma queda en **parada pendiente**, con los reintentos limitados de STOP existentes, sin abrir simultáneamente el respaldo ni iniciar un fade. No se presenta ese caso como silencio confirmado.

Acortar el timeout del cliente `systemctl` no cancela una transacción del helper de invitados que ya está ejecutándose como unidad systemd. Esa transacción puede terminar y restaurar la identidad privada después de arrancar el respaldo; no continúa con órdenes HTTP de alarma. El helper conserva su bloqueo y rollback propios.

La espera del cliente `systemctl` del helper también se revisa cada 100 ms: STOP lo termina y recoge su proceso. Eso cancela la espera de Groove, con el límite de transacción externa explicado arriba.

## Raspotify cuando vuelve Internet

En Linux, `RASPOTIFY_NETWORK_RECOVERY=on` activa un job del scheduler cada **30 segundos**, independiente de que haya alarmas programadas. Se arma al iniciar Groove y cuando falla la conexión con Spotify. Cada comprobación tiene un presupuesto de 10 segundos.

1. Confirma la conexión con **dos lecturas válidas de la API Spotify**. Estar asociado al Wi-Fi no basta.
2. Comprueba si el dispositivo configurado aparece y si el servicio está activo. Si ambas cosas están bien, no lo reinicia y cierra el episodio.
3. Si sigue ausente, o el servicio está `failed`/`inactive`, puede reiniciar únicamente el servicio Raspotify configurado y habilitado. No toca servicios deshabilitados, enmascarados o en transición.
4. Permite hasta **tres intentos por episodio**, separados al menos **60 segundos**. Cuenta también los reinicios rechazados o no confirmados. Después sigue consultando el estado, sin nuevos reinicios hasta otro episodio de red o arranque.

Se aplaza mientras haya una alarma activa, un arranque o mantenimiento Spotify, un cambio de invitados, invitados activos o identidad desconocida. Los errores de autenticación, límites 429, respuestas 5xx y nombres ambiguos no justifican reiniciar. El preflight conserva su reinicio preventivo existente y comparte el bloqueo de mantenimiento.

Se necesita Spotify vinculado y el dispositivo de Groove seleccionado o su nombre configurado. La recuperación empieza cuando está ejecutándose la aplicación y su scheduler; no cambia la sincronización de la hora ni recupera alarmas vencidas durante un apagón.

Se reutiliza la regla de polkit de `deploy/install-raspotify-permission.sh`, limitada a reiniciar el servicio elegido. Si ya está instalada para el usuario del servicio Groove, basta con actualizar código y reiniciar la aplicación. Si el log muestra un rechazo de permisos, ejecutar desde la carpeta del proyecto:

```bash
bash deploy/install-raspotify-permission.sh
sudo systemctl restart pi-music-alarm
```

## Pruebas prácticas

Validación local del 03/10/2026 en Windows: **801 tests, OK, 20 omitidos** (781 ejecutados correctamente), incluyendo 37 pruebas nuevas. El proceso de espera/cancelación del cliente se comprueba también con un subprocess real; Spotify y systemd se simulan. `git diff --check` pasa.

1. **Router tardío:** apagar el router y reiniciar la Pi de forma ordenada. Encender el router después. Sin reiniciar Raspotify a mano, esperar dos consultas válidas (habitualmente 30–60 s desde que Groove puede consultar Spotify) y comprobar que Groove reaparece. Probar una alarma Spotify y STOP. Puede necesitar más de un intento; revisar el log.
2. **Arranque normal:** reiniciar con Internet disponible. Si Groove aparece normalmente, el monitor no debe ordenar reinicios. Una alarma debe reproducirse como antes.
3. **Internet caído:** con la hora correcta, probar una alarma Spotify sin acceso a Internet y cronometrar el respaldo. El intento Spotify debe acabar dentro de 20 s, más el tiempo de las operaciones de audio anteriores/posteriores. No debe aparecer música Spotify tardía al recuperar Internet.
4. **STOP durante conexión:** pulsar STOP mientras intenta arrancar. Restaurar Internet y esperar: no debe empezar una reproducción por ese intento cancelado. Si la orden ya llegó a Spotify y no se pudo confirmar su pausa, comprobar el aviso y la recuperación de parada pendiente.
5. **Invitados:** activar invitados y repetir la caída de red. La recuperación debe aplazarse sin expulsar al invitado ni cambiar la identidad. Al volver a privado y quedar sin alarma activa, debe poder recuperarse.
6. **Límites y servicios:** comprobar en una instalación de prueba un reinicio rechazado por permisos y servicio `failed`/`inactive`. Deben registrarse como máximo tres intentos por episodio, sin bucle rápido. Un servicio deshabilitado deliberadamente debe permanecer así.

Para observar las órdenes y sus resultados:

```bash
journalctl -u pi-music-alarm --since "10 minutes ago"
journalctl -u raspotify --since "10 minutes ago"
systemctl status raspotify --no-pager
```

Registrar fecha, versión desplegada, tiempos, presencia de Groove, sonido real y STOP. Las pruebas automatizadas usan red y systemd simulados; no acreditan permisos ni conectividad reales.
