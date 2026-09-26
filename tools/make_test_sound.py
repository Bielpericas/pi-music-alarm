"""Genera un WAV de prueba (tres pitidos, ~2 s) sin dependencias externas.

Uso:
    python tools/make_test_sound.py              # crea sounds/alarm.wav
    python tools/make_test_sound.py otra/ruta.wav
"""
import math
import struct
import sys
import wave
from pathlib import Path

RATE = 16000      # Hz; suficiente para un pitido y el fichero ocupa ~60 KB
FREQ = 880        # La5
VOLUME = 0.5


def beep_samples(seconds, freq=FREQ):
    n = int(RATE * seconds)
    fade = int(RATE * 0.01)  # 10 ms de fundido para evitar "clics"
    for i in range(n):
        envelope = min(1.0, i / fade, (n - i) / fade)
        yield VOLUME * envelope * math.sin(2 * math.pi * freq * i / RATE)


def silence(seconds):
    return [0.0] * int(RATE * seconds)


def main():
    default = Path(__file__).resolve().parent.parent / "sounds" / "alarm.wav"
    out = Path(sys.argv[1]) if len(sys.argv) > 1 else default
    out.parent.mkdir(parents=True, exist_ok=True)

    samples = []
    for _ in range(3):
        samples += list(beep_samples(0.35))
        samples += silence(0.25)

    with wave.open(str(out), "wb") as wav:
        wav.setnchannels(1)       # mono
        wav.setsampwidth(2)       # PCM 16 bits
        wav.setframerate(RATE)
        wav.writeframes(b"".join(struct.pack("<h", int(s * 32767)) for s in samples))

    print(f"Sonido de prueba creado en {out}")


if __name__ == "__main__":
    main()
