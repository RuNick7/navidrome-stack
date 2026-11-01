#!/usr/bin/env python3
"""Склеивает альбомы, которые Navidrome разбил на части из-за разных тегов.

Navidrome считает альбомом «название + исполнитель альбома + дата» (а если есть — MusicBrainz ID
альбома). Треки из плейлистов Spotify (explo) и Soulseek приходят поштучно: у мюзикла каждый
трек получает в исполнители альбома своего певца, у обычного альбома исполнитель бывает
записан по-разному («Max Korzh» и «Макс Корж»). Скрипт находит такие части одного релиза и
выставляет их файлам теги той части, что главнее:

* часть из Lidarr (у неё теги из MusicBrainz), иначе самая большая;
* исполнитель альбома у мюзиклов — состав («Original Broadway Cast of Hamilton»), у сборников
  и саундтреков без общего исполнителя — «Various Artists»;
* те же название альбома, дата и MusicBrainz ID альбома, что у главной части.

Файлы Lidarr не трогаются: их теги ведёт он сам. Отдельный трек, похожий на песню, которая
в альбоме Lidarr уже есть (другая версия или длительность), в альбом не вливается, а попадает в
data/tidy-albums-variants.tsv для ручной проверки — иначе внутри альбома появились бы повторы.
Старые значения тегов сохраняются в data/tidy-albums-<время>.json, по нему всё можно вернуть.

    python3 scripts/tidy-albums.py           # показать, что склеится
    docker run --rm -v /root/navidrome-stack:/stack -v /mnt/music:/mnt/music python:3.13-slim \\
        sh -c "pip install -q mutagen && python /stack/scripts/tidy-albums.py --apply"
"""
import argparse
import collections
import csv
import importlib.util
import json
import os
import re
import sqlite3
import time

STACK = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(STACK, "data", "navidrome", "navidrome.db")
CAST = re.compile(r"\b(original|broadway|west end|london|world premiere|revival|movie)\b.*\bcast\b|\bcast of\b", re.I)
# альбом с разными исполнителями: без общего исполнителя склеиваем только такие
COMPILATION = re.compile(r"soundtrack|motion picture|music from|\bost\b|original score|cast recording|\bcast\b|musical", re.I)
SPLIT = re.compile(r"\s*(?:,|;|&|•|\bfeat\.?|\bft\.?)\s*", re.I)

spec = importlib.util.spec_from_file_location("dedupe", os.path.join(STACK, "scripts", "dedupe-library.py"))
dedupe = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dedupe)
skeleton = dedupe.skeleton


def loose(title):
    """Название без скобок и хвостов « - …»: «Essentielles (Duo Version)» ≈ «Essentielles»."""
    return skeleton(re.split(r"\s[-–—]\s", re.sub(r"[\(\[].*?[\)\]]", " ", title or ""))[0])


def credited(row):
    return {skeleton(n) for field in (row["album_artist"], row["artist"]) for n in SPLIT.split(field or "") if skeleton(n)}


def plan_groups(db):
    db.row_factory = sqlite3.Row
    gone = {c[0] for c, _ in dedupe.find_copies(db)}  # копии, которые удалит dedupe-library, не трогаем
    rows = [r for r in db.execute(
        "select id, path, title, duration, album, album_artist, artist, album_id, date, mbz_album_id, mbz_album_artist_id "
        "from media_file where missing = 0") if r["id"] not in gone]
    by_name = collections.defaultdict(lambda: collections.defaultdict(list))
    for r in rows:
        by_name[skeleton(r["album"])][r["album_id"]].append(r)
    plans, variants = [], []
    for parts in by_name.values():
        if len(parts) < 2 or not next(iter(parts.values()))[0]["album"]:
            continue
        sets = [set.union(*(credited(r) for r in tracks)) for tracks in parts.values()]
        folders = {"/".join(t["path"].split("/")[:2]) for tracks in parts.values() for t in tracks}
        # мюзикл или саундтрек, скачанный поштучно (explo, Soulseek): части одного сборника
        same_import = not any(f.startswith("lidarr/") for f in folders) and bool(
            COMPILATION.search(next(iter(parts.values()))[0]["album"]))
        common = set.intersection(*sets)
        if not (common or same_import):
            continue  # тёзки: разные альбомы с одним названием
        lidarr = [p for p in parts.values() if p[0]["path"].startswith("lidarr/")]
        anchor = max(lidarr or parts.values(), key=len)
        cast = collections.Counter(n.strip() for tracks in parts.values() for t in tracks
                                   for n in SPLIT.split(f"{t['album_artist']},{t['artist']}") if CAST.search(n))
        album_artist = (cast.most_common(1)[0][0] if cast
                        else anchor[0]["album_artist"] if common else "Various Artists")
        target = {"album": anchor[0]["album"], "albumartist": album_artist, "date": anchor[0]["date"] or "",
                  "musicbrainz_albumid": anchor[0]["mbz_album_id"] or "",
                  "musicbrainz_albumartistid": anchor[0]["mbz_album_artist_id"] or ""}
        change = [t for tracks in parts.values() for t in tracks
                  if not t["path"].startswith("lidarr/") and (t["album_artist"] != album_artist or tracks is not anchor)]
        if lidarr:  # в альбом Lidarr не вливаем другие версии уже имеющихся в нём песен
            own = [(loose(a["title"]), a["duration"] or 0, a) for a in anchor]
            alike = lambda t: next((a for title, d, a in own if title == loose(t["title"]) and abs(d - (t["duration"] or 0)) <= 15), None)
            variants += [(t, alike(t)) for t in change if alike(t)]
            change = [t for t in change if not alike(t)]
        if change:
            plans.append((target, change, len(parts)))
    return plans, variants


def retag(path, target):
    import mutagen  # только для --apply: ставится в контейнере

    audio = mutagen.File(path, easy=True)
    if audio is None:
        raise ValueError("не аудиофайл")
    old = {k: (audio.get(k) or [""])[0] for k in target}
    for key, value in target.items():
        if value:
            audio[key] = [value]
        elif key in audio:
            del audio[key]
    audio.save()
    return old


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--apply", action="store_true", help="записать теги (нужен mutagen)")
    args = p.parse_args()
    db = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    music = db.execute("select path from library where id = 1").fetchone()[0]
    plans, variants = plan_groups(db)
    print(f"альбомов склеится: {len(plans)}, файлов поменяет: {sum(len(c) for _, c, _ in plans)}")
    report = os.path.join(STACK, "data", "tidy-albums-variants.tsv")
    with open(report, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f, delimiter="\t")
        w.writerow(["отдельный трек", "длительность", "похож на трек альбома", "длительность"])
        for t, a in variants:
            w.writerow([t["path"], round(t["duration"] or 0), f"{a['title']} ({a['path']})", round(a["duration"] or 0)])
    print(f"похожих на уже имеющиеся песни (не вливаю, проверьте вручную): {len(variants)} — {report}")
    for target, change, parts in sorted(plans, key=lambda x: -x[2]):
        print(f"  {parts} частей → «{target['album'][:50]}» — {target['albumartist'][:40]} ({len(change)} файлов)")
    if not args.apply:
        return
    backup, failed = [], 0
    for target, change, _ in plans:
        for t in change:
            path = os.path.join(music, t["path"])
            try:
                backup.append({"path": t["path"], "old": retag(path, target), "new": target})
            except Exception as e:  # noqa: BLE001
                failed += 1
                print(f"  не записал {t['path']}: {e}")
    log = os.path.join(STACK, "data", f"tidy-albums-{time.strftime('%Y%m%d-%H%M%S')}.json")
    with open(log, "w", encoding="utf-8") as f:
        json.dump(backup, f, ensure_ascii=False, indent=1)
    print(f"записано: {len(backup)}, ошибок: {failed}; старые теги: {log}")


if __name__ == "__main__":
    main()
