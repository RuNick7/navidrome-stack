#!/usr/bin/env python3
"""Находит лишние копии песен: одна и та же песня скачана отдельным треком (explo, Soulseek)
и целым альбомом через Lidarr. Оставляет копию из Lidarr, остальные предлагает удалить.

Копиями считаются треки с тем же названием (без «feat.», но «(Live)», «(Remix)» и т. п.
различаются), длительностью в пределах 3 секунд и тем же основным исполнителем (с учётом
транслита: Max Korzh = Макс Корж) или из того же альбома — у мюзиклов исполнители треков
в разных источниках записаны по-разному. Дубли внутри Lidarr (альбом и сингл) не трогаем: Lidarr
сам следит за своими файлами и скачал бы их снова.

    python3 scripts/dedupe-library.py                 # отчёт data/dedupe-report.tsv, ничего не меняет
    python3 scripts/dedupe-library.py --apply         # перенести историю, удалить копии

С --apply скрипт на полминуты останавливает Navidrome и, сохранив копию его базы, переносит
на оставшуюся копию лайки, прослушивания и оценки всех пользователей и заменяет удаляемые
копии в плейлистах. Через API этого не сделать: прослушивания пришлось бы переигрывать
скробблами, а они ушли бы в Last.fm и ListenBrainz. Потом удаляет файлы, запускает Navidrome
и просит его пересканировать библиотеку. Историю VaginaWave (/opt/volna/data) тоже переносит.
"""
import argparse
import glob
import shutil
import subprocess
import collections
import csv
import hashlib
import json
import os
import re
import secrets
import sqlite3
import sys
import time
import unicodedata
import urllib.parse
import urllib.request

STACK = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(STACK, "data", "navidrome", "navidrome.db")
REPORT = os.path.join(STACK, "data", "dedupe-report.tsv")
KEEP_ROOT = "lidarr"
DROP_ROOTS = ("explo", "soulseek")
SECONDS = 3

CYRILLIC = dict(zip("абвгдеёжзийклмнопрстуфхцчшщъыьэюя", [
    "a", "b", "v", "g", "d", "e", "e", "zh", "z", "i", "i", "k", "l", "m", "n", "o", "p", "r", "s", "t", "u",
    "f", "h", "ts", "ch", "sh", "sch", "", "i", "", "e", "yu", "ya"]))


def skeleton(text):
    s = "".join(CYRILLIC.get(c, c) for c in (text or "").casefold())
    s = "".join(c for c in unicodedata.normalize("NFKD", s) if c.isalnum())
    return s.replace("kh", "h").replace("x", "ks").replace("y", "i").replace("j", "i")


def main_artist(artist):
    return skeleton(re.split(r"\s*(?:,|;|&|•|\bfeat\.?|\bft\.?)\s*", artist or "", flags=re.I)[0])


def title_key(title):
    # «(feat. X)», «[ft. Y]» и « feat. Z» — не другая песня; «(Live)», «(Remix)» — другая
    t = re.sub(r"[\(\[]\s*(feat|ft|with)\b[^\)\]]*[\)\]]", " ", title or "", flags=re.I)
    t = re.sub(r"\s(feat|ft)\.?\s.*$", " ", t, flags=re.I)
    return skeleton(t)


def load_env(path):
    env = {}
    if os.path.exists(path):
        for line in open(path, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def find_copies(db):
    rows = db.execute(
        "select id, path, title, artist, album, duration from media_file where missing = 0"
    ).fetchall()
    by_artist, by_album = collections.defaultdict(list), collections.defaultdict(list)
    for r in rows:
        by_artist[(main_artist(r[3]), title_key(r[2]))].append(r)
        by_album[(skeleton(r[4]), title_key(r[2]))].append(r)
    plan, seen = [], set()  # (удаляем, оставляем)
    for copies in [*by_artist.values(), *by_album.values()]:
        keep = [c for c in copies if c[1].split("/")[0] == KEEP_ROOT]
        if not keep:
            continue
        for c in copies:
            if c[1].split("/")[0] not in DROP_ROOTS or c[0] in seen:
                continue
            twin = min(keep, key=lambda k: abs((k[5] or 0) - (c[5] or 0)))
            if abs((twin[5] or 0) - (c[5] or 0)) <= SECONDS:
                plan.append((c, twin))
                seen.add(c[0])
    return plan


def playlists_of(db, ids):
    found = collections.defaultdict(list)
    for chunk in range(0, len(ids), 500):
        part = ids[chunk:chunk + 500]
        q = f"""select pt.media_file_id, p.id, p.name, p.path, u.user_name
                from playlist_tracks pt join playlist p on p.id = pt.playlist_id
                left join user u on u.id = p.owner_id
                where pt.media_file_id in ({','.join('?' * len(part))})"""
        for file_id, pid, name, path, owner in db.execute(q, part):
            found[file_id].append((pid, name, path, owner))
    return found


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--apply", action="store_true", help="заменить копии в плейлистах и удалить файлы")
    args = p.parse_args()

    db = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    plan = find_copies(db)
    used = playlists_of(db, [c[0] for c, _ in plan])
    music = db.execute("select path from library where id = 1").fetchone()[0]

    with open(REPORT, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f, delimiter="\t")
        w.writerow(["исполнитель", "песня", "удалить", "оставить (Lidarr)", "разница, с", "в плейлистах"])
        for c, k in sorted(plan, key=lambda x: (x[0][3] or "", x[0][2] or "")):
            names = sorted({f"{name} ({owner})" for _, name, _, owner in used.get(c[0], [])})
            w.writerow([c[3], c[2], c[1], k[1], f"{abs((c[5] or 0) - (k[5] or 0)):.1f}", "; ".join(names)])

    by_root = collections.Counter(c[1].split("/")[0] for c, _ in plan)
    touched = {pid: (name, path, owner) for refs in used.values() for pid, name, path, owner in refs}
    size = sum(os.path.getsize(os.path.join(music, c[1])) for c, _ in plan if os.path.exists(os.path.join(music, c[1])))
    print(f"лишних копий: {len(plan)} ({', '.join(f'{k} {v}' for k, v in by_root.items())}), {size / 2**30:.1f} ГБ")
    print(f"плейлистов, где они есть: {len(touched)} (из файлов .m3u: {sum(1 for _, p, _ in touched.values() if p)})")
    for name, path, owner in sorted(touched.values()):
        print(f"  {name} — {owner}{' — .m3u' if path else ''}")
    print(f"полный список: {REPORT}")
    if not args.apply:
        return
    db.close()
    if not plan:
        return  # удалять нечего — Navidrome не останавливаем
    apply(plan, music)


def compose(*args):
    subprocess.run(["docker", "compose", *args], cwd=STACK, check=True)


def merge_annotations(db, drop, keep):
    """Лайки, прослушивания и оценки удаляемой копии — на оставшуюся, у каждого пользователя."""
    rows = db.execute("select user_id, play_count, play_date, rating, starred, starred_at, rated_at from annotation "
                      "where item_id = ? and item_type = 'media_file'", (drop,)).fetchall()
    for user, plays, played, rating, starred, starred_at, rated_at in rows:
        mine = db.execute("select play_count, play_date, rating, starred, starred_at, rated_at from annotation "
                          "where item_id = ? and item_type = 'media_file' and user_id = ?", (keep, user)).fetchone()
        if mine is None:
            db.execute("update annotation set item_id = ? where item_id = ? and item_type = 'media_file' and user_id = ?",
                       (keep, drop, user))
            continue
        later = lambda a, b: max(x for x in (a, b) if x) if (a or b) else None
        earlier = lambda a, b: min(x for x in (a, b) if x) if (a or b) else None
        db.execute(
            "update annotation set play_count = ?, play_date = ?, rating = ?, starred = ?, starred_at = ?, rated_at = ? "
            "where item_id = ? and item_type = 'media_file' and user_id = ?",
            ((mine[0] or 0) + (plays or 0), later(mine[1], played), max(mine[2] or 0, rating or 0),
             1 if (mine[3] or starred) else 0, earlier(mine[4], starred_at), later(mine[5], rated_at), keep, user),
        )
        db.execute("delete from annotation where item_id = ? and item_type = 'media_file' and user_id = ?", (drop, user))


def apply(plan, music):
    stamp = time.strftime("%Y%m%d-%H%M%S")
    mapping = {c[0]: k[0] for c, k in plan}
    log = os.path.join(STACK, "data", f"dedupe-{stamp}.json")
    print("останавливаю Navidrome…")
    compose("stop", "navidrome")
    try:
        for suffix in ("", "-wal", "-shm"):
            if os.path.exists(DB + suffix):
                shutil.copy2(DB + suffix, f"{DB}.bak-dedupe-{stamp}{suffix}")
        db = sqlite3.connect(DB)
        with db:
            for drop, keep in mapping.items():
                merge_annotations(db, drop, keep)
                db.execute("update playlist_tracks set media_file_id = ? where media_file_id = ?", (keep, drop))
        db.close()
    finally:
        compose("start", "navidrome")
    print(f"история и плейлисты перенесены, копия базы: {DB}.bak-dedupe-{stamp}")

    removed = []
    for c, k in plan:
        path = os.path.join(music, c[1])
        try:
            os.remove(path)
            removed.append({"deleted": c[1], "kept": k[1], "id": c[0], "kept_id": k[0]})
        except FileNotFoundError:
            pass
        folder = os.path.dirname(path)
        while folder.startswith(music + "/") and folder != music and not os.listdir(folder):
            os.rmdir(folder)  # пустые папки исполнителя и альбома после удаления
            folder = os.path.dirname(folder)
    with open(log, "w", encoding="utf-8") as f:
        json.dump(removed, f, ensure_ascii=False, indent=1)
    print(f"удалено файлов: {len(removed)}, журнал: {log}")

    for store in glob.glob("/opt/volna/data/server-*.db"):  # история VaginaWave — на оставшиеся копии
        vdb = sqlite3.connect(store)
        with vdb:
            vdb.executemany("update events set track_id = ? where track_id = ?", [(k, d) for d, k in mapping.items()])
        vdb.close()

    env = load_env(os.path.join(STACK, ".env"))
    salt = secrets.token_hex(8)
    token = hashlib.md5((env["ND_PASSWORD"] + salt).encode()).hexdigest()
    query = urllib.parse.urlencode({"u": env["ND_USER"], "t": token, "s": salt, "v": "1.16.1", "c": "dedupe", "f": "json"})
    for _ in range(30):  # Navidrome поднимается несколько секунд
        try:
            urllib.request.urlopen(f"{env.get('ND_URL', 'http://127.0.0.1:4533')}/rest/startScan?{query}", timeout=10)
            print("Navidrome пересканирует библиотеку")
            break
        except Exception:
            time.sleep(2)


if __name__ == "__main__":
    main()
