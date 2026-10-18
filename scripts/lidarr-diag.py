#!/usr/bin/env python3
"""Почему отмеченные альбомы Lidarr не попадают в Wanted → Missing (и Soularr их не ищет).

Lidarr показывает в Missing альбом, только если отмечены и альбом, и исполнитель,
есть отмеченное издание с загруженным списком треков и дата выхода уже наступила.

    python3 scripts/lidarr-diag.py         # показать причины
    python3 scripts/lidarr-diag.py --fix   # отметить исполнителей и перечитать альбомы без треков
"""
import argparse
import datetime
import json
import os
import sys
import urllib.request

STACK = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


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
if not env.get("LIDARR_API_KEY"):
    sys.exit("Нет LIDARR_API_KEY в .env")
BASE = env.get("LIDARR_URL", "http://127.0.0.1:8686").rstrip("/") + "/api/v1/"
H = {"X-Api-Key": env["LIDARR_API_KEY"], "Content-Type": "application/json"}


def api(method, path, body=None):
    req = urllib.request.Request(BASE + path, method=method, headers=H,
                                 data=json.dumps(body).encode() if body is not None else None)
    with urllib.request.urlopen(req, timeout=300) as r:
        raw = r.read()
    return json.loads(raw) if raw else None


ap = argparse.ArgumentParser()
ap.add_argument("--fix", action="store_true")
args = ap.parse_args()

artists = {a["id"]: a for a in api("GET", "artist")}
now = datetime.datetime.now(datetime.timezone.utc).isoformat()
reasons = {"исполнитель не отмечен": [], "нет даты выхода или ещё не вышел": [],
           "нет отмеченного издания": [], "список треков не загружен": [], "должен быть в Missing": []}
for al in api("GET", "album"):
    st = al.get("statistics") or {}
    if not al.get("monitored") or st.get("trackFileCount", 0) > 0:
        continue
    releases = al.get("releases") or []
    if not artists.get(al["artistId"], {}).get("monitored"):
        key = "исполнитель не отмечен"
    elif not al.get("releaseDate") or al["releaseDate"] > now:
        key = "нет даты выхода или ещё не вышел"
    elif releases and not any(r.get("monitored") for r in releases):
        key = "нет отмеченного издания"
    elif not st.get("trackCount"):
        key = "список треков не загружен"
    else:
        key = "должен быть в Missing"
    reasons[key].append(al)

for key, items in reasons.items():
    if items:
        sample = ", ".join(f"{artists.get(a['artistId'], {}).get('artistName', '?')} — {a['title']}" for a in items[:3])
        print(f"{key}: {len(items)}  (например: {sample})")

if args.fix:
    ids = sorted({a["artistId"] for a in reasons["исполнитель не отмечен"]})
    if ids:
        api("PUT", "artist/editor", {"artistIds": ids, "monitored": True})
        print(f"отмечено исполнителей: {len(ids)}")
    for al in reasons["список треков не загружен"]:
        api("POST", "command", {"name": "RefreshAlbum", "albumId": al["id"]})
    if reasons["список треков не загружен"]:
        print(f"поставлено на перечитывание альбомов: {len(reasons['список треков не загружен'])} "
              "(ход в Lidarr: System → Tasks)")
