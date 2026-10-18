#!/usr/bin/env python3
"""Сводка по серверу за последние сутки: что скачано, что в очереди, где ошибки.

    python3 scripts/status.py          # за 24 часа
    python3 scripts/status.py 6        # за 6 часов
"""
import hashlib
import json
import os
import re
import secrets
import subprocess
import sys
import time
import urllib.parse
import urllib.request

STACK = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HOURS = int(sys.argv[1]) if len(sys.argv) > 1 else 24
AUDIO = (".opus", ".mp3", ".flac", ".m4a", ".ogg")


def load_env(path):
    env = {}
    if os.path.exists(path):
        for line in open(path, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    return env


env = {**load_env(os.path.join(STACK, ".env")), **os.environ}
MUSIC = env.get("MUSIC_DIR", "/mnt/music")


def get(url, headers=None):
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers or {}), timeout=30) as r:
            return json.load(r)
    except Exception as e:
        return {"_error": str(e)}


def logs(service):
    r = subprocess.run(["docker", "compose", "logs", "--no-color", "--since", f"{HOURS}h", service],
                       cwd=STACK, capture_output=True, text=True)
    return r.stdout.splitlines()


def section(title):
    print(f"\n== {title}")


section("Контейнеры")
ps = subprocess.run(["docker", "compose", "ps", "-a", "--format", "{{.Service}}\t{{.State}}\t{{.Status}}"],
                    cwd=STACK, capture_output=True, text=True).stdout.split("\n")
for line in filter(None, ps):
    service, state, status = line.split("\t")
    mark = "" if state == "running" else "   <-- не работает"
    print(f"  {service:16} {status}{mark}")

section("Диск Google и библиотека")
if not os.path.isdir(MUSIC) or not os.listdir(MUSIC):
    print(f"  {MUSIC} пуст или не смонтирован   <-- проверьте rclone")
else:
    since = time.time() - HOURS * 3600
    per_dir, remaining, total = {}, 0, 0
    for root, dirs, files in os.walk(MUSIC):
        dirs[:] = [d for d in dirs if not d.startswith((".", "_playlists"))]
        top = os.path.relpath(root, MUSIC).split(os.sep)[0]
        for f in files:
            low = f.lower()
            if not low.endswith(AUDIO):
                continue
            total += 1
            if not low.endswith(".opus"):
                remaining += 1
            try:
                if os.path.getmtime(os.path.join(root, f)) > since:
                    per_dir[top] = per_dir.get(top, 0) + 1
            except OSError:
                pass
    print(f"  всего треков: {total}, ещё не в Opus: {remaining}")
    print(f"  новых за {HOURS} ч: " + (", ".join(f"{k} {v}" for k, v in sorted(per_dir.items())) or "нет"))
df = subprocess.run(["df", "-h", "/", env.get("BUFFER_DIR", "/")], capture_output=True, text=True).stdout
print("  " + "\n  ".join(df.strip().splitlines()))

section("Navidrome")
if env.get("ND_USER") and env.get("ND_PASSWORD"):
    salt = secrets.token_hex(6)
    q = urllib.parse.urlencode({"u": env["ND_USER"], "s": salt, "v": "1.16.1", "c": "status", "f": "json",
                                "t": hashlib.md5((env["ND_PASSWORD"] + salt).encode()).hexdigest()})
    r = get(f"http://127.0.0.1:4533/rest/getScanStatus?{q}")
    s = r.get("subsonic-response", {}).get("scanStatus", {})
    print(f"  треков в базе: {s.get('count', '?')}, сканирование идёт: {s.get('scanning', '?')}, "
          f"последнее: {s.get('lastScan', '?')}" if s else f"  нет ответа: {r.get('_error', r)}")
else:
    print("  нет ND_USER/ND_PASSWORD в .env")

section("Lidarr")
if env.get("LIDARR_API_KEY"):
    h = {"X-Api-Key": env["LIDARR_API_KEY"]}
    missing = get("http://127.0.0.1:8686/api/v1/wanted/missing?pageSize=1", h)
    queue = get("http://127.0.0.1:8686/api/v1/queue?pageSize=1", h)
    hist = get("http://127.0.0.1:8686/api/v1/history/since?date="
               + time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - HOURS * 3600)), h)
    events = {}
    for e in hist if isinstance(hist, list) else []:
        events[e["eventType"]] = events.get(e["eventType"], 0) + 1
    albums = get("http://127.0.0.1:8686/api/v1/album", h)
    albums = albums if isinstance(albums, list) else []
    mon = [a for a in albums if a.get("monitored")]
    partial = [a for a in mon if 0 < a.get("statistics", {}).get("percentOfTracks", 0) < 100]
    done = [a for a in mon if a.get("statistics", {}).get("percentOfTracks", 0) >= 100]
    print(f"  отмечено альбомов: {len(mon)} (скачаны {len(done)}, частично {len(partial)}, "
          f"без файлов {len(mon) - len(done) - len(partial)}); в Missing: {missing.get('totalRecords', '?')}, "
          f"в очереди: {queue.get('totalRecords', '?')}")
    print(f"  за {HOURS} ч: импортировано альбомов {events.get('downloadImported', 0)}, "
          f"треков {events.get('trackFileImported', 0)}, не импортировано {events.get('albumImportIncomplete', 0)}")
    for e in (get("http://127.0.0.1:8686/api/v1/health", h) or []):
        if isinstance(e, dict) and e.get("type") == "error" and "indexers" not in e.get("message", ""):
            print(f"  ошибка: {e.get('message')}")
else:
    print("  нет LIDARR_API_KEY в .env")

section("Поиск и загрузки (Soularr, slskd, track-search)")
sl = logs("soularr")
added = sum(int(m.group(1)) for line in sl if (m := re.search(r"Total Downloads added: (\d+)", line)))
failed = sum(1 for line in sl if "Search failed for Album" in line)
imp_fail = sum(1 for line in sl if "Failed import moved" in line)
print(f"  Soularr: поставлено на загрузку {added}, не найдено {failed}, не принято Lidarr {imp_fail}")
if env.get("SLSKD_API_KEY"):
    dl = get("http://127.0.0.1:5030/api/v0/transfers/downloads", {"X-API-Key": env["SLSKD_API_KEY"]})
    states = {}
    for u in dl if isinstance(dl, list) else []:
        for d in u.get("directories", []):
            for f in d.get("files", []):
                st = f["state"].replace("Completed, ", "")
                states[st] = states.get(st, 0) + 1
    print("  slskd: " + (", ".join(f"{k} {v}" for k, v in sorted(states.items())) or "загрузок нет"))
ts_log = os.path.join(STACK, "data", "track-search.log")
if os.path.exists(ts_log):
    tail = open(ts_log, encoding="utf-8", errors="replace").read().splitlines()[-200:]
    print(f"  track-search (последние запуски): найдено {sum(l.startswith('+ ') for l in tail)}, "
          f"не найдено {sum('не найдено' in l for l in tail)}, импорт {sum('импорт в Lidarr' in l for l in tail)}")

section("Конвертер Opus")
cv = logs("opus-converter")
print(f"  перекодировано {sum('готово:' in l for l in cv)}, удалено дублей {sum('удалён дубль' in l for l in cv)}, "
      f"ошибок {sum('ошибка:' in l for l in cv)}, длительность не совпала {sum('не совпала' in l for l in cv)}")

section(f"Ошибки в логах за {HOURS} ч")
services = [line.split("\t")[0] for line in filter(None, ps)]
pattern = re.compile(r"\b(error|fatal|panic|exception|traceback)\b|level=error|\[ERR", re.I)
# обычный шум: нет текста песни, обрыв соединения клиентом, повтор загрузки, битый файл в конвертере
noise = re.compile(r"no lyrics found|error getting lyrics|incomplete response|Requeue file|"
                   r"Error submitting packet|Header missing|Invalid data found|decoder lost sync", re.I)
for service in services:
    errs = [l for l in logs(service) if pattern.search(l) and not noise.search(l)]
    if errs:
        print(f"  {service}: {len(errs)}, последняя: {errs[-1].split('|', 1)[-1].strip()[:160]}")
print("  (подробно: docker compose logs --since 24h <сервис> | grep -i error)")
