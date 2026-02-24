FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

WORKDIR /app

COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir -r /app/requirements.txt

COPY . /app

# Non-root runtime user
RUN useradd -u 10001 -m appuser && chown -R appuser:appuser /app
USER appuser

# Default: run the Web UI (includes auto-run logic). Mount /config (config.yaml + optional .env) and /data (sqlite db).
CMD ["python", "webui_main.py", "--config", "/config/config.yaml", "--host", "0.0.0.0", "--port", "8788", "--allow-public"]
