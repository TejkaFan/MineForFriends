"""Рисует icon.ico лаунчера (пиксельный блок травы) без сторонних библиотек.

    .venv\\Scripts\\python.exe make_icon.py
"""
import struct
import zlib
from pathlib import Path

# 16×16: g — трава, G — светлая трава, d — земля, D — тёмная земля, s — камешек
ART = [
    "GgGGgGGGgGGgGGgG",
    "gGGgGGgGGGgGGgGg",
    "GGgGgGGgGgGGgGGG",
    "gGGGGgGGGGgGGGgG",
    "dgDgdgdGdgDgdgGd",
    "ddDdgdddDdgdDddd",
    "dDdddDdsddddDdsd",
    "ddddDdddddDddddd",
    "dsdDdddDdsdddDdd",
    "dddddsddddddsddd",
    "DddDddddDdDddddD",
    "dddddDdsdddddsdd",
    "dsdDddddddDddddd",
    "ddddddDddsdddDdd",
    "dDdsdddddddDdddd",
    "ddddDddDdsddddsd",
]
COLORS = {
    "g": (0x5d, 0x9c, 0x3a), "G": (0x7c, 0xbd, 0x4f),
    "d": (0x86, 0x60, 0x43), "D": (0x6b, 0x4a, 0x32), "s": (0x9a, 0x9a, 0x9a),
}


def pixel(size: int, x: int, y: int):
    return COLORS[ART[y * 16 // size][x * 16 // size]]


def png(size: int) -> bytes:
    rows = [b"\x00" + b"".join(bytes(pixel(size, x, y)) + b"\xff" for x in range(size)) for y in range(size)]

    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    header = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", header)
            + chunk(b"IDAT", zlib.compress(b"".join(rows), 9)) + chunk(b"IEND", b""))


def bmp(size: int) -> bytes:
    # Маленькие размеры — обычный 32-битный DIB: старый GDI+ не читает в них PNG
    header = struct.pack("<IiiHHIIiiII", 40, size, size * 2, 1, 32, 0, 0, 0, 0, 0, 0)
    pixels = b"".join(bytes(reversed(pixel(size, x, y))) + b"\xff"
                      for y in reversed(range(size)) for x in range(size))
    mask = b"\x00" * (((size + 31) // 32) * 4 * size)
    return header + pixels + mask


def ico(sizes) -> bytes:
    images = [png(s) if s >= 256 else bmp(s) for s in sizes]
    out = struct.pack("<HHH", 0, 1, len(images))
    offset = 6 + 16 * len(images)
    for s, data in zip(sizes, images):
        out += struct.pack("<BBBBHHII", s % 256, s % 256, 0, 0, 1, 32, len(data), offset)
        offset += len(data)
    return out + b"".join(images)


if __name__ == "__main__":
    path = Path(__file__).resolve().parent / "icon.ico"
    path.write_bytes(ico([16, 32, 48, 256]))
    print(f"{path} готов")
