"""Soularr с доработками navidrome-stack. В образе лежит как /app/soularr.py, оригинал — /app/soularr_core.py.

Что меняет по сравнению с Soularr 1.2.2:

* Запрещённые имена. Сервер Soulseek рассылает клиентам список запрещённых фраз, и на поиск
  «The Beatles Abbey Road» не отвечает никто. Тот же поиск со звёздочкой вместо первой буквы
  («*eatles Abbey Road») находит тысячи файлов. Если поиск ничего не дал, пробуем название без
  скобок и затем имя со звёздочками; исполнителей, которым помогли звёздочки, запоминаем и дальше
  ищем сразу так.
* Очередь по приоритету (search_type = priority) вместо прохода по алфавиту: сначала альбомы и EP
  исполнителей, добавленных за последние две недели, затем все альбомы, EP и только потом синглы.
  Внутри — сначала то, что ещё не искали, затем популярное (оценки MusicBrainz) и новое.
* Повтор с паузой. Что не нашлось, снова ищем через 7 дней, потом через 14, 28 и не реже раза
  в 60 дней. Что нашлось, но не скачалось (пир пропал, очередь, не тот трек-лист), — через день,
  потом через 2, 4… до 30 дней: в следующий раз ответит другой пир.
* Синглы-дубли. Сингл, все песни которого есть на альбоме или EP того же исполнителя в Lidarr,
  не ищем: песня придёт с альбомом.

Состояние — /data/soularr-plus.json и /data/soularr-plus-tracks.json.

    python /app/soularr.py --plan    # показать, что возьмёт следующий проход, ничего не качая
"""
import configparser
import json
import os
import re
import sys
import time
import unicodedata
from datetime import datetime, timezone

import requests

import soularr_core as core

FRESH_DAYS = 14  # исполнители, добавленные недавно, идут первыми
TRACKS_TTL = 3 * 86400  # как часто обновлять список песен с альбомов исполнителя
TRACKS_REFRESH_PER_RUN = 150
NOTFOUND_BASE, NOTFOUND_MAX = 7 * 86400, 60 * 86400
NOGRAB_BASE, NOGRAB_MAX = 86400, 30 * 86400
PENDING = 6 * 3600  # если проход оборвался, не брать тот же альбом раньше
ARTICLES = {"the", "a", "an"}

_orig_get_records = core.get_records
_orig_search_for_album = core.search_for_album
_orig_search_and_queue = core.search_and_queue
_orig_grab_most_wanted = core.grab_most_wanted
_batch = {}


# --- состояние --------------------------------------------------------------------------------


def _path(name):
    base = os.path.dirname(core.current_page_file_path) if core.current_page_file_path else "/data"
    return os.path.join(base, name)


def _load(name, default):
    try:
        with open(_path(name), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def _save(name, data):
    tmp = _path(name) + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
    os.replace(tmp, _path(name))


def load_state():
    st = _load("soularr-plus.json", {})
    st.setdefault("albums", {})
    st.setdefault("banned", {})
    return st


# --- текст --------------------------------------------------------------------------------------


def norm(s):
    """Название песни без скобок, «- Radio Edit», регистра и пунктуации."""
    s = unicodedata.normalize("NFKD", s.casefold())
    s = re.sub(r"[\(\[\{].*?[\)\]\}]", " ", s)
    s = re.sub(r"\s[-–—]\s.*$", " ", s)
    return "".join(c for c in s if c.isalnum())


def strip_brackets(title):
    return " ".join(re.sub(r"[\(\[\{].*?[\)\]\}]", " ", title).split())


def wildcard(text, only=None):
    """«The Beatles» -> «*eatles»: звёздочка вместо первой буквы обходит запрещённые фразы Soulseek.

    only — менять только эти слова (имя исполнителя внутри названия альбома)."""
    words = text.split()
    if only is None:
        words = [w for w in words if w.casefold() not in ARTICLES] or words
    out = []
    for w in words:
        if (only is None or w.casefold() in only) and sum(c.isalnum() for c in w) >= 3 and w[0].isalnum():
            out.append("*" + w[1:])
        else:
            out.append(w)
    return " ".join(out)


def query_variants(artist, title, banned):
    """Запросы по очереди: как есть, без скобок, со звёздочками в имени, во всём, одно название."""
    short = strip_brackets(title) or title.strip("[](){} ") or title
    wild = wildcard(artist)
    in_title = wildcard(short, only={w.casefold() for w in artist.split()})
    variants = [] if banned else [(artist, title), (artist, short)]
    variants += [(wild, in_title), (wild, wildcard(short))]
    if len(norm(short)) >= 6:  # имя в файлах записано иначе: «Ye» и «Kanye West», «Земфира» и «Zemfira»
        variants.append(("", short))
    out, seen = [], set()
    for a, t in variants:
        q = " ".join(f"{a} {t}".split()).casefold()
        if q not in seen:
            seen.add(q)
            out.append((a, t))
    return out


# --- поиск --------------------------------------------------------------------------------------


def search_for_album(album):
    artist, title = album["artist"]["artistName"], album["title"]
    st = load_state()
    banned = str(album["artistId"]) in st["banned"]

    for i, (a, t) in enumerate(query_variants(artist, title, banned)):
        if i:
            core.logger.info(f"Nothing for the previous query, trying: {a} {t}".replace("  ", " "))
            time.sleep(max(core.minimum_search_interval or 0, 3))
        query_album = {**album, "title": t, "artist": {**album["artist"], "artistName": a}}
        if _orig_search_for_album(query_album):
            if a and a != artist and not banned:
                st = load_state()
                st["banned"][str(album["artistId"])] = {"name": artist, "t": int(time.time())}
                _save("soularr-plus.json", st)
                core.logger.info(f"Artist name is filtered on Soulseek, will search with wildcards: {artist} -> {a}")
            return True
    return False


def search_and_queue(albums):
    grab_list, failed_search, failed_grab = _orig_search_and_queue(albums)
    # failed_grab дополняется дальше, в monitor_downloads (пир пропал, таймаут): держим ссылку
    _batch.update(albums=albums, failed_search=failed_search, failed_grab=failed_grab)
    return grab_list, failed_search, failed_grab


def grab_most_wanted(albums):
    _batch.clear()
    try:
        return _orig_grab_most_wanted(albums)
    finally:
        record_outcomes()


def _backoff(base, cap, tries):
    return min(base * 2 ** max(tries - 1, 0), cap)


def record_outcomes():
    if not _batch:
        return
    now = time.time()
    st = load_state()
    nosearch = {a["id"] for a in _batch["failed_search"]}
    nograb = {a["id"] for a in _batch["failed_grab"] if isinstance(a, dict) and "id" in a}
    for a in _batch["albums"]:
        s = st["albums"].setdefault(str(a["id"]), {"n": 1, "t": int(now)})
        if a["id"] in nosearch:
            s["last"], s["next"] = "notfound", int(now + _backoff(NOTFOUND_BASE, NOTFOUND_MAX, s["n"]))
        elif a["id"] in nograb:
            s["last"], s["next"] = "nograb", int(now + _backoff(NOGRAB_BASE, NOGRAB_MAX, s["n"]))
        else:  # скачано и отдано Lidarr; если импорт не прошёл, через сутки попробуем ещё
            s["last"], s["next"] = "grabbed", int(now + NOGRAB_BASE)
    _save("soularr-plus.json", st)


# --- очередь ------------------------------------------------------------------------------------


def _lidarr_get(path, **params):
    r = requests.get(
        core.lidarr_host_url.rstrip("/") + "/api/v1/" + path,
        params=params,
        headers={"X-Api-Key": core.lidarr_api_key},
        timeout=600,
    )
    r.raise_for_status()
    return r.json()


def _all_missing():
    records, page = [], 1
    while True:
        r = _lidarr_get("wanted/missing", page=page, pageSize=5000, sortKey="albums.title",
                        sortDirection="ascending", includeArtist="true", monitored="true")
        records.extend(r["records"])
        if page * 5000 >= r["totalRecords"]:
            return records
        page += 1


def _queued_ids():
    try:
        return {q["albumId"] for q in _lidarr_get("queue", pageSize=1000).get("records", []) if "albumId" in q}
    except Exception:
        return set()


def _blacklisted(title):
    words = core.config.get("Search Settings", "title_blacklist", fallback="").lower().split(",")
    return any(w and w in title.lower() for w in words)


def album_titles(artist_ids, budget=TRACKS_REFRESH_PER_RUN):
    """Песни с альбомов и EP каждого исполнителя (нормализованные), с кешем на TRACKS_TTL."""
    cache = _load("soularr-plus-tracks.json", {})
    now, fetched = time.time(), 0
    for aid in artist_ids:
        c = cache.get(str(aid))
        if c and now - c["t"] < TRACKS_TTL:
            continue
        if fetched >= budget:
            continue
        try:
            kinds = {al["id"] for al in _lidarr_get("album", artistId=aid) if al.get("albumType") in ("Album", "EP")}
            titles = {norm(t["title"]) for t in _lidarr_get("track", artistId=aid) if t.get("albumId") in kinds}
        except Exception as e:
            core.logger.warning(f"Could not read tracks of artist {aid}: {e}")
            continue
        cache[str(aid)] = {"t": int(now), "titles": sorted(x for x in titles if x)}
        fetched += 1
    if fetched:
        _save("soularr-plus-tracks.json", cache)
    return {int(k): set(v["titles"]) for k, v in cache.items()}


def single_covered(album, titles):
    """Все стороны сингла («A / B») уже есть на альбомах исполнителя."""
    have = titles.get(album["artistId"])
    if have is None:
        return None  # список песен ещё не загружен
    parts = [norm(p) for p in album["title"].split(" / ")]
    return all(p and p in have for p in parts)


def _added(artist):
    try:
        return datetime.fromisoformat(artist.get("added", "").replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0


TIER = {"Album": 1, "EP": 2, "Single": 4}


def prioritized(records, st, now):
    """Отсортированные кандидаты и сводка: что пропущено и почему."""
    queued = _queued_ids()
    deny = core.load_failed_import_denylist(core.failed_import_denylist_file_path) if core.failed_import_denylist else {}
    stats = {"waiting": 0, "covered": 0, "denied": 0, "no_tracks_yet": 0}
    pool = []
    for a in records:
        s = st["albums"].get(str(a["id"]))
        if a["id"] in queued:
            continue
        if str(a["id"]) in deny or _blacklisted(a["title"]):
            stats["denied"] += 1
            continue
        if s and s.get("next", 0) > now:
            stats["waiting"] += 1
            continue
        pool.append(a)

    singles = {a["artistId"] for a in pool if a.get("albumType") == "Single"}
    titles = album_titles(sorted(singles)) if singles else {}
    fresh_after = now - FRESH_DAYS * 86400
    ranked = []
    for a in pool:
        kind = a.get("albumType")
        if kind == "Single":
            covered = single_covered(a, titles)
            if covered is None:
                stats["no_tracks_yet"] += 1
                continue
            if covered:
                stats["covered"] += 1
                continue
        tier = TIER.get(kind, 5)
        if _added(a["artist"]) >= fresh_after and tier < 4:
            tier = 0
        elif _added(a["artist"]) >= fresh_after and tier == 4:
            tier = 3
        tries = (st["albums"].get(str(a["id"])) or {}).get("n", 0)
        votes = (a.get("ratings") or {}).get("votes", 0)
        year = int((a.get("releaseDate") or "0")[:4] or 0)
        ranked.append(((tier, tries, -votes, -year, a["title"].casefold()), a))
    ranked.sort(key=lambda x: x[0])
    return [a for _, a in ranked], stats


def get_records(missing):
    if core.search_type != "priority" or not missing:
        return _orig_get_records(missing)
    now = time.time()
    st = load_state()
    records = _all_missing()
    ranked, stats = prioritized(records, st, now)
    chosen = ranked[: core.page_size]
    kinds = {}
    for a in ranked:
        kinds[a.get("albumType")] = kinds.get(a.get("albumType"), 0) + 1
    core.logger.info(
        f"Priority queue: missing {len(records)}, ready {len(ranked)} {kinds}, "
        f"waiting for retry {stats['waiting']}, singles already on albums {stats['covered']}, "
        f"singles not checked yet {stats['no_tracks_yet']}, denylisted {stats['denied']}"
    )
    missing_ids = {str(a["id"]) for a in records}
    st["albums"] = {k: v for k, v in st["albums"].items() if k in missing_ids}  # скачанное забываем
    for a in chosen:
        s = st["albums"].setdefault(str(a["id"]), {"n": 0})
        s.update(n=s["n"] + 1, t=int(now), next=int(now + PENDING))
        core.logger.info(f"Picked: {a['artist']['artistName']} - {a['title']} ({a.get('albumType')}, try {s['n']})")
    _save("soularr-plus.json", st)
    return chosen


core.get_records = get_records
core.search_for_album = search_for_album
core.search_and_queue = search_and_queue
core.grab_most_wanted = grab_most_wanted


def plan():
    """Что возьмут следующие проходы, без поиска и загрузок."""
    data = "/data" if core.is_docker() else os.getcwd()
    config = configparser.RawConfigParser()
    config.read(os.path.join(data, "config.ini"))
    core.config = config
    core.current_page_file_path = os.path.join(data, ".current_page.txt")
    core.lidarr_host_url = config["Lidarr"]["host_url"]
    core.lidarr_api_key = config["Lidarr"]["api_key"]
    core.failed_import_denylist = config.getboolean("Search Settings", "failed_import_denylist", fallback=True)
    core.failed_import_denylist_file_path = os.path.join(data, "failed_imports.json")
    size = config.getint("Search Settings", "number_of_albums_to_grab", fallback=10)
    st = load_state()
    records = _all_missing()
    ranked, stats = prioritized(records, st, time.time())
    kinds = {}
    for a in ranked:
        kinds[a.get("albumType")] = kinds.get(a.get("albumType"), 0) + 1
    print(f"missing {len(records)}, ready {len(ranked)} {kinds}, {stats}")
    print(f"known filtered artists: {', '.join(v['name'] for v in st['banned'].values()) or '-'}")
    for i, a in enumerate(ranked[: size * 3]):
        if i % size == 0:
            print(f"-- pass {i // size + 1}")
        print(f"  {a.get('albumType'):6} {a['artist']['artistName']} - {a['title']}  [{(a.get('releaseDate') or '')[:4]}]")


if __name__ == "__main__":
    if "--plan" in sys.argv:
        plan()
    else:
        core.main()
