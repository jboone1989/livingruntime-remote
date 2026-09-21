from __future__ import annotations

import struct
import zlib
from pathlib import Path


def _chunk(tag: bytes, data: bytes) -> bytes:
    return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)


def write_logo(path: Path, size: int = 256) -> None:
    raw = bytearray()
    for y in range(size):
        raw.append(0)
        for x in range(size):
            dx = x - size / 2
            dy = y - size / 2
            inside = dx * dx + dy * dy <= (size * 0.42) ** 2
            if inside:
                raw.extend((11, 61, 46, 255))
            else:
                raw.extend((246, 248, 247, 255))
    path.write_bytes(
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
        + _chunk(b"IDAT", zlib.compress(bytes(raw), 9))
        + _chunk(b"IEND", b"")
    )


if __name__ == "__main__":
    target = Path(__file__).resolve().parents[1] / "assets" / "logo.png"
    target.parent.mkdir(parents=True, exist_ok=True)
    write_logo(target)
    print(target)
