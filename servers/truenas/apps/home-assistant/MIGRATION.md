# Home Assistant cutover runbook (go-time)

This app is staged with `enabled: false`. The supporting apps (mosquitto, voice,
esphome, uptime-kuma) are already deployed and verified. Do the steps below when
you're ready to cut over. The dongle move decommissions the old HA's Zigbee, so
plan to retire the old VM at the same time. **Keep the old VM intact** as rollback
until the new one is proven.

## 0. Prereqs
- Sonoff dongle physically moved to the TrueNAS host.
- A DNS record for `homeassistant.bajaber.ca` (grey-cloud / DNS-only is fine).
- A current full HA backup, OR access to the old VM's `/config` for a final sync.

## 1. Confirm the dongle path (on TrueNAS shell)
```
ls -l /dev/serial/by-id/
```
It must show:
`usb-ITead_Sonoff_Zigbee_3.0_USB_Dongle_Plus_3ed6580b58a4ed11aa8f82582981d5c7-if00-port0`
(identical to what the compose `devices:` maps — hardware-derived, so it matches).
If the string differs, update `compose.yml` `devices:` accordingly.

## 2. Cleanly stop the old HA
Shut down the old HA VM cleanly (flushes the SQLite WAL so the DB copy isn't
half-written). Grab the freshest `/config` (a fresh full backup is simplest).

## 3. Seed /config onto TrueNAS
The reconciler creates the dataset on apply, but the container would boot empty.
So pre-create the dataset and seed it BEFORE enabling:
- Create the dataset (via the reconcile path or `pool.dataset.create`) at
  `redsea/apps/home-assistant`, then copy the backup `/config` tree into
  `/mnt/redsea/apps/home-assistant/config/` (includes `.storage/`,
  `configuration.yaml`, `automations.yaml`, `home-assistant_v2.db`, `zigbee.db`,
  `custom_components/`, `www/`, `esphome/` is NOT needed here — it moved to the
  esphome app).
- Fix ownership: `chown -R 568:568 /mnt/redsea/apps/home-assistant/config`.

## 4. Edit /config before first boot
In `configuration.yaml`:
```yaml
http:
  use_x_forwarded_for: true
  trusted_proxies:
    - 172.16.0.0/12      # TrueNAS docker bridge (Traefik forwards from here)
    - 127.0.0.1
    - ::1
homeassistant:
  internal_url: "http://192.168.1.138:8123"     # Cast pulls media over the LAN
  external_url: "https://homeassistant.bajaber.ca"
```
Move the cleartext luci router password to `secrets.yaml` (`!secret luci_password`).

## 5. Deploy HA
- Set `enabled: true` in `app.yml`.
- `ansible-playbook playbooks/truenas_sync.yml -e mode=apply` (dataset already
  exists, so the reconciler skips creating it and just deploys the container).

## 6. Re-point the Supervisor-internal hostnames (after boot)
The old config references `core-*` add-on hostnames that no longer exist. Re-point
(UI: Settings > Devices & Services, or edit `.storage/core.config_entries` while
stopped):
- **MQTT**: broker `core-mosquitto` -> `127.0.0.1`, user `homeassistant`, port 1883
- **Wyoming Whisper**: `core-whisper:10300` -> `127.0.0.1:10300`
- **Wyoming Piper**: `core-piper:10200` -> `127.0.0.1:10200`  (Arabic voice)

## 7. Add the new voice pieces
- **Wyoming** -> `127.0.0.1:10500` (wyoming-openai = Kokoro English TTS)
- **Wyoming** -> `127.0.0.1:10400` (openwakeword) — only once a mic'd satellite exists
- In the Assist pipeline, pick Kokoro for English TTS, Piper for Arabic.

## 8. Wire Traefik + DNS
Merge `traefik-file-provider.snippet.yml` into
`/mnt/redsea/apps/traefik/file-provider.yml`, add the DNS record, then browse
`https://homeassistant.bajaber.ca`. Set the companion app to ALWAYS use the
external URL.

## 9. Verify
- ZHA: devices online (same dongle + zigbee.db = mesh preserved, no re-pair).
- Cast: cast a TTS message to a Chromecast.
- Cameras (Reolink/ONVIF/Frigate), HACS (Mushroom, Frigate card), automations.

## Rollback
Stop the new HA, move the dongle back to the old VM, boot it. The old VM and the
`redsea/apps/home-assistant` dataset are untouched by each other.
