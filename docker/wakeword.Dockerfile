FROM tensorflow/tensorflow:2.16.2-gpu

ENV DEBIAN_FRONTEND=noninteractive
ENV PYTHONUNBUFFERED=1
ARG TENSORFLOW_WHEEL_PATH=""
ARG TENSORFLOW_WHEEL_URL=""

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        ca-certificates \
        ffmpeg \
        git \
        unzip \
        wget \
    && rm -rf /var/lib/apt/lists/*

COPY third_party/tensorflow-wheels /tmp/tensorflow-wheels
RUN if [ -n "$TENSORFLOW_WHEEL_PATH" ]; then \
        python3 -m pip uninstall -y tensorflow \
        && python3 -m pip install --no-cache-dir "$TENSORFLOW_WHEEL_PATH"; \
    elif [ -n "$TENSORFLOW_WHEEL_URL" ]; then \
        python3 -m pip uninstall -y tensorflow \
        && python3 -m pip install --no-cache-dir "$TENSORFLOW_WHEEL_URL"; \
    fi

COPY vendor/micro-wake-word /app/vendor/micro-wake-word
RUN python3 -m pip install --no-cache-dir -e /app/vendor/micro-wake-word

COPY tools/lib /app/tools/lib

ENTRYPOINT ["python3"]
