#!/bin/sh
# Fly volumes mount root-owned over the image's /data, so ownership must be
# fixed at runtime; then drop privileges — the app itself never runs as root.
set -e
if [ "$(id -u)" = "0" ]; then
    mkdir -p "${DATA_DIR:-/data}"
    chown -R appuser:appuser "${DATA_DIR:-/data}"
    exec runuser -u appuser -- "$@"
fi
exec "$@"
