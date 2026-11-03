#!/usr/bin/env python3
"""Что раздавать в Soulseek: самые слушаемые альбомы, копией на локальном диске сервера.

Многие пользователи Soulseek не отдают файлы тем, кто сам ничего не раздаёт. Раздавать всю
библиотеку с Google Диска дорого: каждая отдача читала бы файл из облака. Поэтому раздаём
небольшую копию — самые слушаемые альбомы из папки Lidarr (у них полные теги), в пределах
--size гигабайт, в ${BUFFER_DIR}/shared, которую slskd видит как /shared. Повторный запуск
досылает новое и убирает то, что выпало из списка.

    python3 scripts/soulseek-share.py             # показать, что попадёт в раздачу
    python3 scripts/soulseek-share.py --apply     # скопировать (по умолчанию 2 ГБ)

После --apply slskd пересканирует раздачу сам при перезапуске, или:
    curl -X PUT -H "X-API-Key: …" http://127.0.0.1:5030/api/v0/shares
"""
import argparse
import os
import re
import shutil
import sqlite3

STACK = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(STACK, "data", "navidrome", "navidrome.db")


def load_env(path):
    env = {}
    if os.path.exists(path):
        for line in open(path, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def safe(name):
    return re.sub(r'[\\/:*?"<>|]+', "_", name).strip(" .")[:120] or "_"


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--size", type=float, default=2.0, help="сколько гигабайт раздавать")
    p.add_argument("--apply", action="store_true", help="скопировать файлы")
    args = p.parse_args()

    env = load_env(os.path.join(STACK, ".env"))
    dest = os.path.join(env.get("BUFFER_DIR", "/srv/music-buffer"), "shared")
    db = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    music = db.execute("select path from library where id = 1").fetchone()[0]

    plays = dict(db.execute(
        "select item_id, sum(play_count) from annotation where item_type = 'media_file' group by item_id"))
    albums = {}
    for fid, path, size, album_id, album, artist, year in db.execute(
        "select id, path, size, album_id, album, album_artist, year from media_file "
        "where missing = 0 and path like 'lidarr/%'"
    ):
        a = albums.setdefault(album_id, {"name": f"{artist} - {album}" + (f" ({year})" if year else ""), "files": [], "size": 0, "plays": 0})
        a["files"].append(path)
        a["size"] += size or 0
        a["plays"] += plays.get(fid, 0)

    limit = args.size * 2**30
    chosen, total = [], 0
    for a in sorted(albums.values(), key=lambda a: (-a["plays"], -len(a["files"]))):
        if len(a["files"]) < 3 or total + a["size"] > limit:
            continue  # синглы не берём: в Soulseek ищут альбомы
        chosen.append(a)
        total += a["size"]
    print(f"в раздачу: {len(chosen)} альбомов, {sum(len(a['files']) for a in chosen)} файлов, {total / 2**30:.2f} ГБ → {dest}")
    for a in chosen[:20]:
        print(f"  {a['plays']:4} прослуш. · {a['name']}")
    if not args.apply:
        return

    keep = set()
    for a in chosen:
        folder = os.path.join(dest, safe(a["name"]))
        keep.add(folder)
        os.makedirs(folder, exist_ok=True)
        for path in a["files"]:
            src = os.path.join(music, path)
            dst = os.path.join(folder, safe(os.path.basename(path)))
            if not os.path.exists(dst) or os.path.getsize(dst) != os.path.getsize(src):
                shutil.copyfile(src, dst)
    for name in os.listdir(dest):
        folder = os.path.join(dest, name)
        if os.path.isdir(folder) and folder not in keep:
            shutil.rmtree(folder)  # выпало из списка самых слушаемых
    print("скопировано")


if __name__ == "__main__":
    main()
