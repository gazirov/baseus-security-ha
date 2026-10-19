"""Download a pinned upstream JADX release into the local analysis directory."""
from pathlib import Path
from urllib.request import urlretrieve
from inspect_package import extract

ROOT = Path(__file__).resolve().parent
VERSION = "1.5.3"
dest = ROOT / "analysis" / "tools"
dest.mkdir(parents=True, exist_ok=True)
archive = dest / f"jadx-{VERSION}.zip"
if not archive.exists():
    urlretrieve(
        f"https://github.com/skylot/jadx/releases/download/v{VERSION}/jadx-{VERSION}.zip",
        archive,
    )
extract(archive, dest / "jadx")
print(dest / "jadx")
