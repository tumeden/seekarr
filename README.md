# Seekarr

Seekarr automatically triggers Radarr/Sonarr searches for items already in your library (missing and/or cutoff-unmet), on a schedule, with cooldown + rate limits to avoid API spam.

Scope:
- Focused strictly on automatic searching via Sonarr/Radarr.
- No unrelated "arr suite" features (download cleanup, etc).

Transparency:
- Built with significant AI assistance. Review and use at your own discretion.

<!-- screenshots -->
<img width="1129" height="515" alt="image" src="https://github.com/user-attachments/assets/f754e7dc-5bb7-4f13-9b42-3b3b6fb495ae" />
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

3. Provide API keys (pick one):

- Recommended: set them in the Web UI (Settings). They are stored encrypted in SQLite.
- Alternative: keep them in `config.yaml` (supports `${ENV_VAR}` interpolation) and provide env vars (or a `.env` file).

Env var examples (if you set `api_key: "${RADARR_API_KEY_1}"` / `api_key: "${SONARR_API_KEY_1}"` in `config.yaml`):

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
    volumes:
      - ./config:/config
      - ./data:/data
```

Notes:
- Put `config.yaml` at `./config/config.yaml` and set `app.db_path: "/data/seekarr.db"` so the DB persists.
- Persist `./data` (it contains `seekarr.db` and `seekarr.masterkey`).
- First load prompts you to set a Web UI password (stored as a salted hash in SQLite).
- Put keys/password in `./config/.env` only if you want to pre-seed them (optional):

```env
SEEKARR_WEBUI_PASSWORD=change-me
RADARR_API_KEY_1=your-radarr-key
SONARR_API_KEY_1=your-sonarr-key
```

---

## Security And Credentials (How It Works)

On first load of the Web UI, Seekarr prompts you to set a password. This password is stored as a salted PBKDF2 hash in the SQLite DB (it is not reversible and is never returned by the UI/API). If you forget it, there is no "forgot password" flow.

Seekarr stores Arr API keys you enter in the Web UI encrypted in the same SQLite DB. The encryption key is auto-generated on first run and stored in `seekarr.masterkey` next to the DB (for Docker, that lives in your `./data` volume). If you delete or lose `seekarr.masterkey`, Seekarr cannot decrypt the saved Arr API keys and you will need to re-enter them.

If you forget your Web UI password, reset it by deleting the stored password hash:
- Simple reset (wipes Seekarr state): stop Seekarr and delete `seekarr.db` in your data directory, then start Seekarr again.
- Advanced reset (keeps state): stop Seekarr, open `seekarr.db` with a SQLite tool, and delete the row from the `webui_auth` table.

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

Optional (console credential set):

```bat
python main.py --config config.yaml --set-api-key radarr 1
python main.py --config config.yaml --set-api-key sonarr 1
```

---

## Common Errors

- Connection refused/unreachable: the Arr URL/port is wrong, or Radarr/Sonarr is not reachable from where Seekarr runs.
- HTTP 401/403: API key is wrong or lacks permission.

---

## Technical Notes

- The Web UI binds to localhost by default. `--allow-public` is required to bind to non-localhost, to avoid accidentally exposing `/api/*` endpoints.
- The Web UI password is stored as a salted hash in the SQLite DB (it cannot be retrieved via the API/UI).
- API keys set in the Web UI are stored encrypted in the SQLite DB (they cannot be retrieved via the API/UI).
- API key encryption uses a master key file stored next to the DB: `seekarr.masterkey`. If you lose this file, you must re-enter API keys.
- `config.yaml` supports `${ENV_VAR}` interpolation.
