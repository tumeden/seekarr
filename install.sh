#!/usr/bin/env bash
set -euo pipefail

MODE="${MODE:-console}" # console|webui
INSTALL_DIR="${INSTALL_DIR:-/opt/seekarr}"
CONFIG_DIR="${CONFIG_DIR:-/etc/seekarr}"
DATA_DIR="${DATA_DIR:-/var/lib/seekarr}"
SERVICE_USER="${SERVICE_USER:-}"
WEBUI_HOST="${WEBUI_HOST:-127.0.0.1}"
WEBUI_PORT="${WEBUI_PORT:-8788}"

usage() {
  cat <<EOF
Usage: sudo ./install.sh [--mode console|webui] [--user USER] [--install-dir DIR] [--config-dir DIR] [--data-dir DIR]

Installs Seekarr as a systemd service.

Defaults:
  --mode         console
  --install-dir  /opt/seekarr
  --config-dir   /etc/seekarr
  --data-dir     /var/lib/seekarr

Web UI options (mode webui):
  --webui-host   127.0.0.1
  --webui-port   8788
EOF
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --mode) MODE="$2"; shift 2;;
    --user) SERVICE_USER="$2"; shift 2;;
    --install-dir) INSTALL_DIR="$2"; shift 2;;
    --config-dir) CONFIG_DIR="$2"; shift 2;;
    --data-dir) DATA_DIR="$2"; shift 2;;
    --webui-host) WEBUI_HOST="$2"; shift 2;;
    --webui-port) WEBUI_PORT="$2"; shift 2;;
    -h|--help) usage; exit 0;;
    *) echo "Unknown arg: $1"; usage; exit 2;;
  esac
done

if [[ "$(id -u)" -ne 0 ]]; then
  echo "This installer must run as root (use sudo)."
  exit 1
fi

if [[ -z "${SERVICE_USER}" ]]; then
  if [[ -n "${SUDO_USER:-}" && "${SUDO_USER}" != "root" ]]; then
    SERVICE_USER="${SUDO_USER}"
  else
    echo "Pick a non-root user for the service: --user <name>"
    exit 1
  fi
fi

SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

echo "Installing from: ${SRC_DIR}"
echo "Install dir:     ${INSTALL_DIR}"
echo "Config dir:      ${CONFIG_DIR}"
echo "Data dir:        ${DATA_DIR}"
echo "Service user:    ${SERVICE_USER}"
echo "Mode:            ${MODE}"

command -v systemctl >/dev/null 2>&1 || { echo "systemctl not found (need systemd)."; exit 1; }
command -v python3 >/dev/null 2>&1 || { echo "python3 not found."; exit 1; }

install -d -m 0755 "${INSTALL_DIR}"
install -d -m 0755 "${CONFIG_DIR}"
install -d -m 0755 "${DATA_DIR}"
chown -R "${SERVICE_USER}:${SERVICE_USER}" "${DATA_DIR}"

echo "Copying app to ${INSTALL_DIR}..."
rm -rf "${INSTALL_DIR}/seekarr" "${INSTALL_DIR}/main.py" "${INSTALL_DIR}/webui_main.py" "${INSTALL_DIR}/requirements.txt" "${INSTALL_DIR}/config.example.yaml" || true
cp -a "${SRC_DIR}/seekarr" "${INSTALL_DIR}/seekarr"
cp -a "${SRC_DIR}/main.py" "${INSTALL_DIR}/main.py"
cp -a "${SRC_DIR}/webui_main.py" "${INSTALL_DIR}/webui_main.py"
cp -a "${SRC_DIR}/requirements.txt" "${INSTALL_DIR}/requirements.txt"
cp -a "${SRC_DIR}/config.example.yaml" "${INSTALL_DIR}/config.example.yaml"

echo "Creating venv + installing requirements..."
python3 -m venv "${INSTALL_DIR}/venv"
"${INSTALL_DIR}/venv/bin/python" -m pip install --upgrade pip >/dev/null
"${INSTALL_DIR}/venv/bin/pip" install -r "${INSTALL_DIR}/requirements.txt" >/dev/null

if [[ ! -f "${CONFIG_DIR}/config.yaml" ]]; then
  echo "Creating ${CONFIG_DIR}/config.yaml from config.example.yaml..."
  cp -a "${SRC_DIR}/config.example.yaml" "${CONFIG_DIR}/config.yaml"
  # Make the DB path service-friendly by default.
  sed -i "s#^  db_path:.*#  db_path: \"${DATA_DIR}/seekarr.db\"#g" "${CONFIG_DIR}/config.yaml" || true
fi

if [[ ! -f "${CONFIG_DIR}/seekarr.env" ]]; then
  cat >"${CONFIG_DIR}/seekarr.env" <<EOF
# Secrets for Seekarr (systemd EnvironmentFile)
# Example:
# RADARR_API_KEY_1=...
# SONARR_API_KEY_1=...
EOF
  chmod 0640 "${CONFIG_DIR}/seekarr.env"
  chown root:"${SERVICE_USER}" "${CONFIG_DIR}/seekarr.env"
fi

echo "Installing systemd unit(s)..."
UNITS=()

if [[ "${MODE}" == "console" || "${MODE}" == "worker" ]]; then
  install -m 0644 "${SRC_DIR}/systemd/seekarr-console.service" /etc/systemd/system/seekarr-console.service
  UNITS+=("/etc/systemd/system/seekarr-console.service")
elif [[ "${MODE}" == "webui" ]]; then
  # Patch host/port in the unit at install time.
  tmp="$(mktemp)"
  sed -e "s/--host 127.0.0.1/--host ${WEBUI_HOST}/" -e "s/--port 8788/--port ${WEBUI_PORT}/" \
    "${SRC_DIR}/systemd/seekarr-webui.service" >"${tmp}"
  install -m 0644 "${tmp}" /etc/systemd/system/seekarr-webui.service
  rm -f "${tmp}"
  UNITS+=("/etc/systemd/system/seekarr-webui.service")
else
  echo "Invalid --mode: ${MODE} (expected console|webui)"
  exit 2
fi

# Patch the service user and data dir into units.
for unit in "${UNITS[@]}"; do
  [[ -f "${unit}" ]] || continue
  if ! grep -q '^User=' "${unit}"; then
    # Add after Type=
    sed -i "s/^Type=simple$/Type=simple\\nUser=${SERVICE_USER}\\nGroup=${SERVICE_USER}/" "${unit}"
  else
    sed -i "s/^User=.*/User=${SERVICE_USER}/" "${unit}"
    sed -i "s/^Group=.*/Group=${SERVICE_USER}/" "${unit}"
  fi
  sed -i "s#ReadWritePaths=/var/lib/seekarr#ReadWritePaths=${DATA_DIR}#g" "${unit}"
done

systemctl daemon-reload

if [[ "${MODE}" == "console" || "${MODE}" == "worker" ]]; then
  systemctl enable --now seekarr-console.service
fi
if [[ "${MODE}" == "webui" ]]; then
  systemctl enable --now seekarr-webui.service
fi

echo
echo "Installed."
echo "Config: ${CONFIG_DIR}/config.yaml"
echo "Env:    ${CONFIG_DIR}/seekarr.env"
echo "Data:   ${DATA_DIR}"
echo
echo "Useful commands:"
if [[ "${MODE}" == "console" || "${MODE}" == "worker" ]]; then
  echo "  systemctl status seekarr-console"
  echo "  journalctl -u seekarr-console -f"
fi
if [[ "${MODE}" == "webui" ]]; then
  echo "  systemctl status seekarr-webui"
  echo "  journalctl -u seekarr-webui -f"
  echo "  Web UI: http://${WEBUI_HOST}:${WEBUI_PORT}"
fi

echo
echo "Note:"
echo "  Your DB path is set in ${CONFIG_DIR}/config.yaml (app.db_path)."
echo "  This installer defaults it to: ${DATA_DIR}/seekarr.db"
