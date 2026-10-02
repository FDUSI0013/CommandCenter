#!/usr/bin/env bash
# FD AI Command Center — prepare a fresh Ubuntu host to run the stack.
#
# Installs Docker, the edge proxy and the AWS CLI, sets the kernel limits the
# analytics store needs, installs the host's cron jobs and log rotation, and
# leaves the host ready for `docker compose up -d`. It does not start
# anything: configuration comes next, and a half-configured public service is
# worse than one that is not running.
#
# Run as root on a fresh Ubuntu 24.04 host:
#   sudo bash deploy/host-bootstrap.sh
#
# Idempotent: safe to re-run after a partial failure.

set -euo pipefail

[ "$(id -u)" -eq 0 ] || { echo "error: run as root" >&2; exit 1; }

echo "== apt =="
# This VPC has no working IPv6 route, and apt prefers it: without this, every
# apt-get stalls for minutes on unreachable mirrors before falling back.
cat > /etc/apt/apt.conf.d/99force-ipv4 <<'CONF'
Acquire::ForceIPv4 "true";
CONF

export DEBIAN_FRONTEND=noninteractive
apt-get update -qq
apt-get install -y -qq ca-certificates curl gnupg unzip jq

echo
echo "== docker =="
if command -v docker >/dev/null 2>&1; then
  echo "already installed: $(docker --version)"
else
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
  chmod a+r /etc/apt/keyrings/docker.asc
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] \
https://download.docker.com/linux/ubuntu $(. /etc/os-release && echo "$VERSION_CODENAME") stable" \
    > /etc/apt/sources.list.d/docker.list
  apt-get update -qq
  apt-get install -y -qq docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin
  systemctl enable --now docker
  echo "installed: $(docker --version)"
fi

# Container logs are the third-largest disk consumer after the two databases.
cat > /etc/docker/daemon.json <<'JSON'
{
  "log-driver": "json-file",
  "log-opts": { "max-size": "20m", "max-file": "5" },
  "live-restore": true
}
JSON
systemctl restart docker

echo
echo "== kernel limits =="
# The analytics store opens a very large number of files; the default 1024 is
# nowhere near enough and it fails in ways that look like corruption.
cat > /etc/sysctl.d/99-fulcrum.conf <<'CONF'
fs.file-max = 2097152
vm.max_map_count = 262144
# The analytics store asks for this; leaving it at 0 costs throughput under load.
vm.overcommit_memory = 1
net.core.somaxconn = 4096
CONF
sysctl -p /etc/sysctl.d/99-fulcrum.conf >/dev/null

cat > /etc/security/limits.d/99-fulcrum.conf <<'CONF'
*  soft  nofile  262144
*  hard  nofile  262144
CONF

echo
echo "== edge proxy =="
if systemctl is-enabled caddy >/dev/null 2>&1; then
  echo "already installed: $(caddy version 2>/dev/null || echo present)"
else
  apt-get install -y -qq debian-keyring debian-archive-keyring apt-transport-https
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' \
    | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
    > /etc/apt/sources.list.d/caddy-stable.list
  apt-get update -qq
  apt-get install -y -qq caddy
fi

install -d -o caddy -g caddy /var/log/caddy
install -d /srv/fulcrum-ops

echo
echo "== aws cli =="
# The nightly backup mirrors the governance database to S3 with it, and refuses
# to call a night a success without it. Ubuntu 24.04 has no awscli package; this
# is AWS's own v2 installer, which lands in /usr/local/bin — a directory cron's
# default PATH does not include, which is why deploy/cron.d/fulcrum-ops sets one.
if command -v aws >/dev/null 2>&1; then
  echo "already installed: $(aws --version 2>&1)"
else
  awstmp="$(mktemp -d)"
  curl -fsSL "https://awscli.amazonaws.com/awscli-exe-linux-$(uname -m).zip" -o "$awstmp/awscliv2.zip"
  unzip -q "$awstmp/awscliv2.zip" -d "$awstmp"
  "$awstmp/aws/install" --update >/dev/null
  rm -rf "$awstmp"
  echo "installed: $(aws --version 2>&1)"
fi

echo
echo "== host jobs =="
# autoheal (every minute), the nightly backup, the weekly build-cache prune, and
# rotation for their logs. The jobs call scripts under /opt/fulcrum/deploy, so
# they are only installed once the repository is actually there — a cron line
# pointing at nothing would write an error a minute until it was.
HOST_JOBS_INSTALLED=0
if [ -f /opt/fulcrum/deploy/autoheal.sh ] && [ -f /opt/fulcrum/deploy/backup.sh ]; then
  install -m 0644 -o root -g root /opt/fulcrum/deploy/cron.d/fulcrum-ops /etc/cron.d/fulcrum-ops
  install -m 0644 -o root -g root /opt/fulcrum/deploy/logrotate.d/fulcrum-ops /etc/logrotate.d/fulcrum-ops
  # The earlier, backup-only cron file: left in place it would run the dump twice.
  rm -f /etc/cron.d/fulcrum-backup
  HOST_JOBS_INSTALLED=1
  echo "installed /etc/cron.d/fulcrum-ops and /etc/logrotate.d/fulcrum-ops"
else
  echo "skipped: /opt/fulcrum/deploy is not in place yet (re-run this script once it is)"
fi

echo
echo "== swap =="
# A small swap file is insurance, not capacity: it turns a brief allocation
# spike during a compaction into slowness instead of an OOM kill.
if ! swapon --show | grep -q /swapfile; then
  fallocate -l 4G /swapfile
  chmod 600 /swapfile
  mkswap /swapfile >/dev/null
  swapon /swapfile
  grep -q '^/swapfile' /etc/fstab || echo '/swapfile none swap sw 0 0' >> /etc/fstab
  sysctl -w vm.swappiness=10 >/dev/null
  echo 'vm.swappiness = 10' > /etc/sysctl.d/99-swappiness.conf
fi

echo
echo "host ready:"
echo "  $(docker --version)"
echo "  $(docker compose version)"
echo "  memory  $(free -g | awk '/^Mem:/ {print $2}') GB"
echo "  disk    $(df -h / | awk 'NR==2 {print $4}') free"
echo
echo "next:"
echo "  1. copy the repository and deploy/.env to this host"
echo "  2. engine/fetch-vendor.sh <archive> && engine/build.sh"
echo "  3. cd deploy && docker compose up -d"
echo "  4. install deploy/Caddyfile to /etc/caddy/Caddyfile and reload caddy"
if [ "$HOST_JOBS_INSTALLED" -eq 0 ]; then
  echo "  5. re-run this script with the repository at /opt/fulcrum, to install the"
  echo "     host jobs (autoheal, nightly backup, log rotation) — see deploy/README.md"
fi
