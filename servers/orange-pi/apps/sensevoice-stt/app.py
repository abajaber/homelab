#!/usr/bin/env python3
"""OpenAI-compatible STT shim: SenseVoice-Small on RK3588 NPU via sherpa-onnx.

Exposes POST /v1/audio/transcriptions (Open WebUI STT) backed by the RKNN
SenseVoice model on the NPU. English (Arabic not supported by SenseVoice).
Audio is decoded with ffmpeg (handles Open WebUI's webm/opus, plus mp3/wav/m4a),
resampled to 16k mono, and — for clips longer than the model window — VAD-split
so nothing is truncated.
"""
import os, subprocess, numpy as np
import sherpa_onnx
from fastapi import FastAPI, UploadFile, File, Form
from fastapi.responses import JSONResponse

MODEL_DIR   = os.environ.get("MODEL_DIR", "/models/sv")
VAD_MODEL   = os.environ.get("VAD_MODEL", "/models/silero_vad.onnx")
NPU_CORE    = int(os.environ.get("NPU_CORE", "-2"))       # RK3588 core 2
LANG        = os.environ.get("LANG_CODE", "en")
MAX_SECONDS = float(os.environ.get("MAX_SECONDS", "10"))  # must match model window
SR = 16000

recognizer = sherpa_onnx.OfflineRecognizer.from_sense_voice(
    model=f"{MODEL_DIR}/model.rknn",
    tokens=f"{MODEL_DIR}/tokens.txt",
    provider="rknn",       # load-bearing: NPU. "cpu" errors on a .rknn model.
    num_threads=NPU_CORE,  # on RK3588 this is the NPU core selector, not a count
    language=LANG,
    use_itn=True,          # punctuation + inverse text normalization
    debug=False,
)

def _make_vad():
    c = sherpa_onnx.VadModelConfig()
    c.silero_vad.model = VAD_MODEL
    c.silero_vad.threshold = 0.4
    c.silero_vad.min_silence_duration = 0.25
    c.silero_vad.max_speech_duration = MAX_SECONDS - 1.0  # stay under the window
    c.sample_rate = SR
    return c
VAD_CFG = _make_vad()

def decode_to_pcm(raw: bytes) -> np.ndarray:
    """Any container (webm/ogg/mp3/wav/m4a) -> 16k mono float32 via ffmpeg."""
    p = subprocess.run(
        ["ffmpeg", "-nostdin", "-hide_banner", "-loglevel", "error",
         "-i", "pipe:0", "-f", "f32le", "-ac", "1", "-ar", str(SR), "pipe:1"],
        input=raw, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
    )
    if p.returncode != 0:
        raise RuntimeError(p.stderr.decode("utf-8", "ignore")[:500])
    return np.frombuffer(p.stdout, dtype=np.float32).copy()

def transcribe_one(samples: np.ndarray) -> str:
    s = recognizer.create_stream()
    s.accept_waveform(SR, samples)
    recognizer.decode_stream(s)
    return s.result.text.strip()

def transcribe(samples: np.ndarray) -> str:
    if len(samples) <= int(MAX_SECONDS * SR):
        return transcribe_one(samples)
    # Long audio: VAD-segment, recognize each speech chunk, join.
    vad = sherpa_onnx.VoiceActivityDetector(VAD_CFG, buffer_size_in_seconds=100)
    out, win = [], 4096
    for i in range(0, len(samples), win):
        vad.accept_waveform(samples[i:i + win])
        while not vad.empty():
            out.append(transcribe_one(vad.front.samples)); vad.pop()
    vad.flush()
    while not vad.empty():
        out.append(transcribe_one(vad.front.samples)); vad.pop()
    return " ".join(t for t in out if t).strip()

app = FastAPI()

@app.get("/v1/models")
def list_models():
    return {"object": "list", "data": [
        {"id": "sensevoice-small", "object": "model", "owned_by": "local"}]}

@app.get("/health")
def health():
    return {"status": "ok"}

@app.post("/v1/audio/transcriptions")
async def transcriptions(file: UploadFile = File(...),
                         model: str = Form("sensevoice-small"),
                         response_format: str = Form("json")):
    try:
        samples = decode_to_pcm(await file.read())
        text = transcribe(samples)
    except Exception as e:
        return JSONResponse(status_code=400, content={"error": {"message": str(e)}})
    if response_format == "text":
        return text
    return {"text": text}
