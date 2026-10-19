# Baseus Security — camera snapshots and Home Assistant

[Русский](README.md) | **English**

An unofficial, experimental client for taking fresh JPEG snapshots from compatible
Baseus Security cameras, without an Android device or ADB. Includes a Docker
companion service and a Home Assistant custom integration.

**The project's primary purpose is operation without a phone or tablet.**
Capturing snapshots requires no mobile app, ADB, USB connection, emulator or active
screen. The client talks to the Baseus cloud and camera directly; it does not
automate an Android application or capture its screen.

The camera must already be configured and linked to your Baseus account.
Provisioning/pairing a new camera is not currently implemented by this project.

**Tested camera: Baseus Security S1.** The integration's name is model-neutral,
but compatibility with other cameras has not been verified. Cameras using Tuya
or other protocols are not supported by this backend.

## Choose a setup

| Setup | Guide |
| --- | --- |
| Home Assistant Container, scheduled snapshots, short history and NAS archive | [Home Assistant installation](home_assistant/README_EN.md) |
| Standalone Windows/Linux command-line client | [Autonomous CLI](AUTONOMOUS_EN.md) |
| Local companion HTTP API and storage contract | [API contract](home_assistant/CONTRACT.md) |

Both supported setups operate directly, **without a mobile device**.

## Home Assistant features

- Configure capture interval, history size and archive retention from the UI.
- Latest snapshot as a camera entity; battery and last successful capture sensors.
- Explicit Baseus cloud login from HA and a separate cloud-session status sensor.
- Keep the latest 10–20 snapshots per camera in HA's Media browser.
- Optional external archive on a mounted NAS directory, with retention in days.
- Manual capture button, bounded retries and no overlapping capture cycles.
- English/Russian UI and an original camera icon on HA 2026.3+.

The companion owns scheduling and storage. HA does not need PyAV or any cloud
credentials installed in its Python environment. This is a custom integration
plus a separate container, **not** a Supervisor add-on or a continuous video stream.

Defaults: 15-minute interval measured **after the previous cycle finishes**,
20 history images per camera, 30-day external archive when enabled, and 10 skipped
initial decoded frames to allow the camera's date/time overlay to appear.

## Important limitations

- The transport currently supports direct XM/PPCS UDP, not relay/TCP fallback.
  Some network/NAT configurations may therefore fail to connect.
- A fresh JPEG from a real S1 has been confirmed. Test your own camera,
  HA version, Docker networking and NAS before relying on this integration.
- Battery is the last value reported to the cloud, not a separate live P2P reading.
- Only an explicit login creates a new cloud session. Automatic re-login is disabled.
  **A new login may sign out the Baseus application on other devices.**
- Account/password are not persisted by the service or HA integration. The resulting
  cloud session is stored locally; protect it and your backups.

## Security

Do not expose the companion's HTTP port to the Internet. Use a trusted local
network or HTTPS, particularly when submitting cloud login credentials from HA.
The local API key and Baseus cloud token are separate credentials.

Private state, configuration, logs, snapshots, packet captures, APKs and generated
release archives are excluded from Git. Do not force-add these files or share
`session.json`, `/data/api_token`, raw network captures or unredacted diagnostics.
Shared SDK application constants in the protocol implementation are not personal
account credentials; they are intentionally retained.

## Tests and packaging

Python 3.11+ is required for the standalone tools; the Docker image uses Python 3.13.

```sh
python -m pip install -r requirements-test.txt
python -m unittest discover -s tests -q
```

The suite includes local UDP/HTTP, storage, API, protocol and flow-logic tests.
HA flow tests use small test doubles: they do **not** replace a real HA UI test.
The optional native-library/PCAP research tools and their inputs are not included
in the public source package.

```sh
python tools/build_ha_brand.py
python tools/build_ha_package.py
```

The packaging script uses an explicit source-file allowlist and refuses to
overwrite an existing release. It does not include local sessions or recordings.

## Development history

An Android/ADB bridge was used during early protocol research. Its code and
[archived instructions, Russian](LEGACY_ADB_RU.md) are retained for historical
reference and compatibility. **It is not a dependency or a required setup step.**
For new installations, use the autonomous CLI or HA integration linked above.
