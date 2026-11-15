#!/usr/bin/env python3
"""Возвращает в библиотеку скачанные, но не импортированные альбомы.

Soularr и track-search качают в /mnt/music/soulseek, а Lidarr переносит в /mnt/music/lidarr.
Не принятые папки Soularr складывает в soulseek/failed_imports и навсегда вносит альбом в стоп-лист,
track-search оставляет на месте. Частые причины разовые: конвертер в Opus перекодировал файлы
посреди импорта, или Lidarr не понял альбом по имени папки («CD 1»). Скрипт заново спрашивает
Lidarr о каждой такой папке (ручной импорт: сопоставление по тегам) и импортирует её, если Lidarr
относит к одному альбому без замечаний хотя бы 80 % файлов, а у альбома ещё нет файлов. Такой альбом
снимается со стоп-листа Soularr. Папку, которую Lidarr не принял, проверяет снова через неделю.

    python3 scripts/recover-imports.py            # показать, что можно вернуть
    python3 scripts/recover-imports.py --apply    # импортировать

Раз в сутки (crontab -e):
    50 4 * * * cd /root/navidrome-stack && python3 scripts/recover-imports.py --apply >> data/recover-imports.log 2>&1
"""
import argparse
import importlib.util
import json
import os
import sys
import time
import urllib.error

STACK = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE = os.path.join(STACK, "data", "recover-imports.json")
DENYLIST = os.path.join(STACK, "data", "soularr", "failed_imports.json")
RECHECK_DAYS = 7
SETTLE_HOURS = 48  # свежие загрузки ещё могут импортироваться сами
MIN_SHARE = 0.8

spec = importlib.util.spec_from_file_location("track_search", os.path.join(STACK, "scripts", "track-search.py"))
ts = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ts)


def folders(downloads, now):
    """Папки с неимпортированными загрузками: failed_imports и старые папки в самой soulseek."""
    out = []
    for base in (os.path.join(downloads, "failed_imports"), downloads):
        try:
            names = os.listdir(base)
        except OSError:
            continue
        for name in names:
            path = os.path.join(base, name)
            if name.startswith(".") or name == "failed_imports" or not os.path.isdir(path):
                continue
            if now - os.path.getmtime(path) > SETTLE_HOURS * 3600:
                out.append(path)
    return sorted(out)


def judge(lidarr, path):
    """(альбом, сколько файлов примет, всего файлов, почему нет) для одной папки."""
    items = lidarr("GET", "manualimport", folder=path, filterExistingFiles="false") or []
    if not items:
        return None, 0, 0, "нет аудиофайлов"
    clean = [i for i in items if not i.get("rejections") and i.get("album") and i.get("tracks")]
    albums = {i["album"]["id"] for i in clean}
    if len(albums) != 1:
        reasons = sorted({r["reason"].split(":")[0][:60] for i in items for r in i.get("rejections", [])})
        return None, len(clean), len(items), "; ".join(reasons) or "несколько альбомов"
    if len(clean) < MIN_SHARE * len(items):
        return None, len(clean), len(items), f"принимает только {len(clean)} из {len(items)}"
    return albums.pop(), len(clean), len(items), ""


def save(state):
    with open(STATE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False)


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--apply", action="store_true", help="импортировать (без него только показать)")
    p.add_argument("--limit", type=int, default=0, help="сколько папок проверить за запуск (0 — все)")
    args = p.parse_args()

    env = {**ts.load_env(os.path.join(STACK, ".env")), **os.environ}
    if not env.get("LIDARR_API_KEY"):
        sys.exit("Нет LIDARR_API_KEY в .env")
    lidarr = ts.Api(env.get("LIDARR_URL", "http://127.0.0.1:8686"), "X-Api-Key", env["LIDARR_API_KEY"], "/api/v1")
    downloads = os.path.join(env.get("MUSIC_DIR", "/mnt/music"), "soulseek")
    now = time.time()
    state = json.load(open(STATE)) if os.path.exists(STATE) else {}
    todo = [f for f in folders(downloads, now) if now - state.get(f, {}).get("t", 0) > RECHECK_DAYS * 86400]
    if args.limit:
        todo = todo[: args.limit]
    print(f"{time.strftime('%Y-%m-%d %H:%M')} папок к проверке: {len(todo)}")

    recovered, refused = [], 0
    for path in todo:
        try:
            album_id, good, total, why = judge(lidarr, path)
            album = lidarr("GET", f"album/{album_id}") if album_id else None
        except (urllib.error.URLError, TimeoutError) as e:
            print(f"  ошибка Lidarr на {path}: {e}")
            continue
        if album and (album.get("statistics") or {}).get("trackFileCount", 0) > 0:
            why = "у альбома уже есть файлы"
        if why:
            refused += 1
            state[path] = {"t": now, "result": why}
            continue
        title = f"{album['artist']['artistName']} — {album['title']}"
        if args.apply:
            moved = ts.import_folder(lidarr, path, album_id)
            if not moved:
                state[path] = {"t": now, "result": "Lidarr передумал"}
                continue
            deny = json.load(open(DENYLIST)) if os.path.exists(DENYLIST) else {}
            if deny.pop(str(album_id), None) is not None:
                with open(DENYLIST, "w") as f:
                    json.dump(deny, f, indent=2)
        recovered.append(f"{title} ({good}/{total})")
        state.pop(path, None)
        if args.apply:
            save(state)

    if args.apply:  # пробный прогон ничего не запоминает: иначе настоящий пропустил бы папки на неделю
        save(state)
    print(f"  {'возвращено' if args.apply else 'можно вернуть'}: {len(recovered)}, Lidarr не принимает: {refused}")
    for line in recovered:
        print(f"  + {line}")


if __name__ == "__main__":
    main()
