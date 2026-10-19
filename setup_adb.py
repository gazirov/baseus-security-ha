"""Install Google's Platform Tools locally; does not change PATH or install drivers."""
from pathlib import Path
import json
import os
import platform
from urllib.request import urlretrieve

from inspect_package import extract


def main():
    root = Path(__file__).resolve().parent
    suffix = {"Windows": "windows", "Linux": "linux", "Darwin": "darwin"}.get(platform.system())
    if not suffix:
        raise RuntimeError("Unsupported OS")
    target = root / "tools"
    target.mkdir(exist_ok=True)
    archive = target / f"platform-tools-{suffix}.zip"
    urlretrieve(f"https://dl.google.com/android/repository/platform-tools-latest-{suffix}.zip", archive)
    extract(archive, target)
    executable = target / "platform-tools" / ("adb.exe" if suffix == "windows" else "adb")
    if os.name != "nt":
        executable.chmod(0o755)
    config_path = root / "config.json"
    if config_path.exists():
        from baseus_s1.common import write_json
        config = json.loads(config_path.read_text(encoding="utf-8-sig"))
        config["android"]["adb"] = str(executable)
        write_json(config_path, config)
    print(executable)


if __name__ == "__main__":
    main()
