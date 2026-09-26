"""Genera los iconos de Groove (PWA) sin dependencias: SVG + PNG (zlib).

Dibujo: fondo azul noche, un sol ámbar saliendo por el horizonte y un surco
(arco) como de vinilo. Se ejecuta una vez; los ficheros resultantes son
pequeños y viven en static/icons/.

    python tools/make_icons.py
"""
import math
import struct
import zlib
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "static" / "icons"

NIGHT = (0x0F, 0x18, 0x30)
SUN = (0xFF, 0xB5, 0x47)
INK = (0xEE, 0xF1, 0xFB)

HORIZON_Y = 0.64
SUN_R = 0.2
ARC_R, ARC_W = 0.31, 0.035
LINE_X0, LINE_X1, LINE_W = 0.2, 0.8, 0.035
CORNER = 0.22  # radio de las esquinas del fondo (iconos "any")


def blend(dst, src, alpha):
    return tuple(d + (s - d) * alpha for d, s in zip(dst, src))


def sample(x, y, rounded):
    """Color RGBA (floats 0-255) del punto (x, y) en [0,1]^2."""
    if rounded:  # fondo con esquinas redondeadas; fuera, transparente
        cx = min(max(x, CORNER), 1 - CORNER)
        cy = min(max(y, CORNER), 1 - CORNER)
        if (x - cx) ** 2 + (y - cy) ** 2 > CORNER ** 2:
            return (0, 0, 0, 0)
    color = NIGHT
    dist = math.hypot(x - 0.5, y - HORIZON_Y)
    if y < HORIZON_Y - LINE_W and abs(dist - ARC_R) < ARC_W / 2:
        color = blend(color, SUN, 0.45)
    if y < HORIZON_Y and dist < SUN_R:
        color = SUN
    if abs(y - HORIZON_Y) < LINE_W / 2 and LINE_X0 <= x <= LINE_X1:
        color = INK
    return (*color, 255)


def render(size, rounded, supersample=3):
    rows = []
    step = 1 / (size * supersample)
    for py in range(size):
        row = bytearray([0])  # filtro PNG: ninguno
        for px in range(size):
            acc = [0.0, 0.0, 0.0, 0.0]
            for sy in range(supersample):
                for sx in range(supersample):
                    x = (px * supersample + sx + 0.5) * step
                    y = (py * supersample + sy + 0.5) * step
                    r, g, b, a = sample(x, y, rounded)
                    acc[0] += r * a
                    acc[1] += g * a
                    acc[2] += b * a
                    acc[3] += a
            n = supersample * supersample
            alpha = acc[3] / n
            if acc[3]:
                row += bytes(round(c / acc[3]) for c in acc[:3])
            else:
                row += b"\x00\x00\x00"
            row.append(round(alpha))
        rows.append(bytes(row))
    return rows


def write_png(path, size, rows):
    def chunk(kind, data):
        body = kind + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body))

    header = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)  # RGBA 8 bits
    data = zlib.compress(b"".join(rows), 9)
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header) + chunk(b"IDAT", data)
                     + chunk(b"IEND", b""))


SVG = """<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 100 100">
  <rect width="100" height="100" rx="22" fill="#0f1830"/>
  <path d="M19 {hy} A31 31 0 0 1 81 {hy}" fill="none" stroke="#ffb547" stroke-opacity=".45" stroke-width="3.5"/>
  <path d="M30 {hy} A20 20 0 0 1 70 {hy} Z" fill="#ffb547"/>
  <path d="M20 {hy} H80" stroke="#eef1fb" stroke-width="3.5" stroke-linecap="round"/>
</svg>
"""


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    (OUT / "groove.svg").write_text(SVG.format(hy=round(HORIZON_Y * 100, 1)), encoding="utf-8")
    targets = [
        ("icon-192.png", 192, True),
        ("icon-512.png", 512, True),
        ("icon-maskable-512.png", 512, False),  # a sangre: el sistema recorta la forma
        ("apple-touch-icon.png", 180, False),   # iOS redondea las esquinas
    ]
    for name, size, rounded in targets:
        write_png(OUT / name, size, render(size, rounded))
        print(f"{name}: {(OUT / name).stat().st_size} bytes")


if __name__ == "__main__":
    main()
