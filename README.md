# Seekarr

<p align="center">
  <img src="./seekarr/assets/seekarr-banner.svg" alt="Seekarr banner" width="100%"/>
</p>

<p align="center">
  <a href="https://github.com/tumeden/seekarr/actions/workflows/ci.yml"><img alt="CI" src="https://img.shields.io/github/actions/workflow/status/tumeden/seekarr/ci.yml?branch=main&style=flat&logo=github&label=CI"></a>
  <a href="https://github.com/tumeden/seekarr/releases"><img alt="Latest Release" src="https://img.shields.io/github/v/release/tumeden/seekarr?style=flat&logo=github&logoColor=white&label=Latest%20Release"></a>
  <img alt="Commits Since Release" src="https://img.shields.io/github/commits-since/tumeden/seekarr/latest?style=flat&logo=github&logoColor=white&label=Commits">
  <img alt="Commits Per Month" src="https://img.shields.io/github/commit-activity/m/tumeden/seekarr?style=flat&logo=github&logoColor=white&label=Commits%2FMonth">
  <img alt="Stars" src="https://img.shields.io/github/stars/tumeden/seekarr?style=flat&logo=github&logoColor=white&label=Stars">
  <a href="https://hub.docker.com/r/tumeden/seekarr"><img alt="Docker Pulls" src="https://img.shields.io/docker/pulls/tumeden/seekarr?style=flat&logo=docker&logoColor=white&label=Docker%20Pulls"></a>
  <a href="https://ko-fi.com/tumeden"><img alt="Donate" src="https://img.shields.io/badge/Donate-Ko--fi-ff5e5b?style=flat&logo=ko-fi&logoColor=white"></a>
  <img alt="License" src="https://img.shields.io/github/license/tumeden/seekarr?style=flat&label=License">
</p>

Seekarr automatically triggers Radarr/Sonarr searches for items already in your library (missing and/or cutoff-unmet), with scheduling, cooldowns, and rate limits.

<!-- screenshots -->
<img width="1129" height="515" alt="image" src="https://github.com/user-attachments/assets/f754e7dc-5bb7-4f13-9b42-3b3b6fb495ae" />
<img width="1474" height="529" alt="Seekarr dashboard" src="https://github.com/user-attachments/assets/85993bc0-6466-4349-9a82-df99ef89a818" />
<img width="1400" height="774" alt="image" src="https://github.com/user-attachments/assets/8da3fdd8-b83b-4bb2-9400-a58499e127bf" />

---

## What It Does

- Pulls wanted lists from Radarr/Sonarr (missing and/or cutoff-unmet).
- Triggers searches per instance on configured intervals.
- Tracks item cooldowns in SQLite to avoid repeated spam searches.
- Applies pacing and rate limits.
- Skips unreleased content until the configured delay passes.
- Supports quiet hours (with configurable timezone in Web UI).

---

## Docker Quick Start

```yaml
services:
  seekarr:
    image: tumeden/seekarr:latest
    container_name: seekarr
    restart: unless-stopped
    ports:
      - "8788:8788"
    volumes:
      - ./data:/data
```

Then:

1. Start container.
2. Open `http://localhost:8788`.
3. Set Web UI password.
4. Configure Radarr/Sonarr instances and settings in **Settings**.

---

## Persistence

Persist `./data`.

It contains:

- `config.yaml` (base startup config; auto-created if missing)
- `seekarr.db` (state + Web UI settings)
- `seekarr.masterkey` (key used to decrypt stored Arr API keys)

Web UI setting changes are stored in `seekarr.db`.

---

## Security

- Web UI requires a password (stored as salted hash in SQLite).
- Arr API keys entered in Web UI are stored encrypted in SQLite.
- Do not lose `seekarr.masterkey`, or stored API keys cannot be decrypted.

---

## Common Errors

- Connection refused/unreachable: Arr URL/port is wrong or unreachable from container.
- HTTP 401/403: Arr API key is invalid.

---

Looking to clean up stuck/failed downloads? Check out https://github.com/ManiMatter/decluttarr
