#!/bin/bash
# Run as root on a fresh Ubuntu 24.04 server. No app credentials belong here.
set -euo pipefail
export DEBIAN_FRONTEND=noninteractive

apt-get update -qq
apt-get upgrade -y -qq
apt-get install -y -qq python3.12 python3.12-venv curl ca-certificates gnupg \
    debian-keyring debian-archive-keyring apt-transport-https cron logrotate \
    unattended-upgrades

# Official Caddy stable repository, with its own signing key.
curl -fsSL https://dl.cloudsmith.io/public/caddy/stable/gpg.key \
    | gpg --batch --yes --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
curl -fsSL https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt \
    -o /etc/apt/sources.list.d/caddy-stable.list
chmod 644 /usr/share/keyrings/caddy-stable-archive-keyring.gpg /etc/apt/sources.list.d/caddy-stable.list
apt-get update -qq
apt-get install -y -qq caddy
systemctl stop caddy

python3.12 -m venv /opt/uv-tools
/opt/uv-tools/bin/pip install --disable-pip-version-check uv
ln -sfn /opt/uv-tools/bin/uv /usr/local/bin/uv

id bello >/dev/null 2>&1 || useradd --system --home-dir /var/lib/bello-signing --shell /usr/sbin/nologin bello
install -d -o root -g root -m 755 /opt/bello-signing
install -d -o bello -g bello -m 700 /var/lib/bello-signing
timedatectl set-timezone America/New_York

cat > /etc/apt/apt.conf.d/20auto-upgrades <<'EOF'
APT::Periodic::Update-Package-Lists "1";
APT::Periodic::Unattended-Upgrade "1";
EOF
cat > /etc/apt/apt.conf.d/52bello-unattended-upgrades <<'EOF'
Unattended-Upgrade::Automatic-Reboot "false";
EOF
systemctl enable --now cron
echo 'Ubuntu bootstrap finished; app and backup schedule are not started yet.'
