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
| [Lidarr](https://lidarr.audio) + [Soularr](https://github.com/mrusse/soularr) | 3.1.0 / 1.2.2 | дискографии исполнителей через Soulseek, `https://<домен>:55555` |
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

**Два имени.** `ND_DOMAIN_ALIAS` в `.env` — второе имя того же сервера: Caddy выпускает сертификат и на него и отдаёт по нему всё то же, на всех портах. Пригодится, если сервер из России открывается только через relay, а прежнее имя менять нельзя (например, оно же адрес VPN-ноды): новое имя ведите на relay и ставьте в `ND_DOMAIN`, прежнее — в `ND_DOMAIN_ALIAS`. После правки: `docker compose up -d caddy feishin`.

```bash
cp .env.example .env && nano .env       # ND_DOMAIN, ACME_EMAIL, HTTPS_PORT
./scripts/install-plugins.sh
docker compose up -d
```

Откройте `https://<домен>/navi`, создайте администратора, загрузите несколько треков в `/mnt/music`.
Готово, когда трек с Диска играет на телефоне через мобильный интернет.

## Фаза 2. Персонализация

1. Ключ Last.fm: <https://www.last.fm/api/account/create> → `LASTFM_API_KEY`, `LASTFM_SECRET` → `docker compose up -d`.
2. В Navidrome: *Настройки → Personal*: подключить Last.fm и ListenBrainz (токен с <https://listenbrainz.org/settings/>).
3. Если есть история на Last.fm, импортируйте её в ListenBrainz (*Settings → Import*), чтобы рекомендации заработали сразу.
4. *Настройки → Plugins*: включить `nd-lyrics`.
5. Клиенты: Symfonium или Tempus (Android), play:Sub или Amperfy (iOS), Feishin (Mac/Windows и веб-версия на `https://<домен>:44444`). Адрес сервера в приложениях — `https://<домен>`, в Feishin на компьютере — `https://<домен>/navi`. В клиенте включить перекодирование вне Wi‑Fi (AAC/Opus 128–256 kbps).

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

### Конвертация в Opus

Сервис `opus-converter` раз в 15 минут перекодирует все MP3/FLAC/M4A старше 30 минут в Opus 192 kbps (`OPUS_BITRATE`) и удаляет оригинал, если длительность совпала. Загрузки в `/mnt/music/soulseek` он не трогает сутки (`DOWNLOADS_MIN_AGE_MINUTES`): их импортируют Soularr и track-search, и перекодирование посреди импорта его срывало. Обложка встраивается в сам файл (до 600 px). MP3 при этом теряют немного качества (повторное сжатие). В Lidarr отключите *Upgrades Allowed* в профиле качества, иначе он будет перекачивать альбомы.

### Дискографии исполнителей (Lidarr + Soularr)

Lidarr хранит список исполнителей и знает их альбомы, Soularr ищет недостающие альбомы через slskd (по 15 за проход, проходы идут друг за другом), Lidarr переносит скачанное в `/mnt/music/lidarr`.

Soularr собирается из `soularr/` с доработками `soularr/soularr_plus.py` (`search_type = priority` в `config.ini`):

- **Запрещённые имена.** Сервер Soulseek рассылает клиентам список запрещённых фраз, и на «The Beatles Abbey Road» не отвечает никто, а «\*eatles Abbey Road» находит тысячи файлов. Если поиск пуст, Soularr пробует название без скобок, имя со звёздочкой вместо первой буквы, звёздочки во всём запросе и одно название альбома (помогает, когда в MusicBrainz имя другое, чем в файлах: Ye и Kanye West). Исполнителей, которым помогли звёздочки, запоминает (`data/soularr/soularr-plus.json`) и дальше ищет сразу так.
- **Очередь по кругу** вместо прохода по алфавиту: сначала лучший релиз каждого исполнителя, потом второй и так далее, чтобы исполнитель с тысячами релизов не занимал все проходы. Пять главных релизов тех, кого попросили кнопкой «Скачать дискографию» в VaginaWave (тег `vaginawave` в Lidarr), идут первыми. В каждом проходе 4 места из 15 — синглам, иначе очередь до них не доходит. У исполнителя первыми — ещё не искавшиеся, альбомы раньше EP, популярные, новые.
- **Повтор с паузой.** Не нашлось — снова через 7, 14, 28 дней, не реже раза в 60; нашлось, но не скачалось — через 1, 2, 4… до 30 дней.
- **Синглы-дубли не ищутся**: если все песни сингла есть на альбоме или EP того же исполнителя, песня придёт с альбомом.
- **Выбор пира.** Из нашедших альбом сначала берутся пиры со свободным слотом, короткой очередью и быстрой отдачей. Если пир не отдал файлы (так срывалось 9 из 10 неудачных загрузок), его до конца прохода не берём и сразу пробуем следующего, до двух раз.
- **Стоп-лист с давностью.** Альбом, который Lidarr не импортировал, Soularr пропускает 14 дней, а потом ищет снова: часто причина была разовой.

Что возьмут следующие проходы, без поиска и загрузок: `docker compose exec soularr python /app/soularr.py --plan`.

1. `./scripts/install-plugins.sh` (создаст `data/soularr/config.ini` и `/mnt/music/lidarr`), `docker compose up -d`.
2. `https://<домен>:55555`: задать логин и пароль Lidarr.
3. *Settings → Media Management → Root Folders*: добавить `/mnt/music/lidarr`.
4. *Settings → General → API Key* скопировать в `data/soularr/config.ini` (`[Lidarr] api_key`), туда же `SLSKD_API_KEY` из `.env` (`[Slskd] api_key`), затем `docker compose restart soularr`.
5. *Artist → Add New*: найти исполнителя, Monitor: All Albums, нажать *Add*. Через несколько минут Soularr начнёт качать (`docker compose logs -f soularr`).

**Заблокированные альбомы.** Некоторые названия Soulseek скрывает и со звёздочками (My Beautiful Dark Twisted Fantasy). `scripts/track-search.py` берёт альбомы, которые Soularr не нашёл, ищет их по названиям треков (для запрещённых имён — со звёздочкой), скачивает папку целиком и, когда все файлы на диске, импортирует в Lidarr с явным альбомом и изданием (папки от пиров называются как угодно, «CD 1», и по имени Lidarr их не узнаёт); через сутки без файлов загрузка считается неудачной, а не висит вечно. Два запуска одновременно не идут (`data/track-search.lock`):

```bash
python3 scripts/track-search.py --limit 20
# автоматически раз в 30 минут (crontab -e):
*/30 * * * * cd /root/navidrome-stack && python3 scripts/track-search.py >> data/track-search.log 2>&1
```

**Дискографии всех, кто есть в библиотеке.** `scripts/lidarr-artists.py` добавляет в Lidarr исполнителей, у которых в Navidrome 3 и больше трека, и включает слежение за всеми их альбомами и новыми релизами. Исполнитель ищется по MusicBrainz ID из тегов, а без него — по имени с точностью до транслита, порядка слов и артикля (Zemfira = Земфира, Cure = The Cure, Béla Bartók = Bartók Béla); из «Moby Featuring Mylene Farmer», «David Bowie/Tony Visconti» берётся главный. Тёзок (Drake, Chief Keef, Tony Bennett) и другое написание (Splean = Сплин, Skryptonite = Скриптонит, A$AP Ferg = FERG) различают альбомы из библиотеки (Lidarr находит их по названию и называет исполнителя), псевдонимы и размер дискографии из метаданных Lidarr (api.lidarr.audio); без уверенности скрипт никого не добавляет. Кого не нашёл, ищет снова через 30 дней или сразу с `--retry`. Той же логикой пользуется кнопка «Скачать дискографию» в VaginaWave. Ошибочно найденного тёзку удаляйте в Lidarr с галочкой *Add List Exclusion*: исключённых скрипт не добавляет снова (так убран диджей Jay Z, которого MusicBrainz ID из тегов подставил вместо JAŸ-Z):

**Состав альбома не считается.** У джазовых и классических пластинок в тегах каждого трека стоят все музыканты («Marc Copland • Joey Baron • Drew Gress»), и раньше каждый сайдмен получал с одного альбома 8–15 треков, а следующей ночью — всю свою дискографию; через три ночи из скачанного так добавлялись уже их собственные сайдмены: за 20–22 ноября 54 джазмена. Теперь трек засчитывается первому исполнителю и автору альбома всегда, а остальным — только если это гость на нескольких треках (фит), а не музыкант, который стоит в тегах половины треков альбома и больше (от 4 треков). Фиты в рэпе так и засчитываются: у Drake и Nicki Minaj счёт не изменился.

**Классика.** «Дискография» Баха, Моцарта или Шуберта в MusicBrainz — это все записи их музыки любыми оркестрами (у Баха 4,6 тыс. релизов), а у оркестра, дирижёра и солиста — сотни записей общего репертуара. К тому же в тегах каждого скачанного классического альбома стоят оркестр, дирижёр и солист, у каждого сразу набирается 3 трека, и их дискографии добавлялись следом: за четыре дня ноября из 81 нового исполнителя 50 были из классики, Missing рос быстрее, чем качался. Композиторов, оркестры, хоры, дирижёров и солистов классики `lidarr-artists.py` ставит на профиль метаданных None: Lidarr убирает из Missing всё без файлов, скачанное остаётся. Классика — это первый жанр в метаданных Lidarr (Classical, Opera, Baroque…), а у кого жанров нет — тип «оркестр» или «хор» в MusicBrainz или описание вроде «cellist», «French tenor». Композиторов кино и игр (Хисаиси) правило не трогает, а исполнителя, чью дискографию попросили из VaginaWave (тег `vaginawave`), не трогает тоже. Вернуть кому-то всю дискографию — сменить ему профиль метаданных в Lidarr и поставить тег `vaginawave`.

**Скачанное, но не импортированное.** `scripts/recover-imports.py` раз в сутки заново спрашивает Lidarr о папках в `soulseek/failed_imports` и о старых загрузках в `soulseek` и импортирует те, которые он теперь относит к одному альбому без замечаний (так возвращаются альбомы, сорванные конвертером или непонятным именем папки), и снимает их со стоп-листа Soularr:

```bash
python3 scripts/recover-imports.py            # показать, что можно вернуть
50 4 * * * cd /root/navidrome-stack && /usr/bin/python3 scripts/recover-imports.py --apply >> data/recover-imports.log 2>&1
```

```bash
python3 scripts/lidarr-artists.py            # показать, что изменится
python3 scripts/lidarr-artists.py --apply    # применить
# раз в сутки (crontab -e):
40 4 * * * cd /root/navidrome-stack && /usr/bin/python3 scripts/lidarr-artists.py --apply >> data/lidarr-artists.log 2>&1
```

**Синглы и EP.** Профиль метаданных Standard ждёт только студийные альбомы, а многие исполнители выпускают в основном синглы (у MORGENSHTERN 7 альбомов и 58 синглов). Поэтому у всех исполнителей стоит профиль «Альбомы, EP и синглы» (студийные, без ремиксов, сборников и концертов), и он же — профиль по умолчанию корневой папки: с ним добавляют исполнителей `scripts/lidarr-artists.py` и VaginaWave. Композиторы мюзиклов (Alan Menken, John Kander и др.) стоят на профиле None: их «дискографии» — сотни саундтреков.

**Раздача в Soulseek.** Многие пиры не отдают файлы тем, кто сам мало раздаёт («Transfer rejected: File not shared», около тысячи отказов в сутки при раздаче 2 ГБ). `scripts/soulseek-share.py` копирует самые слушаемые альбомы из папки Lidarr (по крону — 10 ГБ, `--size 10`, раз в сутки, и сам просит slskd пересканировать раздачу) на локальный диск, в `${BUFFER_DIR}/shared`, которую slskd раздаёт как `/shared`: облако при отдаче не читается. Ограничения: `SLSKD_UPLOAD_SPEED_LIMIT_KIB=512` и 2 слота в `.env`, на одного пользователя 600 МБ в очереди, 1,5 ГБ в день и 5 ГБ в неделю, тем, кто сам ничего не раздаёт, — 1 слот и 64 КБ/с (`data/slskd/slskd.yml`, раздел `transfers`).

```bash
python3 scripts/soulseek-share.py             # показать, что попадёт в раздачу
python3 scripts/soulseek-share.py --apply     # скопировать; затем пересканировать раздачу:
curl -X PUT -H "X-API-Key: $SLSKD_API_KEY" http://127.0.0.1:5030/api/v0/shares
```

**Докачка с YouTube Music** (`youtube/`, сервис `youtube-fallback`, выключен). Берёт из «Missing» альбомы, на которых Soularr споткнулся, и синглы, находит релиз в YouTube Music, сверяет треки с Lidarr, качает звук без перекодирования, пишет теги MusicBrainz и отдаёт Lidarr на импорт. Поиск работает, но с IP этого сервера YouTube не отдаёт звук без проверки «вы не робот», поэтому сервис в отдельном профиле и сам не запускается. Включить: `docker compose --profile youtube up -d youtube-fallback`; проверить без скачивания: `docker compose run --rm youtube-fallback python /app/fallback.py --once --dry`.

### Дубли и разбитые альбомы

Треки из плейлистов (explo) и Soulseek приходят поштучно, поэтому одна песня оказывается и отдельным треком, и в альбоме из Lidarr, а мюзикл распадается на альбомы по исполнителям (у каждого трека свой «исполнитель альбома»).

- `scripts/dedupe-library.py` находит копии песен, которые есть в Lidarr (то же название ±3 с, тот же исполнитель с учётом транслита или тот же альбом), и пишет отчёт `data/dedupe-report.tsv`. С `--apply` на полминуты останавливает Navidrome, переносит лайки, прослушивания, оценки и записи плейлистов на копию из Lidarr (копия базы — `data/navidrome/navidrome.db.bak-dedupe-*`), удаляет файлы и запускает сканирование.
- `scripts/tidy-albums.py` склеивает части одного релиза: выставляет файлам explo и Soulseek название, исполнителя альбома (у мюзиклов — состав, у саундтреков — Various Artists), дату и MusicBrainz ID главной части. Файлы Lidarr не трогает; похожие версии песен, которые в альбоме уже есть, складывает в `data/tidy-albums-variants.tsv` для ручной проверки. Старые теги — в `data/tidy-albums-*.json`.

```bash
python3 scripts/dedupe-library.py && python3 scripts/tidy-albums.py    # только отчёты
python3 scripts/dedupe-library.py --apply
docker run --rm -v /root/navidrome-stack:/stack -v /mnt/music:/mnt/music python:3.13-slim \
  sh -c "pip install -q --root-user-action=ignore mutagen && python /stack/scripts/tidy-albums.py --apply"
```

## Фаза 4. Моя волна (AudioMuse-AI)

```bash
# в .env: AUDIOMUSE_DB_PASSWORD; COMPOSE_PROFILES=discovery,ai
docker compose up -d
ssh -L 8000:127.0.0.1:8000 <сервер>   # первый вход: http://127.0.0.1:8000
```

1. Setup Wizard: сервер Navidrome, URL `http://navidrome:4533/navi`, логин и пароль Navidrome; задать логин, пароль и **API token** AudioMuse.
2. После мастера (включена аутентификация) интерфейс доступен по `https://<домен>:33333`.
3. Запустить анализ библиотеки ночью. Распознавание текстов (Whisper) отключено ради памяти. Новые треки AudioMuse анализирует сам: на странице *Cron* заведён ежедневный анализ в 03:30.
4. В Navidrome *Настройки → Plugins → AudioMuse-AI*: URL `http://audiomuse-flask:8000`, тот же API token. Поле *Server Name* оставить пустым или вписать имя сервера из мастера AudioMuse, иначе AudioMuse отвечает «Unknown server».

Плейлисты кластеризации AudioMuse называются тегами вроде `Indie_Rock_Fast_Danceable_automatic`. `scripts/tidy-playlists.py` переименовывает их в короткие русские («Инди, рок · быстро, танцевально») и ставит обложку по настроению; при новой кластеризации удаляет прошлый набор. Запуск по cron: `15 * * * * cd /root/navidrome-stack && python3 scripts/tidy-playlists.py >> data/tidy-playlists.log 2>&1`.

Готово, когда Instant Mix в Navidrome подбирает по звучанию, а в AudioMuse-AI работает Sonic Fingerprint.

### VaginaWave

Клиент с «Моей волной» живёт отдельным контейнером в `/opt/volna` (репозиторий RuNick7/VaginaWave), в той же сети Docker. Caddy отдаёт ему корень домена `https://<домен>/` и прежний адрес `https://<домен>/volna/`.

Navidrome живёт под `/navi` (`ND_BASEURL`): веб-интерфейс `https://<домен>/navi`, внутри стека — `http://navidrome:4533/navi` (скрипты, Explo, AudioMuse). Subsonic API (`/rest/*`) Caddy отдаёт в Navidrome и из корня, поэтому приложениям и VaginaWave с адресом `https://<домен>` менять ничего не нужно. Feishin на компьютере ходит и в API веб-интерфейса, ему нужен адрес `https://<домен>/navi`. Старые закладки `/app` и ссылки `/share/…` перенаправляются под `/navi`.

### Исполнители через запятую

По умолчанию Navidrome делит исполнителей только по `feat.`, `/` и `;`, поэтому «Скриптонит, Andy Panda» становится одним исполнителем. В [`navidrome/navidrome.toml`](navidrome/navidrome.toml) добавлено деление по `, `, ` & `, ` x `; группы с запятой или & в названии перечислены в `ArtistSplitExceptions`. Теги `ARTISTS`/`ALBUMARTISTS` (списки от Lidarr) делятся по `;`, `/` и `, `: при перекодировании в Opus ffmpeg склеивает список в одну строку вроде «9mice;Kai Angel». Группы из Lidarr с такими знаками в названии («Joey Valence & Brae», «25/17») добавляет в исключения `python3 scripts/split-exceptions.py`. После изменения: `docker compose up -d navidrome && docker compose exec navidrome /app/navidrome scan --full`.

## Фаза 5. Умные плейлисты

Файлы `.nsp` из [`navidrome/playlists/`](navidrome/playlists) `install-plugins.sh` копирует в `/mnt/music/_playlists`; плейлисты обновляются при открытии:

| Файл | Плейлист |
|---|---|
| `my-wave.nsp` | Моя волна: один стартовый трек из любимого, частого или нового, дальше похожие (непрерывное воспроизведение / Auto DJ) |
| `favorites.nsp` | Любимое |
| `loved-forgotten.nsp` | Любимое, давно не слушал |
| `top-month.nsp` | Топ месяца |
| `heavy-rotation.nsp` | На повторе |
| `new-this-week.nsp` | Новое за неделю |
| `never-played.nsp` | Ни разу не играло |
| `highly-rated.nsp` | Оценка 4+ |

Новые удобно собирать в Feishin. Синтаксис: <https://www.navidrome.org/docs/usage/features/smart-playlists/>.

## Лимиты

| Где | Значение | Настройка |
|---|---|---|
| Navidrome, скан | слежение за папкой + полный скан раз в час | `ND_SCANNER_SCHEDULE` |
| Explo | ~50 треков в неделю (Weekly Exploration) | расписание в Explo UI |
| Soulseek, загрузки | 12 файлов одновременно | `SLSKD_DOWNLOAD_SLOTS` |
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

## Что происходит на сервере

`python3 scripts/status.py` (или `status.py 6` за 6 часов): контейнеры, новые треки по папкам, сколько ещё не в Opus, очередь Lidarr и импорт за сутки, итоги Soularr/slskd/track-search, конвертер и последние ошибки каждого сервиса.
