"""Extract the supplied XAPK and inventory APK contents for offline analysis."""
from pathlib import Path
import json
import zipfile

ROOT = Path(__file__).resolve().parent
OUT = ROOT / "analysis"


def extract(archive, dest):
    dest.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(archive) as z:
        names = z.namelist()
        for name in names:
            target = (dest / name).resolve()
            if not target.is_relative_to(dest.resolve()):
                raise ValueError(f"Unsafe archive member: {name}")
        z.extractall(dest)
    return names


def main():
    OUT.mkdir(exist_ok=True)
    packages = list(ROOT.glob("*.xapk"))
    if len(packages) != 1:
        raise ValueError("Expected exactly one XAPK")
    names = extract(packages[0], OUT / "xapk")
    print("XAPK contents:", json.dumps(names, ensure_ascii=False, indent=2))
    for apk in (OUT / "xapk").glob("*.apk"):
        members = extract(apk, OUT / apk.stem)
        (OUT / f"{apk.stem}-files.txt").write_text("\n".join(members), encoding="utf-8")
        print(f"APK {apk.name}: {len(members)} members")
        print("\n".join(n for n in members if n.endswith((".dex", ".so"))))


if __name__ == "__main__":
    main()
