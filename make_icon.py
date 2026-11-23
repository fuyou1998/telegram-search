"""Generate the app icon: a rounded green tile with a white magnifier.

Run once and commit the output; it has no dependencies beyond the standard library.
"""

from __future__ import annotations

import struct
import zlib
from pathlib import Path

ROOT = Path(__file__).resolve().parent
OUTPUT = ROOT / "static"
SIZES = (16, 32, 48, 64, 128, 256)
SUPERSAMPLE = 4

TOP = (0x14, 0x7D, 0x5C)
BOTTOM = (0x08, 0x45, 0x32)
GLYPH = (0xF6, 0xFB, 0xF8)


def _rounded_alpha(x: float, y: float, size: float, radius: float) -> bool:
    """True inside a rounded square that fills the canvas."""
    if x < radius and y < radius:
        return (x - radius) ** 2 + (y - radius) ** 2 <= radius ** 2
    if x > size - radius and y < radius:
        return (x - (size - radius)) ** 2 + (y - radius) ** 2 <= radius ** 2
    if x < radius and y > size - radius:
        return (x - radius) ** 2 + (y - (size - radius)) ** 2 <= radius ** 2
    if x > size - radius and y > size - radius:
        return (x - (size - radius)) ** 2 + (y - (size - radius)) ** 2 <= radius ** 2
    return 0 <= x <= size and 0 <= y <= size


def _glyph(x: float, y: float, size: float) -> bool:
    """Magnifier: a ring plus a diagonal handle, in normalised coordinates."""
    cx, cy = size * 0.44, size * 0.42
    outer = size * 0.27
    inner = outer - size * 0.085
    distance = ((x - cx) ** 2 + (y - cy) ** 2) ** 0.5
    if inner <= distance <= outer:
        return True
    # Handle from the lower-right of the ring, with a rounded cap.
    ax, ay = size * 0.60, size * 0.59
    bx, by = size * 0.79, size * 0.78
    half = size * 0.055
    dx, dy = bx - ax, by - ay
    length2 = dx * dx + dy * dy
    t = max(0.0, min(1.0, ((x - ax) * dx + (y - ay) * dy) / length2))
    px, py = ax + t * dx, ay + t * dy
    return (x - px) ** 2 + (y - py) ** 2 <= half * half


def render(size: int) -> list[bytes]:
    """Return RGBA rows for one icon size."""
    scale = SUPERSAMPLE
    big = size * scale
    radius = big * 0.22
    width = height = size
    # Accumulate supersampled alpha and colour, then box-filter down.
    rows: list[bytes] = []
    for py in range(height):
        row = bytearray()
        for px in range(width):
            r = g = b = a = 0
            for sy in range(scale):
                for sx in range(scale):
                    x = px * scale + sx + 0.5
                    y = py * scale + sy + 0.5
                    if not _rounded_alpha(x, y, big, radius):
                        continue
                    if _glyph(x, y, big):
                        r += GLYPH[0]
                        g += GLYPH[1]
                        b += GLYPH[2]
                    else:
                        ratio = y / big
                        r += int(TOP[0] + (BOTTOM[0] - TOP[0]) * ratio)
                        g += int(TOP[1] + (BOTTOM[1] - TOP[1]) * ratio)
                        b += int(TOP[2] + (BOTTOM[2] - TOP[2]) * ratio)
                    a += 255
            samples = scale * scale
            if a == 0:
                row += bytes((0, 0, 0, 0))
            else:
                covered = a // 255
                row += bytes((r // covered, g // covered, b // covered, a // samples))
        rows.append(bytes(row))
    return rows


def png_bytes(size: int, rows: list[bytes]) -> bytes:
    def chunk(tag: bytes, payload: bytes) -> bytes:
        return (
            struct.pack(">I", len(payload)) + tag + payload
            + struct.pack(">I", zlib.crc32(tag + payload) & 0xFFFFFFFF)
        )

    raw = b"".join(b"\x00" + row for row in rows)
    header = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b"")
    )


def ico_bytes(images: dict[int, bytes]) -> bytes:
    header = struct.pack("<HHH", 0, 1, len(images))
    offset = 6 + 16 * len(images)
    entries, blobs = b"", b""
    for size in sorted(images):
        data = images[size]
        entries += struct.pack(
            "<BBBBHHII", size if size < 256 else 0, size if size < 256 else 0,
            0, 0, 1, 32, len(data), offset,
        )
        blobs += data
        offset += len(data)
    return header + entries + blobs


def main() -> None:
    OUTPUT.mkdir(exist_ok=True)
    images = {}
    for size in SIZES:
        rows = render(size)
        images[size] = png_bytes(size, rows)
        print(f"rendered {size}x{size}")
    (OUTPUT / "icon.ico").write_bytes(ico_bytes(images))
    (OUTPUT / "icon-32.png").write_bytes(images[32])
    (OUTPUT / "icon-128.png").write_bytes(images[128])
    print(f"wrote {OUTPUT / 'icon.ico'} ({len(ico_bytes(images))} bytes)")


if __name__ == "__main__":
    main()
