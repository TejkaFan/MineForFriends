# MineSerc launcher

Обновлятор модов и ресурспаков для сервера MineSerc (Minecraft 26.3, Fabric) через TLauncher.

- `launcher.py` — сам лаунчер (MineSerc.exe): создаёт версию «MineSerc 26.3» в TLauncher, добавляет сервер в «Сетевую игру», качает моды и ресурспаки с облака и включает паки.
- `cloud_server.py` — облако на ПК хоста (порт 8518), раздаёт папку `launcher-cloud` и принимает скины игроков (`POST /skins/upload`). В игре скины показывает мод CustomSkinLoader. Токены ников лежат в `skin_tokens.json` (не в git); чтобы сбросить ник, удали его строку оттуда. Запускается из `start.bat`.
- `update_manifest.py` — пересобирает `launcher-cloud/manifest.json` после изменения модов или паков.
- `get_mods.py` — скачивает моды с Modrinth под Fabric 26.3 вместе с зависимостями.

Эта папка должна лежать в папке сервера рядом с `launcher-cloud/`.

## Окружение

```
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
```

## Сборка MineSerc.exe

```
.venv\Scripts\pyinstaller.exe --noconfirm --onefile --windowed --name MineSerc --distpath dist --workpath build --specpath build launcher.py
copy dist\MineSerc.exe ..\launcher-cloud\MineSerc.exe
```

## Отладка

```
.venv\Scripts\python.exe launcher.py --cli --no-open
```
