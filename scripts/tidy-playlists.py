#!/usr/bin/env python3
"""Переименовывает плейлисты кластеризации AudioMuse в короткие русские и ставит им обложки.

AudioMuse называет плейлисты по тегам: «Indie_Rock_Alternative_Fast_Danceable_automatic».
Скрипт делает из этого «Инди, рок · быстро, танцевально» и загружает обложку-градиент
по настроению. После новой кластеризации прошлые переименованные плейлисты удаляются,
иначе они копились бы: AudioMuse сам удаляет только плейлисты с «_automatic».

    python3 scripts/tidy-playlists.py --dry-run   # показать новые названия
    python3 scripts/tidy-playlists.py

Для автоматической работы (crontab -e):
    15 * * * * cd /root/navidrome-stack && python3 scripts/tidy-playlists.py >> data/tidy-playlists.log 2>&1
"""
import argparse
import hashlib
import json
import os
import re
import secrets
import struct
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zlib

STACK = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATE = os.path.join(STACK, "data", "tidy-playlists.json")
AUTO = re.compile(r"_automatic(?:\s*\((\d+)\))?$", re.IGNORECASE)

TAGS = {
    "rock": "рок", "pop": "поп", "alternative": "альтернатива", "indie": "инди",
    "electronic": "электроника", "electronica": "электроника", "electro": "электро",
    "female vocalists": "женский вокал", "female vocalist": "женский вокал",
    "male vocalists": "мужской вокал", "dance": "танцевальное", "jazz": "джаз",
    "beautiful": "красивое", "metal": "метал", "chillout": "чил-аут", "chill": "чил",
    "classic rock": "классический рок", "soul": "соул", "indie rock": "инди-рок",
    "alternative rock": "альт-рок", "mellow": "мягкое", "folk": "фолк",
    "instrumental": "инструментал", "punk": "панк", "oldies": "старое", "blues": "блюз",
    "hard rock": "хард-рок", "ambient": "эмбиент", "acoustic": "акустика",
    "experimental": "эксперимент", "guitar": "гитара", "hip-hop": "хип-хоп",
    "party": "вечеринка", "country": "кантри", "easy listening": "лёгкое",
    "sexy": "чувственное", "catchy": "цепляющее", "funk": "фанк",
    "heavy metal": "хеви-метал", "progressive rock": "прог-рок", "rnb": "R&B",
    "indie pop": "инди-поп", "sad": "грустное", "house": "хаус", "happy": "весёлое",
    "mixed": "микс", "60s": "60-е", "70s": "70-е", "80s": "80-е", "90s": "90-е", "00s": "2000-е",
}
TEMPO = {"slow": "медленно", "medium": "", "fast": "быстро"}
FEATURES = {
    "danceable": "танцевально", "aggressive": "агрессивно", "happy": "весело",
    "party": "для вечеринки", "relaxed": "спокойно", "sad": "грустно",
}
# цвета обложки (верх, низ) по главной черте плейлиста
COLORS = {
    "aggressive": ((198, 40, 40), (40, 6, 6)), "happy": ((249, 168, 37), (191, 54, 12)),
    "party": ((142, 36, 170), (36, 16, 92)), "relaxed": ((38, 166, 154), (0, 50, 45)),
    "sad": ((57, 73, 171), (10, 18, 48)), "danceable": ((216, 27, 96), (60, 12, 110)),
    "slow": ((84, 110, 122), (20, 28, 34)), "fast": ((255, 112, 67), (120, 20, 40)),
}
PALETTE = [((0, 137, 123), (0, 40, 60)), ((94, 53, 177), (20, 10, 50)),
           ((239, 108, 0), (90, 20, 10)), ((3, 155, 229), (10, 30, 70)),
           ((124, 179, 66), (20, 50, 20)), ((216, 67, 21), (60, 10, 30))]


def load_env(path):
    env = {}
    if os.path.exists(path):
        for line in open(path, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    return env


class Navidrome:
    def __init__(self, url, user, password):
        self.url, self.user, self.password, self.jwt = url.rstrip("/"), user, password, None

    def call(self, method, params=()):
        salt = secrets.token_hex(6)
        token = hashlib.md5((self.password + salt).encode()).hexdigest()
        q = [("u", self.user), ("t", token), ("s", salt), ("v", "1.16.1"),
             ("c", "tidy-playlists"), ("f", "json"), *params]
        with urllib.request.urlopen(f"{self.url}/rest/{method}?{urllib.parse.urlencode(q)}", timeout=60) as r:
            res = json.load(r)["subsonic-response"]
        if res["status"] != "ok":
            sys.exit(f"Navidrome: {res['error']['message']}")
        return res

    def upload_cover(self, playlist_id, png):
        if not self.jwt:
            req = urllib.request.Request(f"{self.url}/auth/login", method="POST",
                                         data=json.dumps({"username": self.user, "password": self.password}).encode(),
                                         headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=60) as r:
                self.jwt = json.load(r)["token"]
        boundary = uuid.uuid4().hex
        body = (f"--{boundary}\r\nContent-Disposition: form-data; name=\"image\"; filename=\"cover.png\"\r\n"
                f"Content-Type: image/png\r\n\r\n").encode() + png + f"\r\n--{boundary}--\r\n".encode()
        req = urllib.request.Request(f"{self.url}/api/playlist/{playlist_id}/image", data=body, method="POST",
                                     headers={"Content-Type": f"multipart/form-data; boundary={boundary}",
                                              "X-ND-Authorization": f"Bearer {self.jwt}"})
        urllib.request.urlopen(req, timeout=60).close()


def parse(name):
    """Теги AudioMuse → (жанры, темп, черты, номер части)."""
    m = AUTO.search(name)
    part = int(m.group(1)) if m and m.group(1) else None
    genres, tempo, features = [], None, []
    for token in name[:m.start()].split("_"):
        t = token.strip().lower()
        if t in TEMPO and tempo is None:
            tempo = t
        elif t in FEATURES and tempo is not None:  # черты идут после темпа
            features.append(t)
        elif t:
            genres.append(t)
    return genres, tempo, features, part


def russian_name(genres, tempo, features, part):
    words = []
    for g in genres:
        ru = TAGS.get(g, g)
        if ru not in words:
            words.append(ru)
    mood = [w for w in [TEMPO.get(tempo or "", "")] + [FEATURES[f] for f in features[:1]] if w]
    name = ", ".join(words[:2]) or "микс"
    name = name[0].upper() + name[1:]
    if mood:
        name += " · " + ", ".join(mood)
    if part and part > 1:
        name += f" {part}"
    return name


def png_gradient(top, bottom, size=600):
    rows = []
    for y in range(size):
        row = bytearray([0])
        for x in range(0, size, 8):
            t = (y + x * 0.35) / (size * 1.35)  # слегка по диагонали
            px = bytes(int(a + (b - a) * t) for a, b in zip(top, bottom))
            row += px * min(8, size - x)
        rows.append(bytes(row))
    raw = b"".join(rows)

    def chunk(kind, data):
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw, 9)) + chunk(b"IEND", b""))


def colors_for(genres, tempo, features):
    for key in features + [tempo or ""]:
        if key in COLORS:
            return COLORS[key]
    return PALETTE[zlib.crc32((genres[0] if genres else "").encode()) % len(PALETTE)]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="только показать новые названия")
    args = ap.parse_args()

    env = {**load_env(os.path.join(STACK, ".env")), **os.environ}
    for k in ("ND_USER", "ND_PASSWORD"):
        if not env.get(k):
            sys.exit(f"Нет {k} в .env")
    nd = Navidrome(env.get("ND_URL", "http://127.0.0.1:4533"), env["ND_USER"], env["ND_PASSWORD"])

    playlists = nd.call("getPlaylists")["playlists"].get("playlist", [])
    auto = [p for p in playlists if AUTO.search(p["name"])]
    if not auto:
        print("Новых плейлистов AudioMuse нет")
        return

    state = json.load(open(STATE)) if os.path.exists(STATE) else {"renamed": []}
    existing = {p["id"] for p in playlists}
    old = [i for i in state["renamed"] if i in existing]
    taken = {p["name"] for p in playlists if p["id"] not in old and not AUTO.search(p["name"])}

    if old and not args.dry_run:
        for i in old:
            nd.call("deletePlaylist", [("id", i)])
        print(f"Удалено прошлых плейлистов кластеризации: {len(old)}")

    renamed = []
    for p in sorted(auto, key=lambda p: p["name"]):
        genres, tempo, features, part = parse(p["name"])
        name = base = russian_name(genres, tempo, features, part)
        n = 2
        while name in taken:
            name = f"{base} ({n})"
            n += 1
        taken.add(name)
        print(f"{p['name']} → {name}")
        if args.dry_run:
            continue
        nd.call("updatePlaylist", [("playlistId", p["id"]), ("name", name)])
        try:
            nd.upload_cover(p["id"], png_gradient(*colors_for(genres, tempo, features)))
        except urllib.error.URLError as e:
            print(f"  обложка не загрузилась: {e}")
        renamed.append(p["id"])

    if not args.dry_run:
        json.dump({"renamed": renamed}, open(STATE, "w"))


if __name__ == "__main__":
    main()
