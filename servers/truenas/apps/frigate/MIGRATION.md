# Migration: Home Assistant → the new TrueNAS Frigate (go2rtc)

Repoint Home Assistant at the new Frigate on the TrueNAS box and have HA consume
camera video through **Frigate's go2rtc restreams** instead of pulling each camera
directly. This cuts the number of live connections to the cameras (they have
limited RTSP sessions) and gives one consistent live-view path.

## Current HA state (before)

- **frigate** integration → `http://192.168.1.170:5000` (the OLD external Frigate box).
- **reolink** integration → doorbell `192.168.1.123` (user `haos`) — pulls video directly.
- **onvif** integration → outdoor `192.168.1.133` (user `admin`) — pulls video directly.
- **go2rtc** (HA's built-in) — restreams those for the dashboard.
- **mqtt** → `127.0.0.1:1883`, user `homeassistant`, protocol v5 (Frigate reuses this).
- living_room (Hikvision `192.168.1.241`) is **not** in HA directly — Frigate-only.

## Target (after)

- One **Frigate** (on `192.168.1.138`) owns all camera pulls via its go2rtc.
- HA's **frigate** integration points at the local Frigate and provides the camera
  entities + live view (MSE/WebRTC) backed by Frigate's go2rtc.
- **reolink** + **onvif** integrations are KEPT — but for **device controls/sensors**
  (doorbell button events, motion/AI sensors, IR/floodlight, PTZ), **not** as the
  dashboard video source.

## Steps

1. **Deploy Frigate** on TrueNAS and confirm it's healthy (see `README.md`). The
   door camera should be live; MQTT connected (Frigate logs `Connected to MQTT`).

2. **Confirm HA MQTT is the same broker** — it already is (`127.0.0.1:1883`,
   `homeassistant`). Frigate publishes under `frigate/…`; HA's Frigate integration
   reads most entities over MQTT, so this must match. No change expected.

3. **Repoint the HA Frigate integration** (Settings → Devices & Services → Frigate):
   - Delete the instance pointing at `http://192.168.1.170:5000`.
   - Re-add with URL **`http://127.0.0.1:5000`** (HA is host-net on the same box;
     5000 is published on loopback, unauthenticated, never on the LAN — so no
     username/password needed). Do **not** use `http://frigate:5000` (container DNS
     won't resolve from host-net HA) and not `192.168.1.170` (old box).
   - Keep **enable_webrtc** on (it was on for the old one).

4. **Update the Frigate HACS integration** to match the server. The installed
   `frigate` custom integration is **v5.11.0**, which predates Frigate 0.16. In
   HACS → Frigate → update to the latest **v5.15.x**, then restart HA. (This is the
   same Frigate-integration update flagged in the HA 2026.6 upgrade follow-ups.)

5. **Swap dashboard video tiles** to the new Frigate camera entities (e.g.
   `camera.door`, `camera.living_room`, `camera.outdoor` from the Frigate
   integration). Use the **Advanced Camera Card** for live view. Live now flows
   browser → HA → Frigate go2rtc (MSE over `127.0.0.1:8554`, or WebRTC over
   `192.168.1.138:8555`). You should see only **Frigate** connecting to each camera.

6. **Demote reolink/onvif from video to controls** — stop using their `camera.*`
   entities as dashboard video tiles, but keep the integrations for the doorbell
   button press, motion/person sensors, switches and PTZ. Remove the three cameras
   from HA's built-in go2rtc if you had them there (no double restream).

7. **Two-way talk (optional, doorbell)** needs WebRTC + HTTPS + a non-backchannel
   stream copy. The go2rtc streams use `#backchannel=0`; add a separate talk stream
   later if you want intercom from HA.

8. **Decommission the old Frigate** at `192.168.1.170` once the new one is verified.

## WebRTC note

For low-latency WebRTC live view the browser connects to `192.168.1.138:8555`
(published on the LAN; `go2rtc.webrtc.candidates` is set to it). If you view over
Tailscale, add your `100.x` tailnet IP to `go2rtc.webrtc.candidates` in
`config.yml` and re-push. Without WebRTC, HA falls back to MSE over 8554 — still
full-res with audio, just slightly higher latency.
