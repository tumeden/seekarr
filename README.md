# Seekarr

Seekarr automatically triggers Radarr/Sonarr searches for items already in your library (missing and/or cutoff-unmet), on a schedule, with cooldown + rate limits to avoid API spam.

This project exists as a minimal replacement for Huntarr’s auto-search behavior after the original repository was removed.

Scope:
- Focused strictly on automatic searching via Sonarr/Radarr.
- No unrelated “arr suite” features (download cleanup, etc).

Transparency:
- Built with significant AI assistance. Review and use at your own discretion.

<!-- screenshots -->
<img width="1474" height="529" alt="Seekarr dashboard" src="https://github.com/user-attachments/assets/85993bc0-6466-4349-9a82-df99ef89a818" />
<img width="1416" height="689" alt="Seekarr settings" src="https://github.com/user-attachments/assets/7f9a56f9-62b5-4c3d-ab2e-16931f9a9069" />



## What It Does

- Pulls “wanted” lists from Radarr/Sonarr (missing and/or cutoff-unmet).
- Triggers searches on a schedule per instance.
- Remembers what it already searched (SQLite cooldown) so it won’t retry the same item constantly.
- Rate-limits and paces search requests so large libraries do not cause bursts.
- Skips unreleased content (default: wait 8 hours after air/release).
- Can pause searching during quiet hours (default: 23:00 to 06:00 local time).
 
## How It Works (In Plain English)

Each configured instance (one Radarr, one Sonarr, or many of each) runs on its own interval:

- When it is “due”, Seekarr fetches that Arr’s wanted list.
- It picks a small number of items (caps per run).
- It triggers searches one-by-one with a short delay between them.
- It records those actions in `seekarr.db` so the same item is not retried until your cooldown passes.

## Quick Start

1. Create your config:

```bash
cp config.example.yaml config.yaml
```

2. Edit `config.yaml` and set your Arr URLs:

- If Seekarr runs on a different machine than Radarr/Sonarr, do not use `localhost`.
- Use the IP/hostname Seekarr can reach (example: `http://192.168.1.50:7878`).

3. Put API keys in `.env` (same folder as `config.yaml`):

```env
RADARR_API_KEY_1=your-radarr-key
SONARR_API_KEY_1=your-sonarr-key
```

4. Install dependencies:

```bash
python -m pip install -r requirements.txt
```

5. Run it (pick one):

Console (worker):

```bash
python main.py --config config.yaml
```

Web UI:

```bash
python webui_main.py --config config.yaml
```

Windows helpers:
- `run-console.bat` (worker)
- `run-webui.bat` (Web UI)

The Web UI uses `waitress` (a production WSGI server) by default.

Optional: immediate run at startup (ignores due time):

```bash
python main.py --config config.yaml --force
```

## Web UI

Start the local UI:

```bash
python webui_main.py --config config.yaml
```

Windows helper:
- `run-webui.bat`

Security note:
- The Web UI refuses to bind to non-localhost hosts unless you pass `--allow-public` (or set `SEEKARR_ALLOW_PUBLIC_WEBUI=1`), to prevent accidentally exposing the `/api/*` endpoints.

If you run the worker as a service, you usually want the Web UI to be monitor-only:
- turn Auto-run off in the UI, or set `WEBUI_AUTORUN_DEFAULT=0` for the Web UI process

## Common Errors

- “Cannot connect (connection refused/unreachable)”: your Arr URL/port is wrong, or the service is not running/reachable.
- “HTTP 401/403”: API key is wrong or lacks permission.

## Technical Reference

### Files

- `config.yaml`: your config (gitignored)
- `.env`: secrets (gitignored)
- `state/seekarr.db`: default SQLite state DB

### Config Keys

App defaults (`app:`):
- `db_path` (default `./state/seekarr.db`)
- `request_timeout_seconds`, `verify_ssl`, `log_level`
- Defaults used when instances do not override: `item_retry_hours`, `min_hours_after_release`, `quiet_hours_start`, `quiet_hours_end`, `min_seconds_between_actions`, `rate_window_minutes`, `rate_cap_per_instance`, `max_missing_actions_per_instance_per_sync`, `max_cutoff_actions_per_instance_per_sync`

Per instance (`radarr.instances[]` / `sonarr.instances[]`):
- `enabled`
- `interval_minutes` (clamped to 15-60 minutes)
- `search_missing`, `search_cutoff_unmet`
- `search_order`: `smart|newest|random|oldest`
- `item_retry_hours`
- `rate_window_minutes`, `rate_cap`
- `min_seconds_between_actions`
- `min_hours_after_release`
- `quiet_hours_start`, `quiet_hours_end`
- `max_missing_actions_per_instance_per_sync`, `max_cutoff_actions_per_instance_per_sync`
- Sonarr-only: `sonarr_missing_mode`: `season_packs|shows|episodes`

Notes:
- `${ENV_VAR}` interpolation is supported in YAML values.
- For extra debugging output, set `SEEKARR_DEBUG=1`.

### Linux (systemd)

Installer script:

```bash
sudo ./install.sh --mode console --user youruser
# OR (standalone Web UI that also runs searches):
sudo ./install.sh --mode webui --user youruser --webui-host 127.0.0.1 --webui-port 8788
```

Logs:

```bash
journalctl -u seekarr-console -f
journalctl -u seekarr-webui -f
```

Note:
- Pick one service mode (console worker or Web UI). They are intended to be run separately.

### Docker

Run Web UI (default):

```bash
docker compose up -d --build
```

Optional: run console worker instead:

```bash
docker compose --profile console up -d --build
```

### Disclaimer

Use this only with indexers and content sources you are authorized to access.
