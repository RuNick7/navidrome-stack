"""Soularr с доработками navidrome-stack. В образе лежит как /app/soularr.py, оригинал — /app/soularr_core.py.

Что меняет по сравнению с Soularr 1.2.2:

* Запрещённые имена. Сервер Soulseek рассылает клиентам список запрещённых фраз, и на поиск
  «The Beatles Abbey Road» не отвечает никто. Тот же поиск со звёздочкой вместо первой буквы
  («*eatles Abbey Road») находит тысячи файлов. Если поиск ничего не дал, пробуем название без
  скобок и затем имя со звёздочками; исполнителей, которым помогли звёздочки, запоминаем и дальше
  ищем сразу так.
* Очередь по приоритету (search_type = priority) вместо прохода по алфавиту. Исполнители идут по
  кругу: сначала лучший релиз каждого, потом второй и так далее, чтобы никто с тысячами релизов не
  занимал все проходы. У тех, кого попросили в VaginaWave (тег vaginawave в Lidarr), вперёд идут
  пять главных релизов. В каждом
  проходе 4 места — синглам. У исполнителя первым идёт то, что ещё не искали, альбомы раньше EP,
  затем популярное (оценки MusicBrainz) и новое.
* Альбом из стоп-листа «не импортировался» через 14 дней ищется снова.
* Повтор с паузой. Что не нашлось, снова ищем через 7 дней, потом через 14, 28 и не реже раза
  в 60 дней. Что нашлось, но не скачалось (пир пропал, очередь, не тот трек-лист), — через день,
  потом через 2, 4… до 30 дней: в следующий раз ответит другой пир.
* Выбор пира. Из нашедших альбом сначала берутся пиры со свободным слотом, короткой очередью и
  быстрой отдачей (их ответы на поиск запоминаются). Если пир не отдал файлы, его до конца прохода
  не берём и тут же пробуем следующего (до двух раз), а не ждём следующего дня.
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
import slskd_api.apis.searches as slskd_searches

import soularr_core as core

# Исполнители, которых попросили кнопкой «Скачать дискографию» в VaginaWave (тег Lidarr): их главные
# релизы идут первыми. Добавленные ночным lidarr-artists.py по библиотеке — по кругу со всеми.
REQUEST_TAG = "vaginawave"
REQUESTED_TOP = 5
SINGLES_PER_PASS = 4  # мест для синглов в каждом проходе: иначе до них очередь не доходит
DENY_DAYS = 14  # через столько неудачный импорт снова пробуется
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
_orig_find_download = core.find_download
_orig_cancel_and_delete = core.cancel_and_delete
_orig_search_responses = slskd_searches.SearchesApi.search_responses
_batch = {}
_peers = {}  # пользователь → (есть свободный слот, длина очереди, скорость отдачи) из ответов поиска
RETRY_PEERS = 2  # сколько раз за проход пробовать другого пира, если этот не отдал файлы


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


def search_responses(self, id):
    """Ответы поиска как есть, а по пути запоминаем, у кого свободный слот, очередь и скорость."""
    rows = _orig_search_responses(self, id)
    for r in rows or []:
        _peers[r.get("username")] = (bool(r.get("hasFreeUploadSlot")), int(r.get("queueLength") or 0),
                                     int(r.get("uploadSpeed") or 0))
    return rows


def _peer_rank(user):
    free, queue, speed = _peers.get(user, (False, 10 ** 9, 0))
    return (not free, queue, -speed)


def find_download(album, grab_list):
    """Пиров пробуем не в порядке ответа на поиск, а сначала со свободным слотом, короткой очередью
    и быстрой отдачей: чаще всего загрузка срывалась на пирах, которые не брали её в очередь."""
    results = core.search_cache.get(album["id"])
    if results:
        core.search_cache[album["id"]] = dict(sorted(results.items(), key=lambda kv: _peer_rank(kv[0])))
    return _orig_find_download(album, grab_list)


def cancel_and_delete(files):
    """Загрузка сорвалась: этого пира до конца прохода больше не берём (broken_user в Soularr)."""
    for f in files:
        if f.get("username") and f["username"] not in core.broken_user:
            core.broken_user.append(f["username"])
    return _orig_cancel_and_delete(files)


def search_and_queue(albums):
    grab_list, failed_search, failed_grab = _orig_search_and_queue(albums)
    # failed_grab дополняется дальше, в monitor_downloads (пир пропал, таймаут): держим ссылку.
    # Что не совпало ни с одной папкой уже сейчас — не повод пробовать другого пира.
    _batch.update(albums=albums, failed_search=failed_search, failed_grab=failed_grab,
                  nomatch={a["id"] for a in failed_grab})
    return grab_list, failed_search, failed_grab


def retry_other_peers():
    """Пир не отдал файлы — тут же пробуем следующего из того же поиска, а не через сутки."""
    if not _batch:
        return
    originals = {a["id"]: a for a in _batch["albums"]}
    for _ in range(RETRY_PEERS):
        failed = [a for a in _batch["failed_grab"] if isinstance(a, dict) and "id" in a]
        transfer = {a["id"] for a in failed if a["id"] not in _batch["nomatch"]}
        grab_list = {}
        for album_id in transfer:
            if album_id in originals and album_id in core.search_cache:
                core.find_download(originals[album_id], grab_list)
        if not grab_list:
            return
        retried = set(grab_list)
        core.logger.info(f"Peers failed to upload, retrying with other peers: {len(retried)} albums")
        again = []
        core.monitor_downloads(grab_list, again)
        _batch["failed_grab"] = [a for a in failed if a["id"] not in retried] + again


def grab_most_wanted(albums):
    _batch.clear()
    try:
        failed = _orig_grab_most_wanted(albums)
        retry_other_peers()
        return failed
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


TIER = {"Album": 1, "EP": 2, "Single": 4}


def _sort_key(a, st):
    tries = (st["albums"].get(str(a["id"])) or {}).get("n", 0)
    votes = (a.get("ratings") or {}).get("votes", 0)
    year = int((a.get("releaseDate") or "0")[:4] or 0)
    return tries, TIER.get(a.get("albumType"), 5), -votes, -year, a["title"].casefold()


def round_robin(items, st, requested_tag=None):
    """Очередь по кругу: у каждого исполнителя сначала лучший релиз, потом второй и так далее.

    Иначе исполнитель с тысячами релизов (Бах, Моцарт) занимает все проходы, и до остальных
    очередь не доходит. У тех, кого попросили в VaginaWave, вперёд идут REQUESTED_TOP главных релизов."""
    by_artist = {}
    for a in items:
        by_artist.setdefault(a["artistId"], []).append(a)
    ranked = []
    for albums in by_artist.values():
        albums.sort(key=lambda a: _sort_key(a, st))
        requested = requested_tag is not None and requested_tag in (albums[0]["artist"].get("tags") or [])
        for rank, a in enumerate(albums):
            ranked.append(((0 if requested and rank < REQUESTED_TOP else 1, rank, *_sort_key(a, st)), a))
    ranked.sort(key=lambda x: x[0])
    return [a for _, a in ranked]


def interleave(main, singles, size):
    """Проходы по size: в каждом SINGLES_PER_PASS синглов, остальное — альбомы и EP; кончилось одно — добираем другим."""
    out, i, j = [], 0, 0
    per_single = min(SINGLES_PER_PASS, size)
    while i < len(main) or j < len(singles):
        page_main, page_single = main[i:i + size - per_single], singles[j:j + per_single]
        i, j = i + len(page_main), j + len(page_single)
        short = size - len(page_main) - len(page_single)
        if short and i < len(main):
            page_main += main[i:i + short]
            i += short
        elif short and j < len(singles):
            page_single += singles[j:j + short]
            j += short
        out += page_main + page_single
    return out


def prioritized(records, st, now, size=None):
    """Отсортированные кандидаты и сводка: что пропущено и почему."""
    size = size or core.page_size or 15
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
    main, single_pool = [], []
    for a in pool:
        if a.get("albumType") == "Single":
            covered = single_covered(a, titles)
            if covered is None:
                stats["no_tracks_yet"] += 1
            elif covered:
                stats["covered"] += 1
            else:
                single_pool.append(a)
        else:
            main.append(a)
    tag = _request_tag()
    return interleave(round_robin(main, st, tag), round_robin(single_pool, st, tag), size), stats


def _request_tag():
    try:
        return next((t["id"] for t in _lidarr_get("tag") if t.get("label") == REQUEST_TAG), None)
    except Exception:
        return None


def expire_denylist(now):
    """Неудачный импорт — не приговор: через DENY_DAYS альбом снова ищется (другой пир, другое издание)."""
    if not core.failed_import_denylist:
        return 0
    path = core.failed_import_denylist_file_path
    deny = core.load_failed_import_denylist(path)
    keep = {}
    for key, entry in deny.items():
        try:
            failed = datetime.strptime(entry.get("failed_at", ""), "%Y-%m-%dT%H:%M:%S").timestamp()
        except ValueError:
            failed = now
        if now - failed < DENY_DAYS * 86400:
            keep[key] = entry
    if len(keep) != len(deny):
        core.save_failed_import_denylist(path, keep)
    return len(deny) - len(keep)


def get_records(missing):
    if core.search_type != "priority" or not missing:
        return _orig_get_records(missing)
    now = time.time()
    expired = expire_denylist(now)
    if expired:
        core.logger.info(f"Failed imports older than {DENY_DAYS} days will be searched again: {expired}")
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
core.find_download = find_download
core.cancel_and_delete = cancel_and_delete
slskd_searches.SearchesApi.search_responses = search_responses


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
    ranked, stats = prioritized(records, st, time.time(), size)
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
