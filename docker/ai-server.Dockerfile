FROM nvidia/cuda:12.8.1-cudnn-runtime-ubuntu24.04

ENV DEBIAN_FRONTEND=noninteractive
ENV HF_HOME=/app/.hf-cache
ENV PIPER_HOME=/app/.piper-cache
ENV PYTHONUNBUFFERED=1

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        avahi-daemon \
        ca-certificates \
        dbus \
        ffmpeg \
        libnss-mdns \
        python3 \
        python3-pip \
    && sed -i 's/^hosts:.*/hosts:          files mdns4_minimal [NOTFOUND=return] dns mdns4/' /etc/nsswitch.conf \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN python3 -m pip install --break-system-packages --no-cache-dir -r requirements.txt

COPY ai_server/ ai_server/
COPY docker/ai-server-entrypoint.sh /usr/local/bin/ai-server-entrypoint.sh
RUN chmod +x /usr/local/bin/ai-server-entrypoint.sh
