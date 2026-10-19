"""Build a source release using an allowlist; never include runtime/private data."""
from pathlib import Path
import zipfile

ROOT = Path(__file__).resolve().parents[1]
VERSION = "0.2.1"


def release_files():
    files = [ROOT / name for name in (
        "Dockerfile", ".dockerignore", "compose.yaml", "baseus_service.py",
        "baseus_autonomous.py", "requirements-service.txt", "requirements-autonomous.txt", "requirements-test.txt",
        "README.md", "README_EN.md", "LEGACY_ADB_RU.md", "AUTONOMOUS_RU.md", "AUTONOMOUS_EN.md",
        "CLOUD_VALIDATION_RU.md", "VALIDATION_RU.md", "RESEARCH_RU.md",
        "P2P_RESEARCH_RU.md", "P2P_IMPLEMENTATION_PLAN_RU.md",
        "home_assistant/README_RU.md", "home_assistant/README_EN.md", "home_assistant/CONTRACT.md",
        "home_assistant/dashboard.example.yaml", "home_assistant/dashboard.example.en.yaml",
        "tools/build_ha_package.py", "tools/build_ha_brand.py")]
    files += sorted((ROOT / "baseus_s1").glob("*.py"))
    files += sorted((ROOT / "tests").glob("test_*.py"))
    integration = ROOT / "custom_components/baseus_s1"
    files += sorted(integration.glob("*.py"))
    files += sorted(integration.glob("*.json"))
    files += sorted((integration / "translations").glob("*.json"))
    files += sorted((integration / "brand").glob("*.png"))
    files += sorted((integration / "brand").glob("*.md"))
    return files


def build(destination=None):
    destination = destination or ROOT / "dist" / f"baseus-security-ha-{VERSION}.zip"
    destination.parent.mkdir(parents=True, exist_ok=True)
    # Exclusive creation: don't overwrite an existing release silently.
    with zipfile.ZipFile(destination, "x", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in release_files():
            if path.is_symlink():
                raise ValueError("Release inputs must not be symlinks")
            archive.write(path, path.relative_to(ROOT).as_posix())
    return destination


if __name__ == "__main__":
    print(build())
