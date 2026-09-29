"""Capa de reproducción de audio.

`AudioPlayer` define la interfaz que usa el resto de la app.
`LocalAudioPlayer` reproduce el WAV de emergencia (aplay / winsound) y
`FfmpegPlayer` las pistas de la biblioteca local (MP3, OGG, WAV) con ffmpeg.

Reglas comunes:
- `play()` lanza la reproducción; ffmpeg espera una breve comprobación de arranque.
- `play()` nunca lanza excepciones: si algo falla lo registra y devuelve False.
- `play_monitored(..., on_finished)` notifica el final desde el hilo vigilante,
  fuera del lock, solo si arrancó y no fue detenido: True = final normal, False = fallo.
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
        return False  # audio desactivado: nunca anunciar una reproducción real


class LocalAudioPlayer(AudioPlayer):
    """Reproduce un fichero WAV local.

    - Windows: `winsound` de la librería estándar (asíncrono, sin dependencias).
    - Linux / Raspberry Pi OS: `aplay` (paquete alsa-utils) en un subproceso.
      `alsa_device` (p. ej. "plughw:CARD=Device,DEV=0") elige la tarjeta de
      sonido; si no se indica, aplay usa la tarjeta por defecto de ALSA.
    """

    def __init__(self, sound_path, platform=None, alsa_device=None):
        self.sound_path = Path(sound_path)
        self.platform = platform or sys.platform
        self.alsa_device = alsa_device or None
        self._process = None
        self._lock = threading.Lock()

    def play(self):
        return self._play()

    def play_monitored(self, on_finished):
        return self._play(on_finished)

    def _play(self, on_finished=None):
        if not self.sound_path.is_file():
            logger.error("No se encuentra el sonido de la alarma: %s", self.sound_path)
            return False
        if not self._is_valid_wav():
            return False
        try:
            if self.stop() is False:
                return False
            if self.platform == "win32":
                if on_finished is None:
                    self._play_winsound()
                else:
                    self._play_windows_process(on_finished)
            elif self.platform.startswith("linux"):
                self._play_aplay(on_finished)
            else:
                logger.error("Audio local no soportado en la plataforma %s", self.platform)
                return False
        except FileNotFoundError as exc:
            if exc.filename == "aplay":
                logger.error("aplay no está instalado. En Raspberry Pi OS: sudo apt install alsa-utils")
            else:
                logger.exception("Error al reproducir %s", self.sound_path)
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
                    try:
                        self._process.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        self._process.kill()
                        self._process.wait(timeout=2)
                self._process = None
            return True
        except Exception:
            logger.exception("Error al detener el audio")
            return False

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

    def _play_aplay(self, on_finished=None):
        command = ["aplay", "-q"]
        if self.alsa_device:
            command += ["-D", self.alsa_device]
        process = subprocess.Popen(
            command + [str(self.sound_path)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
        )
        with self._lock:
            self._process = process
        # Un hilo ligero espera a aplay para registrar errores sin bloquear.
        threading.Thread(target=self._watch, args=(process, on_finished), daemon=True).start()

    def _play_windows_process(self, on_finished):
        # winsound asíncrono no informa del final. Un proceso aislado permite
        # observar PlaySound síncrono y detenerlo sin carreras con el siguiente WAV.
        process = subprocess.Popen(
            [sys.executable, "-c", "import sys, winsound; "
             "winsound.PlaySound(sys.argv[1], winsound.SND_FILENAME | winsound.SND_NODEFAULT)",
             str(self.sound_path)], stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        with self._lock:
            self._process = process
        threading.Thread(target=self._watch, args=(process, on_finished), daemon=True).start()

    def _watch(self, process, on_finished=None):
        _, stderr = process.communicate()
        with self._lock:
            current = self._process is process
            if current:
                self._process = None
        # Si ya no es el proceso actual, lo ha parado stop(): en Windows
        # terminate() deja código 1, que no es un fallo del reproductor.
        if current and process.returncode and process.returncode > 0:
            message = stderr.decode(errors="replace").strip() if stderr else ""
            logger.error("Reproductor WAV terminó con código %s: %s", process.returncode, message)
        if current and on_finished is not None:
            on_finished(process.returncode == 0)


DEFAULT_MUSIC_DEVICE = "plughw:CARD=Device,DEV=0"


class FfmpegPlayer:
    """Reproduce una pista (MP3, OGG o WAV) con ffmpeg hacia ALSA, en bucle.

    - Un único proceso a la vez: play() para el anterior antes de lanzar otro.
    - Sin shell: la ruta va como argumento suelto y ffmpeg no lee stdin.
    - Si ffmpeg termina durante los primeros `startup_grace` segundos (no
      instalado, fichero corrupto, tarjeta ocupada), play() devuelve False
      para que la alarma use el WAV de emergencia.
    - stop(): SIGTERM y, si no termina en `stop_timeout` s, SIGKILL. Nunca
      deja procesos huérfanos ni lanza excepciones.
    """

    def __init__(self, binary="ffmpeg", alsa_device=DEFAULT_MUSIC_DEVICE,
                 popen=subprocess.Popen, startup_grace=0.5, stop_timeout=2.0):
        self.binary = binary or "ffmpeg"
        self.alsa_device = alsa_device or DEFAULT_MUSIC_DEVICE
        self._popen = popen
        self.startup_grace = startup_grace
        self.stop_timeout = stop_timeout
        self._process = None
        self._lock = threading.Lock()

    def command(self, path):
        return [self.binary, "-hide_banner", "-nostdin", "-loglevel", "error",
                "-stream_loop", "-1", "-i", str(path), "-f", "alsa", self.alsa_device]

    def play(self, path):
        return self._play(path)

    def play_monitored(self, path, on_finished):
        return self._play(path, on_finished)

    def _play(self, path, on_finished=None):
        if not self.stop():
            return False
        try:
            process = self._popen(self.command(path), stdin=subprocess.DEVNULL,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        except FileNotFoundError:
            logger.error("ffmpeg no está instalado (%s). En Raspberry Pi OS: "
                         "sudo apt install -y ffmpeg", self.binary)
            return False
        except Exception:
            logger.exception("No se pudo lanzar ffmpeg para %s", path)
            return False
        try:
            process.wait(timeout=self.startup_grace)
        except subprocess.TimeoutExpired:
            with self._lock:
                self._process = process
            threading.Thread(target=self._watch, args=(process, on_finished), daemon=True).start()
            return True
        except Exception:
            logger.exception("Error esperando a ffmpeg")
            self._terminate(process)
            return False
        # Terminó enseguida: no está sonando nada.
        logger.error("ffmpeg terminó al empezar (código %s) con %s: %s",
                     process.returncode, Path(path).name, _stderr_text(process))
        return False

    def stop(self):
        with self._lock:
            process = self._process
            if process is None:
                return True
            if not self._terminate(process):
                return False  # conservar el proceso para reintentar STOP
            self._process = None
            return True

    def _terminate(self, process):
        try:
            if process.poll() is not None:
                return True
            process.terminate()
            try:
                process.wait(timeout=self.stop_timeout)
                return True
            except subprocess.TimeoutExpired:
                logger.warning("ffmpeg no terminó en %s s: se fuerza (kill)", self.stop_timeout)
            process.kill()
            process.wait(timeout=self.stop_timeout)
            return True
        except Exception:
            logger.exception("No se pudo detener ffmpeg")
            return False

    def _watch(self, process, on_finished=None):
        message = _stderr_text(process)
        process.wait()
        # Código negativo = lo hemos parado nosotros (SIGTERM/SIGKILL).
        if process.returncode and process.returncode > 0:
            logger.error("ffmpeg terminó con código %s: %s", process.returncode, message)
        with self._lock:
            current = self._process is process
            if current:
                self._process = None
        if current and on_finished is not None:
            # Una pista en bucle nunca debe terminar por sí sola, incluso con código 0.
            on_finished(False)


def _stderr_text(process):
    try:
        data = process.stderr.read() if process.stderr is not None else b""
    except Exception:
        return ""
    return data.decode(errors="replace").strip() if data else ""


def create_music_player(config, platform=None):
    """FfmpegPlayer para la biblioteca local, o None si no se puede usar aquí."""
    platform = platform or sys.platform
    if config.get("AUDIO_BACKEND", "local") != "local" or not platform.startswith("linux"):
        return None  # Windows (desarrollo) o audio desactivado: solo WAV de emergencia
    return FfmpegPlayer(config.get("FFMPEG_BINARY") or "ffmpeg",
                        config.get("ALSA_DEVICE") or DEFAULT_MUSIC_DEVICE)


def create_player(config):
    """Construye el reproductor según la configuración de la app."""
    backend = config.get("AUDIO_BACKEND", "local")
    if backend == "local":
        return LocalAudioPlayer(config["SOUND_PATH"], alsa_device=config.get("ALSA_DEVICE"))
    if backend == "none":
        return NullAudioPlayer()
    raise ValueError(f"AUDIO_BACKEND desconocido: {backend}")
