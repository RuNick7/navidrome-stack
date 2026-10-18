#!/usr/bin/env python3
"""Лайкает все треки плейлиста Navidrome.

    python3 scripts/like-playlist.py "Название плейлиста"
"""
import hashlib
import json
import os
import secrets
import sys
import urllib.parse
import urllib.request

STACK = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def load_env(path):
    env = {}
    if os.path.exists(path):
        for line in open(path, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    return env


env = {**load_env(os.path.join(STACK, ".env")), **os.environ}
URL = env.get("ND_URL", "http://127.0.0.1:4533")


def call(method, params):
    salt = secrets.token_hex(6)
    token = hashlib.md5((env["ND_PASSWORD"] + salt).encode()).hexdigest()
    q = [("u", env["ND_USER"]), ("t", token), ("s", salt), ("v", "1.16.1"), ("c", "like-playlist"), ("f", "json")] + params
    with urllib.request.urlopen(f"{URL}/rest/{method}?{urllib.parse.urlencode(q)}", timeout=60) as r:
        res = json.load(r)["subsonic-response"]
    if res["status"] != "ok":
        sys.exit(f"Navidrome: {res['error']['message']}")
    return res


if len(sys.argv) != 2:
    sys.exit(__doc__)
name = sys.argv[1].lower()
playlists = call("getPlaylists", [])["playlists"].get("playlist", [])
match = [p for p in playlists if p["name"].lower() == name]
if not match:
    sys.exit("Нет такого плейлиста. Есть: " + ", ".join(p["name"] for p in playlists))
songs = call("getPlaylist", [("id", match[0]["id"])])["playlist"].get("entry", [])
ids = [s["id"] for s in songs if not s.get("starred")]
for i in range(0, len(ids), 100):
    call("star", [("id", x) for x in ids[i:i + 100]])
print(f"Лайкнуто {len(ids)} из {len(songs)} треков")
