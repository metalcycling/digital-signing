#!/bin/sh
# Run as the application's service user, using the same .env and runtime directory.
set -eu
umask 077
APP_DIR=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
cd "$APP_DIR"
exec "$APP_DIR/.venv/bin/python" -m digital_signing.backups run
