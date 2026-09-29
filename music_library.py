"""Biblioteca de música local (instance/music/) para las alarmas.

- `MusicLibrary` descubre las pistas (.mp3, .ogg, .wav) de UNA carpeta plana,
  valida nombres (nada de rutas: solo un nombre de fichero dentro de la
  carpeta), sube y borra ficheros. Es la única que toca esa carpeta.
- `LocalMusic` elige la pista de una alarma (una concreta o al azar) y la
  hace sonar con el reproductor de pistas (FfmpegPlayer).

En la base de datos cada alarma guarda solo el nombre del fichero
(`local_track`) o NULL = Aleatorio. Si el fichero ya no existe, la alarma no
se rompe: se registra y suena el WAV de emergencia (ver playback.py).
"""
import logging
import os
import random
import threading
import uuid
from dataclasses import dataclass
from pathlib import Path

from werkzeug.utils import secure_filename

logger = logging.getLogger("alarms")

EXTENSIONS = (".mp3", ".ogg", ".wav")
UPLOAD_CHUNK = 64 * 1024
TEMP_PREFIX = ".upload-"  # ficheros a medio subir: ocultos, nunca se listan


@dataclass(frozen=True)
class Track:
    name: str        # nombre del fichero: lo único que se guarda en la BD
    size: int        # bytes

    @property
    def kind(self):
        return self.name.rsplit(".", 1)[-1].upper()


class UploadError(Exception):
    """Subida rechazada; el mensaje se muestra tal cual en la interfaz."""


def is_safe_name(name):
    """True si `name` es solo un nombre de fichero admitido (sin rutas ni ocultos)."""
    if not isinstance(name, str) or not name or len(name) > 255:
        return False
    if name != name.strip() or "\0" in name or "/" in name or "\\" in name:
        return False
    if name.startswith(".") or ":" in name:  # ocultos, "..", unidades de Windows
        return False
    return name.lower().endswith(EXTENSIONS)


def sniff_format(header):
    """Extensión que corresponde a los primeros bytes, o None si no es MP3/OGG/WAV."""
    if header[:4] == b"RIFF" and header[8:12] == b"WAVE":
        return ".wav"
    if header[:4] == b"OggS":
        return ".ogg"
    if header[:3] == b"ID3":
        return ".mp3"
    if len(header) >= 2 and header[0] == 0xFF and header[1] & 0xE0 == 0xE0:
        return ".mp3"  # MP3 sin etiqueta ID3: empieza por una cabecera de frame
    return None


class MusicLibrary:
    def __init__(self, root):
        self.root = Path(root)
        self._lock = threading.Lock()  # subidas y borrados, uno a la vez

    def ensure_dir(self):
        """Crea la carpeta si falta. Nunca rompe Groove: sin carpeta = biblioteca vacía."""
        try:
            self.root.mkdir(parents=True, exist_ok=True)
            return True
        except OSError:
            logger.exception("No se pudo crear la biblioteca de música %s", self.root)
            return False

    # --- Lectura ---

    def tracks(self):
        """Pistas disponibles, en orden estable (alfabético sin mayúsculas)."""
        try:
            entries = list(self.root.iterdir())
        except OSError:
            return []  # no existe o no se puede leer: biblioteca vacía
        found = []
        for entry in entries:
            if not is_safe_name(entry.name) or self.path(entry.name) is None:
                continue
            try:
                found.append(Track(entry.name, entry.stat().st_size))
            except OSError:
                continue
        return sorted(found, key=lambda track: (track.name.casefold(), track.name))

    def names(self):
        return [track.name for track in self.tracks()]

    def path(self, name):
        """Ruta de la pista `name` si es válida y existe DENTRO de la carpeta; si no, None."""
        if not is_safe_name(name):
            return None
        candidate = self.root / name
        try:
            root = self.root.resolve()
            resolved = candidate.resolve()
        except OSError:
            return None
        if resolved.parent != root or not resolved.is_file():
            return None  # p. ej. un enlace simbólico que apunta fuera
        return candidate

    def pick(self, selection, choice=random.choice):
        """Pista para una alarma: `selection` concreta, o al azar si es None/"".

        Devuelve la ruta o None (biblioteca vacía o pista que ya no existe),
        registrando el motivo.
        """
        if selection:
            path = self.path(selection)
            if path is None:
                logger.warning("La pista «%s» ya no está en la biblioteca", selection)
            return path
        names = self.names()
        if not names:
            logger.warning("La biblioteca de música (%s) está vacía", self.root)
            return None
        return self.root / choice(names)

    # --- Gestión desde la web ---

    def save_upload(self, filename, stream, max_bytes):
        """Guarda una subida y devuelve su nombre final. Lanza UploadError.

        - El nombre se sanea (secure_filename) y solo se admiten .mp3/.ogg/.wav.
        - El contenido debe coincidir con la extensión (cabecera del fichero).
        - Se escribe a un temporal oculto dentro de la carpeta y se enlaza con su
          nombre sin sobrescribir nunca un fichero existente.
        """
        name = secure_filename(filename or "")
        if not name or not name.lower().endswith(EXTENSIONS):
            raise UploadError("Tipo de archivo no admitido. Sube un MP3, OGG o WAV.")
        stem, ext = name.rsplit(".", 1)
        name = f"{stem}.{ext.lower()}"
        if not is_safe_name(name):
            raise UploadError("El nombre del archivo no es válido.")
        if not self.ensure_dir():
            raise UploadError("No se pudo crear la carpeta de la biblioteca.")

        temp = self.root / f"{TEMP_PREFIX}{uuid.uuid4().hex}.part"
        try:
            self._write_limited(temp, stream, max_bytes)
            with temp.open("rb") as handle:
                header = handle.read(12)
            if not header:
                raise UploadError("El archivo está vacío.")
            if sniff_format(header) != os.path.splitext(name)[1]:
                raise UploadError(f"«{name}» no parece un {ext.upper()} válido.")
            with self._lock:
                self._publish(temp, self.root / name)
        except UploadError:
            raise
        except OSError:
            logger.exception("No se pudo guardar «%s» en la biblioteca", name)
            raise UploadError("No se pudo guardar el archivo en la Raspberry.") from None
        finally:
            try:
                temp.unlink()
            except OSError:
                pass
        logger.info("Música subida a la biblioteca: %s", name)
        return name

    def delete(self, name):
        """Borra la pista `name` de la biblioteca. False si no es una pista de la biblioteca."""
        with self._lock:
            path = self.path(name)
            if path is None:
                return False
            path.unlink()
        logger.info("Música borrada de la biblioteca: %s", name)
        return True

    def _write_limited(self, temp, stream, max_bytes):
        written = 0
        with temp.open("xb") as handle:
            while True:
                chunk = stream.read(UPLOAD_CHUNK)
                if not chunk:
                    break
                written += len(chunk)
                if written > max_bytes:
                    raise UploadError(too_big_message(max_bytes))
                handle.write(chunk)

    @staticmethod
    def _publish(temp, target):
        """Da al temporal su nombre final sin sobrescribir nunca nada."""
        if target.exists():
            raise UploadError(f"Ya existe «{target.name}» en la biblioteca. "
                              "Bórralo antes o cambia el nombre del archivo.")
        try:
            os.link(temp, target)  # atómico: falla si el destino ya existe
        except FileExistsError:
            raise UploadError(f"Ya existe «{target.name}» en la biblioteca.") from None
        except OSError:
            # Sistemas de ficheros sin enlaces duros: se comprobó justo antes.
            os.replace(temp, target)


def too_big_message(max_bytes):
    return f"El archivo es demasiado grande (máximo {max_bytes // (1024 * 1024)} MB)."


class LocalMusic:
    """Hace sonar la música local de una alarma: su pista o una al azar."""

    def __init__(self, library, player, choice=random.choice):
        self.library = library
        self.player = player      # FfmpegPlayer: play(path) -> bool, stop()
        self._choice = choice

    def play(self, alarm):
        return self._play(alarm)

    def play_monitored(self, alarm, on_finished):
        return self._play(alarm, on_finished)

    def _play(self, alarm, on_finished=None):
        """Devuelve el nombre de la pista que suena, o None si no ha podido sonar."""
        name = alarm["name"]
        try:
            path = self.library.pick(alarm.get("local_track"), self._choice)
        except Exception:
            logger.exception("Error al elegir la música local de «%s»", name)
            return None
        if path is None:
            return None
        started = (self.player.play_monitored(path, on_finished=on_finished)
                   if on_finished is not None else self.player.play(path))
        if not started:
            logger.warning("No se pudo reproducir «%s» para «%s»", path.name, name)
            return None
        how = "elegida" if alarm.get("local_track") else "al azar"
        logger.info("Música local para «%s»: %s (%s)", name, path.name, how)
        return path.name

    def stop(self):
        return self.player.stop()
