# Recuperación de alarmas tras una parada fallida

Implementado el 2 de octubre de 2026 y versionado en el commit local `3744ebf`. El 3 de octubre el usuario confirma que ha hecho las pruebas de la mejora 1 en la Raspberry y que funciona. No se han aportado resultados individuales ni SHA de la Pi; véase [HE-20 del historial](../historial/historialcodex.md#he-20).

## Política

- Un STOP, snooze o auto-stop que no consigue detener el audio mantiene `stop_pending`, cancela el fade y conserva Bluetooth pausado. No se confirma una parada que ha fallado.
- Se programan hasta **cinco reintentos de STOP**, con **30 segundos** entre intentos. Se cuenta cada intento automático; pulsar STOP sigue siendo posible y no multiplica los jobs ni reinicia el límite. Tras agotar los intentos se mantiene el aviso y se requiere intervención manual.
- Cada reintento comprueba la generación de reproducción y su identidad. Una ejecución antigua o duplicada no puede detener una alarma nueva.
- Una alarma programada rechazada por `stop_pending` queda reservada y se revisa cada minuto durante **dos minutos desde su hora original**. No aparece en la lista devuelta de reproducciones iniciadas y una alarma de una vez permanece activada mientras espera.
- Cuando se acepta el arranque se cierra el intento y se desactiva la alarma de una vez. Los resultados `failed` y `cancelled` también cierran el intento, con su resultado explícito, sin presentarlo como reproducción.
- Si vence el margen, el intento queda `expired`, visible en la web y en el log. La alarma de una vez se desactiva para evitar que reaparezca al día siguiente; se puede volver a activar o editar deliberadamente. Una recurrente conserva sus días y sigue habilitada para su siguiente horario.
- Editar, desactivar o borrar una alarma cancela su reintento de disparo programado pendiente. No cancela un sonido que ya haya sido iniciado; se mantiene STOP como control de reproducción.
- Si vence un snooze mientras otra alarma tiene una parada pendiente, se conserva en memoria y se reintenta cada 30 segundos dentro de los mismos dos minutos desde su vencimiento original. Los reintentos no aumentan el contador de snoozes ni amplían el margen. Cancelarlo o borrar la alarma cancela también ese reintento; tras agotar el margen se registra la omisión en el log. Sigue sin persistirse al reiniciar.
- Los reintentos pendientes se procesan antes de las alarmas del minuto actual. Para una misma hora se usa el ID ascendente y se conserva la regla histórica de sustitución: la última aceptada gana. No se promete reproducción completa de todas las alarmas coincidentes.

## Reserva, persistencia y reinicio

SQLite mantiene la reserva antes de iniciar audio, en una transacción corta; no se mantiene una transacción abierta durante la reproducción. Un segundo scheduler no puede reservar el mismo disparo ni el mismo reintento en curso.

La tabla `alarm_triggers` guarda **un único último intento por alarma**, con hora original, límite, próxima comprobación y resultado. Se crea también en instalaciones existentes y se borra la fila al borrar la alarma. No es un historial completo de reproducciones ni una prueba de sonido audible.

Los rechazos confirmados sobreviven al reinicio y pueden reintentarse si todavía están dentro del margen. Al iniciar el único scheduler de producción, un intento que quedó `starting` se marca `interrupted`: pudo haber iniciado sonido antes del corte, por lo que no se repite automáticamente. Las alarmas de una vez se desactivan en ese caso y la interfaz muestra que no se confirmó el resultado. Las recurrentes conservan su siguiente horario.

Los reintentos de STOP y el estado de reproducción permanecen en memoria, igual que los temporizadores existentes. Esta mejora no añade recuperación general de minutos perdidos. El parche posterior del 03/10 limita el intento Spotify y recupera Raspotify tras un router tardío; véase [arranque y recuperación Spotify](spotify-startup-recovery.md).

## Validación local

Suite completa ejecutada en Windows el 02/10/2026: **764 tests, sin fallos, 20 omitidos** por sus requisitos de plataforma. Incluye 19 pruebas nuevas de recuperación, límites, cancelación, snoozes, reinicios, concurrencia y actualización del estado web. `git diff --check` también pasó. Los reproductores de estas pruebas están simulados; el resultado no acredita sonido ni permisos reales en la Raspberry.

## Plan de pruebas en la Pi

El usuario ha confirmado el funcionamiento tras realizar las pruebas. Los escenarios siguientes se conservan como procedimiento para repetirlas y registrar resultados concretos.

1. Iniciar una alarma Spotify, simular un fallo temporal al pausar y comprobar `stop_pending`. Recuperar la conectividad y verificar que un reintento automático confirma la parada y devuelve Bluetooth.
2. Repetir con auto-stop, sin pulsar STOP después del fallo.
3. Programar una alarma de una vez para el minuto siguiente durante la parada pendiente. Comprobar que sigue habilitada, muestra «Inicio pendiente» y arranca una sola vez si se recupera dentro del margen.
4. Mantener el fallo más de dos minutos: comprobar «No iniciada», la desactivación de la puntual y que no vuelve a sonar al día siguiente. Una recurrente debe conservar su próximo horario.
5. Agotar los cinco reintentos: comprobar que no hay un ciclo indefinido y que STOP manual sigue funcionando.
6. Cancelar o editar la alarma mientras espera y comprobar que no arranca por el reintento anterior. Ejecutar una nueva reproducción después de recuperar STOP y verificar que los jobs anteriores no la detienen.

Registrar fecha, versión, pasos, resultado y evidencia del sonido y de la parada. Los tests locales usan reproductores simulados, SQLite temporal y relojes controlados.
