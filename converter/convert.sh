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
# Файлы, которые не удалось перекодировать (битые): не трогаем повторно каждые 15 минут
FAILED="$ROOT/.opus-failed"
touch "$FAILED"

duration() {
  ffprobe -v error -show_entries format=duration -of csv=p=0 "$1" 2>/dev/null | cut -d. -f1
}

# Обложка внутрь Opus: тег METADATA_BLOCK_PICTURE (блок PICTURE из FLAC в base64).
# Пустая строка, если обложки нет.
u32() { local o=""; for s in 24 16 8 0; do o+="\\$(printf %03o $(($1 >> s & 255)))"; done; printf "$o"; }
picture_tag() {
  local img="$TMP/cover.jpg" size
  rm -f "$img"
  ffmpeg -v quiet -nostdin -i "$1" -an -frames:v 1 -vf "scale='min(600,iw)':-2" -q:v 4 -c:v mjpeg -y "$img" || return
  [ -s "$img" ] || return
  size=$(stat -c %s "$img")
  [ "$size" -lt 90000 ] || return  # иначе аргумент ffmpeg превысит лимит ядра
  { u32 3; u32 10; printf image/jpeg; u32 0; u32 0; u32 0; u32 0; u32 0; u32 "$size"; cat "$img"; } | base64 -w0
}

convert() {
  local src="$1" dir base dst tmp a b pic
  dir="$(dirname "$src")"
  base="$(basename "${src%.*}")"
  dst="$dir/$base.opus"
  tmp="$TMP/$$.opus"

  if [ -e "$dst" ]; then
    # тот же трек скачан повторно рядом с готовым Opus: оригинал лишний
    a="$(duration "$src")"
    b="$(duration "$dst")"
    if [ -n "$a" ] && [ -n "$b" ] && [ $((a - b)) -le 2 ] && [ $((b - a)) -le 2 ]; then
      rm -f "$src" && echo "удалён дубль: $src"
    else
      echo "пропуск, уже есть $dst с другой длительностью: $src"
    fi
    return
  fi

  pic="$(picture_tag "$src")"
  if ! nice -n 10 ffmpeg -v error -nostdin -i "$src" -map 0:a:0 -map_metadata 0 \
      ${pic:+-metadata "METADATA_BLOCK_PICTURE=$pic"} \
      -c:a libopus -b:a "$BITRATE" -vbr on -y "$tmp"; then
    echo "ошибка: $src"
    echo "$src" >> "$FAILED"
    rm -f "$tmp"
    return
  fi

  a="$(duration "$src")"
  b="$(duration "$tmp")"
  if [ -z "$a" ] || [ -z "$b" ] || [ $((a - b)) -gt 2 ] || [ $((b - a)) -gt 2 ]; then
    echo "длительность не совпала ($a/$b с), оригинал оставлен: $src"
    echo "$src" >> "$FAILED"
    rm -f "$tmp"
    return
  fi

  cp "$tmp" "$dst" && rm -f "$tmp" "$src" && echo "готово: $dst"
}

while true; do
  find "$ROOT" \( -path "$ROOT/_playlists" -o -name failed_imports \) -prune -o -type f \
    \( -iname '*.flac' -o -iname '*.mp3' -o -iname '*.m4a' -o -iname '*.aac' \
       -o -iname '*.ogg' -o -iname '*.wav' -o -iname '*.alac' -o -iname '*.wma' \) \
    -mmin +"$MIN_AGE" -print0 |
    while IFS= read -r -d '' f; do grep -qxF -- "$f" "$FAILED" || convert "$f"; done
  [ "${RUN_ONCE:-false}" = "true" ] && exit 0
  sleep "$INTERVAL"
done
