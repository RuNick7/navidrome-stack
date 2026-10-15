# navidrome-stack

Личный музыкальный сервер с опытом в духе Spotify / Яндекс Музыки: Navidrome, библиотека на Google Диске (rclone crypt), рекомендации по истории (ListenBrainz, Last.fm), автодокачка (Explo + Soulseek), «Моя волна» по звучанию (AudioMuse-AI), умные плейлисты.

```
Приложения ──HTTPS──> Caddy ──> Navidrome ──> AudioMuse-AI (похожее по звуку)
                                   │   └────> Last.fm / ListenBrainz (скробблы, похожие)
                                   │
ListenBrainz ──> Explo ──> slskd / YouTube ──> /mnt/music/explo
                                   │
                         rclone mount + crypt (кэш на SSD) ──> Google Диск
```

| Компонент | Версия | Зачем |
|---|---|---|
| [Navidrome](https://github.com/navidrome/navidrome) | 0.64.2 | сервер, Subsonic API, умные плейлисты |
| [Caddy](https://caddyserver.com) | 2.10 | HTTPS с автосертификатом |
| [Feishin](https://github.com/jeffvli/feishin) | 1.17.0 | веб-плеер в стиле Spotify, `https://<домен>:44444` |
| [rclone](https://rclone.org) | последняя | Google Диск как диск, шифрование |
| [slskd](https://github.com/slskd/slskd) | 0.26.1 | Soulseek: MP3 320 / FLAC |
| [Explo](https://github.com/LumePart/Explo) | v1.2.0 | Weekly Exploration из ListenBrainz с докачкой |
| [AudioMuse-AI](https://github.com/NeptuneHub/AudioMuse-AI) | 3.6.3 + плагин v10 | похожее по звучанию, Instant Mix, Sonic Fingerprint |
| [navidrome-lyrics-plugin](https://github.com/J0R6IT0/navidrome-lyrics-plugin) | v8.1.0 | синхронные тексты (LRCLIB и др.) |

Требования: VPS с Ubuntu 22.04/24.04, 4 ядра, 8 ГБ RAM, 120 ГБ SSD; домен; Google Диск.

## Фаза 1. Сервер и Google Диск

```bash
git clone <этот репозиторий> && cd navidrome-stack
sudo ./scripts/setup-server.sh        # docker, rclone, fuse, swap 4G, ufw, пользователь music
```

Скрипт сообщит, есть ли AVX2. Если нет, в `.env` поставьте `AUDIOMUSE_VERSION=3.6.3-noavx2`.

**rclone.** Создайте свой OAuth client в Google Cloud Console ([инструкция rclone](https://rclone.org/drive/#making-your-own-client-id)): общий ключ rclone сильно ограничен по скорости. Затем:

```bash
sudo -u music rclone config
#   n) gdrive   -> type: drive, client_id/secret ваши, scope: drive
#   n) gcrypt   -> type: crypt, remote: gdrive:navidrome, пароль + salt (сохраните их!)
sudo systemctl enable --now rclone-music
mkdir -p /mnt/music/explo /mnt/music/soulseek /mnt/music/_playlists   # от пользователя music
```

Без пароля crypt библиотеку не расшифровать: храните его вне сервера.

**Если на сервере remnanode** (Xray занимает 443): `HTTPS_PORT=8443` в `.env`. Сертификат выпустится через порт 80. Адрес в приложениях: `https://music.example.com:8443`.

```bash
cp .env.example .env && nano .env       # ND_DOMAIN, ACME_EMAIL, HTTPS_PORT
./scripts/install-plugins.sh
docker compose up -d
```

Откройте `https://<домен>`, создайте администратора, загрузите несколько треков в `/mnt/music`.
Готово, когда трек с Диска играет на телефоне через мобильный интернет.

## Фаза 2. Персонализация

1. Ключ Last.fm: <https://www.last.fm/api/account/create> → `LASTFM_API_KEY`, `LASTFM_SECRET` → `docker compose up -d`.
2. В Navidrome: *Настройки → Personal*: подключить Last.fm и ListenBrainz (токен с <https://listenbrainz.org/settings/>).
3. Если есть история на Last.fm, импортируйте её в ListenBrainz (*Settings → Import*), чтобы рекомендации заработали сразу.
4. *Настройки → Plugins*: включить `nd-lyrics`.
5. Клиенты: Symfonium или Tempus (Android), play:Sub или Amperfy (iOS), Feishin (Mac/Windows и веб-версия на `https://<домен>:44444`). В клиенте включить перекодирование вне Wi‑Fi (AAC/Opus 128–256 kbps).

Готово, когда прослушивания видны в Last.fm и ListenBrainz, а в плеере есть синхронный текст.

## Фаза 3. Докачка нового

```bash
cp explo/explo.env.example explo/explo.env && nano explo/explo.env   # ListenBrainz, логин Navidrome, SLSKD_API_KEY
# в .env: SLSK_*, SLSKD_*, EXPLO_*; COMPOSE_PROFILES=discovery
docker compose up -d
```

- slskd: `https://<домен>:11111`, логин `SLSKD_WEB_*`. Скачанное сохраняется в `/mnt/music/soulseek` и появляется в Navidrome без команд (слежение за папкой, плюс скан раз в час).
- Explo: `https://<домен>:22222`, логин `EXPLO_UI_*`, там же расписание (по умолчанию Weekly Exploration раз в неделю, 50 треков).
- Порты меняются в `.env` (`SLSKD_HTTPS_PORT`, `EXPLO_HTTPS_PORT`) и должны быть открыты в ufw.
- Порядок источников: Soulseek (MP3 320/FLAC, не ниже 256 kbps), затем YouTube Music.

Готово, когда в Navidrome появился плейлист Weekly Exploration со скачанными треками и обложками.

## Фаза 4. Моя волна (AudioMuse-AI)

```bash
# в .env: AUDIOMUSE_DB_PASSWORD; COMPOSE_PROFILES=discovery,ai
docker compose up -d
ssh -L 8000:127.0.0.1:8000 <сервер>   # первый вход: http://127.0.0.1:8000
```

1. Setup Wizard: сервер Navidrome, URL `http://navidrome:4533`, логин и пароль Navidrome; задать логин, пароль и **API token** AudioMuse.
2. После мастера (включена аутентификация) интерфейс доступен по `https://<домен>:33333`.
3. Запустить анализ библиотеки ночью. Распознавание текстов (Whisper) отключено ради памяти.
4. В Navidrome *Настройки → Plugins → AudioMuse-AI*: URL `http://audiomuse-flask:8000`, тот же API token.

Готово, когда Instant Mix в Navidrome подбирает по звучанию, а в AudioMuse-AI работает Sonic Fingerprint.

## Фаза 5. Умные плейлисты

Файлы `.nsp` из [`navidrome/playlists/`](navidrome/playlists) `install-plugins.sh` копирует в `/mnt/music/_playlists`; плейлисты обновляются при открытии:

| Файл | Плейлист |
|---|---|
| `favorites.nsp` | Любимое |
| `loved-forgotten.nsp` | Любимое, давно не слушал |
| `top-month.nsp` | Топ месяца |
| `heavy-rotation.nsp` | На повторе |
| `new-this-week.nsp` | Новое за неделю |
| `never-played.nsp` | Ни разу не играло |
| `highly-rated.nsp` | Оценка 4+ |
| `rock-90s.nsp` | пример по жанру и году |

Новые удобно собирать в Feishin. Синтаксис: <https://www.navidrome.org/docs/usage/features/smart-playlists/>.

## Лимиты

| Где | Значение | Настройка |
|---|---|---|
| Navidrome, скан | слежение за папкой + полный скан раз в час | `ND_SCANNER_SCHEDULE` |
| Explo | ~50 треков в неделю (Weekly Exploration) | расписание в Explo UI |
| Soulseek, загрузки | 3 одновременно | `SLSKD_DOWNLOAD_SLOTS` |
| Soulseek, раздача | 2 слота, 2 МиБ/с, только локальная папка `shared` | `SLSKD_UPLOAD_*` |
| Кэш rclone | 45 ГБ на SSD | `rclone-music.service` |
| Выгрузка на Диск | остановка при суточном лимите Google 750 ГБ | `--drive-stop-on-upload-limit` |
| AudioMuse-AI | воркер до 4 ГБ RAM | `AUDIOMUSE_WORKER_MEM_LIMIT` |

## Обновление

Navidrome, AudioMuse-AI и его плагин обновляются только вместе: поменять версии в `.env`, затем `./scripts/install-plugins.sh && docker compose pull && docker compose up -d`.

## Известные ограничения

- Треки с YouTube Explo перекодирует в MP3 со стандартным битрейтом ffmpeg, поэтому основной источник Soulseek.
- FLAC из Soulseek пока сохраняется как есть; конвертация в MP3 320 и beets для тегов в следующей фазе.
- Скачивание музыки без прав нарушает авторские права. Сервер для личного использования, без публичного доступа.
