#!/usr/bin/env python3
"""Ставит в Lidarr дискографии исполнителей, у которых в библиотеке 3 и больше трека.

Navidrome знает, кто есть в библиотеке, Lidarr — какие у этих исполнителей альбомы.
Скрипт считает треки каждого исполнителя (совместные треки «A;B» засчитываются обоим) и
для тех, у кого их не меньше --min-tracks:

* добавляет исполнителя в Lidarr, если его там нет: по MusicBrainz ID из тегов, а без него —
  по имени — поиском Lidarr (он знает псевдонимы: Splean = Сплин), с точностью до пунктуации,
  транслита, порядка слов и артикля (Cure = The Cure, Béla Bartók = Bartók Béla); из записей вроде
  «Moby Featuring Mylene Farmer», «David Bowie/Tony Visconti», «ITZY (있지)» берётся главный
  исполнитель. Если тёзок несколько (Drake, Tony Bennett) или имя записано иначе (Skryptonite),
  решают альбомы из библиотеки (Lidarr ищет их по названию и называет исполнителя), псевдонимы и
  размер дискографии в метаданных Lidarr; без уверенности не угадывает;
* включает слежение за исполнителем, за всеми его альбомами и за новыми релизами.

Качает Soularr: он берёт альбомы из «Missing» в Lidarr (очередь по приоритету — soularr/soularr_plus.py).
Какие альбомы считаются, задаёт профиль метаданных Lidarr.

    python3 scripts/lidarr-artists.py              # показать, что изменится
    python3 scripts/lidarr-artists.py --apply      # применить; новых — не больше 50 за запуск

Для автоматической работы (crontab -e):
    40 4 * * * cd /root/navidrome-stack && python3 scripts/lidarr-artists.py --apply >> data/lidarr-artists.log 2>&1
"""
import argparse
import collections
import hashlib
import difflib
import json
import os
import re
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
FILLER = {"the", "a", "an", "and", "&", "и", "+"}
AGENT = "lidarr-artists/2.0"
METADATA = os.environ.get("LIDARR_METADATA_URL", "https://api.lidarr.audio/api/v0.4/")
ALBUMS_PER_ARTIST = 6  # сколько названий альбомов из библиотеки держать для сверки с Lidarr
NOT_ALBUMS = {"", "singles", "single", "unknown album", "[unknown album]", "unknown", "explo"}


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


def name_keys(name):
    """Ключи сравнения имён: целиком, без артикля и «and», с любым порядком слов («Bowie, David»)."""
    words = [w for w in re.split(r"[\s,]+", key(name)) if skeleton(w)]
    core = [w for w in words if w not in FILLER] or words
    keys = {skeleton(name), "".join(skeleton(w) for w in core), "".join(sorted(skeleton(w) for w in core))}
    return {k for k in keys if k}


COLLAB = re.compile(
    r"\s+(?:featuring|feat\.?|ft\.?|vs\.?|versus|meets|presents|with)\s+|\s*/\s*|\s+[–—=]\s+"
    r"|\s+(?:and|&)\s+(?:his|her|their)\s+",
    re.IGNORECASE,
)


def name_variants(name):
    """Как ещё может называться исполнитель из тегов: без пояснения в скобках, главный из совместных."""
    out = [name]
    bare = " ".join(re.sub(r"[\(\[].*?[\)\]]", " ", name).split())
    if bare and bare != name:
        out.append(bare)
    for v in list(out):
        main = COLLAB.split(v)[0].strip()
        if main and main != v and len(skeleton(main)) >= 2:
            out.append(main)
    return list(dict.fromkeys(out))


def similar(a, b):
    """Имена почти совпадают после транслита: «Skryptonite» и «Скриптонит»."""
    return any(difflib.SequenceMatcher(None, x, y).ratio() >= 0.85
               for x in name_keys(a) for y in name_keys(b) if min(len(x), len(y)) >= 5)


def album_key(title):
    return skeleton(re.sub(r"[\(\[].*?[\)\]]", " ", title)) or skeleton(title)


def retrying(lidarr, path, **params):
    """Поиск Lidarr через api.lidarr.audio: при 5xx повторить с паузой."""
    for attempt in range(4):
        try:
            return lidarr("GET", path, **params) or []
        except urllib.error.HTTPError as e:
            if e.code < 500 or attempt == 3:
                raise
            time.sleep(5 * (attempt + 1))


_meta = {}


def metadata(mbid):
    """Карточка исполнителя из метаданных Lidarr (api.lidarr.audio): псевдонимы и все релизы."""
    if mbid not in _meta:
        try:
            _meta[mbid] = http("GET", f"{METADATA}artist/{mbid}", {"Accept": "application/json", "User-Agent": AGENT}) or {}
        except (urllib.error.URLError, TimeoutError, ValueError):
            _meta[mbid] = {}
    return _meta[mbid]


def no_releases(mbid):
    """Метаданные точно знают исполнителя, и релизов у него нет. Метаданные недоступны — не знаем."""
    m = metadata(mbid)
    return "Albums" in m and not m["Albums"]


def alias_match(name, mbid):
    m = metadata(mbid)
    names = [m.get("artistname") or "", m.get("sortname") or ""]
    names += [a if isinstance(a, str) else (a.get("Name") or a.get("name") or "") for a in m.get("artistaliases") or []]
    return bool(name_keys(name) & set().union(*(name_keys(n) for n in names if n)))


def album_hits(lidarr, albums):
    """Чьи альбомы из библиотеки: Lidarr ищет альбом по названию и называет исполнителя релиза.

    Возвращает (сколько альбомов у каждого mbid, имя каждого mbid)."""
    hits, names = collections.Counter(), {}
    for album in albums[:4]:
        try:  # без «(Deluxe)» и подобного: поиск Lidarr точнее по основному названию
            found = retrying(lidarr, "album/lookup", term=" ".join(re.sub(r"[\(\[].*?[\)\]]", " ", album).split()) or album)
        except (urllib.error.URLError, TimeoutError):
            continue
        seen = set()
        for r in found:
            artist = r.get("artist") or {}
            if artist.get("foreignArtistId") and album_key(r.get("title", "")) == album_key(album):
                seen.add(artist["foreignArtistId"])
                names[artist["foreignArtistId"]] = artist.get("artistName", "")
        hits.update(seen)
    return hits, names


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
    """Сколько треков у каждого исполнителя, его имя как в тегах, MusicBrainz ID и названия альбомов."""
    counts, names, albums_of, offset = collections.Counter(), {}, collections.defaultdict(list), 0
    while True:
        songs = nd.call("search3", query="", songCount=PAGE, songOffset=offset, artistCount=0, albumCount=0)
        songs = songs.get("searchResult3", {}).get("song", [])
        for s in songs:
            credited = {n.strip() for a in (s.get("artists") or [{"name": s.get("artist", "")}]) for n in a["name"].split(";")}
            for n in credited:
                if n and key(n) not in SKIP:
                    counts[key(n)] += 1
                    names.setdefault(key(n), n)
                    album = s.get("album") or ""
                    if (key(album) not in NOT_ALBUMS and len(albums_of[key(n)]) < ALBUMS_PER_ARTIST
                            and album not in albums_of[key(n)]):
                        albums_of[key(n)].append(album)
        if len(songs) < PAGE:
            break
        offset += PAGE
    mbids = {}
    for index in nd.call("getArtists").get("artists", {}).get("index", []):
        for a in index.get("artist", []):
            if a.get("musicBrainzId"):
                mbids[key(a["name"])] = a["musicBrainzId"]
    return counts, names, mbids, albums_of


def elsewhere_of(name, exact, evidence, names):
    """Авторы альбомов из библиотеки, которые могут быть этим исполнителем под другим именем: почти то
    же имя, имя с прозвищем внутри («David Newman» и «David “Fathead” Newman») или псевдоним."""
    words = {skeleton(w) for w in re.split(r"[\s,]+", key(name)) if skeleton(w) and w not in FILLER}

    def by_name(other):
        theirs = {skeleton(w) for w in re.split(r"[\s,]+", key(other)) if skeleton(w)}
        return similar(name, other) or (len(words) >= 2 and words <= theirs)

    out = [(n, i) for i, n in evidence.items() if i not in exact and by_name(names.get(i, ""))]
    out += [(n, i) for i, n in evidence.most_common(5) if i not in exact and (n, i) not in out and alias_match(name, i)]
    return sorted(out, reverse=True)


def resolve(lidarr, name, albums, in_lidarr):
    """MusicBrainz ID исполнителя по имени из тегов. Возвращает (mbid или None, почему не нашли).

    Перебирает варианты имени. Один исполнитель с таким именем (с точностью до транслита, порядка
    слов и артикля) и хоть с одним релизом — он. Среди тёзок: тот, кто уже есть в Lidarr; тот, чьи
    альбомы из библиотеки знает Lidarr; иначе тот, у кого в разы больше релизов и кто первый в поиске,
    если альбомы из библиотеки не указывают на кого-то с похожим именем. Имя записано иначе (Splean =
    Сплин, Skryptonite = Скриптонит) — нужен псевдоним, почти то же имя или имя с прозвищем у автора
    альбомов из библиотеки. Без такой уверенности никого не берёт."""
    why, evidence, authors = "нет в MusicBrainz", None, {}
    for variant in name_variants(name):
        found = retrying(lidarr, "artist/lookup", term=variant)
        want = name_keys(variant)
        exact = list(dict.fromkeys(c["foreignArtistId"] for c in found
                                   if want & (name_keys(c.get("artistName", "")) | name_keys(c.get("sortName") or ""))))
        known = [i for i in exact if i in in_lidarr]
        if len(known) == 1:
            return known[0], ""
        if len(exact) == 1:
            if not no_releases(exact[0]):
                return exact[0], ""
            why = "в MusicBrainz есть только тёзка без релизов"
        if evidence is None:
            evidence, authors = album_hits(lidarr, albums) if albums else (collections.Counter(), {})
        elsewhere = elsewhere_of(variant, exact, evidence, authors)
        if len(exact) > 1:
            ranked = sorted(((evidence[i], i) for i in exact), reverse=True)
            if ranked[0][0] > ranked[1][0]:
                return ranked[0][1], ""
            sizes = sorted(((len(metadata(i).get("Albums") or []), i) for i in exact[:6]), reverse=True)
            if (not elsewhere and sizes[0][1] == exact[0] and sizes[0][0] >= 5
                    and sizes[0][0] >= 4 * max(sizes[1][0], 1)):
                return sizes[0][1], ""
            why = "несколько исполнителей с таким именем"
        if not exact:
            aliased = list(dict.fromkeys(c["foreignArtistId"] for c in found[:5] if alias_match(variant, c["foreignArtistId"])))
            if len(aliased) == 1 and not no_releases(aliased[0]):
                return aliased[0], ""
        if elsewhere and (len(elsewhere) == 1 or elsewhere[0][0] > elsewhere[1][0]):
            return elsewhere[0][1], ""
    return None, why


def lookup(lidarr, name, mbid, albums, in_lidarr):
    """Исполнитель в базе Lidarr: по ID из тегов, а без него — найденный по имени и альбомам.

    Возвращает (исполнитель или None, почему не взяли)."""
    if not mbid:
        mbid, why = resolve(lidarr, name, albums, in_lidarr)
        if not mbid:
            return None, why
    found = retrying(lidarr, "artist/lookup", term=f"lidarr:{mbid}")
    return (found[0], "") if found else (None, f"Lidarr не знает {mbid}")


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--apply", action="store_true", help="применить (без него только показать)")
    p.add_argument("--min-tracks", type=int, default=3, help="сколько треков в библиотеке нужно исполнителю")
    p.add_argument("--limit", type=int, default=50, help="сколько новых исполнителей искать и добавлять за запуск")
    p.add_argument("--retry", action="store_true", help="искать и тех, кого не нашли в последние 30 дней")
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

    counts, names, mbids, albums_of = library_artists(nd)
    wanted = sorted((k for k, n in counts.items() if n >= args.min_tracks), key=lambda k: -counts[k])
    artists = lidarr("GET", "artist")
    by_id = {a["id"]: a for a in artists}
    by_mbid = {a["foreignArtistId"]: a for a in artists}
    by_name = {key(a["artistName"]): a for a in artists}
    by_skeleton = {}  # «Max Korzh» в тегах и «Макс Корж» в Lidarr, «Cure» и «The Cure»; совпали у двух — никого
    for a in artists:
        for sk in name_keys(a["artistName"]) | name_keys(a.get("sortName") or ""):
            by_skeleton[sk] = None if by_skeleton.get(sk, a) is not a else a

    def in_lidarr(k):
        for v in name_variants(names[k]):
            found = by_name.get(key(v)) or next((by_skeleton[sk] for sk in name_keys(v) if by_skeleton.get(sk)), None)
            if found:
                return found
        return None
    root = lidarr("GET", "rootfolder")[0]
    # удалённых из Lidarr с пометкой «не добавлять снова» (Import List Exclusions) не возвращаем:
    # так убирают тёзок, которых подставил MusicBrainz ID в тегах (диджей Jay Z вместо JAŸ-Z)
    excluded = {e["foreignId"] for e in lidarr("GET", "importlistexclusion") or []}

    known, new = {}, []
    for k in wanted:
        artist = by_mbid.get(mbids.get(k, "")) or in_lidarr(k)
        if artist is not None:
            known[artist["id"]] = artist
        elif args.retry or now - state.get(k, 0) > RETRY_DAYS * 86400:
            new.append(k)
    print(f"{stamp} исполнителей с {args.min_tracks}+ треками: {len(wanted)}, уже в Lidarr: {len(known)}, "
          f"нет в Lidarr: {len(new)} (за запуск ищу {min(len(new), args.limit)})")

    # новых — найти в MusicBrainz и добавить со всеми альбомами
    added, missing = [], []
    for k in new[: args.limit]:
        name = names[k]
        try:
            found, why = lookup(lidarr, name, mbids.get(k), albums_of.get(k, []), by_mbid)
        except (urllib.error.URLError, TimeoutError) as e:
            print(f"  {name}: поиск не удался ({e})")
            continue
        if found is not None and found.get("foreignArtistId") in by_mbid:  # уже в Lidarr, но записан иначе
            known[by_mbid[found["foreignArtistId"]]["id"]] = by_mbid[found["foreignArtistId"]]
            continue
        if found is None:
            missing.append(f"{name} — {why}")
            state[k] = now
            continue
        if found.get("foreignArtistId") in excluded:
            missing.append(f"{name} — исключён в Lidarr")
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
        added.append(f"{name} ({counts[k]})" + (f" = {found['artistName']}" if key(found["artistName"]) != k else ""))
        by_mbid[found["foreignArtistId"]] = found  # «Drake» и «drake» в одном запуске — один исполнитель

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
