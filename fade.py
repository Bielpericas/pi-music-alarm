"""Fade-in de volumen para alarmas Spotify.

`fade_plan()` calcula los pasos: cada FADE_STEP_SECONDS segundos sube un poco,
y el último paso es exactamente el volumen final. Con 20 → 60 % en 5 min son
20 peticiones (una cada 15 s), muy lejos de los límites de la API de Spotify.

`VolumeFade` ejecuta el plan en un único hilo daemon que termina solo al
acabar, al fallar o al cancelarse. La espera usa un Event, así que cancel()
despierta el hilo al momento: no quedan hilos ni timers huérfanos.
"""
import logging
import math
import threading
import time

logger = logging.getLogger("alarms")

FADE_STEP_SECONDS = 15


def fade_plan(start, end, seconds, step=FADE_STEP_SECONDS):
    """Devuelve [(segundos_desde_el_inicio, volumen), ...].

    No incluye el volumen inicial (se fija antes de reproducir). Se omiten
    pasos que repetirían el mismo volumen, y el último es exactamente `end`.
    """
    if start == end:
        return []
    if seconds <= 0:
        return [(0, end)]
    steps = max(1, math.ceil(seconds / step))
    plan, last = [], start
    for k in range(1, steps + 1):
        # int() trunca hacia el volumen inicial: el objetivo llega justo al final.
        volume = end if k == steps else start + int((end - start) * k / steps)
        if volume != last:
            plan.append((seconds * k / steps, volume))
            last = volume
    return plan


class VolumeFade:
    """Aplica un fade_plan llamando a set_volume(volumen) -> bool.

    Si set_volume falla (devuelve False o lanza), el fade se detiene y la
    música sigue sonando con el último volumen conseguido.
    """

    def __init__(self, set_volume, plan, wait=None, clock=time.monotonic):
        self._set_volume = set_volume
        self.plan = list(plan)
        self._cancelled = threading.Event()
        self._wait = wait or self._cancelled.wait  # wait(segundos) -> True si se canceló
        self._clock = clock
        self._thread = None
        self.last_volume = None

    def start(self):
        self._thread = threading.Thread(target=self.run, name="volume-fade", daemon=True)
        self._thread.start()
        return self

    def cancel(self):
        self._cancelled.set()

    @property
    def cancelled(self):
        return self._cancelled.is_set()

    def is_alive(self):
        return self._thread is not None and self._thread.is_alive()

    def run(self):
        began = self._clock()
        for at, volume in self.plan:
            remaining = at - (self._clock() - began)
            if remaining > 0 and self._wait(remaining):
                return
            if self.cancelled:
                return
            try:
                ok = self._set_volume(volume)
            except Exception:
                logger.exception("Error ajustando el volumen a %s%%", volume)
                ok = False
            if not ok:
                logger.warning("Fade-in interrumpido; se queda en %s",
                               f"{self.last_volume}%" if self.last_volume is not None
                               else "el volumen inicial")
                return
            self.last_volume = volume
        if self.plan:
            logger.info("Fade-in completado: %s%%", self.last_volume)


def start_fade(set_volume, plan):
    """Fábrica por defecto: crea el fade y lo arranca en su hilo."""
    return VolumeFade(set_volume, plan).start()
