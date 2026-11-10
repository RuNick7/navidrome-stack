#!/usr/bin/env python3
"""Докачивает альбомы, которые Soularr не нашёл по названию альбома.

Soulseek блокирует названия многих популярных альбомов, но сами файлы находятся
по названиям треков. Скрипт берёт альбомы, которые Soularr не нашёл (состояние
soularr_plus.py и «Search failed» в логе), ищет «Исполнитель Трек» (для запрещённых
в Soulseek имён — со звёздочкой: «*eatles Something»), открывает папку найденного
пользователя, проверяет, что в ней весь альбом, и ставит папку на загрузку в slskd.
Когда все файлы папки лежат на диске, она отдаётся Lidarr на импорт; через сутки без
файлов загрузка считается неудачной, и альбом снова ищется через RETRY_DAYS.

    python3 scripts/track-search.py            # 5 альбомов за запуск
    python3 scripts/track-search.py --limit 20

Для автоматической работы добавьте в crontab (crontab -e):
    */30 * * * * cd /root/navidrome-stack && python3 scripts/track-search.py >> data/track-search.log 2>&1
"""
import argparse
import glob
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

STACK = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE = os.path.join(STACK, "data", "track-search.json")
SOULARR_LOG = os.path.join(STACK, "data", "soularr", "soularr.log")
SOULARR_PLUS = os.path.join(STACK, "data", "soularr", "soularr-plus.json")
AUDIO = ("mp3", "flac")
RETRY_DAYS = 7
DOWNLOAD_DAYS = 1  # сколько ждать файлов на диске, прежде чем считать загрузку неудачной
ARTICLES = {"the", "a", "an"}


def load_env(path):
    env = {}
    if os.path.exists(path):
        for line in open(path, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def http(method, url, headers, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    if data is not None:
        req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=120) as r:
        raw = r.read()
    return json.loads(raw) if raw else None


def norm(s):
    s = re.sub(r"[\(\[].*?[\)\]]", " ", s.lower())
    s = re.sub(r"[^\w]+", " ", s)
    return " ".join(s.split())


def ext(name):
    return name.rsplit(".", 1)[-1].lower() if "." in name else ""


class Api:
    def __init__(self, url, header, key, prefix):
        self.base, self.h = url.rstrip("/") + prefix, {header: key}

    def __call__(self, method, path, body=None, **params):
        q = f"?{urllib.parse.urlencode(params)}" if params else ""
        return http(method, f"{self.base}/{path}{q}", self.h, body)


def wildcard(name):
    """«The Beatles» -> «*eatles»: так поиск обходит запрещённые в Soulseek имена."""
    words = [w for w in name.split() if w.casefold() not in ARTICLES] or name.split()
    return " ".join("*" + w[1:] if sum(c.isalnum() for c in w) >= 3 and w[0].isalnum() else w for w in words)


def soularr_state():
    try:
        return json.load(open(SOULARR_PLUS, encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def failed_searches():
    """Альбомы, которые Soularr не нашёл: id из состояния soularr_plus.py и пары (альбом, исполнитель) из лога."""
    ids = {k for k, v in soularr_state().get("albums", {}).items() if v.get("last") == "notfound"}
    pairs = set()
    for log in sorted(glob.glob(SOULARR_LOG + "*")):
        for line in open(log, encoding="utf-8", errors="replace"):
            m = re.search(r"Search failed for Album: (.*) - Artist: (.*)$", line.rstrip())
            if m:
                pairs.add((norm(m.group(1)), norm(m.group(2))))
    return ids, pairs


def wanted_albums(lidarr):
    page = 1
    while True:
        r = lidarr("GET", "wanted/missing", page=page, pageSize=200, includeArtist="true")
        yield from r["records"]
        if page * 200 >= r["totalRecords"]:
            return
        page += 1


def search(slskd, text):
    s = slskd("POST", "searches", {"searchText": text, "searchTimeout": 15000, "filterResponses": True})
    for _ in range(60):
        time.sleep(3)
        if slskd("GET", f"searches/{s['id']}").get("isComplete"):
            break
    responses = slskd("GET", f"searches/{s['id']}/responses")
    slskd("DELETE", f"searches/{s['id']}")
    return responses


def pick_folder(slskd, responses, titles):
    """Папка пользователя, в которой лежит весь альбом одним форматом."""
    candidates = {}
    for r in responses:
        for f in r.get("files", []):
            if ext(f["filename"]) in AUDIO and "\\" in f["filename"]:
                key = (r["username"], f["filename"].rsplit("\\", 1)[0])
                candidates[key] = (r.get("hasFreeUploadSlot", False), r.get("uploadSpeed", 0))
    ranked = sorted(candidates, key=lambda k: candidates[k], reverse=True)
    wanted = [norm(t) for t in titles]
    for user, folder in ranked[:8]:
        try:
            listing = slskd("POST", f"users/{urllib.parse.quote(user)}/directory", {"directory": folder})
        except (urllib.error.URLError, TimeoutError):
            continue
        files = (listing[0] if isinstance(listing, list) and listing else listing or {}).get("files", [])
        for fmt in AUDIO:
            audio = [f for f in files if ext(f["filename"]) == fmt]
            names = [norm(f["filename"]) for f in audio]
            matched = sum(1 for t in wanted if any(t in n for n in names))
            # почти весь альбом или другое издание (обычное вместо deluxe и наоборот)
            whole = matched >= 0.8 * len(wanted) and len(audio) <= len(wanted) + 6
            edition = matched >= 0.6 * len(wanted) and matched >= 0.9 * len(audio)
            if audio and (whole or edition):
                return user, folder, audio
    return None


def on_disk(path):
    """Сколько готовых аудиофайлов в папке (конвертер мог уже перекодировать их в Opus)."""
    try:
        return sum(1 for f in os.listdir(path) if ext(f) in AUDIO + ("opus", "m4a", "ogg"))
    except OSError:
        return 0


def import_finished(state, slskd, lidarr, now):
    """Загрузки, которые закончились: отдать Lidarr на импорт; через сутки без файлов — неудача.

    Список загрузок slskd ненадёжен: Soularr в конце каждого прохода чистит завершённые,
    поэтому готовность определяется по файлам на диске."""
    for album_id, s in state.items():
        if s.get("status") == "imported" and not s.get("checked"):
            try:
                stats = lidarr("GET", f"album/{album_id}").get("statistics") or {}
            except urllib.error.HTTPError:
                stats = {}
            if stats.get("trackFileCount", 0) > 0:
                s["checked"] = True
            elif now - s.get("imported_at", s["tried"]) > DOWNLOAD_DAYS * 86400:
                s.update(status="failed", tried=now)
                print(f"Lidarr не импортировал: {s['title']} ({s.get('local')})")
            continue
        if s.get("status") != "downloading":
            continue
        try:
            dl = slskd("GET", f"transfers/downloads/{urllib.parse.quote(s['user'])}")
        except urllib.error.URLError:
            dl = {"directories": []}
        states = {f["state"] for d in dl.get("directories", []) if d["directory"] == s["folder"] for f in d["files"]}
        if states and not all(x.startswith("Completed") for x in states):
            continue  # ещё качается
        failed = states and states != {"Completed, Succeeded"}
        have = on_disk(s["local"])
        if not failed and have >= s.get("count", 1):
            lidarr("POST", "command", {"name": "DownloadedAlbumsScan", "path": s["local"]})
            s.update(status="imported", imported_at=now)
            print(f"импорт в Lidarr: {s['title']}")
        elif failed or now - s["tried"] > DOWNLOAD_DAYS * 86400:
            s.update(status="failed", tried=now)
            print(f"загрузка не удалась: {s['title']} (на диске {have} из {s.get('count', '?')})")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=5, help="сколько альбомов искать за запуск")
    args = ap.parse_args()

    env = {**load_env(os.path.join(STACK, ".env")), **os.environ}
    for k in ("LIDARR_API_KEY", "SLSKD_API_KEY"):
        if not env.get(k):
            sys.exit(f"Нет {k} в .env")
    lidarr = Api(env.get("LIDARR_URL", "http://127.0.0.1:8686"), "X-Api-Key", env["LIDARR_API_KEY"], "/api/v1")
    slskd = Api(env.get("SLSKD_URL", "http://127.0.0.1:5030"), "X-API-Key", env["SLSKD_API_KEY"], "/api/v0")
    downloads = os.path.join(env.get("MUSIC_DIR", "/mnt/music"), "soulseek")

    state = json.load(open(STATE)) if os.path.exists(STATE) else {}
    now = time.time()
    import_finished(state, slskd, lidarr, now)

    failed_ids, failed_pairs = failed_searches()
    banned = {v.get("name") for v in soularr_state().get("banned", {}).values()}
    todo = []
    for a in wanted_albums(lidarr):
        s = state.get(str(a["id"]))
        if s and (s["status"] in ("downloading", "imported") or now - s["tried"] < RETRY_DAYS * 86400):
            continue
        if str(a["id"]) in failed_ids or (norm(a["title"]), norm(a["artist"]["artistName"])) in failed_pairs:
            todo.append(a)
    # сначала альбомы, потом EP и синглы, как в очереди Soularr
    todo.sort(key=lambda a: {"Album": 0, "EP": 1}.get(a.get("albumType"), 2))
    print(f"Soularr не нашёл: {len(todo)} альбомов, ищу {min(len(todo), args.limit)}")

    for a in todo[:args.limit]:
        artist, title = a["artist"]["artistName"], a["title"]
        tracks = [t["title"] for t in lidarr("GET", "track", albumId=a["id"])]
        entry = {"title": f"{artist} — {title}", "tried": now, "status": "notfound"}
        names = [wildcard(artist)] if artist in banned else [artist, wildcard(artist)]
        found = None
        # три самых длинных названия: короткие («Go», «Intro») дают мусор
        for track in sorted(set(tracks), key=len, reverse=True)[:3]:
            for name in dict.fromkeys(names):
                try:
                    responses = search(slskd, f"{name} {track}")
                    found = pick_folder(slskd, responses, tracks) if responses else None
                except (urllib.error.URLError, TimeoutError) as e:
                    print(f"  ошибка slskd ({e}) на {name} — {track}")
                    responses, found = [], None
                time.sleep(10)  # Soulseek ограничивает частые поиски
                if responses:
                    break  # ответы есть: звёздочки не нужны
            if found:
                user, folder, files = found
                slskd("POST", f"transfers/downloads/{urllib.parse.quote(user)}",
                      [{"filename": f"{folder}\\{f['filename']}", "size": f["size"]} for f in files])
                entry.update(status="downloading", user=user, folder=folder, count=len(files),
                             local=os.path.join(downloads, folder.rsplit("\\", 1)[-1]))
                print(f"+ {artist} — {title}: {len(files)} файлов от {user}")
                break
        else:
            print(f"  не найдено: {artist} — {title}")
        state[str(a["id"])] = entry
        json.dump(state, open(STATE, "w"), ensure_ascii=False, indent=1)

    json.dump(state, open(STATE, "w"), ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main()
