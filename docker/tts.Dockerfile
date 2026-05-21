FROM nvidia/cuda:12.8.1-cudnn-runtime-ubuntu24.04

ENV DEBIAN_FRONTEND=noninteractive
ENV PIPER_HOME=/app/.piper-cache
ENV PYTHONUNBUFFERED=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        ffmpeg \
        python3 \
        python3-pip \
    && rm -rf /var/lib/apt/lists/*

COPY requirements-tts.txt /app/requirements-tts.txt
RUN python3 -m pip install --break-system-packages --no-cache-dir -r /app/requirements-tts.txt

COPY tools/lib /app/tools/lib

ENTRYPOINT ["python3", "-u", "tools/lib/box3_tts_piper.py"]
