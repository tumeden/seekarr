FROM python:3.11-slim

ARG SEEKARR_VERSION=dev

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    SEEKARR_VERSION=${SEEKARR_VERSION}

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY . /app

# Non-root runtime user
RUN useradd -u 10001 -m appuser && \
    mkdir -p /data && \
    chown -R appuser:appuser /app /data && \
    chmod 775 /data
USER appuser

# Default: run the Web UI (includes auto-run logic). Mount /data for persistence:
# - /data/config.yaml is auto-created on first run if missing
# - /data/seekarr.db stores state
# - /data/seekarr.masterkey stores the encryption key for stored Arr API keys
VOLUME ["/data"]
CMD ["python", "webui_main.py", "--config", "/data/config.yaml", "--host", "0.0.0.0", "--port", "8788", "--allow-public"]
