# Relevo y restauración de las fuentes de audio

Implementado localmente el 03/10/2026 sobre `a5ac2e9`. Pendiente de probar sonido y servicios reales en la Raspberry.

## Qué cambia

Antes, sustituir una alarma local ignoraba si su parada había funcionado. Al pasar de Spotify a local, la pausa llegaba después de abrir el sonido nuevo. Bluetooth anotaba «pausado» antes de confirmar STOP y olvidaba la restauración incluso si START fallaba.

Ahora el gestor confirma la parada de la alarma anterior **antes de iniciar la siguiente**, también al sustituir Spotify por Spotify. Si falla, conserva la anterior como **Parada pendiente** y programa los reintentos de STOP existentes. La nueva alarma programada conserva la reserva y el margen de dos minutos de la mejora 1. Una prueba manual rechazada debe volver a solicitarse tras recuperar la parada.

Bluetooth comprueba el estado real después de STOP/START. No basta con que el comando devuelva éxito. Si la salida no se confirma libre, Groove **aplaza el arranque** y muestra el motivo; no abre aplay, ffmpeg ni Spotify encima de un servicio cuya parada es incierta. STOP puede reintentar la liberación y los cinco reintentos automáticos conservan su límite. Desactivar explícitamente la gestión Bluetooth (`BLUETOOTH_SERVICE=none`) mantiene el comportamiento sin gestión.

Se conserva si Bluetooth estaba activo antes de tomar la salida. Si ya estaba parado, no se arranca después. Un estado inicial desconocido tampoco autoriza activar automáticamente un servicio que quizá estuviera parado deliberadamente. Las transiciones no desconectan ni desemparejan dispositivos.

Si el receptor Bluetooth no vuelve a arrancar tras terminar una alarma, se conserva la intención de restaurarlo. Hay **tres reintentos adicionales cada 30 s**, después del intento inicial. La web muestra el pendiente y ofrece **Reintentar Bluetooth**. Un job viejo o duplicado no puede devolver Bluetooth durante otra alarma o reproducción manual controlada. El botón tampoco reinicia el contador automático. Se restaura el receptor, sin prometer que una canción Bluetooth vaya a continuar sola.

Si la música local falla al arrancar, se confirma la limpieza de su proceso antes de abrir el WAV. Un proceso que no pudo detenerse se conserva para STOP, también si falló la espera inicial de ffmpeg. El mismo criterio se aplica a un arranque fallido del WAV.

## Spotify manual dentro de Groove

Los botones **Transferir**, **Play** y **Pause** comparten la exclusión con el gestor de alarmas. Durante una alarma activa, conexión o parada pendiente se rechazan con un aviso para usar STOP.

Transferir/Play reclaman la salida Bluetooth y conservan el dispositivo al que se envió la orden. Pause devuelve Bluetooth tras confirmar la pausa del dispositivo controlado. Pausar otro dispositivo no libera una salida que sigue asignada al anterior. Una orden cuya respuesta se pierde conserva su destino hasta confirmar pausa; un rechazo conocido sin una reproducción previa permite devolver Bluetooth.

Al llegar una alarma, primero se pausa esa reproducción manual. Si no se confirma, se conserva el control y se aplaza la alarma. Los controles manuales tienen un plazo compartido de 10 s para adquirir el gestor, operaciones Bluetooth y peticiones Spotify. La expiración del temporizador de sueño comparte el mismo orden de locks y devuelve Bluetooth cuando pausa la reproducción controlada. Preflight y recuperación de Raspotify se aplazan durante ese control manual.

**Alcance:** esto coordina las acciones que pasan por Groove. No añade un monitor de reproducción iniciada directamente desde la app de Spotify del móvil, ni de otra cuenta en modo invitados. Ese arbitraje externo y su política de reanudación siguen pendientes. El estado de propiedad y las tareas de restauración permanecen en memoria; un reinicio no los reconstruye.

## Pruebas sin apagar el router

1. **Spotify → local:** iniciar una alarma Spotify y probar una local distinta. La primera debe pausarse antes de comenzar la segunda; no deben superponerse. Repetir local → Spotify y Spotify → Spotify.
2. **Bluetooth → alarma:** poner música Bluetooth, probar una alarma local y otra Spotify. El receptor debe detenerse para liberar la tarjeta. Tras STOP debe volver a estar disponible. Repetir con snooze, auto-stop y dos alarmas seguidas; no debe arrancarse entre las dos.
3. **Bluetooth previamente parado:** parar deliberadamente `bluealsa-aplay`, probar una alarma y pulsar STOP. Debe seguir parado. Restaurarlo manualmente al terminar el ensayo si se quiere usar.
4. **Controles manuales:** desde la página Spotify de Groove, Transferir/Play y después Pause. Comprobar cesión y devolución Bluetooth. Con una alarma sonando, probar esos tres botones: deben mostrar el aviso y conservar el sonido de la alarma.
5. **Temporizador de sueño:** iniciar Spotify con Play de Groove y programar un temporizador. Al vencer, debe pausar el dispositivo correspondiente y devolver Bluetooth. Una alarma iniciada antes debe anularlo; su callback anterior no puede pausar la nueva alarma.
6. **Fallos controlados, en una instalación de prueba:** denegar STOP Bluetooth o la parada del audio anterior. Debe aparecer Parada pendiente sin abrir otra fuente. Recuperar permisos y comprobar STOP/reintento. Denegar START Bluetooth después de la alarma: debe quedar el aviso y producirse como máximo tres reintentos adicionales, con recuperación automática o por el botón manual al corregir el fallo.

Registrar fecha, versión, fuentes, orden audible, estado web y extracto de `journalctl -u pi-music-alarm --since "10 minutes ago"`. Los fallos se cubren también mediante dobles automatizados; las pruebas físicas siguen pendientes.

## Validación local

Windows, SQLite temporal, servicios y reproducción simulados. Suite completa final: **830 tests en 80,417 s, OK (skipped=20)**; 810 ejecutados correctamente. Se añaden 29 pruebas de transiciones, limpieza, estados observados, conservación del destino manual, límites, callbacks antiguos/duplicados, concurrencia con preflight, STOP durante la liberación de fuentes, temporizador y estado web. No hay nuevas dependencias, servicios ni permisos.
