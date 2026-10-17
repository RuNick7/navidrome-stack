#!/usr/bin/env bash
# Перекодирует всю музыку в MUSIC_DIR в Opus и удаляет оригиналы.
# Берёт только файлы старше MIN_AGE_MINUTES, чтобы не трогать то, что ещё качается или переносится.
set -uo pipefail

ROOT="${MUSIC_DIR:?MUSIC_DIR не задан}"
BITRATE="${OPUS_BITRATE:-192k}"
MIN_AGE="${MIN_AGE_MINUTES:-30}"
INTERVAL="${INTERVAL_SECONDS:-900}"
TMP="${TMPDIR:-/tmp}/opus"
mkdir -p "$TMP"

duration() {
  ffprobe -v error -show_entries format=duration -of csv=p=0 "$1" 2>/dev/null | cut -d. -f1
}

convert() {
  local src="$1" dir base dst tmp a b
  dir="$(dirname "$src")"
  base="$(basename "${src%.*}")"
  dst="$dir/$base.opus"
  tmp="$TMP/$$.opus"

  if [ -e "$dst" ]; then
    echo "пропуск, уже есть: $dst"
    return
  fi

  # Opus в Ogg плохо хранит обложку, поэтому выносим её в cover.jpg папки альбома
  if [ ! -e "$dir/cover.jpg" ]; then
    rm -f "$TMP/cover.jpg"
    if ffmpeg -v quiet -nostdin -i "$src" -an -frames:v 1 -c:v mjpeg "$TMP/cover.jpg" && [ -s "$TMP/cover.jpg" ]; then
      cp "$TMP/cover.jpg" "$dir/cover.jpg"
    fi
  fi

  if ! nice -n 10 ffmpeg -v error -nostdin -i "$src" -map 0:a:0 -map_metadata 0 \
      -c:a libopus -b:a "$BITRATE" -vbr on -y "$tmp"; then
    echo "ошибка: $src"
    rm -f "$tmp"
    return
  fi

  a="$(duration "$src")"
  b="$(duration "$tmp")"
  if [ -z "$a" ] || [ -z "$b" ] || [ $((a - b)) -gt 2 ] || [ $((b - a)) -gt 2 ]; then
    echo "длительность не совпала ($a/$b с), оригинал оставлен: $src"
    rm -f "$tmp"
    return
  fi

  cp "$tmp" "$dst" && rm -f "$tmp" "$src" && echo "готово: $dst"
}

while true; do
  find "$ROOT" -path "$ROOT/_playlists" -prune -o -type f \
    \( -iname '*.flac' -o -iname '*.mp3' -o -iname '*.m4a' -o -iname '*.aac' \
       -o -iname '*.ogg' -o -iname '*.wav' -o -iname '*.alac' -o -iname '*.wma' \) \
    -mmin +"$MIN_AGE" -print0 |
    while IFS= read -r -d '' f; do convert "$f"; done
  [ "${RUN_ONCE:-false}" = "true" ] && exit 0
  sleep "$INTERVAL"
done
