#!/usr/bin/env bash
# Первичная настройка VPS (Ubuntu 22.04/24.04). Запуск: sudo ./scripts/setup-server.sh
set -euo pipefail

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"
SWAP_SIZE="${SWAP_SIZE:-4G}"

if grep -qw avx2 /proc/cpuinfo; then
  echo "AVX2: есть"
else
  echo "AVX2: нет. В .env поставьте AUDIOMUSE_VERSION=<версия>-noavx2"
fi

apt-get update
apt-get install -y ca-certificates curl fuse3 ufw unzip

if ! command -v docker >/dev/null; then
  curl -fsSL https://get.docker.com | sh
fi

if ! command -v rclone >/dev/null; then
  curl -fsSL https://rclone.org/install.sh | bash
fi

# Пользователь, от имени которого работает rclone и контейнеры (PUID/PGID в .env)
id music >/dev/null 2>&1 || useradd -m -s /bin/bash music
usermod -aG docker music
MUSIC_UID="$(id -u music)"
MUSIC_GID="$(id -g music)"

grep -q '^user_allow_other' /etc/fuse.conf || echo user_allow_other >> /etc/fuse.conf

mkdir -p /mnt/music /srv/rclone-cache \
  /srv/music-buffer/slskd/{downloads,incomplete} /srv/music-buffer/shared
chown -R music:music /mnt/music /srv/rclone-cache /srv/music-buffer

if ! swapon --show | grep -q /swapfile; then
  fallocate -l "$SWAP_SIZE" /swapfile
  chmod 600 /swapfile
  mkswap /swapfile
  swapon /swapfile
  echo '/swapfile none swap sw 0 0' >> /etc/fstab
fi
sysctl -w vm.swappiness=10
echo 'vm.swappiness=10' > /etc/sysctl.d/99-swappiness.conf

sed -e "s/__UID__/$MUSIC_UID/; s/__GID__/$MUSIC_GID/" \
  "$REPO_DIR/rclone/rclone-music.service" > /etc/systemd/system/rclone-music.service
systemctl daemon-reload

ufw allow OpenSSH
ufw allow 80/tcp
ufw allow "${HTTPS_PORT:-443}/tcp"
ufw allow "${SLSK_LISTEN_PORT:-50300}/tcp"
ufw --force enable

cat <<EOF

Пользователь music: UID=$MUSIC_UID GID=$MUSIC_GID (впишите в .env как PUID/PGID)

Готово. Дальше:
  1. sudo -u music rclone config        # remote "gdrive" (drive) и "gcrypt" (crypt поверх gdrive:)
  2. sudo systemctl enable --now rclone-music
  3. cp .env.example .env && nano .env
  4. ./scripts/install-plugins.sh
  5. docker compose up -d
EOF
