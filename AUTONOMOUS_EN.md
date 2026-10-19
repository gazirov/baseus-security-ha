# Autonomous snapshot client

[Русский](AUTONOMOUS_RU.md) | **English**

`baseus_autonomous.py` connects to the Baseus cloud, obtains a fresh device list
and attempts to capture a new JPEG from each enabled camera. It does not use
Android, ADB or the Android configuration file.

For scheduled operation inside Home Assistant, use the
[Docker and HA guide](home_assistant/README_EN.md).

## Compatibility

Fresh snapshots have been confirmed with a real **Baseus Security S1**. Other
models are unverified. The backend implements direct XM/PPCS UDP only; it does
not implement relay/TCP fallback, Tuya, CAT/DDMPP or other protocol families.
If direct peer setup fails, capture reports an error rather than returning an old image.

Cloud `online_status` is not treated as proof of reachability: sleeping cameras
are attempted too. Devices with `camera_on=0` are skipped without changing their state.

## Install

Python 3.11+ is required. Run commands from the project directory.

### Windows PowerShell

```powershell
python -m venv .venv
& .\.venv\Scripts\python.exe -m pip install -r requirements-autonomous.txt
```

### Linux

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-autonomous.txt
```

For the `python` commands below, activate this environment first
(`.\.venv\Scripts\Activate.ps1` in PowerShell, `. .venv/bin/activate` on Linux),
or replace `python` with its explicit virtual-environment path.

## Login and capture

**Warning:** an explicit Baseus login may terminate sessions on your phone or tablet.
If you already have a working session, reuse it instead of logging in again.

```sh
python baseus_autonomous.py login --country RU
python baseus_autonomous.py cameras --country RU
python baseus_autonomous.py capture-all --country RU --wake --skip-frames 10
```

Replace `RU` with your account's supported country/region. It must match the saved
session. Login prompts for email and a hidden password; do not pass the password
as a command-line argument. For an explicitly requested noninteractive `login`,
`BASEUS_ACCOUNT` and `BASEUS_PASSWORD` can come from a protected environment.

**Automatic login is disabled.** `cameras` and `capture-all` use only the saved
session in `state/session.json`. A missing or rejected token stops the operation;
there is no silent re-login. Protect this directory and do not share its contents.

## Options

| Option | Default | Meaning |
| --- | --- | --- |
| `--country` | `RU` | Cloud account country/region |
| `--state` | `state` | Saved session and process-lock directory |
| `--output` | `captures/autonomous` | JPEG and `last-run.json` directory |
| `--wake` | Off | Send MQTT `WatchDevice` before each attempt |
| `--connect-timeout` | `45` | UDP handshake deadline, >0 and at most 300 seconds |
| `--timeout` | `120` | XM/video/JPEG deadline after connection, >0 and at most 300 seconds |
| `--attempts` | `3` | Maximum attempts per camera, 1–3 |
| `--retry-delay` | `20` | Delay between attempts, 0–300 seconds |
| `--skip-frames` | `0` | Discard initial decoded images, 0–300; try 10 for date/time OSD |
| `--log-file` | `logs/autonomous.log` | Rotating diagnostic log |
| `--verbose` | Off | Additional protocol metadata, never wire payloads or credentials |
| `--bind-address` | Unset | Explicit local IPv4 for PPCS UDP sockets only |
| `--interface-index` | Unset | Windows-only PPCS outgoing interface index |

The CLI skips zero frames by default; the **Docker companion defaults to 10**.
Connection and video deadlines are separate and apply to each attempt. System DNS
has its own OS timeout. Three complete attempts can take more than eight minutes.

```sh
python baseus_autonomous.py capture-all --wake --skip-frames 10 --attempts 3 --retry-delay 20
```

Each retry closes the previous P2P session, opens a new socket and, with `--wake`,
sends a new wake request. Capture stops retrying after success. Known transient
timeouts and transport failures may be retried; login rejection, unknown formats,
decryption and save errors are not. Cloud authentication failure aborts the run
without another login or repeated wake attempts.

## Output and battery

JPEGs are written atomically. Filenames use a camera identifier hash, UTC time
and a random suffix, not a cloud-provided name or raw serial number.
`last-run.json` contains per-camera `ok`, `error` or `disabled` results, actual
attempt counts and safe failure metadata. A successful image always comes from
the current P2P session; cached images are never substituted for a new capture.

`cameras` and `capture-all` include:

```json
{"battery_percent": 96, "battery_source": "cloud_device_info"}
```

This is the last battery value in `device_info.battery`, returned by a fresh cloud
list request. The measurement itself may be older; its age is not supplied here.
It does not wake the camera separately. Missing/invalid values produce `null` for
both fields; only integer percentages 0–100 are accepted.
The separate SDK P2P battery command is not used as a validated live measurement.

Exit codes: `0` no camera errors; `2` per-camera errors; `1` startup/list/authentication
failure; `130` cancelled. An empty device list returns an empty result.

## Date/time overlay

The camera's native date/time overlay may appear after its first decoded image.
In the analyzed real capture, the first frame had no date while frame 10 already
contained it. `--skip-frames 10` waits for a later decoded image without modifying
camera settings or drawing the computer's time over the picture.

This does not detect OSD automatically or guarantee that it appears by a particular
frame. Skipping does not extend the original frame deadline; insufficient frames
produce an error rather than an earlier snapshot.

## Logs and troubleshooting

Logs go to stderr and a rotating file (2 MB, two backups); stdout remains JSON.
INFO reports cloud calls, wake, discovery, candidate counts, peer readiness,
XM login, video, JPEG and safe error stages/codes. `--verbose` adds packet types,
sizes, channels and sequence numbers. Peer addresses, DID, credentials and payloads
are not logged. Third-party HTTP/MQTT wire debugging is not enabled.

Cloud HTTP 500 with JSON code `100401`/`401` is treated as authentication rejection,
not a generic server failure. The response alone does not distinguish expiry from
token revocation. Wake acknowledgement does not prove peer readiness or image delivery.

For a multi-interface Windows machine, inspect local interfaces:

```powershell
Get-NetIPAddress -AddressFamily IPv4 | Select-Object InterfaceAlias,InterfaceIndex,IPAddress
```

If necessary, select your own LAN address/index with `--bind-address` and
`--interface-index`. These affect PPCS sockets only, not HTTPS, MQTT, DNS, system
routes or VPN state. A bind address alone is not a universal VPN bypass. These
options are optional; successful captures have also been reported without them.
Do not copy another machine's address/index into your setup.

## Tests and validation

```sh
python -m pip install -r requirements-test.txt
python -m unittest discover -s tests -q
```

Tests cover protocol parsing, AES, reliable UDP, retry behavior, failure isolation,
JPEG validation and real synthetic H.264/HEVC decoding through PyAV. Real S1 video
has also been decoded from a local capture. These checks do not establish
compatibility with untested camera models or networks.
