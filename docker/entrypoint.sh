#!/bin/bash
set -e

# Support PUID/PGID for volume permissions
PUID=${PUID:-10001}
PGID=${PGID:-10001}

echo "Starting Seekarr with PUID=$PUID and PGID=$PGID"

# Update appuser UID and GID
if [ "$(id -u appuser)" != "$PUID" ]; then
    usermod -o -u "$PUID" appuser
fi
if [ "$(id -g appuser)" != "$PGID" ]; then
    groupmod -o -g "$PGID" appuser
fi

# Ensure /data is owned correctly
if [ -d "/data" ]; then
    # Only chown if ownership is currently different to avoid unnecessary overhead
    CURRENT_UID=$(stat -c '%u' /data)
    CURRENT_GID=$(stat -c '%g' /data)
    if [ "$CURRENT_UID" != "$PUID" ] || [ "$CURRENT_GID" != "$PGID" ]; then
        echo "Updating ownership of /data to $PUID:$PGID..."
        chown -R appuser:appuser /data
    fi
fi

# Execute the application as the appuser
exec gosu appuser "$@"
