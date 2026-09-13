FROM python:3.11-slim

ENV AJIASU_PATH=/usr/local/bin/ajiasu \
    GATEWAY_PORT=8000 \
    GATEWAY_DATA_DIR=/data

COPY vendor/ /vendor/
RUN ARCH="$(uname -m)" && \
    case "$ARCH" in \
      x86_64) A="amd64" ;; \
      aarch64|arm64) A="aarch64" ;; \
      *) echo "Unsupported arch: $ARCH"; exit 1 ;; \
    esac && \
    tar xzf "/vendor/ajiasu-$A-4.2.3.0.tar.gz" -C /tmp && \
    mv /tmp/ajiasu /usr/local/bin/ajiasu && \
    chmod +x /usr/local/bin/ajiasu && \
    mkdir -p /wheels && \
    cp /vendor/wheels/common/*.whl /wheels/ && \
    cp /vendor/wheels/$A/*.whl /wheels/ && \
    rm -rf /vendor

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir --no-index --find-links=/wheels -r requirements.txt && \
    rm -rf /wheels

COPY app ./app
COPY run.py .
COPY templates ./templates

RUN mkdir -p /data
VOLUME ["/data"]

EXPOSE 8000 1080 10801

CMD ["python", "run.py"]
