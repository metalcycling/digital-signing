#!/bin/sh
set -eu
umask 077
# Production environment is a root-owned, service-readable shell-compatible file.
set -a
. /etc/bello-signing.env
set +a
cd /opt/bello-signing
exec .venv/bin/python -m digital_signing.backups run
