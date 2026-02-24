# Seekarr

Seekarr automatically triggers Radarr/Sonarr searches for items already in your library (missing and/or cutoff-unmet), on a schedule, with cooldown + rate limits to avoid API spam.

Scope:
- Focused strictly on automatic searching via Sonarr/Radarr.
- No unrelated "arr suite" features (download cleanup, etc).

Transparency:
- Built with significant AI assistance. Review and use at your own discretion.

<!-- screenshots -->
<img width="1474" height="529" alt="Seekarr dashboard" src="https://github.com/user-attachments/assets/85993bc0-6466-4349-9a82-df99ef89a818" />
<img width="1400" height="774" alt="image" src="https://github.com/user-attachments/assets/8da3fdd8-b83b-4bb2-9400-a58499e127bf" />

---

## What It Does

- Pulls "wanted" lists from Radarr/Sonarr (missing and/or cutoff-unmet).
- Triggers searches per instance on its interval.
- Remembers what it already searched (SQLite cooldown) so it will not retry the same item constantly.
- Paces requests so large libraries do not cause bursts.
- Skips unreleased content (default: wait 8 hours after air/release).
- Can pause searching during quiet hours (default: 23:00 to 06:00 local time).

---

## Quick Start (Config)

1. Create your config:

```bash
cp config.example.yaml config.yaml
```

2. Edit `config.yaml` and set your Arr URLs:

- If Seekarr runs on a different machine than Radarr/Sonarr, do not use `localhost`.
- Use an IP/hostname Seekarr can reach (example: `http://192.168.1.50:7878`).

3. Provide API keys as environment variables (examples):

```env
RADARR_API_KEY_1=your-radarr-key
SONARR_API_KEY_1=your-sonarr-key
```

---

## Docker (Easiest)

Seekarr publishes Docker images. `:latest` tracks the newest `v*` release tag.

Pull:

```bash
docker pull tumeden/seekarr:latest
```

Minimal `docker-compose.yml` (Web UI):

```yaml
services:
  seekarr:
    image: tumeden/seekarr:latest
    container_name: seekarr
    restart: unless-stopped
    command: ["python", "webui_main.py", "--config", "/config/config.yaml", "--host", "0.0.0.0", "--port", "8788", "--allow-public"]
    ports:
      - "127.0.0.1:8788:8788"
    environment:
      WEBUI_AUTORUN_DEFAULT: "1"
    env_file:
      - ./config/seekarr.env
    volumes:
      - ./config:/config:ro
      - ./data:/data
```

Notes:
- Put `config.yaml` at `./config/config.yaml` and set `app.db_path: "/data/seekarr.db"` so the DB persists.
- Put keys in `./config/seekarr.env`.

---

## Linux (systemd via install.sh)

Console mode (no UI):

```bash
sudo ./install.sh --mode console --user youruser
```

Web UI mode (also runs the automation):

```bash
sudo ./install.sh --mode webui --user youruser --webui-host 127.0.0.1 --webui-port 8788
```

Logs:

```bash
journalctl -u seekarr-console -f
journalctl -u seekarr-webui -f
```

---

## Windows (Quick Run)

1. Install Python 3.11+
2. Install deps:

```bat
python -m pip install -r requirements.txt
```

3. Run (pick one):
- `run-webui.bat`
- `run-console.bat`

---

## Common Errors

- Connection refused/unreachable: the Arr URL/port is wrong, or Radarr/Sonarr is not reachable from where Seekarr runs.
- HTTP 401/403: API key is wrong or lacks permission.

---

## Technical Notes

- The Web UI binds to localhost by default. `--allow-public` is required to bind to non-localhost, to avoid accidentally exposing `/api/*` endpoints.
- `config.yaml` supports `${ENV_VAR}` interpolation.

