#!/usr/bin/env python3
"""Находит в Navidrome альбомы, от которых скачано мало треков, и ставит их в Lidarr на докачку.

Lidarr добавляет исполнителя без слежения за дискографией и отмечает только нужный альбом;
дальше Soularr скачивает его через slskd. Запускать на сервере из папки стека:

    python3 scripts/complete-albums.py            # показать, что будет сделано
    python3 scripts/complete-albums.py --apply    # отправить в Lidarr

Только что добавленные исполнители появляются в Lidarr с задержкой: если альбомы
остались в «ждут обновления Lidarr», запустите скрипт ещё раз через пару минут.
"""
import argparse
import hashlib
import json
import os
import re
import secrets
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

SKIP_ARTISTS = {"various artists", "various", "va", "[unknown artist]", "unknown artist"}


def load_env(path):
    env = {}
    if os.path.exists(path):
        for line in open(path, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def http(method, url, headers=None, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
    if data is not None:
        req.add_header("Content-Type", "application/json")
    for attempt in range(6):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                raw = r.read()
            return json.loads(raw) if raw else None
        except (urllib.error.URLError, TimeoutError) as e:
            # метадата-сервер Lidarr отвечает 503/429 или не отвечает при частых запросах
            code = getattr(e, "code", None)
            if (code and code not in (429, 500, 502, 503, 504)) or attempt == 5:
                raise
            time.sleep(5 * 2 ** attempt)


def norm(s):
    s = s.lower()
    s = re.sub(r"[\(\[].*?[\)\]]", " ", s)  # (Deluxe Edition), [Remastered]
    s = re.sub(r"\s[-–]\s.*$", " ", s)  # " - 2022 Remaster"
    s = re.sub(r"\b(deluxe|remaster(ed)?|edition|expanded|version)\b", " ", s)
    s = re.sub(r"[^\w]+", " ", s)
    return " ".join(s.split())


class Navidrome:
    def __init__(self, url, user, password):
        self.url, self.user, self.password = url.rstrip("/"), user, password

    def call(self, method, **params):
        salt = secrets.token_hex(6)
        token = hashlib.md5((self.password + salt).encode()).hexdigest()
        q = dict(u=self.user, t=token, s=salt, v="1.16.1", c="complete-albums", f="json", **params)
        r = http("GET", f"{self.url}/rest/{method}?{urllib.parse.urlencode(q)}")["subsonic-response"]
        if r.get("status") != "ok":
            sys.exit(f"Navidrome: {r.get('error', {}).get('message', r)}")
        return r

    def albums(self):
        offset = 0
        while True:
            page = self.call("getAlbumList2", type="alphabeticalByName", size=500, offset=offset)
            items = page.get("albumList2", {}).get("album", [])
            yield from items
            if len(items) < 500:
                return
            offset += 500


class Lidarr:
    def __init__(self, url, key):
        self.url, self.h = url.rstrip("/"), {"X-Api-Key": key}

    def get(self, path, **params):
        q = f"?{urllib.parse.urlencode(params)}" if params else ""
        return http("GET", f"{self.url}/api/v1/{path}{q}", self.h)

    def send(self, method, path, body):
        return http(method, f"{self.url}/api/v1/{path}", self.h, body)


def process(lid, args, artist, albums, existing, root, quality, metadata, stats):
    # "A, B, Cast of X" или "A feat. B": ищем целиком, потом по первому исполнителю
    names = list(dict.fromkeys([artist, re.split(r",|&| feat\.? | ft\.? ", artist, flags=re.I)[0].strip()]))
    known = next((existing[norm(n)] for n in names if norm(n) in existing), None)
    if not known:
        time.sleep(1)  # не перегружать метадата-сервер Lidarr
        match = None
        for name in names:
            found = lid.get("artist/lookup", term=name)
            match = next((x for x in found if norm(x["artistName"]) == norm(name)), None)
            if match:
                break
        if not match:
            print(f"  исполнитель не найден в MusicBrainz: {artist}")
            stats["notfound"] += len(albums)
            return
        print(f"  + исполнитель {match['artistName']}")
        if args.apply:
            lid.send("POST", "artist", {
                **match,
                "qualityProfileId": quality,
                "metadataProfileId": metadata,
                "rootFolderPath": root[0]["path"],
                "monitored": True,
                "monitorNewItems": "none",
                "addOptions": {"monitor": "none", "searchForMissingAlbums": False},
            })
        stats["pending"] += len(albums)
        return

    catalogue = {norm(x["title"]): x for x in lid.get("album", artistId=known["id"])}
    wanted = False
    for title in sorted(albums):
        alb = catalogue.get(norm(title))
        if not alb:
            print(f"  нет в каталоге Lidarr: {artist} — {title} (сингл или другое издание)")
            stats["notfound"] += 1
        elif alb["monitored"]:
            stats["already"] += 1
            wanted = True
        else:
            print(f"  → {artist} — {alb['title']}")
            if args.apply:
                lid.send("PUT", "album/monitor", {"albumIds": [alb["id"]], "monitored": True})
            stats["queued"] += 1
            wanted = True
    # addOptions monitor=none оставляет исполнителя неотмеченным, и Lidarr не кладёт его альбомы в Missing
    if args.apply and wanted and not known["monitored"]:
        lid.send("PUT", "artist/editor", {"artistIds": [known["id"]], "monitored": True})
        known["monitored"] = True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true", help="отправить в Lidarr (без флага только показать)")
    ap.add_argument("--max-tracks", type=int, default=2, help="альбом считается неполным, если треков не больше (по умолчанию 2)")
    ap.add_argument("--env", default=".env")
    args = ap.parse_args()

    env = {**load_env(args.env), **os.environ}
    for k in ("ND_USER", "ND_PASSWORD", "LIDARR_API_KEY"):
        if not env.get(k):
            sys.exit(f"Нет {k}: добавьте в {args.env} или передайте переменной окружения")

    nd = Navidrome(env.get("ND_URL", "http://127.0.0.1:4533"), env["ND_USER"], env["ND_PASSWORD"])
    lid = Lidarr(env.get("LIDARR_URL", "http://127.0.0.1:8686"), env["LIDARR_API_KEY"])

    wanted = {}
    for a in nd.albums():
        artist = (a.get("artist") or "").strip()
        if a.get("songCount", 0) <= args.max_tracks and artist.lower() not in SKIP_ARTISTS:
            wanted.setdefault(artist, set()).add(a.get("name", ""))
    total = sum(len(v) for v in wanted.values())
    print(f"Неполных альбомов: {total} у {len(wanted)} исполнителей")
    if not total:
        return

    root = lid.get("rootfolder")
    if not root:
        sys.exit("В Lidarr нет Root Folder: добавьте /mnt/music/lidarr в Settings → Media Management")
    quality = lid.get("qualityprofile")[0]["id"]
    metadata = lid.get("metadataprofile")[0]["id"]
    existing = {norm(x["artistName"]): x for x in lid.get("artist")}

    stats = {"queued": 0, "already": 0, "pending": 0, "notfound": 0}
    for artist, albums in sorted(wanted.items()):
        try:
            process(lid, args, artist, albums, existing, root, quality, metadata, stats)
        except (urllib.error.URLError, TimeoutError) as e:
            print(f"  ошибка Lidarr ({e}) на {artist}, пропускаю")
            stats["notfound"] += len(albums)

    print
    print(f"\nПоставлено на докачку: {stats['queued']}, уже в очереди: {stats['already']}, "
          f"ждут обновления Lidarr: {stats['pending']}, не найдено: {stats['notfound']}")
    if not args.apply:
        print("Это пробный прогон. Для отправки в Lidarr добавьте --apply")
    elif stats["pending"]:
        print("Новые исполнители обновляются в Lidarr. Запустите скрипт ещё раз через пару минут.")


if __name__ == "__main__":
    main()
