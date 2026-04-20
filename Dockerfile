FROM python:3.11-slim

ARG SEEKARR_VERSION=dev

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

# Install gosu and shadow utilities
RUN apt-get update && apt-get install -y --no-install-recommends \
    gosu \
    shadow \
    && rm -rf /var/lib/apt/lists/*

RUN echo "${SEEKARR_VERSION}" > version.txt

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY . /app

# Non-root runtime user setup
RUN useradd -u 10001 -m appuser && \
    mkdir -p /data && \
    chown -R appuser:appuser /app /data && \
    chmod 775 /data && \
    chmod +x /app/docker/entrypoint.sh

# Privilege dropping is handled by the entrypoint
ENTRYPOINT ["/app/docker/entrypoint.sh"]

# Default: run the Web UI (includes auto-run logic). Mount /data for persistence:
# - /data/seekarr.db stores state and UI-managed settings
# - /data/seekarr.masterkey stores the encryption key for stored Arr API keys
VOLUME ["/data"]
CMD ["python", "webui_main.py", "--db-path", "/data/seekarr.db", "--host", "0.0.0.0", "--port", "8788"]
