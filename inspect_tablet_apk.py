"""Extract the installed app's APK and locate selected defined DEX classes."""
from pathlib import Path
import struct

from inspect_package import extract

root = Path(__file__).resolve().parent / "analysis"
folder = root / "tablet-apk"
extract(root / "tablet-1.5.0.apk", folder)
targets = {"Lcom/baseus/devices/fragment/LiveFragment;", "Lcom/baseus/component/xm/utils/AppFileUtil;"}
for dex in folder.glob("*.dex"):
    data = dex.read_bytes()
    def u32(offset):
        return struct.unpack_from("<I", data, offset)[0]
    strings = []
    for i in range(u32(56)):
        pos = u32(u32(60) + i * 4)
        while data[pos] & 128:
            pos += 1
        pos += 1
        strings.append(data[pos:data.index(b"\x00", pos)].decode("utf-8", errors="replace"))
    types = [strings[u32(u32(68) + i * 4)] for i in range(u32(64))]
    classes = {types[u32(u32(100) + i * 32)] for i in range(u32(96))}
    for name in sorted(targets & classes):
        print(dex.name, name)
