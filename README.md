# MineSerc launcher

Обновлятор сборок для наших серверов через TLauncher.

- `launcher.py` — сам лаунчер (MineSerc.exe): для каждой сборки, отмеченной в «Сборки…», создаёт свою версию в TLauncher и свою папку игры, добавляет сервер в «Сетевую игру», качает файлы сборки с облака.
- Сборки в облаке:
  - основная `MineSerc 26.3` — `launcher-cloud/manifest.json`, `mods/`, `resourcepacks/`;
  - дополнительные — `launcher-cloud/packs/<id>/` (все файлы папки игры + `pack.json` с версией, загрузчиком и адресом сервера). Сейчас там `homestead` (Homestead 1.20.1, сервер на порту 25566).
  - Файлы из `"once"` в `pack.json` (например, `options.txt`) кладутся только при первой установке; правки игрока в конфигах живут до следующего обновления этого файла в облаке.
- `custom_packs.py` — вкладка «Свои сборки»: игрок выбирает папку или архив со сборкой (CurseForge, Modrinth `.mrpack`, MultiMC/Prism или просто папку с модами). Лаунчер определяет версию Minecraft и загрузчик (Fabric, Quilt, Forge, NeoForge), докачивает недостающие моды с CurseForge/Modrinth и их зависимости с Modrinth, ставит загрузчик (Forge/NeoForge — официальным установщиком) и создаёт версию TLauncher с папкой игры `.minecraft/custom/<имя>`.
- `cloud_server.py` — облако на ПК хоста (порт 8518), раздаёт папку `launcher-cloud` и принимает скины игроков (`POST /skins/upload`). В игре скины показывает мод CustomSkinLoader. Токены ников лежат в `skin_tokens.json` (не в git); чтобы сбросить ник, удали его строку оттуда. Запускается из `start.bat`.
- `update_manifest.py` — пересобирает манифесты всех сборок, `launcher-cloud/packs.json` и `launcher-cloud/launcher.json` (хэш свежего `MineSerc.exe`) после любых изменений в облаке.
- Лаунчер сам обновляется: при запуске сверяет себя с `launcher.json` и, если в облаке новая версия, скачивает её, подменяет свой exe и перезапускается. Друзьям хватает один раз получить `MineSerc.exe`.
- `make_icon.py` — рисует `icon.ico` (пиксельный блок травы).
- `get_mods.py` — скачивает моды с Modrinth под Fabric 26.3 вместе с зависимостями.

Эта папка должна лежать в папке сервера рядом с `launcher-cloud/`.

## Окружение

```
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
```

## Сборка MineSerc.exe

```
.venv\Scripts\pyinstaller.exe --noconfirm --onefile --windowed --name MineSerc --icon "%CD%\icon.ico" --add-data "%CD%\icon.ico;." --exclude-module minecraft_launcher_lib --distpath dist --workpath build --specpath build launcher.py
copy dist\MineSerc.exe ..\launcher-cloud\MineSerc.exe
.venv\Scripts\python.exe update_manifest.py
```

## Отладка

```
.venv\Scripts\python.exe launcher.py --cli --no-open
```
