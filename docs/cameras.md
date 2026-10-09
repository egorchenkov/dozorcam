# Cameras

How Dozorcam finds a camera's video stream, and which cameras have been checked live.
Russian: [cameras.ru.md](cameras.ru.md).

## Three ways to add a camera

1. **Found by itself.** `/add` scans the node's own /24 networks (and `CCTV_DISCOVERY_NETWORKS`)
   for RTSP (554) and ONVIF, plus multicast WS-Discovery in the same LAN. Pick the camera,
   send "login password" — the bot deletes that message at once — and the engine asks the
   camera over ONVIF for its streams and snapshot address.
2. **By IP, without knowing the RTSP path.** `/add 192.0.2.64` (or "✍️ Enter address" under
   the search result), then "login password". If the camera has no ONVIF or ONVIF gives
   nothing usable, the engine tries the typical paths below one by one — at most 3 s per
   path, about a minute for the whole table — and takes the first one the camera answers
   with `200 OK`. The bot shows which path worked, e.g. "Stream path found by trying typical
   ones: /h264Preview_01_main (Reolink)", and offers the second (low-resolution) stream to
   the person detector if the camera has one. If the camera answered `401` on every path,
   the bot says the login or password is wrong instead of "no stream".
3. **Manually.** `/add rtsp://host:port/path` for anything else (non-standard port, NVR
   channel, RTSP server). A second address after a space is the detector stream:
   `/add rtsp://host/main rtsp://host/sub`.

Never put the login or password into the address: the bot refuses `user:pass@` and
`password=` in the path and asks for them in a separate message that it deletes.

## Typical paths

Tried in this order; the brand recognised from ONVIF, the RTSP banner or the web page goes
first, generic paths always last. Port 554.

| Brand | Main stream | Detector stream |
|---|---|---|
| Hikvision / HiWatch | `/Streaming/Channels/101` | `/Streaming/Channels/102` |
| Dahua / Imou / Amcrest | `/cam/realmonitor?channel=1&subtype=0` | `…&subtype=1` |
| Tantos / QualVision | `/stream?mode=real&idc=1&ids=1` | `…&ids=2` |
| Reolink | `/h264Preview_01_main` | `/h264Preview_01_sub` |
| TP-Link Tapo / VIGI | `/stream1` | `/stream2` |
| Uniview | `/media/video1` | `/media/video2` |
| Axis | `/axis-media/media.amp` | `/axis-media/media.amp?resolution=640x360` |
| Xiongmai / XMEye | `/user=<login>&password=<password>&channel=1&stream=0.sdp` | `…&stream=1.sdp` |
| generic | `/live/main`, `/live`, `/11`, `/live/ch00_0`, `/videoMain`, `/onvif1` | `/live/sub`, —, `/12`, `/live/ch00_1`, `/videoSub`, `/onvif2` |

**Xiongmai / XMEye** cameras take the login and password in the path itself, not as RTSP
authentication. The engine builds that path from the login and password you sent in the
separate (deleted) message. The address is stored only in the camera registry on your node;
everywhere it is shown — chat, logs, `GET /v1/cameras` — the password is masked
(`password=***`), the same as `user:***@` in ordinary addresses.

**RTSP login: Digest only.** A camera with a password is recorded through the engine's local
RTSP proxy, which answers the camera's Digest challenge (Hikvision, Dahua, Reolink, Tapo,
Uniview, Axis and most others use Digest). A camera that offers only Basic authentication is
found and probed, but its recording does not start (`rtsp_auth_failed reason=unsupported_challenge`
in `dozorcam logs engine`, and the bot reports "no video" after two minutes): switch the camera's
RTSP authentication to Digest (often "digest" or "digest/basic" in its web settings).

## Checked cameras

✅ — checked live; ☐ — the path is in the table above, but not yet confirmed on a real camera
of that brand. Please report what works for you (issue or pull request to this file).

| Camera | Found by itself | By IP + login | Manual address |
|---|---|---|---|
| Hikvision DS-2CD2xx3G0/G2 (incl. new, not activated) | ✅ | ✅ | ✅ |
| Hikvision DS-2CD2543G2-IS, G5 series | ✅ | ✅ | ✅ |
| Tantos (QualVision platform) | ✅ | ✅ | ☐ |
| ffmpeg / MediaMTX test stream (`testsrc`) | — | — | ✅ |
| Dahua / Imou / Amcrest | ☐ | ☐ | ☐ |
| Reolink | ☐ | ☐ | ☐ |
| TP-Link Tapo / VIGI | ☐ | ☐ | ☐ |
| Uniview | ☐ | ☐ | ☐ |
| Axis | ☐ | ☐ | ☐ |
| Xiongmai / XMEye | ☐ | ☐ | ☐ |

A new camera without a password (first setup) is a separate step: Hikvision is activated
from `/add` itself; for the other brands the bot shows short brand instructions — see
[vendor-activation.md](vendor-activation.md).
