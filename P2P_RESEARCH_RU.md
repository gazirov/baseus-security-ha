# XM/PPCS research status

## Confirmed from the Baseus Android SDK

The cloud response supplies the DID, UDP port, and `service_string`. For a
remote connection the Android SDK calls `PPCS_ConnectByServer(did,
connect_mode, udp_port, service_string)`, rather than opening a UDP socket to
the camera itself.

After the vendor transport returns a session handle, Baseus writes XM signal
frames on PPCS channel 0:

- Login is command `1`. Its JSON `auth` is lowercase MD5 of
  `username:random:p2p_password`.
- Start video is command `2`, with JSON containing `channel`, `streamType`,
  and `sign`.
- Normal XM signal frames have a 24-byte, little-endian header with magic
  `0x55AA55AA`; command and payload length are 16-bit fields at offsets 14
  and 12 respectively.
- When cloud P2P encryption is enabled, the signal payload is
  AES-128-ECB/PKCS7. The key is MD5 of `p2p_password + "xmitech"`.

`baseus_s1/xm_p2p.py` implements only these application frames and is covered
by offline tests. It deliberately does not log or persist P2P secrets.

## Not compatible as-is

`analysis/ppcs2rtsp-reference` was reverse engineered for V360/FH8616
cameras. It implements LAN/direct PPCS UDP handshaking and sends a different
IOCTRL command protocol. It has no implementation of `PPCS_ConnectByServer`
or of Baseus XM signals, so using it against the S1 would not be a valid
diagnostic.

## Remaining transport work

An autonomous client needs a compatible implementation of the vendor PPCS
transport's `ConnectByServer` path, including the opaque cloud
`service_string`, relay selection, reliable channels, and the channel mapping
returned by the camera. That transport is inside the ARM Android
`libPPCS_API.so`; it cannot be loaded by this Windows Python process.

The least speculative next validation is to capture an Android live-view P2P
session, redact P2P credentials, and compare its PPCS packet types and
channels with the reference implementation. Once that establishes transport
compatibility, the XM packet module can be connected to a desktop transport.

## Android metadata capture, 2026-09-19

PCAPdroid 2.0.1 was installed from its official GitHub release and restricted
to the Baseus package. It ran in metadata-only mode: no payload was exported
or retained. With Android Private DNS disabled temporarily, a fresh live-view
attempt showed XM HTTPS connections on port 443 and UDP broadcast discovery on
port 7000, but no separate PPCS peer was exposed by the VPN metadata.

Private DNS was restored to its previous opportunistic setting and the
PCAPdroid VPN was stopped. This observation does not establish that the
PPCS-to-RTSP transport is compatible with S1; a capture method that can show
the vendor transport below the app's TLS/relay layer is still required.

## Router captures, 2026-10-13

`logs/captureOUT.pcapng` (tablet side) and `logs/captureIN.pcapng` (camera
side) are **different sessions**. They must not be matched by time, address,
or port. Their common PPCS packet format nevertheless allows the client and
camera/relay phases to be analysed independently.

### Confirmed transport sequence

1. Discovery is UDP to the XM nodes on ports `32100`–`32102`; LAN discovery
   also uses UDP `32108`.
2. The client sends `F1 41` punch packets to the candidates supplied by the
   discovery service.
3. The camera-side capture shows successful peer readiness: after `F1 41`,
   it sends `F1 42`, then exchanges PPCS DRW data (`F1 D0`) and acknowledgements
   (`F1 D1`) with a public endpoint. `F1 42` alone does not prove relay
   fallback; that earlier interpretation was too strong.
4. The tablet capture likewise transitions to `F1 D0/D1` after discovery.
   Thus the capture proves the current app uses the same PPCS transport family
   as `libPPCS_API`, rather than an HTTPS video stream.

### Confirmed Baseus channel map

- PPCS channel `0`: encrypted XM control frames. The captures contain command
  `1` (login), command `2` (open video), and `2001` (heartbeat).
- PPCS channels `1` and `2`: encrypted camera-to-client media frames.
- The XM frame header has encryption type `1` for both command and media
  frames. This does not independently validate the AES key derivation or prove
  that signal and media use the same cipher.

The captures contain device identifiers and transport metadata. They remain
local diagnostic artifacts and must not be uploaded or committed. They are
not used as fixed relay addresses: a desktop client must obtain fresh
discovery hosts from the cloud `service_string`, and obtain current peer/relay
candidates during negotiation.

## Transport core implementation

`baseus_s1/ppcs.py` implements declared-length frame parsing, DRW data/ACK
encoding, bounded receive reordering, duplicate suppression, sender
fragmentation and bounded retransmissions. Payload and vendor trailers are
excluded from object representations. The caller must ACK duplicates and
accept datagrams only from its established peer.

Offline validation against the two independent router captures parsed 1180
PPCS packets in IN and 5030 in OUT. Every DRW data/ACK packet re-encoded
byte-for-byte to its declared length; 29 IN packets contained separate vendor
trailers. This is framing validation, not a live connection test.

Discovery wire codecs and service-string decoding are now implemented in
`baseus_s1/ppcs_discovery.py`. Experimental network discovery and direct-peer
selection are implemented in `baseus_s1/ppcs_udp.py`; relay negotiation is not.
See `AUTONOMOUS_RU.md` for the experimental camera client and live-validation
limitations. The previous suggestion that all media
encryption uses the signal cipher still needs verification against the media
filter; encryption-type bits alone do not prove the same encryption scheme.

## Discovery codec validation

The Baseus library's exported `cs2p2p_PPPP_DecodeString` function at `0x189ac`
references a 54-byte table at `0x107c2`. Its decoded bytes match the Python
implementation on four synthetic vectors in ARM64 Unicorn emulation,
including multiple table cycles. No real service string was exported.

Discovery `0x20` contains a 20-byte DID followed by a 16-byte vendor sockaddr:
family is big-endian, while the port and IPv4 octets are reversed compared
with conventional network encoding. All 276 requests in OUT re-encode
byte-for-byte, as do 3024 punch/ready packets across the independent captures.
Twelve hello/punch-to addresses in IN also parse successfully.

OUT contains 5030 outbound PPCS packets and no inbound PPCS packets. It cannot
by itself validate discovery replies or the complete client state machine.
IN contains both directions. A fresh `GetUserDevice` request during this work
returned HTTP 500, so live discovery was not attempted with stale addresses.

Reproduce offline validation: `python -m analysis.verify_ppcs_codecs` from the
project root. Scapy and Unicorn are research dependencies only. The verifier
is tied to the current local ARM64 library; it emits counts, not payloads.

Implementation plan and stage gates: [P2P_IMPLEMENTATION_PLAN_RU.md](P2P_IMPLEMENTATION_PLAN_RU.md).

## Experimental autonomous pipeline

`baseus_autonomous.py` provides cloud login, public camera enumeration and
isolated attempts to capture each camera. `ppcs_udp.py` opens a fresh socket,
discovers candidates, validates candidate source/DID and manages DRW channels.
The proprietary packet cipher matches the current native ARM64 library on six
synthetic encrypt/decrypt vectors; this library masks table indices to 8 bits,
unlike the unrelated Rust reference's extended-table workaround.

`xm_stream.py` reassembles normal 24-byte XM frames and implements AES ECB/CBC
as specified by SDK `b.java` and media filter `h.java`. Fresh stream login uses
the camera SN as username, as shown by app `DeviceConfig.java`. Start-video
uses `camera_sn`, `channel`, `streamType`, `videoKeepAlive_2`; the old module's
`sign` field is not used by the new backend.

143 complete camera media frames from IN reassemble correctly when real
payload decryption is bypassed for metadata-only validation. They declare H.264
in the SDK video-type bit; actual camera decode remains unverified. PyAV
H.264-to-JPEG and synthetic full login/video flow pass locally. The saved cloud
session still receives HTTP 500. No live direct connection, camera JPEG or
relay fallback is claimed. A new password-based login is the next live gate.

## Diagnostic retry after user login

The user reports successful cloud login/listing and logout of other clients
after Python login. The cause of the logout is unverified. Autonomous CLI no
longer logs in implicitly or retries login after AuthError; only explicit
`login` performs authentication and warns about this observed risk.

One diagnostic capture with the saved token confirmed cloud HTTP 200 and
MQTT WatchDevice acknowledgement. Discovery received 48 packets, advertised
14 candidate endpoints, and timed out after 45 seconds without peer punch/ready.
There was no XM login or video attempt in that run. Timeout logging therefore
does not establish that a larger JPEG deadline fixes capture.

Connection and frame defaults are now 45/120 seconds, independently adjustable.
Metadata-only rotating logs are in `logs/autonomous.log`. XM keepalive polling
was corrected so a silent peer cannot block heartbeat until the frame deadline.

## Re-audit while the phone app works, 2026-10-14

The user reports successful live view in the phone app while the utility still
fails. Prior utility logs show failure before peer readiness, not a JPEG decoder
failure. Bad mobile link quality alone is not established as the explanation.

Two concrete implementation deficiencies were corrected:

- P2PReqAck (`0x21`, four-byte body with one status byte) was ignored. Numeric
  statuses are now logged, along with bounded counts of hello-ACK mappings.
- Punch retries shared discovery's timeout-dependent interval (4.5 seconds
  with the default 45-second deadline). They now have a separate 0.5-second
  timer, with discovery requests once per second and one initial hello.
  The recorded app requests discovery at roughly one-second intervals; exact
  traffic rates should not be inferred from counts because the router capture
  contains duplicates.

Full vendor ConnectByServer equivalence remains unimplemented. The cloud
configuration previously returned mode 122 (`0x7A`). The current ARM64 binary's
Connect_Do at `0x387f4` contains mode branches and calls TCP relay helpers
(e.g. TCPConnect/TCPRecv around `0x3a61c`–`0x3a65c`, TCPRelay_Setup at `0x3b998`).
The utility still implements only direct UDP. Neither the old independent
captures nor the user's report prove which transport the phone currently uses.
No claim is made that relay is the sole cause of the failed direct connection.

A fresh cloud-only diagnostic returned HTTP 500 with numeric JSON code 100401.
CloudClient now preserves that auth classification even on non-2xx HTTP,
without logging the response body or trying login. Expiration vs revocation is
unknown. A new live connection test could not proceed with this saved token;
no fresh login was performed. Offline codecs still match both PCAPs and native
synthetic crypto vectors. Retry count does not fix unsupported relay paths.

## Live recheck after the user renewed login

One `capture-all --wake --attempts 1 --verbose` run with the saved session
confirmed GetUserDeviceList and GetUserMQTTS HTTP 200, followed by correlated
WatchDevice acknowledgement. No login was performed by the agent.

The UDP discovery servers replied continuously: 132 received packets,
including 129 P2PReqAck replies with status 254 (`0xFE`) and three hello ACKs.
No candidate addresses, peer punch/ready or DRW data arrived within 45 seconds.
The utility did not reach XM login, open-video or JPEG decoding.

The user's preceding run differs: its final attempt had seven candidates,
81 punch rounds and ACK statuses 254 then 0, but likewise no peer readiness.
Neither result proves poor radio quality, wrong camera credentials or a JPEG
problem. Current discovery-server status 254 is recorded numerically; it is
not treated as proof that the physical camera is offline. Full native
ConnectByServer paths, including TCP/relay, remain the outstanding transport
work. Increasing frame timeout cannot address a failure before peer readiness.

## Confirmed tablet live session, 2026-10-14

The connected tablet now runs Baseus 1.12.0, not the configured 1.5.0. The
diagnostic session used a temporary in-memory version override; config.json
was not changed. The app opened the selected camera successfully, showed live
traffic and created fresh native JPEGs. One JPEG decoded fully at 2304x1296.
The stream was closed by returning to the home screen. Timing metadata is in
`logs/tablet_debug_session.json`.

`logs/tablet_live_20261014.pcapng` spans 300.161 seconds, but all 12787 IPv4
records have the tablet as source; **zero have the tablet as destination**.
There are no VLAN frames concealing incoming traffic. Outgoing video-channel
DRW ACKs show the successful session, but discovery replies and peer readiness
are absent, so the capture still does not expose the full handshake.

The outgoing F9 session reports do provide a useful transport indication:
they decode and validate with the vendor's proprietary report cipher. DID
format and all three sockaddr fields validate; the reported remote endpoint
matches the video ACK endpoint. The declared session mode is **0 (P2P)** and
the observed media transport is UDP, not TCP. This successful session therefore
does not establish that relay is necessary; direct UDP can work for the app.

Important format correction: the 32-bit field at report payload offset 20 is
**not** the mode in the current library. Native ReportSessionRdy writes its w7
argument at payload byte 31, and native Connect_Do loads that argument from
session offset 0x55. Native Check copies session bytes 0x54/0x55 to SessionInfo
0x50/0x51, confirming the mode byte. The initial offset-20 interpretation as
Relay was incorrect and corrected. The installed 1.12.0 PPCS library was
extracted locally to `analysis/baseus-1.12.0/libPPCS_API.so` and is byte-identical
by SHA-256 to the previously analysed ARM64 library.

Reproduce metadata analysis with:
`python -m analysis.inspect_tablet_capture logs/tablet_live_20261014.pcapng`.
The tool emits counts, pseudonymous endpoints and validated mode values only;
decrypted reports and credentials are never persisted. Safe metadata is in
`logs/tablet_live_20261014.metadata.json`. The next capture must first be
verified bidirectional (including ordinary TCP responses) before opening live
again. Fixing an unsupported relay path alone is not a demonstrated remedy
for this particular successful app connection.

## Bidirectional capture and LAN source diagnosis

`logs/tablet_live_bidir.pcapng` finally contains both tablet directions: 12731
outgoing and 12924 incoming IPv4 records, with about 6.8 MB incoming UDP payload
on the successful media flow. Three native attempts on distinct sockets fail
after about eight seconds and report signed result -24; they also attempt UDP
relay packets 0x67/0x69, 0x70 and 0x82/0x83. No relay success is inferred from
these failed attempts. A fourth socket establishes direct P2P: request ACK
changes from 254 to 0, fresh candidate arrives, peer punch and ready follow,
then control/media DRW data. Failed and successful attempts advertise different
camera UDP ports. Replay of the successful socket's incoming packets through
our actual _handshake completes at 1.354 seconds with the observed media peer.

The file ends before the recorded UI close time; full teardown is not captured,
but both failed handshakes and the successful handshake/media are present.
Failure F9 reports are now classified separately from malformed packets and
never counted as successful transport-mode reports.

Fresh cloud DID and resolved discovery nodes match the successful tablet
session. However the default Windows UDP source route selected VPN adapter
`redlinkk490sofx` (10.66.133.38), not the tablet's LAN. The user explicitly
allowed P2P-only testing on LAN. CLI options `--bind-address` and
`--interface-index` now apply Windows IP_UNICAST_IF only to PPCS sockets;
system routes, VPN state and HTTPS/MQTT routing are untouched. LAN source
192.168.88.36, interface 5, produced real peer readiness within seconds.

Further live validation exposed and corrected two XM assumptions: camera
control replies have version 0/encryption 1/model 0, and successful replies
use the string result "ok", matching SDK SignaCallback. Login and video-start
then succeeded, and the utility received/decrypted media continuously, but
the old H.264 hint selected the wrong decoder. Real decrypted parameter-set
NAL types 32/33/34 and IDR 19 prove HEVC. Codec selection now uses actual NAL
parameter sets, not just the header hint. The fixed decoder produced a valid
2304x1296 JPEG from the real capture after two frames.

The last live attempt after that decoder fix timed out before readiness; no
fresh autonomous JPEG is claimed yet. The user declined another live cycle
and will test manually with the LAN options and bounded retries. Capture-derived
JPEG stays in diagnostics and is never reused as a new capture result.
