#!/usr/bin/env bash
# Скачивает плагины Navidrome версий из .env в data/navidrome/plugins
# и создаёт папки данных сервисов с владельцем PUID:PGID.
set -euo pipefail

cd "$(dirname "$0")/.."
set -a; . ./.env; set +a

dir=data/navidrome/plugins
mkdir -p "$dir" data/slskd data/explo data/lidarr data/soularr
[ -f data/soularr/config.ini ] || cp soularr/config.ini.example data/soularr/config.ini

curl -fL -o "$dir/audiomuseai.ndp" \
  "https://github.com/NeptuneHub/AudioMuse-AI-NV-plugin/releases/download/${AUDIOMUSE_PLUGIN_VERSION}/audiomuseai.ndp"
curl -fL -o "$dir/nd-lyrics.ndp" \
  "https://github.com/J0R6IT0/navidrome-lyrics-plugin/releases/download/${LYRICS_PLUGIN_VERSION}/nd-lyrics.ndp"

chown -R "${PUID}:${PGID}" data 2>/dev/null || true

# Умные плейлисты кладём прямо на Диск: Navidrome ищет их в MUSIC_DIR/_playlists
if mountpoint -q "$MUSIC_DIR"; then
  mkdir -p "$MUSIC_DIR/_playlists" "$MUSIC_DIR/lidarr" "$MUSIC_DIR/soulseek/failed_imports"
  # сюда Soularr складывает альбомы, которые Lidarr не принял: в Navidrome это дубли
  touch "$MUSIC_DIR/soulseek/failed_imports/.ndignore"
  cp navidrome/playlists/*.nsp "$MUSIC_DIR/_playlists/"
else
  echo "$MUSIC_DIR не смонтирован: плейлисты не скопированы, запустите скрипт после rclone" >&2
fi
ls -l "$dir"
