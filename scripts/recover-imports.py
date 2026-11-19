#!/usr/bin/env python3
"""Возвращает в библиотеку скачанные, но не импортированные альбомы.

Soularr и track-search качают в /mnt/music/soulseek, а Lidarr переносит в /mnt/music/lidarr.
Не принятые папки Soularr складывает в soulseek/failed_imports и навсегда вносит альбом в стоп-лист,
track-search оставляет на месте. Частые причины разовые: конвертер в Opus перекодировал файлы
посреди импорта, или Lidarr не понял альбом по имени папки («CD 1»). Скрипт заново спрашивает
Lidarr о каждой такой папке (ручной импорт: сопоставление по тегам) и импортирует её, если Lidarr
относит к одному альбому без замечаний хотя бы 80 % файлов, а у альбома ещё нет файлов. Для папок,
которые Soularr скачал под конкретный альбом, прощается одно замечание — «Album match is not close
enough» при сходстве от 60 %: так Lidarr отвергал синглы, у которых в тегах альбом, откуда трек.
Такой альбом снимается со стоп-листа Soularr. Не принятую папку проверяет снова через неделю.

    python3 scripts/recover-imports.py            # показать, что можно вернуть
    python3 scripts/recover-imports.py --apply    # импортировать

Раз в сутки (crontab -e):
    50 4 * * * cd /root/navidrome-stack && python3 scripts/recover-imports.py --apply >> data/recover-imports.log 2>&1
"""
import argparse
import fcntl
import importlib.util
import json
import os
import re
import sys
import time
import urllib.error

STACK = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE = os.path.join(STACK, "data", "recover-imports.json")
LOCK = os.path.join(STACK, "data", "recover-imports.lock")  # ночной запуск не пересечётся с ручным
DENYLIST = os.path.join(STACK, "data", "soularr", "failed_imports.json")
RECHECK_DAYS = 7
SETTLE_HOURS = 48  # свежие загрузки ещё могут импортироваться сами
FAILED_SETTLE_HOURS = 1  # failed_imports Soularr уже бросил: ждать нечего
MIN_SHARE = 0.8
# Отказ, который не значит «не тот альбом»: теги файла (год, издатель, название альбома, у сингла —
# альбом, откуда трек) не похожи на релиз в MusicBrainz. Если Soularr скачал папку под этот альбом
# (он сверил названия и число треков) и Lidarr сам относит файлы к нему же, импортируем с явным
# изданием, как ручной импорт в интерфейсе Lidarr. Сходство ниже MIN_MATCH — нет.
SOFT_REASON = "Album match is not close enough"
MIN_MATCH = 60.0

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
            settle = FAILED_SETTLE_HOURS if base.endswith("failed_imports") else SETTLE_HOURS
            if now - os.path.getmtime(path) > settle * 3600:
                out.append(path)
    return sorted(out)


def soft(item, expected):
    """Файл отнесён к ожидаемому альбому, а замечание только «теги не похожи» и сходство не ниже MIN_MATCH."""
    if expected is None or not item.get("album") or item["album"]["id"] != expected or not item.get("tracks"):
        return False
    reasons = [r["reason"] for r in item.get("rejections", [])]
    for reason in reasons:
        m = re.search(r"([\d.]+) % vs", reason)
        if not reason.startswith(SOFT_REASON) or not m or float(m.group(1)) < MIN_MATCH:
            return False
    return bool(reasons)


def judge(lidarr, path, expected=None):
    """(альбом, файлы к импорту, всего файлов, почему нет, с явным изданием ли) для одной папки.

    expected — альбом, под который папку скачал Soularr (из его стоп-листа): только для таких папок
    прощается «теги не похожи на релиз»."""
    items = lidarr("GET", "manualimport", folder=path, filterExistingFiles="false") or []
    if not items:
        return None, [], 0, "нет аудиофайлов", False
    clean = [i for i in items if not i.get("rejections") and i.get("album") and i.get("tracks")]
    usable = clean + [i for i in items if soft(i, expected)]
    albums = {i["album"]["id"] for i in usable}
    forced = len(usable) > len(clean)
    if len(albums) != 1:
        reasons = sorted({r["reason"].split(":")[0][:60] for i in items for r in i.get("rejections", [])})
        return None, usable, len(items), "; ".join(reasons) or "несколько альбомов", forced
    if len(usable) < MIN_SHARE * len(items):
        return None, usable, len(items), f"принимает только {len(usable)} из {len(items)}", forced
    return albums.pop(), usable, len(items), "", forced


def import_items(lidarr, items, forced):
    """Ручной импорт этих файлов в Lidarr с альбомом, изданием и треками, которые он сам сопоставил."""
    files = [{"path": i["path"], "artistId": i["artist"]["id"], "albumId": i["album"]["id"],
              "albumReleaseId": i["albumReleaseId"], "trackIds": [t["id"] for t in i["tracks"]],
              "quality": i["quality"], "indexerFlags": 0, "disableReleaseSwitching": forced} for i in items]
    lidarr("POST", "command", {"name": "ManualImport", "files": files, "importMode": "move", "replaceExistingFiles": False})
    return len(files)


def save(state):
    with open(STATE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False)


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--apply", action="store_true", help="импортировать (без него только показать)")
    p.add_argument("--limit", type=int, default=0, help="сколько папок проверить за запуск (0 — все)")
    p.add_argument("--recheck", action="store_true", help="проверить и те, что Lidarr не принял в последнюю неделю")
    args = p.parse_args()
    lock = open(LOCK, "w")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        sys.exit("recover-imports уже идёт")

    env = {**ts.load_env(os.path.join(STACK, ".env")), **os.environ}
    if not env.get("LIDARR_API_KEY"):
        sys.exit("Нет LIDARR_API_KEY в .env")
    lidarr = ts.Api(env.get("LIDARR_URL", "http://127.0.0.1:8686"), "X-Api-Key", env["LIDARR_API_KEY"], "/api/v1")
    downloads = os.path.join(env.get("MUSIC_DIR", "/mnt/music"), "soulseek")
    now = time.time()
    state = json.load(open(STATE)) if os.path.exists(STATE) else {}
    deny = json.load(open(DENYLIST)) if os.path.exists(DENYLIST) else {}
    expected = {os.path.normpath(v["folder_path"]): int(v["album_id"]) for v in deny.values()
                if v.get("folder_path") and v.get("album_id")}
    todo = [f for f in folders(downloads, now) if args.recheck or now - state.get(f, {}).get("t", 0) > RECHECK_DAYS * 86400]
    todo.sort(key=lambda f: os.path.normpath(f) not in expected)  # сначала то, что Soularr качал под альбом
    if args.limit:
        todo = todo[: args.limit]
    print(f"{time.strftime('%Y-%m-%d %H:%M')} папок к проверке: {len(todo)}")

    recovered, refused = [], 0
    for path in todo:
        try:
            album_id, items, total, why, forced = judge(lidarr, path, expected.get(os.path.normpath(path)))
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
            import_items(lidarr, items, forced)
            deny = json.load(open(DENYLIST)) if os.path.exists(DENYLIST) else {}
            if deny.pop(str(album_id), None) is not None:
                with open(DENYLIST, "w") as f:
                    json.dump(deny, f, indent=2)
        recovered.append(f"{title} ({len(items)}/{total})" + (" [теги не похожи, с явным изданием]" if forced else ""))
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
