#!/usr/bin/env bash
# Скачивает плагины Navidrome версий из .env в data/navidrome/plugins
# и создаёт папки данных сервисов с владельцем PUID:PGID.
set -euo pipefail

cd "$(dirname "$0")/.."
set -a; . ./.env; set +a

dir=data/navidrome/plugins
mkdir -p "$dir" data/slskd data/explo

curl -fL -o "$dir/audiomuseai.ndp" \
  "https://github.com/NeptuneHub/AudioMuse-AI-NV-plugin/releases/download/${AUDIOMUSE_PLUGIN_VERSION}/audiomuseai.ndp"
curl -fL -o "$dir/nd-lyrics.ndp" \
  "https://github.com/J0R6IT0/navidrome-lyrics-plugin/releases/download/${LYRICS_PLUGIN_VERSION}/nd-lyrics.ndp"

chown -R "${PUID}:${PGID}" data 2>/dev/null || true
ls -l "$dir"
