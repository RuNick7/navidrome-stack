#!/usr/bin/env python3
"""Докачка с YouTube Music того, что Soularr не смог скачать через Soulseek.

Soulseek хорош для западной музыки, но русскоязычные альбомы там лежат у пары медленных
пиров, а синглов почти нет. Раз в полчаса скрипт берёт из «Wanted → Missing» Lidarr:

* альбомы, на которых Soularr уже споткнулся («Failed grab», «Search failed» в его журнале);
* синглы и EP — их Lidarr ждёт только у исполнителей с профилем «Альбомы, EP и синглы».

Для каждого ищет релиз в YouTube Music, сверяет треки с Lidarr (названия и длительность),
качает звук как есть (Opus, без перекодирования), пишет теги MusicBrainz из Lidarr и обложку
и отдаёт папку Lidarr на импорт — дальше всё как у скачанного через Soulseek. Что не нашлось
или не импортировалось, пробует снова через неделю. Состояние — в /data/state.json.

    python fallback.py --loop          # так работает контейнер
    python fallback.py --once --dry    # показать, что взял бы, ничего не качая
    python fallback.py --album 123     # один альбом Lidarr по id
"""
import argparse
import base64
import difflib
import glob
import json
import os
import re
import shutil
import subprocess
import sys
import time
import unicodedata
import urllib.request

LIDARR = os.environ.get("LIDARR_URL", "http://lidarr:8686").rstrip("/") + "/api/v1"
KEY = os.environ.get("LIDARR_API_KEY", "")
STAGING = os.environ.get("YOUTUBE_STAGING", "/mnt/music/youtube")
DATA = os.environ.get("YOUTUBE_DATA", "/data")
SOULARR_LOGS = os.environ.get("SOULARR_LOGS", "/soularr")
PER_RUN = int(os.environ.get("YOUTUBE_ALBUMS_PER_RUN", "4"))
INTERVAL = int(os.environ.get("YOUTUBE_INTERVAL", "1800"))
RETRY_DAYS = float(os.environ.get("YOUTUBE_RETRY_DAYS", "7"))
MIN_SCORE = 0.8  # доля треков Lidarr, которые должны найтись в релизе YouTube Music
STATE = os.path.join(DATA, "state.json")
SITE = os.path.join(DATA, "site")  # свежий yt-dlp: YouTube меняется чаще, чем образ
DAY = 86400

FAILED = re.compile(r"(?:Failed grab of Album: (?P<a>.+?) Artist: (?P<b>.+)$|"
                    r"(?:Search|Download) failed for Album: (?P<c>.+?) - Artist: (?P<d>.+)$)")

CYR = dict(zip("абвгдеёжзийклмнопрстуфхцчшщъыьэюя", [
    "a", "b", "v", "g", "d", "e", "e", "zh", "z", "i", "i", "k", "l", "m", "n", "o", "p", "r", "s", "t", "u",
    "f", "h", "ts", "ch", "sh", "sch", "", "i", "", "e", "yu", "ya"]))


def log(*parts):
    print(time.strftime("%Y-%m-%d %H:%M:%S"), *parts, flush=True)


def skeleton(text):
    s = "".join(CYR.get(c, c) for c in (text or "").casefold())
    s = "".join(c for c in unicodedata.normalize("NFKD", s) if c.isalnum())
    return s.replace("kh", "h").replace("x", "ks").replace("y", "i").replace("j", "i")


def plain_title(text):
    """Название без «(feat. …)», «[Explicit]», «- Remastered»: так сравниваем с YouTube."""
    t = re.sub(r"[\(\[][^\)\]]*(feat|ft\.|with|explicit|clean|remaster|bonus|version|edit)[^\)\]]*[\)\]]", " ", text or "", flags=re.I)
    t = re.sub(r"\s[-–—]\s.*(remaster|version|edit).*$", " ", t, flags=re.I)
    return skeleton(t)


def similar(a, b):
    a, b = plain_title(a), plain_title(b)
    if not a or not b:
        return 0.0
    if a == b:
        return 1.0
    return difflib.SequenceMatcher(None, a, b).ratio()


# --- Lidarr ---

def lidarr(method, path, body=None):
    req = urllib.request.Request(LIDARR + path, method=method, data=json.dumps(body).encode() if body is not None else None,
                                 headers={"X-Api-Key": KEY, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r:
        text = r.read()
        return json.loads(text) if text else None


def missing_albums():
    return lidarr("GET", "/wanted/missing?pageSize=10000&includeArtist=true&monitored=true")["records"]


# --- состояние ---

def load_state():
    try:
        return json.load(open(STATE, encoding="utf-8"))
    except (OSError, ValueError):
        return {"albums": {}, "soularr": {}, "updated_ytdlp": 0}


def save_state(state):
    tmp = STATE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)
    os.replace(tmp, STATE)


def note_soularr_failures(state):
    """Журнал Soularr живёт часов восемь (ротация), поэтому неудачи копим у себя."""
    seen = state["soularr"]
    for path in glob.glob(os.path.join(SOULARR_LOGS, "soularr.log*")):
        try:
            lines = open(path, encoding="utf-8", errors="replace").read().splitlines()
        except OSError:
            continue
        for line in lines:
            m = FAILED.search(line)
            if not m:
                continue
            title, artist = (m.group("a"), m.group("b")) if m.group("a") else (m.group("c"), m.group("d"))
            stamp = line.split("]", 1)[1].strip()[:24] if "]" in line else line[:24]
            key = f"{skeleton(artist)}|{skeleton(title)}"
            entry = seen.setdefault(key, {"count": 0, "stamps": []})
            if stamp not in entry["stamps"]:
                entry["stamps"] = (entry["stamps"] + [stamp])[-5:]
                entry["count"] += 1


def candidates(state, albums):
    now = time.time()
    chosen = []
    for a in albums:
        mine = state["albums"].get(str(a["id"]), {})
        if mine.get("status") == "done":
            continue
        if mine.get("status") and now - mine.get("at", 0) < RETRY_DAYS * DAY:
            continue  # не нашлось или не импортировалось — через неделю снова
        failures = state["soularr"].get(f"{skeleton(a['artist']['artistName'])}|{skeleton(a['title'])}", {}).get("count", 0)
        if a.get("albumType") in ("Single", "EP") or failures:
            chosen.append((0 if a.get("albumType") in ("Single", "EP") else 1, -failures, a.get("releaseDate") or "", a))
    chosen.sort(key=lambda x: x[:3])
    return [x[3] for x in chosen]


# --- YouTube Music ---

def ytmusic():
    from ytmusicapi import YTMusic
    return YTMusic()  # с language="ru" ytmusicapi не разбирает результаты поиска


def find_release(yt, album, tracks):
    """Релиз YouTube Music, где нашлись треки альбома Lidarr: (оценка, релиз, пары трек → видео)."""
    artist = album["artist"]["artistName"]
    want = skeleton(artist)
    seen, best = set(), (0.0, None, [])
    for query in (f"{artist} {album['title']}", album["title"]):
        for hit in yt.search(query, filter="albums", limit=10)[:8]:
            bid = hit.get("browseId")
            if not bid or bid in seen:
                continue
            seen.add(bid)
            names = [skeleton(x.get("name")) for x in hit.get("artists") or []]
            if not any(n and (n in want or want in n) for n in names):
                continue
            if similar(hit.get("title"), album["title"]) < 0.6:
                continue
            release = yt.get_album(bid)
            pairs = match_tracks(tracks, release.get("tracks") or [])
            score = len(pairs) / max(1, len(tracks))
            if score > best[0]:
                best = (score, release, pairs)
            if score >= 0.999:
                return best
    return best


def match_tracks(tracks, yt_tracks):
    pairs, used = [], set()
    for t in tracks:
        length = (t.get("duration") or 0) / 1000
        options = []
        for y in yt_tracks:
            if not y.get("videoId") or y["videoId"] in used:
                continue
            title = similar(t["title"], y.get("title"))
            gap = abs((y.get("duration_seconds") or 0) - length) if length else 0
            if title >= 0.75 and (not length or gap <= max(8, length * 0.06)):
                options.append((title, -gap, y))
        if options:
            options.sort(key=lambda o: o[:2], reverse=True)
            used.add(options[0][2]["videoId"])
            pairs.append((t, options[0][2]))
    return pairs


def update_ytdlp(state):
    if time.time() - state.get("updated_ytdlp", 0) < DAY:
        return
    r = subprocess.run([sys.executable, "-m", "pip", "install", "-q", "--disable-pip-version-check", "--upgrade",
                        "--target", SITE, "yt-dlp"], capture_output=True, text=True)
    if r.returncode == 0:
        state["updated_ytdlp"] = time.time()
        log("yt-dlp обновлён")
    else:
        log("не обновил yt-dlp:", r.stderr.strip()[-200:])


def download(video_id, target):
    env = {**os.environ, "PYTHONPATH": SITE + os.pathsep + os.environ.get("PYTHONPATH", "")}
    cmd = [sys.executable, "-m", "yt_dlp", "-q", "--no-progress", "--no-playlist", "--no-warnings",
           "-f", "bestaudio[acodec=opus]/bestaudio", "-x", "--audio-format", "opus",
           "-o", target + ".%(ext)s", f"https://music.youtube.com/watch?v={video_id}"]
    for attempt in range(3):
        r = subprocess.run(cmd, capture_output=True, text=True, env=env)
        if r.returncode == 0 and os.path.exists(target + ".opus"):
            return target + ".opus"
        time.sleep(10 * (attempt + 1))
    raise RuntimeError((r.stderr or r.stdout).strip()[-300:])


def tag(path, album, release, track, artist, cover):
    from mutagen.flac import Picture
    from mutagen.oggopus import OggOpus

    audio = OggOpus(path)
    audio.delete()
    values = {
        "title": track["title"],
        "artist": artist["artistName"],
        "albumartist": artist["artistName"],
        "album": album["title"],
        "tracknumber": str(track.get("absoluteTrackNumber") or track.get("trackNumber") or ""),
        "discnumber": str(track.get("mediumNumber") or 1),
        "date": (album.get("releaseDate") or "")[:10],
        "musicbrainz_albumid": release.get("foreignReleaseId", ""),
        "musicbrainz_releasegroupid": album.get("foreignAlbumId", ""),
        "musicbrainz_artistid": artist.get("foreignArtistId", ""),
        "musicbrainz_albumartistid": artist.get("foreignArtistId", ""),
        "musicbrainz_trackid": track.get("foreignRecordingId", ""),
        "musicbrainz_releasetrackid": track.get("foreignTrackId", ""),
        "comment": "YouTube Music",
    }
    for k, v in values.items():
        if v:
            audio[k] = [v]
    if cover:
        pic = Picture()
        pic.type, pic.mime, pic.data = 3, "image/jpeg", cover
        audio["metadata_block_picture"] = [base64.b64encode(pic.write()).decode()]
    audio.save()


def fetch_cover(release):
    thumbs = sorted(release.get("thumbnails") or [], key=lambda t: t.get("width", 0))
    if not thumbs:
        return b""
    url = re.sub(r"=w\d+-h\d+", "=w600-h600", thumbs[-1]["url"])
    try:
        with urllib.request.urlopen(url, timeout=30) as r:
            return r.read()
    except OSError:
        return b""


def import_folder(folder, album_id):
    cmd = lidarr("POST", "/command", {"name": "DownloadedAlbumsScan", "path": folder, "importMode": "Move"})
    for _ in range(120):
        time.sleep(5)
        status = lidarr("GET", f"/command/{cmd['id']}")
        if status.get("status") in ("completed", "failed", "aborted", "cancelled"):
            break
    album = lidarr("GET", f"/album/{album_id}")
    return (album.get("statistics") or {}).get("trackFileCount", 0)


def safe(name):
    return re.sub(r'[\\/:*?"<>|]+', "_", name).strip(" .")[:100] or "_"


def process(yt, album, state, dry=False):
    key = str(album["id"])
    artist = album["artist"]
    label = f"{artist['artistName']} — {album['title']} ({album.get('albumType')})"
    full = lidarr("GET", f"/album/{album['id']}")
    release = next((r for r in full.get("releases", []) if r.get("monitored")), (full.get("releases") or [{}])[0])
    tracks = lidarr("GET", f"/track?albumId={album['id']}")
    tracks = [t for t in tracks if not release.get("id") or t.get("albumReleaseId") in (None, release.get("id"))]
    if not tracks:
        log("нет треков в Lidarr:", label)
        state["albums"][key] = {"status": "no_tracks", "at": time.time(), "label": label}
        return
    score, found, pairs = find_release(yt, album, tracks)
    need = 1.0 if len(tracks) <= 2 else MIN_SCORE
    if not found or score < need:
        log(f"не нашёл в YouTube Music ({score:.0%}):", label)
        state["albums"][key] = {"status": "no_match", "at": time.time(), "label": label, "score": round(score, 2)}
        return
    log(f"нашёл {found.get('title')} ({len(pairs)}/{len(tracks)} треков):", label)
    if dry:
        return
    folder = os.path.join(STAGING, safe(f"{artist['artistName']} - {album['title']}"))
    shutil.rmtree(folder, ignore_errors=True)
    os.makedirs(folder)
    cover = fetch_cover(found)
    try:
        for track, video in pairs:
            number = int(track.get("absoluteTrackNumber") or track.get("trackNumber") or 0)
            path = download(video["videoId"], os.path.join(folder, safe(f"{number:02d} - {track['title']}")))
            tag(path, album, release, track, artist, cover)
            time.sleep(3)
    except Exception as exc:  # noqa: BLE001
        log("не скачал:", label, exc)
        shutil.rmtree(folder, ignore_errors=True)
        state["albums"][key] = {"status": "download_failed", "at": time.time(), "label": label, "error": str(exc)[:200]}
        return
    have = import_folder(folder, album["id"])
    if have:
        log(f"импортирован ({have} треков):", label)
        state["albums"][key] = {"status": "done", "at": time.time(), "label": label, "tracks": have}
        shutil.rmtree(folder, ignore_errors=True)
    else:
        failed = os.path.join(DATA, "failed", os.path.basename(folder))
        shutil.rmtree(failed, ignore_errors=True)
        shutil.move(folder, failed)  # вне библиотеки: Navidrome не покажет недоимпортированное
        log("Lidarr не импортировал, папка в", failed, ":", label)
        state["albums"][key] = {"status": "import_failed", "at": time.time(), "label": label}


def run_once(state, dry=False, only=None):
    note_soularr_failures(state)
    albums = missing_albums()
    if only:
        todo = [a for a in albums if a["id"] == only] or [lidarr("GET", f"/album/{only}")]
        for a in todo:
            a.setdefault("artist", lidarr("GET", f"/artist/{a['artistId']}"))
    else:
        todo = candidates(state, albums)
    log(f"в Missing {len(albums)}, ждут YouTube {len(todo)}, беру {min(len(todo), PER_RUN) if not only else len(todo)}")
    yt = ytmusic()
    for album in todo[: PER_RUN if not only else None]:
        try:
            process(yt, album, state, dry)
        except Exception as exc:  # noqa: BLE001 — один альбом не должен останавливать остальные
            log("ошибка:", album.get("title"), exc)
            state["albums"][str(album["id"])] = {"status": "error", "at": time.time(), "error": str(exc)[:200]}
        if not dry:
            save_state(state)


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--loop", action="store_true")
    p.add_argument("--once", action="store_true")
    p.add_argument("--dry", action="store_true", help="только показать, что нашлось бы")
    p.add_argument("--album", type=int, help="один альбом Lidarr по id")
    args = p.parse_args()
    if not KEY:
        sys.exit("нужен LIDARR_API_KEY")
    os.makedirs(os.path.join(DATA, "failed"), exist_ok=True)
    os.makedirs(STAGING, exist_ok=True)
    sys.path.insert(0, SITE)
    while True:
        state = load_state()
        if not args.dry:
            update_ytdlp(state)
        try:
            run_once(state, args.dry, args.album)
        except Exception as exc:  # noqa: BLE001 — Lidarr перезапускается, сеть моргнула
            log("проход не удался:", exc)
        if not args.dry:
            save_state(state)
        if not args.loop:
            break
        time.sleep(INTERVAL)


if __name__ == "__main__":
    main()
