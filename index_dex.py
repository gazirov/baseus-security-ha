"""Index DEX strings and defined classes without third-party dependencies."""
from pathlib import Path
import struct

ROOT = Path(__file__).resolve().parent / "analysis"
for dex in (ROOT / "com.baseus.security.ipc").glob("*.dex"):
    data = dex.read_bytes()
    def u32(offset):
        return struct.unpack_from("<I", data, offset)[0]
    strings = []
    for i in range(u32(56)):
        pos = u32(u32(60) + i * 4)
        while data[pos] & 128:
            pos += 1
        pos += 1
        end = data.index(b"\x00", pos)
        strings.append(data[pos:end].decode("utf-8", errors="replace"))
    types = [strings[u32(u32(68) + i * 4)] for i in range(u32(64))]
    classes = [types[u32(u32(100) + i * 32)] for i in range(u32(96))]
    (ROOT / f"{dex.stem}-strings.txt").write_text("\n".join(strings), encoding="utf-8")
    (ROOT / f"{dex.stem}-classes.txt").write_text("\n".join(classes), encoding="utf-8")
    print(dex.name, len(classes), "classes")
