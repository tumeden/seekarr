# Seekarr

<p align="center">
  <img src="./seekarr/assets/seekarr-banner.svg" alt="Seekarr banner" width="100%"/>
</p>

<p align="center">
  <a href="https://github.com/tumeden/seekarr/actions/workflows/quality.yml"><img alt="Quality" src="https://img.shields.io/github/actions/workflow/status/tumeden/seekarr/quality.yml?branch=main&style=flat&logo=github&label=Quality"></a>
  <a href="https://github.com/tumeden/seekarr/actions/workflows/security.yml"><img alt="Security" src="https://img.shields.io/github/actions/workflow/status/tumeden/seekarr/security.yml?branch=main&style=flat&logo=github&label=Security"></a>
  <a href="https://github.com/tumeden/seekarr/actions/workflows/docker.yml"><img alt="Docker" src="https://img.shields.io/github/actions/workflow/status/tumeden/seekarr/docker.yml?branch=main&style=flat&logo=github&label=Docker"></a>
  <a href="https://github.com/tumeden/seekarr/actions/workflows/workflow-lint.yml"><img alt="Workflow Lint" src="https://img.shields.io/github/actions/workflow/status/tumeden/seekarr/workflow-lint.yml?branch=main&style=flat&logo=github&label=Workflow%20Lint"></a>
  <a href="https://github.com/tumeden/seekarr/actions/workflows/dockerfile-lint.yml"><img alt="Dockerfile Lint" src="https://img.shields.io/github/actions/workflow/status/tumeden/seekarr/dockerfile-lint.yml?branch=main&style=flat&logo=github&label=Dockerfile%20Lint"></a>
  <a href="https://github.com/tumeden/seekarr/releases"><img alt="Latest Release" src="https://img.shields.io/github/v/release/tumeden/seekarr?style=flat&logo=github&logoColor=white&label=Latest%20Release"></a>
  <img alt="Commits Since Last Release" src="https://img.shields.io/github/commits-since/tumeden/seekarr/latest?style=flat&logo=github&logoColor=white&label=Commits%20Since%20Last%20Release">
  <img alt="Commits Per Month" src="https://img.shields.io/github/commit-activity/m/tumeden/seekarr?style=flat&logo=github&logoColor=white&label=Commits%2FMonth">
  <img alt="Stars" src="https://img.shields.io/github/stars/tumeden/seekarr?style=flat&logo=github&logoColor=white&label=Stars">
  <a href="https://hub.docker.com/r/tumeden/seekarr"><img alt="Docker Pulls" src="https://img.shields.io/docker/pulls/tumeden/seekarr?style=flat&logo=docker&logoColor=white&label=Docker%20Pulls"></a>
  <a href="https://github.com/tumeden/seekarr/pkgs/container/seekarr"><img alt="GHCR" src="https://img.shields.io/badge/GHCR-ghcr.io-blue?style=flat&logo=github"></a>
  <a href="https://ko-fi.com/tumeden"><img alt="Donate" src="https://img.shields.io/badge/Donate-Ko--fi-ff5e5b?style=flat&logo=ko-fi&logoColor=white"></a>
  <img alt="License" src="https://img.shields.io/github/license/tumeden/seekarr?style=flat&label=License">
</p>

Seekarr keeps your Radarr and Sonarr stack actively searching for missing media and better releases over time, so you do not have to keep manually triggering searches yourself.

Seekarr is configured from the Web UI. Arr instance URLs, API keys, schedules, and search behavior are stored in SQLite.

<!-- screenshots -->
<img width="1095" height="708" alt="image" src="https://github.com/user-attachments/assets/79c854f6-c56b-47c1-9737-fcf4ec551ac9" />
<img width="1446" height="897" alt="image" src="https://github.com/user-attachments/assets/1bcfe1b1-bba1-4e4c-a889-2d615d2749a7" />
<img width="1400" height="774" alt="image" src="https://github.com/user-attachments/assets/8da3fdd8-b83b-4bb2-9400-a58499e127bf" />

---

## What It Does

- Re-runs searches for monitored movies and episodes that are still missing.
- Keeps checking for better releases when you want ongoing upgrades, not just first-time grabs.
- Lets you set different schedules and behavior for each Radarr or Sonarr instance.
- Helps avoid wasteful searches with release delays, quiet hours, cooldowns, and rate limits.

Upgrade source modes:

- `Wanted List Only`: only search items Arr is currently asking to upgrade.
- `Monitored Items Only`: search monitored items that already have files so Seekarr can keep looking for better releases.
- `Both`: combine Arr's current upgrade list with monitored items that already have files.

---


## Docker Quick Start

### Docker Images

The image is available on both Docker Hub and GitHub Container Registry:

- **Docker Hub**: `tumeden/seekarr:latest`
- **GHCR**: `ghcr.io/tumeden/seekarr:latest`

### Docker Compose

```yaml
services:
  seekarr:
    image: tumeden/seekarr:latest
    container_name: seekarr
    restart: unless-stopped
    ports:
      - "8788:8788"
    # environment:
    #   - PUID=1000
    #   - PGID=1000
    volumes:
      - ./data:/data
```

Then:

1. Start container.
2. Open `http://localhost:8788`
3. Set Web UI password.
4. Configure Radarr/Sonarr instances and settings in **Settings**. You can add or remove multiple instances for either app from the UI.

By default the container stores data in `/data/seekarr.db`.

### Updating

To update Seekarr to the latest version:

```bash
docker compose pull
docker compose up -d
```

---

## Persistence

Persist `./data`.

It contains:

- `seekarr.db` (state + Web UI settings)
- `seekarr.masterkey` (key used to decrypt stored Arr API keys)

Web UI setting changes are stored in `seekarr.db`.

If you need to fully reset Seekarr's configured instances and UI settings, stop the app and delete `seekarr.db`. Keep `seekarr.masterkey` unless you intentionally want to discard access to stored API keys as well.

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

## Related Projects

- Need help cleaning up stuck or failed downloads? [Decluttarr](https://github.com/ManiMatter/decluttarr)
- Looking for library maintenance automation for Plex/Jellyfin? [Maintainerr](https://github.com/Maintainerr/Maintainerr)
