"""Capa de reproducción de audio.

`AudioPlayer` define la interfaz que usa el resto de la app. De momento solo
existe `LocalAudioPlayer` (fichero WAV local); más adelante se podrá añadir un
`SpotifyAudioPlayer` sin tocar el scheduler ni las rutas.

Reglas comunes:
- `play()` nunca bloquea: lanza la reproducción y vuelve enseguida.
- `play()` nunca lanza excepciones: si algo falla lo registra y devuelve False.
"""
import logging
import subprocess
import sys
import threading
import wave
from pathlib import Path

logger = logging.getLogger("alarms")


class AudioPlayer:
    """Interfaz común para todos los reproductores."""

    def play(self):
        """Empieza a sonar sin bloquear. Devuelve True si se ha lanzado."""
        raise NotImplementedError

    def stop(self):
        """Detiene lo que esté sonando (si hay algo)."""


class NullAudioPlayer(AudioPlayer):
    """No reproduce nada. Útil para desactivar el audio."""

    def play(self):
        return True


class LocalAudioPlayer(AudioPlayer):
    """Reproduce un fichero WAV local.

    - Windows: `winsound` de la librería estándar (asíncrono, sin dependencias).
    - Linux / Raspberry Pi OS: `aplay` (paquete alsa-utils) en un subproceso.
    """

    def __init__(self, sound_path, platform=None):
        self.sound_path = Path(sound_path)
        self.platform = platform or sys.platform
        self._process = None
        self._lock = threading.Lock()

    def play(self):
        if not self.sound_path.is_file():
            logger.error("No se encuentra el sonido de la alarma: %s", self.sound_path)
            return False
        if not self._is_valid_wav():
            return False
        try:
            self.stop()
            if self.platform == "win32":
                self._play_winsound()
            elif self.platform.startswith("linux"):
                self._play_aplay()
            else:
                logger.error("Audio local no soportado en la plataforma %s", self.platform)
                return False
        except Exception:
            logger.exception("Error al reproducir %s", self.sound_path)
            return False
        return True

    def stop(self):
        try:
            if self.platform == "win32":
                import winsound

                winsound.PlaySound(None, 0)
            with self._lock:
                if self._process is not None and self._process.poll() is None:
                    self._process.terminate()
                self._process = None
        except Exception:
            logger.exception("Error al detener el audio")

    def _is_valid_wav(self):
        """Comprueba la cabecera: en modo asíncrono los errores no llegarían."""
        try:
            with wave.open(str(self.sound_path), "rb") as wav:
                wav.getnframes()
            return True
        except (wave.Error, EOFError, OSError) as exc:
            logger.error("%s no es un WAV PCM válido: %s", self.sound_path, exc)
            return False

    def _play_winsound(self):
        import winsound

        # SND_ASYNC: vuelve inmediatamente y el sonido sigue en segundo plano.
        # SND_NODEFAULT: si falla, no suena el "ding" de Windows.
        winsound.PlaySound(
            str(self.sound_path),
            winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_NODEFAULT,
        )

    def _play_aplay(self):
        process = subprocess.Popen(
            ["aplay", "-q", str(self.sound_path)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        with self._lock:
            self._process = process
        # Un hilo ligero espera a aplay para registrar errores sin bloquear.
        threading.Thread(target=self._watch, args=(process,), daemon=True).start()

    def _watch(self, process):
        _, stderr = process.communicate()
        # Código negativo = lo hemos parado nosotros con terminate().
        if process.returncode and process.returncode > 0:
            message = stderr.decode(errors="replace").strip() if stderr else ""
            logger.error("aplay terminó con código %s: %s", process.returncode, message)


def create_player(config):
    """Construye el reproductor según la configuración de la app."""
    backend = config.get("AUDIO_BACKEND", "local")
    if backend == "local":
        return LocalAudioPlayer(config["SOUND_PATH"])
    if backend == "none":
        return NullAudioPlayer()
    raise ValueError(f"AUDIO_BACKEND desconocido: {backend}")
