#!/bin/sh
# Fly volumes mount root-owned over the image's /data, so ownership must be
# fixed at runtime; then drop privileges — the app itself never runs as root.
set -e
if [ "$(id -u)" = "0" ]; then
    data_dir="${DATA_DIR:-/data}"
    # The image runs as root only to prepare the mounted Fly volume. Refuse a
    # typo such as DATA_DIR=/ (or a traversal/symlink escaping /data) before
    # recursive ownership changes can damage the container filesystem.
    case "$data_dir" in
        /data|/data/*) ;;
        *) echo "Refusing unsafe DATA_DIR: $data_dir" >&2; exit 64 ;;
    esac
    mkdir -p -- "$data_dir"
    data_dir="$(realpath -m -- "$data_dir")"
    case "$data_dir" in
        /data|/data/*) ;;
        *) echo "Refusing DATA_DIR outside /data: $data_dir" >&2; exit 64 ;;
    esac
    ownership_marker="$data_dir/.ownership-appuser-10001-v1"
    # The runtime user owns DATA_DIR and can replace entries between restarts.
    # Never follow a forged marker symlink (or block on a FIFO/device) while
    # this process still has root privileges.
    if [ -L "$ownership_marker" ] || { [ -e "$ownership_marker" ] && [ ! -f "$ownership_marker" ]; }; then
        echo "Refusing unsafe ownership marker: $ownership_marker" >&2
        exit 64
    fi
    if [ ! -f "$ownership_marker" ]; then
        # First mount (or an intentional UID/version change): repair existing
        # content once, then record it. Normal restarts avoid traversing a
        # near-gigabyte volume and only verify the two top-level inodes.
        chown -R --one-file-system appuser:appuser "$data_dir"
        : > "$ownership_marker"
    fi
    # Keep the marker root-owned and never pass its path to chown. The app
    # needs DATA_DIR itself, not the marker, to be writable.
    chown appuser:appuser "$data_dir"
    exec runuser -u appuser -- "$@"
fi
exec "$@"
