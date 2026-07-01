FROM python:3.11-slim-bookworm

# ffmpeg = decode Open WebUI's webm/opus (+ mp3/wav/m4a); libgomp1 for onnxruntime
# (Silero VAD); curl for asset fetch + healthcheck.
RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg libgomp1 ca-certificates curl bzip2 \
    && rm -rf /var/lib/apt/lists/*

# 1) RKNN runtime. PIN 2.2.0 (docs-blessed for rk3588; pairs with kernel driver
#    0.9.8). The sherpa-onnx wheel dynamically links this but does NOT bundle it,
#    and the host has none — so bake it in. `import sherpa_onnx` fails outright if
#    it's missing, so it must land before the pip step.
RUN curl -fSL -o /usr/lib/librknnrt.so \
      https://github.com/airockchip/rknn-toolkit2/raw/v2.2.0/rknpu2/runtime/Linux/librknn_api/aarch64/librknnrt.so \
    && ldconfig \
    && grep -a "librknnrt version" /usr/lib/librknnrt.so

# 2) sherpa-onnx RKNN-enabled wheel (NOT the PyPI CPU build) + shim deps.
RUN pip install --no-cache-dir \
      https://huggingface.co/csukuangfj2/sherpa-onnx-wheels/resolve/main/rknn/1.13.3/sherpa_onnx-1.13.3-cp311-cp311-manylinux_2_27_aarch64.whl \
      fastapi "uvicorn[standard]" python-multipart numpy \
    && python -c "import sherpa_onnx; print('sherpa-onnx', sherpa_onnx.__version__)"

# Hard-fail the build if RKNN isn't linked into the Python extension the API uses.
RUN SO=$(python -c "import glob,os,sherpa_onnx; print(glob.glob(os.path.join(os.path.dirname(sherpa_onnx.__file__),'lib','_sherpa_onnx*.so'))[0])") \
    && ldd "$SO" | grep -q librknnrt.so \
    && echo "RKNN linked into python extension OK"

# 3) Model (10-second window) + Silero VAD, baked in.
RUN mkdir -p /models/sv && cd /tmp \
    && curl -fSL -o m.tar.bz2 \
       https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/sherpa-onnx-rk3588-10-seconds-sense-voice-zh-en-ja-ko-yue-2024-07-17.tar.bz2 \
    && tar xf m.tar.bz2 \
    && cp sherpa-onnx-rk3588-10-seconds-sense-voice-zh-en-ja-ko-yue-2024-07-17/model.rknn  /models/sv/ \
    && cp sherpa-onnx-rk3588-10-seconds-sense-voice-zh-en-ja-ko-yue-2024-07-17/tokens.txt /models/sv/ \
    && curl -fSL -o /models/silero_vad.onnx \
       https://github.com/k2-fsa/sherpa-onnx/releases/download/asr-models/silero_vad.onnx \
    && rm -rf /tmp/*

COPY app.py /app/app.py
WORKDIR /app
ENV MODEL_DIR=/models/sv VAD_MODEL=/models/silero_vad.onnx \
    NPU_CORE=-2 LANG_CODE=en MAX_SECONDS=10
EXPOSE 8000
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8000"]
