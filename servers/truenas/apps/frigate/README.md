# Frigate (TrueNAS)

Frigate NVR at `https://frigate.bajaber.ca`. Object detection on CPU (OpenVINO),
hardware video decode on the NVIDIA GPU (NVDEC). Three cameras restreamed through
Frigate's embedded go2rtc so Home Assistant consumes one feed per camera instead
of hitting the cameras directly (see `MIGRATION.md`).

## GPU strategy

The only GPU is the NVIDIA **GTX 1660 SUPER (6 GB)**, with **no Intel iGPU**. It's
shared with Ollama, faster-whisper, kokoro TTS, Jellyfin NVENC and Immich ML.
Both Frigate jobs run on the GPU (the other models are barely used right now):

| Job | Where | Notes |
|---|---|---|
| **Detection** (object inference) | **NVIDIA GPU** — ONNX detector, **yolov9-t @ 320**, via the `-tensorrt` image's onnxruntime TensorRT EP | ~1–1.5 GB VRAM (CUDA context + engine + activations). yolov9-**t** (tiny) at 320 is the smallest footprint. |
| **Decode** (video) | **NVIDIA NVDEC** (`preset-nvidia`) | Small VRAM; only the detect substreams decode, record is stream-copied (`-c copy`). |

The original Intel config (`openvino device: GPU`, `preset-vaapi`, `/dev/dri`) does
**not** apply here and was re-targeted to NVIDIA.

**VRAM contention warning:** when Ollama (~3.6 GB) + whisper (~1 GB) are both active,
Frigate's ~1.5 GB pushes the card to/over its 6 GB. It's fine while those are idle
("barely used for now"), but under concurrent load it can contend. **Fallback** =
switch the detector back to OpenVINO-CPU (0 VRAM): replace the `detectors` + `model`
block in `config.yml` with the OpenVINO-CPU block quoted inline there, and you can
drop back to the plain `0.16.4` image. To drop GPU decode too, delete
`ffmpeg.hwaccel_args: preset-nvidia` (CPU decode is cheap at 640×360).

### GPU detection model (yolov9-t-320.onnx) — required before first start

Frigate 0.16 does **not** auto-download detection models. Build the ONNX once (CPU
build, needs Docker + internet; ~a few min + a torch download) and push it into the
app's `config/model_cache`:

```bash
# Build yolov9-t at 320 -> ./yolov9-t-320.onnx (run on any host with Docker)
docker build . --build-arg MODEL_SIZE=t --build-arg IMG_SIZE=320 --output . -f- <<'EOF'
FROM python:3.11 AS build
RUN apt-get update && apt-get install --no-install-recommends -y libgl1 && rm -rf /var/lib/apt/lists/*
COPY --from=ghcr.io/astral-sh/uv:0.8.0 /uv /bin/
WORKDIR /yolov9
ADD https://github.com/WongKinYiu/yolov9.git .
RUN uv pip install --system -r requirements.txt
RUN uv pip install --system onnx==1.18.0 onnxruntime onnx-simplifier>=0.4.1 onnxscript
ARG MODEL_SIZE
ARG IMG_SIZE
ADD https://github.com/WongKinYiu/yolov9/releases/download/v0.1/yolov9-${MODEL_SIZE}-converted.pt yolov9-${MODEL_SIZE}.pt
RUN sed -i "s/ckpt = torch.load(attempt_download(w), map_location='cpu')/ckpt = torch.load(attempt_download(w), map_location='cpu', weights_only=False)/g" models/experimental.py
RUN python3 export.py --weights ./yolov9-${MODEL_SIZE}.pt --imgsz ${IMG_SIZE} --simplify --include onnx
FROM scratch
ARG MODEL_SIZE
ARG IMG_SIZE
COPY --from=build /yolov9/yolov9-${MODEL_SIZE}.onnx /yolov9-${MODEL_SIZE}-${IMG_SIZE}.onnx
EOF

# Push it to the app dataset (binary -> use a raw upload, NOT ot.py write which is text/escapes !)
. ./.open-terminal.env
curl -sS -H "Authorization: Bearer $OPEN_TERMINAL_API_KEY" \
  -F "path=/mnt/apps/frigate/config/model_cache/yolov9-t-320.onnx" \
  -F "file=@yolov9-t-320.onnx" "$OPEN_TERMINAL_URL/files/upload"   # confirm the upload endpoint in /docs
```

On first start Frigate compiles a TensorRT engine from the ONNX into
`/config/model_cache` (slow first boot, a few minutes; cached after).

## Layout & conventions

- `compose.yml` — the only thing the reconciler ships. Pinned to **0.16.4** (see
  "Version" below). GPU reserved by UUID like Jellyfin; `NVIDIA_DRIVER_CAPABILITIES=all`
  so NVDEC's `video` capability is exposed.
- `config.yml` — Frigate's config. **Delivered out-of-band** (TrueNAS ships only the
  compose body), like the Authentik blueprints. **Not drift-tracked.** Frigate's
  migrator may reformat/stamp it on boot, so the live copy drifts from this seed —
  expected.
- `.env` — vault-encrypted RTSP + MQTT secrets. Referenced from `compose.yml` as
  `${VAR}`, surfaced to Frigate as `{FRIGATE_*}` in `config.yml`.

### Secret flow & the URL-encoding gotcha

`.env` (vault) → `compose.yml` `${FRIGATE_*}` (reconciler substitutes) → container
env → Frigate resolves `{FRIGATE_*}` in `config.yml`. The `FRIGATE_` prefix and
plain-brace `{...}` syntax are **mandatory** (not `${}`, not `!secret`).

Inside the `go2rtc:` section Frigate does **not** auto-URL-encode passwords, so any
special character must be percent-encoded in the `.env` value. The door password's
`^` is stored as `%5E`. (In a Frigate `ffmpeg.inputs[].path` it *would* be auto-encoded
— but we only use raw passwords in `go2rtc:`.)

## Storage

- `config` and `media` bind under the per-app dataset `/mnt/redsea/apps/frigate/`
  (reconciler creates the dataset + folders on first apply).
- **Recordings churn heavily.** If `redsea/apps` has a snapshot task, the media will
  bloat snapshots. Recommended (manual, one-time): carve `redsea/apps/frigate/media`
  into its own child dataset with a short/no snapshot retention. The reconciler is
  idempotent and won't touch a pre-existing child dataset.

## Ports

HA runs `network_mode: host` on the same box, so it reaches Frigate over
**loopback-published** ports (kept off the LAN):

| Port | Bind | Purpose |
|---|---|---|
| 5000 | `127.0.0.1` | Frigate API/UI (unauth) — HA integration |
| 8554 | `127.0.0.1` | go2rtc RTSP restream — HA live view (MSE) |
| 1984 | `127.0.0.1` | go2rtc API + MSE fallback |
| 8555/tcp+udp | `0.0.0.0` | WebRTC — browser live view (needs LAN reach) |

The external UI is fronted by **Traefik → `frigate:5000` + `authentik@docker`**
(Authentik SSO); 5000 is never exposed raw on the LAN.

## Deploy runbook

```bash
source .venv/bin/activate

# 1) plan — expect frigate under '+ to-create'
ansible-playbook playbooks/truenas_sync.yml

# 2) apply — creates the dataset + binds and starts the container (no config/model yet)
ansible-playbook playbooks/truenas_sync.yml -e mode=apply

# 3) build + push the GPU detection model (see "GPU detection model" above), into
#    /mnt/apps/frigate/config/model_cache/yolov9-t-320.onnx
./scripts/ot.py exec --timeout 10 'mkdir -p /mnt/apps/frigate/config/model_cache'
#    ...then upload the .onnx (binary) as shown above.

# 4) push the Frigate config out-of-band (config.yml has no '!' so ot.py write is safe;
#    verify afterwards that no '!' got mangled to '\!')
./scripts/ot.py write /mnt/apps/frigate/config/config.yml < servers/truenas/apps/frigate/config.yml
./scripts/ot.py exec --timeout 10 'grep -n "\\\\!" /mnt/apps/frigate/config/config.yml || echo "no \\! corruption - good"'

# 5) restart Frigate so it reads the config + builds the TensorRT engine (slow first
#    boot, a few minutes). app.stop + app.start, or re-apply.

# 6) DNS: add an AdGuard rewrite frigate.bajaber.ca -> 192.168.1.138 (control API),
#    same as the other *.bajaber.ca hosts.
```

Verify: `https://frigate.bajaber.ca` loads (via Authentik), the **door** camera is
live (living_room/outdoor are offline for now — fine), Frigate logs show no config
errors and `Camera processor started`, and `nvidia-smi` shows a small Frigate NVDEC
allocation without starving the LLM.

## Version

Running **0.17.2**. **0.17 reworked record retention**: `record.retain: {days, mode}`
→ tiered `record.continuous: {days}` / `record.motion: {days}`. Each camera's
continuous recording is `record.continuous.days: 1`; `alerts.retain` /
`detections.retain` (30 days, mode motion) are unchanged. Other 0.17 breaks that do
**not** affect this config: go2rtc drops `exec`/`expr`/`echo` sources (we only use
`rtsp://`), `genai` moved under `objects.genai` (unused), `strftime_fmt` removed
(unused), and detect `width`/`height` auto-detect changed (already set explicitly on
every camera). Watch the first boot log for schema errors after any image bump.
