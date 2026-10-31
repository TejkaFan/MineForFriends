# MineForFriends by TEJKA

Лаунчер-помощник для игры с друзьями через TLauncher: наши сборки и серверы, свои сборки, хостинг сервера из сборки и скины.

## Скачать

**[MineForFriends.exe — последняя версия](https://github.com/TejkaFan/MineForFriends/releases/latest/download/MineForFriends.exe)**

1. Установи [TLauncher](https://tlauncher.org/) и [Radmin VPN](https://www.radmin-vpn.com/ru/), зайди в нашу сеть Radmin (название и пароль — у хоста).
2. Скачай `MineForFriends.exe` по ссылке выше и запусти. Если Windows покажет «Система Windows защитила ваш компьютер» — «Подробнее» → «Выполнить в любом случае».
3. Дальше лаунчер обновляется сам.

## Для разработки

«MineSerc» ниже — имя нашего основного сервера и его сборки, а не приложения.

- `launcher.py` — сам лаунчер (`MineForFriends.exe`): для каждой сборки, отмеченной на карточках «Наши серверы», создаёт свою версию в TLauncher и свою папку игры, добавляет сервер в «Сетевую игру», качает файлы сборки с облака.
- Сборки в облаке:
  - основная `MineSerc 26.3` — `launcher-cloud/manifest.json`, `mods/`, `resourcepacks/`;
  - дополнительные — `launcher-cloud/packs/<id>/` (все файлы папки игры + `pack.json` с версией, загрузчиком и адресом сервера). Сейчас там `homestead` (Homestead 1.20.1, сервер на порту 25566).
  - Файлы из `"once"` в `pack.json` (например, `options.txt`) кладутся только при первой установке; правки игрока в конфигах живут до следующего обновления этого файла в облаке.
- `ui/` — интерфейс лаунчера (HTML/CSS/JS, тёмная тема «Ночной мир») в окне pywebview на WebView2; логика — класс `Api` в `launcher.py`. Шрифт Press Start 2P (OFL), иконки Tabler (MIT) и 3D-предпросмотр скина skinview3d (MIT, `ui/vendor`) лежат локально.
- `custom_packs.py` — вкладка «Свои сборки»: игрок выбирает папку или архив со сборкой (CurseForge, Modrinth `.mrpack`, MultiMC/Prism или просто папку с модами). Лаунчер определяет версию Minecraft и загрузчик (Fabric, Quilt, Forge, NeoForge), докачивает недостающие моды с CurseForge/Modrinth и их зависимости с Modrinth, ставит загрузчик (Forge/NeoForge — официальным установщиком) и создаёт версию TLauncher с папкой игры `.minecraft/custom/<имя>`.
- `server_host.py` — вкладка «Сервер»: из сборки, добавленной в «Свои сборки», собирает сервер в выбранной папке — моды без клиентских (по метаданным и по Modrinth, нужные другим модам возвращаются), конфиги, KubeJS и датапаки, серверный загрузчик, полная Java нужной версии (или официальная от Mojang), `start.bat`, `server.properties` с датапаками OpenLoader в `initial-enabled-packs`. Настройки сервера правятся в окне лаунчера.
- `cloud_server.py` — облако на ПК хоста (порт 8518), раздаёт папку `launcher-cloud` и принимает скины игроков (`POST /skins/upload`). В игре скины показывает мод CustomSkinLoader. Токены ников лежат в `skin_tokens.json` (не в git); чтобы сбросить ник, удали его строку оттуда. Запускается из `start.bat`.
- `update_manifest.py` — пересобирает манифесты всех сборок, `launcher-cloud/packs.json` и `launcher-cloud/launcher.json` (хэш свежего `MineForFriends.exe`; копия `MineSerc.exe` — для лаунчеров до переименования) после любых изменений в облаке.
- Лаунчер сам обновляется: при запуске сверяет себя с последним релизом на GitHub (или с `launcher.json` в облаке, если GitHub недоступен) и, если вышла новая версия, скачивает её, подменяет свой exe и перезапускается. Друзьям хватает один раз получить `MineForFriends.exe`. Настройки прежнего MineSerc (`%APPDATA%\MineSerc`) переезжают в `%APPDATA%\MineForFriends` сами.
- `make_icon.py` — рисует `icon.ico` (пиксельный блок травы).
- `get_mods.py` — скачивает моды с Modrinth под Fabric 26.3 вместе с зависимостями.

Эта папка должна лежать в папке сервера рядом с `launcher-cloud/`.

## Окружение

```
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
```

## Сборка MineForFriends.exe

```
.venv\Scripts\python.exe -m PyInstaller --noconfirm --onefile --windowed --name MineForFriends --icon "%CD%\icon.ico" --add-data "%CD%\icon.ico;." --add-data "%CD%\ui;ui" --exclude-module minecraft_launcher_lib --exclude-module tkinter --distpath dist --workpath build --specpath build launcher.py
copy dist\MineForFriends.exe ..\launcher-cloud\MineForFriends.exe
.venv\Scripts\python.exe update_manifest.py
gh release create v<версия> dist\MineForFriends.exe --title "MineForFriends <версия>" --latest
```

Версию (`APP_VERSION` в `launcher.py`) поднимай перед каждой сборкой. Лаунчер берёт обновления из последнего релиза на GitHub (сверяет SHA-256 файла), а если GitHub недоступен — из облака; старые версии до 0.5.1 знают только облако, поэтому выкладывай в оба места.

## Отладка

```
.venv\Scripts\python.exe launcher.py --cli --no-open
```

`MineForFriends.exe --no-update` — запустить свежесобранный exe без самообновления из облака.
