# Baseus Security for Home Assistant Container

[Русский](README_RU.md) | **English** | [Project overview](../README_EN.md)

Version **0.2.1** combines a Docker capture companion and a Home Assistant custom
integration. Android/ADB is not required. Fresh autonomous snapshots have been
confirmed with a real **S1**; other models remain unverified. Test the complete
HA/Docker/NAS setup on your own server before relying on it.

## Features and defaults

- UI-configurable capture interval: 60–86400 seconds, shared by all cameras.
- Latest snapshot as a `camera`; battery, last-capture and cloud-session sensors.
- Explicit Baseus login from HA, with progress and safe result reporting.
- Keep 10–20 JPEGs per camera in **Media → Baseus Security history**.
- Optional external NAS archive, with 1–3650 days of retention.
- Manual **Capture all cameras** button and no overlapping capture/login jobs.
- Persistent sessions, settings and snapshots across container restarts.
- Local integration icon on Home Assistant **2026.3+**.

Defaults: **900 seconds (15 minutes)**, **20 images per camera**, **30 archive days**
when an archive is enabled, and **10 skipped initial decoded frames** to allow
the camera's native date/time overlay to appear.

The interval starts **after a capture cycle finishes**, not at the start of the
previous one. A cycle runs immediately at service startup. Missing/rejected cloud
authentication pauses the schedule; waiting for the interval will not fix that.
Frequent wake-ups consume battery and mobile data. Continuous live video is not provided.

## 1. Build the companion

Use Docker Compose on a Linux amd64/arm64 server. Extract the package into its own
directory containing `compose.yaml`, `Dockerfile`, `baseus_service.py`,
`baseus_autonomous.py` and `baseus_s1/`. Run the following commands there.

```sh
docker compose build
docker compose run --rm -T baseus python baseus_service.py init
```

Data is stored in the `baseus_data` Docker volume. The service runs as UID/GID
10001. Cloud session files and the local API key are not exposed through HTTP.
Run commands **one at a time**, waiting for the shell prompt before pasting the next.

## 2. Choose cloud authentication

### Recommended UI workflow

You can skip file transfer: complete networking and installation in steps 3–5,
then use **Configure → Sign in to Baseus**. Until a session is available, capture
will pause with `auth_required`. Successful explicit login resumes capture without
restarting the companion. If an existing session works, do not log in again.

**A new login may sign out your phone/tablet.** Every UI login requires explicit
confirmation; there is no automatic re-login or silent credential retry.

### Reuse an existing session instead

To avoid creating another cloud session, securely transfer **your own** working
`state/session.json` from the standalone client to the Docker server. This is not
`settings.json`. Do not post the file or token in chat, logs or an issue.
If the companion is already running, stop it with `docker compose stop baseus`
before replacing its session, then start it again after the import.

```sh
docker compose create
docker compose run --rm -T baseus python -c 'from pathlib import Path; Path("/data/state").mkdir(exist_ok=True)'
docker compose cp ./session.json baseus:/data/state/session.json
docker compose run --rm -T --user root baseus chown 10001:10001 /data/state/session.json
docker compose run --rm -T baseus chmod 600 /data/state/session.json
```

Here `./session.json` is the protected copy transferred to the server. The
`BASEUS_COUNTRY` environment value must match the session/account region; the
example defaults to `RU`, not an automatically detected country. Start the
companion after the import, following step 5.

### Optional terminal login

If you prefer a terminal, stop the running companion before using this alternative
to prevent contention over its state directory, then run an **interactive** login:

```sh
docker compose run --rm baseus python baseus_autonomous.py login --state /data/state --log-file /data/login.log
```

Enter the email and hidden password at the prompts. Do not put the password in
process arguments. This has the same session-reset risk as a UI login. Start the
service again afterward. Terminal login is not required for the HA workflow.

## 3. Connect HA to the companion

The default Compose file publishes the API only on the Docker host's
`127.0.0.1:8099`.

- **HA uses host networking on the same Linux server:** use `http://127.0.0.1:8099`.
- **HA uses bridge networking:** its `localhost`/`127.0.0.1` refers to the HA
  container, not the Docker host or the Baseus container. Join both containers
  to the same user-defined Docker network and use `http://baseus:8099`, or
  configure access through the host's trusted LAN address.
- **HA is on a different server:** replace the port mapping with
  `"YOUR_SERVER_LAN_IP:8099:8099"` and allow only HA through the firewall.

Always include **`http://` or `https://`** in the integration URL. Do not enter
`0.0.0.0`: the startup message means the process listens inside its container,
not that other containers can reach it.

Do not expose port 8099 to the Internet. Use HTTPS on an untrusted network,
especially when submitting cloud credentials. The API key is sent in an
Authorization header; it does not encrypt an HTTP connection. Redirects are
not followed and TLS verification is not disabled.
Windows `--interface-index` settings are not applicable to the Linux container.

## 4. Optional external archive / NAS

Mount the NAS on the **Docker host first**, using your host's SMB/NFS configuration.
The companion does not store NAS passwords or mount network filesystems for you.
The directory must be writable by UID 10001.

Inside the `baseus` service in `compose.yaml`, configure:

```yaml
volumes:
  - baseus_data:/data
  - type: bind
    source: /mnt/nas/cameras
    target: /archive
    bind:
      create_host_path: false
environment:
  BASEUS_DATA: /data
  BASEUS_COUNTRY: RU
  BASEUS_LOG_LEVEL: INFO
  BASEUS_ARCHIVE: /archive/baseus_s1
```

This is a **service fragment**, not a replacement for the whole Compose file.
Use a dedicated `baseus_s1` archive directory. The internal name remains unchanged
for compatibility. Short history lives in `/data`; external copies live in
`/archive/baseus_s1/<camera_id>/*.jpg`.

Retention deletes only strict service-generated JPEG filenames in this service's
camera directories. Other files are left alone. Reducing retention/history limits
can delete older images. Archive retention is configured in HA, not in Compose;
the default is 30 days, not unlimited storage.

If NAS writes fail, the local snapshot/history remains available and `archive_error`
is set. Missed archive copies are not automatically backfilled. Check that the NAS
is actually mounted: an existing empty mountpoint does not prove NAS availability.
Do not mix unrelated files with matching generated names into the archive namespace.

## 5. Start the service and install the integration

```sh
docker compose up -d
docker compose logs --tail 50 baseus
```

The service schedules captures independently of whether a dashboard or HA itself
is running. Settings are persisted in `/data/settings.json` inside the volume.

Copy the entire `custom_components/baseus_s1` directory into your HA configuration:

```text
/config/custom_components/baseus_s1/
```

Include **all Python/JSON files, `translations/` and `brand/`**. Do not rename the
directory. Restart **Home Assistant**, then open:

**Settings → Devices & services → Add integration → Baseus Security**

Enter the companion URL and its **local API key**. To view that key privately:

```sh
docker compose exec -T baseus cat /data/api_token
```

Do not share the output. The local key is **not** the Baseus cloud token.
It is stored in the HA config entry, so protect HA backups and the Docker volume.
The Baseus cloud account/password are not stored in HA config entries/options.
Changing the companion URL/key currently requires removing and re-adding the
integration entry; that does not delete companion snapshots.

Cameras are discovered after the first completed capture cycle. HA polls status
every **15 seconds**, but this does not wake the camera or trigger a new capture.
Viewing a cached camera image also does not trigger capture.

### Configure, login and auth feedback

Open **Settings → Devices & services → Baseus Security → Configure**:

- **Snapshots and storage:** capture interval, 10–20 history images, skipped
  frames and archive days. Submitted settings remain filled after save errors.
- **Sign in to Baseus:** account email, password and mandatory acknowledgement
  that a new login may sign out other devices. Only submitting this form requests login.
- **Cloud session status:** missing / unverified / accepted at last check /
  rejected / currently logging in. A token's presence is not proof of validity.

The connection form preserves the entered server URL/API key after errors too.
Login runs as a background job. HA shows progress, then success, rejection,
an error or an unconfirmed result; waiting polls status without resending credentials.
After a timeout/disconnection, check **Cloud session status** before retrying: the
server may still complete the original request. Login/capture requests are rejected
as busy while another operation runs; they are not queued.

The **Cloud session** sensor appears even before any camera is discovered. Its
attributes show session presence, last login time/result and a safe error code.
After a companion restart, a saved token is marked unverified until a cloud request
checks it; previous login results are retained separately.

**Account/password are not persisted** by the companion or HA integration;
`credentials_stored` is always false. The password is held in memory while entering
or performing an explicitly requested login, then discarded; only the resulting
cloud session persists. After rejection, re-enter the password for another explicit
attempt. Automatic re-login is disabled. Region comes from `BASEUS_COUNTRY` in Docker.

### Upgrade from 0.1.x to 0.2.x

Update companion sources, including `baseus_s1/`, while preserving your own
network/NAS settings in `compose.yaml`, then:

```sh
docker compose build baseus
docker compose up -d baseus
```

Update the **entire** `/config/custom_components/baseus_s1/` directory and restart
HA. Keep the existing integration entry and Docker volume: settings, sessions and
snapshots should remain. No cloud login is automatically triggered by upgrading.
Updating only the container does not update the HA UI.

For only the **0.2.0 → 0.2.1 name/icon change**, the companion does not need a rebuild;
update the HA integration files and restart HA. Its internal domain and unique IDs
remain `baseus_s1`-based. The old default entry title is renamed automatically;
custom titles and camera names are not overwritten. This does not establish support
for additional camera models.

Local icons require **HA 2026.3+**. Older HA may show a placeholder. If menu text or
images look stale, check that all translation/brand files were replaced, restart HA,
then hard-refresh the browser or try a private window. The icon is original artwork,
not an official Baseus or Home Assistant logo.

## 6. Dashboard and history

Use [dashboard.example.en.yaml](dashboard.example.en.yaml), replacing its example
entity IDs with the real IDs shown in HA. A picture-entity card displays the cached
snapshot; adjacent entities show battery, last-capture time and the capture button.
History is available through **Media → Baseus Security history → your camera**.
This uses HA's built-in Media browser, not a separate custom frontend gallery.
Browse the external archive using your NAS tools.

History file captions use the successful attempt's **start time in UTC**.
The last-capture sensor records when the JPEG was received. Neither is presented
as the camera sensor's own clock.

Images are not published in `/config/www` or `/local`. History is proxied through
HA authentication and temporary signed URLs (5 minutes); do not share those URLs.
If a capture fails, the previous image stays, but `last_capture` does **not** advance.
Camera attributes include `status`, `error_code`, `archive_error`, `service_error`
and `capture_in_progress` for troubleshooting.

Battery source `cloud_device_info` means the last cloud-reported reading, not a
separate live battery measurement. Date/time in the image comes from camera OSD,
not a computer-generated overlay; skipping frames does not guarantee OSD detection.

## Logs

```sh
docker compose logs -f --tail 100 baseus
```

Default `BASEUS_LOG_LEVEL=INFO` logs startup settings, authenticated API activity
(first request and after 120 seconds of inactivity), settings changes, scheduled/
manual cycles, camera connection/capture stages, history/archive writes, cycle
counts and the next capture time in UTC.

`auth_required; schedule paused; automatic login disabled` means captures will
**not** resume just by waiting. Restore a working session or use an explicit login.
`API client authenticated` only verifies the local API key; it does not prove a
working cloud session or that the client is specifically HA.

Normal status/image reads do not spam INFO. Rejected API-key warnings are limited
to once per minute. To temporarily enable additional metadata, set this inside
the service's existing environment section and recreate the container:

```yaml
BASEUS_LOG_LEVEL: DEBUG
```

DEBUG enables only project metadata, not third-party HTTP/MQTT wire dumps.
Full URLs, query strings, headers, request/response bodies, account credentials,
cloud/P2P/MQTT secrets and arbitrary exception messages are not logged.
Docker collects stdout/stderr; the service has no separate log file in its volume.

A plain `restart` does not rebuild embedded sources. For source changes use
`docker compose build baseus` followed by `docker compose up -d baseus`.
**Do not use `docker compose down -v`**: it removes the volume containing sessions,
keys and snapshots.

## Installation checklist

1. Verify that a capture produces a new JPEG and advances the last-capture sensor.
2. Set history to 10; after more than 10 successful captures, only 10 remain.
3. Press capture twice quickly: there should be no parallel/queued second cycle.
4. Restart the companion: previous snapshots/history/settings should remain.
5. Restart HA: no duplicate entities; history still requires authentication.
6. Verify NAS copies and `archive_error` without deleting unrelated data.

Local tests, without a camera:

```sh
python -m pip install -r requirements-test.txt
python -m unittest discover -s tests -q
```

Flow tests use small HA test doubles, not the real UI. Offline tests do not prove
your HA/Docker/NAS installation works end-to-end.

## Why a separate container?

Home Assistant Container does not provide Supervisor add-ons. Keeping native PyAV
and long PPCS operations in a companion avoids blocking HA's event loop or changing
its dependency versions. The trade-off is two components and an authenticated local
API. See the [API and storage contract](CONTRACT.md) for details.
