# Baseus Security Docker + Home Assistant contract

The existing autonomous client remains the only cloud/PPCS implementation.
The companion container owns scheduling and storage; HA reads its API, changes
settings and requests asynchronous captures or explicitly confirmed cloud login.
One worker processes all cameras sequentially.
No queued overlapping runs, automatic login, Android, or continuous stream.

All `/v1` routes require `Authorization: Bearer <local API key>`. The key is
separate from the Baseus cloud token. Do not expose this service to the Internet.
Responses never contain cloud tokens, raw device settings or exception messages.

- `GET /v1/status`: instance ID, settings, busy flag, error code and camera list.
- `PUT /v1/settings`: interval_seconds (60..86400), history_limit (10..20),
  skip_frames (0..300), archive_days (1..3650). Atomic persistence; new settings
  apply to subsequent captures. Lower retention prunes only app-owned files.
- `POST /v1/capture`: 202 accepted, 409 already busy; responds without waiting
  for a camera. Captures all enabled cameras, respecting existing retry limits.
- `GET /v1/cameras/{id}/history`: newest-first image IDs and `requested_at` UTC
  timestamps (start of the successful attempt, not the camera's sensor clock).
- `GET /v1/cameras/{id}/images/{image}`: JPEG, including `latest` alias;
  no-cache; strict opaque ID validation; no user-provided filesystem paths.

Image failures retain the last successful JPEG, explicitly dated by last_capture.
Battery is labelled `cloud_device_info`, not presented as a fresh P2P reading.
Successful images are atomically installed before status publication; `last_image`
identifies the immutable image matching `last_capture`. History
is bounded per camera. External archive is optional, separate from short history,
and retention applies only to service-generated JPEGs in its own namespace.
Failure to archive must not remove the short-history image.

HA config stores only the companion URL/API key, not the Baseus password.
Config flow validates the server before creating an entry; options change
interval, retention and OSD warm-up. Camera entities never wake a device when
displayed. Media history is proxied through HA authentication/signed URLs, never
published in `/local`. External archive paths are configured on the Docker host,
not accepted over HTTP.

## Explicit cloud login (0.2.0)

`POST /v1/auth/login` accepts only account (1..254 chars), password (1..1024
chars), and `confirm_session_reset: true`, behind the existing local API key.
Returns 202 with an opaque job_id, 409 if capture/login is busy, or 400 for
invalid input. No automatic retries. Work continues server-side if HA disconnects.
`GET /v1/status` adds `auth`: token presence is separate from last observed
session validity, login progress/result and last attempt UTC. No password,
account, cloud token or upstream error message is returned. Login and capture
are mutually exclusive. Only the cloud session and safe auth result persist;
account/password exist only in memory during an explicitly confirmed login.
HA polls the login job without resubmitting credentials and stores no cloud
credentials in config entries/options. Failed connection/settings forms preserve
their inputs for the lifetime of the flow. Cloud password is dropped after login
acceptance; closing/restarting HA does not persist cloud inputs.
Use trusted local networking or HTTPS: a local API key is not transport encryption.

Validation target: Linux amd64/arm64 companion; HA custom integration requires
a real HA smoke test before production use. Offline tests cannot prove HA UI
compatibility or camera/NAS connectivity.
