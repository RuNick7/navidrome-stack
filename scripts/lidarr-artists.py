#!/usr/bin/env python3
"""Ставит в Lidarr дискографии исполнителей, у которых в библиотеке 3 и больше трека.

Navidrome знает, кто есть в библиотеке, Lidarr — какие у этих исполнителей альбомы.
Скрипт считает треки каждого исполнителя (совместные треки «A;B» засчитываются обоим) и
для тех, у кого их не меньше --min-tracks:

* добавляет исполнителя в Lidarr, если его там нет: по MusicBrainz ID из тегов, а без него —
  по имени (с точностью до пунктуации и транслита: Zemfira = Земфира), и только если такой
  исполнитель в MusicBrainz один, чтобы не скачать дискографию тёзки;
* включает слежение за исполнителем, за всеми его альбомами и за новыми релизами.

Качает Soularr: он берёт альбомы из «Missing» в Lidarr, по 10 каждые 5 минут. Какие
альбомы считаются (студийные, без EP и сборников), задаёт профиль метаданных Lidarr.

    python3 scripts/lidarr-artists.py              # показать, что изменится
    python3 scripts/lidarr-artists.py --apply      # применить; новых — не больше 50 за запуск

Для автоматической работы (crontab -e):
    40 4 * * * cd /root/navidrome-stack && python3 scripts/lidarr-artists.py --apply >> data/lidarr-artists.log 2>&1
"""
import argparse
import collections
import hashlib
import json
import os
import secrets
import sys
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request

STACK = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE = os.path.join(STACK, "data", "lidarr-artists.json")  # кого Lidarr не нашёл: не искать каждую ночь
RETRY_DAYS = 30
SKIP = {"various artists", "various", "va", "unknown artist", "[unknown artist]"}
PAGE = 500


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


def key(name):
    return unicodedata.normalize("NFKC", name).casefold().strip()


CYRILLIC = dict(zip("абвгдеёжзийклмнопрстуфхцчшщъыьэюя", [
    "a", "b", "v", "g", "d", "e", "e", "zh", "z", "i", "i", "k", "l", "m", "n", "o", "p", "r", "s", "t", "u",
    "f", "h", "ts", "ch", "sh", "sch", "", "i", "", "e", "yu", "ya"]))


def skeleton(name):
    """Имя без пунктуации, пробелов и регистра, кириллица латиницей: «alt‐J» = «alt-J», «Земфира» = «Zemfira»."""
    s = "".join(CYRILLIC.get(c, c) for c in key(name))
    s = "".join(c for c in unicodedata.normalize("NFKD", s) if c.isalnum())
    return s.replace("kh", "h").replace("x", "ks").replace("y", "i").replace("j", "i")


class Api:
    def __init__(self, url, header, key, prefix):
        self.base, self.h = url.rstrip("/") + prefix, {header: key}

    def __call__(self, method, path, body=None, **params):
        q = f"?{urllib.parse.urlencode(params)}" if params else ""
        return http(method, f"{self.base}/{path}{q}", self.h, body)


class Navidrome:
    def __init__(self, url, user, password):
        self.base, self.user, self.password = url.rstrip("/"), user, password

    def call(self, endpoint, **params):
        salt = secrets.token_hex(8)
        token = hashlib.md5((self.password + salt).encode()).hexdigest()
        q = {"u": self.user, "t": token, "s": salt, "v": "1.16.1", "c": "lidarr-artists", "f": "json", **params}
        body = http("GET", f"{self.base}/rest/{endpoint}?{urllib.parse.urlencode(q)}", {})["subsonic-response"]
        if body.get("status") != "ok":
            raise RuntimeError(body.get("error", {}).get("message", endpoint))
        return body


def library_artists(nd):
    """Сколько треков у каждого исполнителя, его имя как в тегах и MusicBrainz ID, если есть."""
    counts, names, offset = collections.Counter(), {}, 0
    while True:
        songs = nd.call("search3", query="", songCount=PAGE, songOffset=offset, artistCount=0, albumCount=0)
        songs = songs.get("searchResult3", {}).get("song", [])
        for s in songs:
            credited = {n.strip() for a in (s.get("artists") or [{"name": s.get("artist", "")}]) for n in a["name"].split(";")}
            for n in credited:
                if n and key(n) not in SKIP:
                    counts[key(n)] += 1
                    names.setdefault(key(n), n)
        if len(songs) < PAGE:
            break
        offset += PAGE
    mbids = {}
    for index in nd.call("getArtists").get("artists", {}).get("index", []):
        for a in index.get("artist", []):
            if a.get("musicBrainzId"):
                mbids[key(a["name"])] = a["musicBrainzId"]
    return counts, names, mbids


def lookup(lidarr, name, mbid):
    """Исполнитель в базе MusicBrainz: по ID из тегов, а без него — единственный с таким же именем.

    Возвращает (исполнитель или None, почему не взяли)."""
    if mbid:
        found = lidarr("GET", "artist/lookup", term=f"lidarr:{mbid}") or []
        return (found[0], "") if found else (None, "нет в MusicBrainz")
    target = skeleton(name)
    same = [c for c in lidarr("GET", "artist/lookup", term=name) or []
            if target and target in (skeleton(c.get("artistName", "")), skeleton(c.get("sortName") or ""))]
    if len(same) > 1:
        return None, "несколько исполнителей с таким именем"
    return (same[0], "") if same else (None, "нет в MusicBrainz")


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--apply", action="store_true", help="применить (без него только показать)")
    p.add_argument("--min-tracks", type=int, default=3, help="сколько треков в библиотеке нужно исполнителю")
    p.add_argument("--limit", type=int, default=50, help="сколько новых исполнителей искать и добавлять за запуск")
    args = p.parse_args()

    env = {**load_env(os.path.join(STACK, ".env")), **os.environ}
    for k in ("ND_USER", "ND_PASSWORD", "LIDARR_API_KEY"):
        if not env.get(k):
            sys.exit(f"Нет {k} в .env")
    nd = Navidrome(env.get("ND_URL", "http://127.0.0.1:4533"), env["ND_USER"], env["ND_PASSWORD"])
    lidarr = Api(env.get("LIDARR_URL", "http://127.0.0.1:8686"), "X-Api-Key", env["LIDARR_API_KEY"], "/api/v1")
    state = json.load(open(STATE)) if os.path.exists(STATE) else {}
    now = time.time()
    stamp = time.strftime("%Y-%m-%d %H:%M")

    counts, names, mbids = library_artists(nd)
    wanted = sorted((k for k, n in counts.items() if n >= args.min_tracks), key=lambda k: -counts[k])
    artists = lidarr("GET", "artist")
    by_id = {a["id"]: a for a in artists}
    by_mbid = {a["foreignArtistId"]: a for a in artists}
    by_name = {key(a["artistName"]): a for a in artists}
    by_skeleton = {}  # «Max Korzh» в тегах и «Макс Корж» в Lidarr; при совпадении у двух — никого
    for a in artists:
        sk = skeleton(a["artistName"])
        by_skeleton[sk] = None if sk in by_skeleton else a
    root = lidarr("GET", "rootfolder")[0]

    known, new = {}, []
    for k in wanted:
        artist = by_mbid.get(mbids.get(k, "")) or by_name.get(k) or by_skeleton.get(skeleton(k))
        if artist is not None:
            known[artist["id"]] = artist
        elif now - state.get(k, 0) > RETRY_DAYS * 86400:
            new.append(k)
    print(f"{stamp} исполнителей с {args.min_tracks}+ треками: {len(wanted)}, уже в Lidarr: {len(known)}, "
          f"нет в Lidarr: {len(new)} (за запуск ищу {min(len(new), args.limit)})")

    # новых — найти в MusicBrainz и добавить со всеми альбомами
    added, missing = [], []
    for k in new[: args.limit]:
        name = names[k]
        try:
            found, why = lookup(lidarr, name, mbids.get(k))
        except urllib.error.URLError as e:
            print(f"  {name}: поиск не удался ({e})")
            continue
        if found is not None and found.get("id") in by_id:  # уже в Lidarr, но записан иначе
            known[found["id"]] = by_id[found["id"]]
            continue
        if found is None:
            missing.append(f"{name} — {why}")
            state[k] = now
            continue
        if args.apply:
            found.update(
                qualityProfileId=root.get("defaultQualityProfileId") or 1,
                metadataProfileId=root.get("defaultMetadataProfileId") or 1,
                rootFolderPath=root["path"],
                monitored=True,
                monitorNewItems="all",
                addOptions={"monitor": "all", "searchForMissingAlbums": False},
            )
            try:
                lidarr("POST", "artist", found)
            except urllib.error.HTTPError as e:
                print(f"  {name}: Lidarr не добавил ({e.code} {e.read()[:200].decode(errors='replace')})")
                continue
        added.append(f"{name} ({counts[k]})")

    # кто уже есть в Lidarr: следить за ним, его новыми релизами и всеми альбомами
    to_follow = [a for a in known.values() if not a["monitored"] or a.get("monitorNewItems") != "all"]
    albums = [al["id"] for al in lidarr("GET", "album") if al["artistId"] in known and not al["monitored"]]
    print(f"  {'включено' if args.apply else 'включу'} слежение: исполнителей {len(to_follow)}, альбомов {len(albums)}")
    if args.apply:
        for artist in to_follow:
            artist.update(monitored=True, monitorNewItems="all")
            lidarr("PUT", f"artist/{artist['id']}", artist)
        for i in range(0, len(albums), 200):
            lidarr("PUT", "album/monitor", {"albumIds": albums[i : i + 200], "monitored": True})
    print(f"  {'добавлено' if args.apply else 'добавлю'}: {len(added)}" + (f" — {', '.join(added)}" if added else ""))
    if missing:
        print(f"  не добавлены ({len(missing)}): {'; '.join(missing)}")
    if args.apply:
        with open(STATE, "w", encoding="utf-8") as f:
            json.dump(state, f, ensure_ascii=False)


if __name__ == "__main__":
    main()
