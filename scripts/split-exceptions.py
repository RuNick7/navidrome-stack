#!/usr/bin/env python3
"""Названия групп из Lidarr — в исключения деления исполнителей Navidrome.

Navidrome делит теги исполнителей по «, », « & », « x », «/», «;» (navidrome/navidrome.toml),
чтобы «Скриптонит, Andy Panda» стал двумя исполнителями. Но так же он разрезал бы и настоящие
группы: «Joey Valence & Brae», «Tyler, The Creator», «AC/DC». У Lidarr имена из MusicBrainz,
где группа — один исполнитель, поэтому все его имена с такими знаками скрипт добавляет в
ArtistSplitExceptions. Список, написанный руками, остаётся как был; добавленное скриптом
лежит после строки-метки и пересобирается при каждом запуске.

    python3 scripts/split-exceptions.py           # обновить navidrome/navidrome.toml
    python3 scripts/split-exceptions.py --check   # только показать, что добавится

После изменения: docker compose restart navidrome и полное пересканирование
(docker compose exec navidrome /app/navidrome scan --full).
"""
import argparse
import json
import os
import re
import sys
import urllib.request

STACK = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TOML = os.path.join(STACK, "navidrome", "navidrome.toml")
MARK = "  # --- ниже — группы из Lidarr, пересобирает scripts/split-exceptions.py ---"
# те же разделители, что в [Tags.*] Split у navidrome.toml
SEPARATORS = re.compile(r"/|;|,\s|\s&\s|\s[xX×]\s|\sfeat\.?\s|\sft\.?\s", re.I)


def load_env(path):
    env = {}
    if os.path.exists(path):
        for line in open(path, encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip().strip('"').strip("'")
    return env


def lidarr_names(env):
    url = env.get("LIDARR_URL", "http://127.0.0.1:8686").rstrip("/") + "/api/v1/artist"
    request = urllib.request.Request(url, headers={"X-Api-Key": env["LIDARR_API_KEY"]})
    with urllib.request.urlopen(request, timeout=60) as r:
        return sorted({a["artistName"] for a in json.load(r) if SEPARATORS.search(a["artistName"])}, key=str.casefold)


def main():
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--check", action="store_true", help="ничего не менять, только показать")
    args = p.parse_args()

    text = open(TOML, encoding="utf-8").read()
    block = re.search(r"(ArtistSplitExceptions = \[\n)(.*?)(\n\])", text, re.S)
    if not block:
        sys.exit("В navidrome.toml нет ArtistSplitExceptions = [ … ]")
    manual_text = block.group(2).split("\n" + MARK)[0].rstrip()
    manual = set(re.findall(r'"((?:[^"\\]|\\.)*)"', manual_text))

    env = load_env(os.path.join(STACK, ".env"))
    names = [n for n in lidarr_names(env) if n not in manual]
    auto = "\n".join(f"  {json.dumps(n, ensure_ascii=False)}," for n in names)
    body = manual_text + ("\n" + MARK + "\n" + auto if names else "")
    updated = text[: block.start(2)] + body + text[block.end(2):]

    print(f"групп из Lidarr с «&», «,», «/», «x»: {len(names)} (руками записано {len(manual)})")
    for n in names:
        print("  " + n)
    if args.check or updated == text:
        print("без изменений" if updated == text else "--check: файл не меняю")
        return
    with open(TOML, "w", encoding="utf-8") as f:
        f.write(updated)
    print(f"обновлён {TOML}: перезапустите Navidrome и пересканируйте библиотеку полностью")


if __name__ == "__main__":
    main()
