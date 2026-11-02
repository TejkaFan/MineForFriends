"""MineForFriends by TEJKA — лаунчер-помощник для игры с друзьями через TLauncher.

Для каждой отмеченной сборки (карточки «Наши серверы») создаёт в TLauncher свою версию
(Fabric, сервер в «Сетевой игре», отдельная папка игры), синхронизирует её файлы
с облака на ПК хоста (через Radmin) и открывает TLauncher.
Ещё загружает скин игрока в облако; в игре его показывает мод CustomSkinLoader.
Интерфейс — HTML-страница из папки ui/ в окне pywebview (WebView2).
"""
import base64
import ctypes
import functools
import hashlib
import io
import json
import os
import re
import secrets
import shutil
import socket
import subprocess
import sys
import threading
import time
import traceback
import urllib.parse
import webbrowser
import zipfile
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

import requests
import webview

import custom_packs
from cloud_server import MAX_SKIN, NICK_RE, png_size, valid_skin_size
import crashes
import friends
import mod_updates
import recipes
import relay
import reports
import screenshots
import server_admin
import server_host
import share

APP_NAME = "MineForFriends by TEJKA"
APP_VERSION = "0.10.1"
# Короткое имя без пробелов: папка настроек, имя exe, загрузчик скинов
APP_ID = "MineForFriends"
LAUNCHER_EXE = f"{APP_ID}.exe"
# Прежнее имя приложения: забираем его настройки и чистим его следы
OLD_APP_ID = "MineSerc"
CLOUD_URL = "http://26.199.180.115:8518"
# Каталог скинов: там можно скачать PNG и выбрать его кнопкой «Выбрать скин»
SKINS_SITE = "https://www.minecraftskins.com/"
# Официальный сайт TLauncher: скачивать установщик программой сайт не даёт, только из браузера
TLAUNCHER_SITE = "https://tlauncher.org/"
REPO_URL = "https://github.com/TejkaFan/MineForFriends"
APPDATA = Path(os.environ.get("APPDATA", Path.home()))
TL_PROPERTIES = APPDATA / ".tlauncher" / "tlauncher-2.0.properties"
SETTINGS_FILE = APPDATA / APP_ID / "settings.json"
OLD_SETTINGS_FILE = APPDATA / OLD_APP_ID / "settings.json"
# Копия последнего загруженного скина — для аватарки в шапке
SKIN_FILE = SETTINGS_FILE.parent / "skin.png"
CAPE_FILE = SETTINGS_FILE.parent / "cape.png"
MOJANG_MANIFEST = "https://piston-meta.mojang.com/mc/game/version_manifest_v2.json"

# Основная сборка; старый манифест облака не содержит этих полей
DEFAULT_PACK = {
    "id": "mineserc", "name": "MineSerc 26.3", "manifest": "manifest.json", "base": "",
    "version_id": "MineSerc 26.3", "game_dir": "mineserc", "server_name": "MineSerc",
    "server": "26.199.180.115:25565",
}
# Как часто обновлять индикатор серверов, мс
STATUS_REFRESH = 30_000
# Настройки из окна «О лаунчере»: крестик сворачивает в трей; уведомления о серверах друзей
OPTIONS = {"tray": True, "notify": True, "presence": True}
RADMIN_SITE = "https://www.radmin-vpn.com/ru/"
# Сколько файлов сборки качать одновременно
DOWNLOAD_THREADS = 6
# Описание свежего MineForFriends.exe в облаке ({"sha1", "size"}), пишет update_manifest.py
LAUNCHER_INFO = "launcher.json"
# Последний релиз на GitHub: основной источник обновлений лаунчера, работает и без Radmin
GITHUB_LATEST = "https://api.github.com/repos/TejkaFan/MineForFriends/releases/latest"
# Список обновлений — описания релизов; последний полученный хранится на случай без интернета
GITHUB_RELEASES = "https://api.github.com/repos/TejkaFan/MineForFriends/releases?per_page=20"
RELEASES_CACHE = SETTINGS_FILE.parent / "releases.json"
# Наши сборки в интернете (publish_packs.py): ставятся и без Radmin. Моды — с Modrinth, остальное —
# из служебного предрелиза packs; облако хоста, если отвечает, — быстрый источник тех же файлов
PACKS_WEB = "https://github.com/TejkaFan/MineForFriends/releases/download/packs/"
PACKS_CACHE = SETTINGS_FILE.parent / "packs"
# Индикатор серверов обновляется часто, а список сборок меняется редко
PACKS_LIST_TTL = 600
# Скины: читает их CustomSkinLoader прямо с GitHub, а загружает Cloudflare Worker (skins-worker/)
SKINS_ROOT = "https://raw.githubusercontent.com/TejkaFan/MineForFriends-skins/main/skins/"
# Внутри exe ресурсы лежат во временной папке PyInstaller
RESOURCES = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))

# К облаку ходим напрямую: системный прокси (VPN-клиенты) не видит сеть Radmin
cloud = requests.Session()
cloud.trust_env = False
# Сайты загрузчиков, модов и обновлений: при обрыве связи — в обход через облако хоста
web = relay.protect(requests.Session())


def migrate_settings() -> None:
    # Настройки прежнего MineSerc (выбранные сборки, свои сборки, серверы) переезжают один раз
    if not SETTINGS_FILE.exists() and OLD_SETTINGS_FILE.exists():
        try:
            SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
            SETTINGS_FILE.write_bytes(OLD_SETTINGS_FILE.read_bytes())
        except OSError:
            pass


def load_json(path: Path, default):
    try:
        # utf-8-sig: Блокнот и PowerShell сохраняют с BOM — без него файл не прочитался бы и затёрся
        return json.loads(path.read_text("utf-8-sig"))
    except (OSError, ValueError):
        return default


def save_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")


def sha1(path: Path, algo: str = "sha1") -> str:
    h = hashlib.new(algo)
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def nbt_string(key: str, value: str) -> bytes:
    k, v = key.encode(), value.encode()
    return b"\x08" + len(k).to_bytes(2, "big") + k + len(v).to_bytes(2, "big") + v


def ensure_server(path: Path, name: str, ip: str) -> None:
    """Добавляет сервер первым в servers.dat (несжатый NBT), если его там ещё нет."""
    entry = nbt_string("name", name) + nbt_string("ip", ip) + b"\x00"
    head = b"\x0a\x00\x00\x09\x00\x07servers"
    data = path.read_bytes() if path.exists() else b""

    if not data:
        path.write_bytes(head + b"\x0a" + (1).to_bytes(4, "big") + entry + b"\x00")
        return
    if nbt_string("ip", ip) in data:
        return
    if data.startswith(head + b"\x0a"):
        pos = len(head) + 1
        count = int.from_bytes(data[pos:pos + 4], "big")
        path.write_bytes(data[:pos] + (count + 1).to_bytes(4, "big") + entry + data[pos + 4:])
    elif data.startswith(head + b"\x00\x00\x00\x00\x00"):
        # Пустой список
        rest = data[len(head) + 5:]
        path.write_bytes(head + b"\x0a" + (1).to_bytes(4, "big") + entry + rest)
    # Иначе формат неожиданный — файл не трогаем


def varint(n: int) -> bytes:
    n &= 0xFFFFFFFF
    out = b""
    while True:
        byte, n = n & 0x7F, n >> 7
        out += bytes([byte | (0x80 if n else 0)])
        if not n:
            return out


def server_status(address: str, timeout: float = 3):
    """Опрос сервера как в списке серверов игры: {"online", "max", "version"} или None, если выключен."""
    host, _, port = address.rpartition(":")
    try:
        with socket.create_connection((host, int(port)), timeout=timeout) as sock:
            sock.settimeout(timeout)
            host_b = host.encode()
            # Рукопожатие (протокол -1 — «любой») и запрос статуса
            hello = b"\x00" + varint(-1) + varint(len(host_b)) + host_b + int(port).to_bytes(2, "big") + varint(1)
            # Отдельными посылками, как игра: модовый сервер может не успеть переключиться
            # на запрос статуса, если оба пакета придут одним куском
            sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
            sock.sendall(varint(len(hello)) + hello)
            time.sleep(0.1)
            sock.sendall(b"\x01\x00")
            f = sock.makefile("rb")

            def read_varint():
                value = 0
                for i in range(5):
                    b = f.read(1)
                    if not b:
                        raise OSError("соединение закрыто")
                    value |= (b[0] & 0x7F) << (7 * i)
                    if not b[0] & 0x80:
                        return value
                raise OSError("неверный ответ")

            read_varint()  # длина пакета
            read_varint()  # id пакета
            data = json.loads(f.read(read_varint()).decode("utf-8", "replace"))
    except (OSError, ValueError):
        return None
    players = data.get("players") or {}
    # Сервер отдаёт до 12 ников онлайн; моды иногда подмешивают туда строки вроде «...and 5 more»
    names = [p.get("name", "") for p in players.get("sample") or [] if isinstance(p, dict)]
    return {"online": players.get("online", 0), "max": players.get("max", 0),
            "version": (data.get("version") or {}).get("name", ""),
            "names": [n for n in names if NICK_RE.match(n)]}


def tlauncher_nick(mc_dir: Path) -> str:
    # Ник выбранного аккаунта TLauncher; пусто, если не нашли
    data = load_json(mc_dir / "TlauncherProfiles.json", {})
    account = data.get("accounts", {}).get(data.get("selectedAccountUUID") or "", {})
    profile = account.get("selectedProfile") or {}
    return profile.get("name") or account.get("displayName") or account.get("username") or ""


def minecraft_dir() -> Path:
    # Папка игры из настроек TLauncher, иначе стандартная
    try:
        for line in TL_PROPERTIES.read_text("utf-8", errors="ignore").splitlines():
            if line.startswith("minecraft.gamedir="):
                value = line.split("=", 1)[1].replace("\\:", ":").replace("\\\\", "\\").strip()
                if value:
                    return Path(value)
    except OSError:
        pass
    return APPDATA / ".minecraft"


def setup_skin_loader(game_dir: Path) -> None:
    # Наши скины с GitHub первыми в списке CustomSkinLoader, остальные его настройки не трогаем
    path = game_dir / "CustomSkinLoader" / "CustomSkinLoader.json"
    config = load_json(path, {})
    ours = {"name": APP_ID, "type": "CustomSkinAPI", "root": SKINS_ROOT}
    loadlist = config.get("loadlist") or []
    if loadlist and loadlist[0] == ours:
        return
    # Прежняя запись нашего облака тоже уходит: скины теперь только на GitHub
    config["loadlist"] = [ours] + [p for p in loadlist if p.get("name") not in (APP_ID, OLD_APP_ID)]
    save_json(path, config)


def prepare_workdir(game_dir, mc_dir: Path) -> None:
    """TLauncher запускает игру из папки .minecraft, а не из папки сборки. FancyMenu (меню и загрузочный
    экран, есть в Homestead) ищет config/fancymenu/customizablemenus.txt и custom_locals от этой рабочей
    папки и без них падает при запуске (NullPointerException в ScreenCustomization). Кладём их туда
    из запускаемой сборки."""
    source = Path(game_dir) / "config" / "fancymenu"
    if not any((Path(game_dir) / "mods").glob("fancymenu*.jar")):
        return
    target = mc_dir / "config" / "fancymenu"
    try:
        (target / "custom_locals").mkdir(parents=True, exist_ok=True)
        menus = source / "customizablemenus.txt"
        if menus.is_file():
            shutil.copyfile(menus, target / "customizablemenus.txt")
    except OSError:
        traceback.print_exc()


def tlauncher_exe():
    # Установщик TLauncher кладёт TLauncher.exe в папку игры; None — TLauncher не установлен
    for folder in dict.fromkeys([minecraft_dir(), APPDATA / ".minecraft"]):
        if (folder / "TLauncher.exe").exists():
            return folder / "TLauncher.exe"
    return None


def selected_packs() -> list:
    return load_json(SETTINGS_FILE, {}).get("packs") or [DEFAULT_PACK["id"]]


def save_selected_packs(ids: list) -> None:
    settings = load_json(SETTINGS_FILE, {})
    settings["packs"] = ids
    save_json(SETTINGS_FILE, settings)


# Последний полученный список сборок: индикатор серверов не спрашивает GitHub каждые 30 секунд
_packs_list = {"at": -PACKS_LIST_TTL, "packs": []}


def normalize_pack(entry: dict, manifest: dict) -> dict:
    """Приводит манифест сборки к одному виду: список файлов с путями внутри папки игры."""
    pack = {**DEFAULT_PACK, **entry, **{k: v for k, v in manifest.items() if k not in ("files", "mods", "resourcepacks")}}
    if "files" in manifest:
        pack["files"] = manifest["files"]
        pack["enable"] = manifest.get("enable_resourcepacks", [])
    else:
        # Старый формат основной сборки: отдельные списки модов и ресурспаков
        mods = manifest.get("mods", [])
        packs = manifest.get("resourcepacks", [])
        pack["files"] = ([{"path": f"mods/{m['file']}", "sha1": m["sha1"], "size": m["size"]} for m in mods]
                         + [{"path": f"resourcepacks/{p['file']}", "sha1": p["sha1"], "size": p["size"]} for p in packs])
        pack["enable"] = [p["file"] for p in packs]
    pack.setdefault("once", [])
    return pack


def inside(base: Path, rel: str):
    # Путь из манифеста не должен выводить за пределы папки игры
    path = (base / rel).resolve()
    return path if base.resolve() in path.parents else None


def launcher_sources() -> list:
    """Откуда взять свежий MineForFriends.exe: [(сессия, ссылка, алгоритм хэша, хэш)] по порядку.

    Облако хоста — первым, если отвечает: по Radmin быстро, а из России GitHub без VPN
    отдаёт маленькие ответы, но обрывает скачивание exe. Затем последний релиз на GitHub.
    """
    sources = []
    try:
        info = cloud.get(f"{relay.CLOUD}/{LAUNCHER_INFO}", timeout=(2, 5)).json()
        if info.get("sha1"):
            sources.append((cloud, f"{relay.CLOUD}/{LAUNCHER_EXE}", "sha1", info["sha1"]))
    except (requests.RequestException, ValueError):
        traceback.print_exc()
    try:
        r = web.get(GITHUB_LATEST, timeout=8, headers={"Accept": "application/vnd.github+json"})
        r.raise_for_status()
        asset = next((a for a in r.json().get("assets", []) if a.get("name") == LAUNCHER_EXE), None)
        algo, _, digest = str((asset or {}).get("digest") or "").partition(":")
        if algo == "sha256" and digest:
            sources.append((web, asset["browser_download_url"], "sha256", digest))
    except (requests.RequestException, ValueError, KeyError):
        traceback.print_exc()
    return sources


def release_notes():
    """Релизы лаунчера с GitHub, новые сверху: [{version, date, notes}]. Без сети — из кэша."""
    try:
        r = web.get(GITHUB_RELEASES, timeout=8, headers={"Accept": "application/vnd.github+json"})
        r.raise_for_status()
        releases = [{"version": (rel.get("name") or rel["tag_name"]).removeprefix("MineForFriends ").strip(),
                     "date": (rel.get("published_at") or "")[:10], "notes": rel.get("body") or ""}
                    for rel in r.json() if not rel.get("draft") and not rel.get("prerelease")]
        save_json(RELEASES_CACHE, releases)
        return releases
    except (requests.RequestException, ValueError, KeyError, TypeError):
        traceback.print_exc()
    releases = load_json(RELEASES_CACHE, None)
    if releases is None:
        raise RuntimeError("Не удалось загрузить список обновлений с GitHub. Проверь интернет.")
    return releases


def self_update(status) -> bool:
    """Если вышел новый MineForFriends.exe — ставит его на место текущего и запускает.

    Запущенный exe Windows не даёт перезаписать, но даёт переименовать: старый
    уезжает в <имя>.old.exe и удаляется при следующем запуске. Имя файла у игрока
    не меняется, чтобы не ломать его ярлыки.
    True — новая версия запущена, этот экземпляр нужно закрыть.
    """
    # --no-update: проверить свежесобранный exe, пока не выложен
    if not getattr(sys, "frozen", False) or "--no-update" in sys.argv:
        return False
    exe = Path(sys.executable)
    old = exe.with_name(exe.stem + ".old.exe")
    try:
        old.unlink(missing_ok=True)
    except OSError:
        pass  # прошлый экземпляр ещё закрывается
    for session, url, algo, digest in launcher_sources():
        try:
            if sha1(exe, algo) == digest:
                return False
            status("Скачиваю новую версию лаунчера…")
            part = exe.with_name(exe.name + ".part")
            with session.get(url, stream=True, timeout=30) as r:
                r.raise_for_status()
                total, done, shown = int(r.headers.get("Content-Length") or 0), 0, -1
                with part.open("wb") as f:
                    for chunk in r.iter_content(1 << 16):
                        f.write(chunk)
                        done += len(chunk)
                        if total and done * 100 // total != shown:
                            shown = done * 100 // total
                            status(f"Скачиваю новую версию лаунчера… {shown}%")
            if sha1(part, algo) != digest:
                part.unlink()
                continue
            exe.replace(old)
            try:
                part.replace(exe)
            except OSError:
                old.replace(exe)
                raise
            break
        except (requests.RequestException, ValueError, OSError):
            # Обрыв загрузки — пробуем следующий источник; не вышло нигде — работаем на текущей версии
            traceback.print_exc()
    else:
        return False
    # Новый exe должен распаковаться заново, а не подхватить временную папку этого
    env = {**os.environ, "PYINSTALLER_RESET_ENVIRONMENT": "1"}
    subprocess.Popen([str(exe)], cwd=str(exe.parent), env=env)
    return True


class Updater:
    def __init__(self, status, progress):
        self.status = status
        self.progress = progress
        self.mc_dir = minecraft_dir()
        # Тут же хранятся ник и токены скинов
        self.game_dir = self.mc_dir / DEFAULT_PACK["game_dir"]
        self.state_file = self.game_dir / "launcher_state.json"
        self._cloud_alive = None

    def fetch_json(self, path: str):
        try:
            r = cloud.get(f"{relay.CLOUD}/{path}", timeout=8)
            r.raise_for_status()
            return r.json()
        except requests.RequestException as e:
            raise RuntimeError(
                "Нет связи с сервером.\n\nПроверь интернет. Если он есть — попробуй ещё раз чуть позже "
                "или зайди в нашу сеть Radmin VPN.\n\n" + str(e)
            ) from None

    def web_json(self, name: str):
        # С GitHub; удачный ответ сохраняем — без интернета и облака сборки всё равно найдутся
        r = web.get(PACKS_WEB + name, timeout=30)
        r.raise_for_status()
        data = r.json()
        save_json(PACKS_CACHE / name, data)
        return data

    def fetch_packs(self, max_age: float = 0) -> list:
        if max_age and time.monotonic() - _packs_list["at"] < max_age:
            return _packs_list["packs"]
        self.status("Получаю список сборок…")
        packs = None
        if self.cloud_alive():
            # По Radmin быстро и не зависит от того, открываются ли у игрока GitHub и Modrinth
            try:
                packs = self.cloud_list()
            except (requests.RequestException, ValueError, KeyError):
                traceback.print_exc()
        if packs is None:
            try:
                packs = [{**p, "source": "web"} for p in self.web_json("packs.json")["packs"]]
            except (requests.RequestException, ValueError, KeyError):
                traceback.print_exc()
                packs = self.cloud_packs()
        _packs_list.update(at=time.monotonic(), packs=packs)
        return packs

    def cloud_list(self) -> list:
        r = cloud.get(f"{relay.CLOUD}/packs.json", timeout=(2, 8))
        if r.status_code == 404:
            return [dict(DEFAULT_PACK)]  # облако без списка сборок
        r.raise_for_status()
        return r.json()["packs"]

    def cloud_packs(self) -> list:
        # Интернет-список недоступен: облако хоста по Radmin, а без него — последний сохранённый список
        try:
            return self.cloud_list()
        except (requests.RequestException, ValueError, KeyError):
            saved = load_json(PACKS_CACHE / "packs.json", None)
            if saved:
                return [{**p, "source": "web"} for p in saved["packs"]]
            return [dict(DEFAULT_PACK)] if self.fetch_json(DEFAULT_PACK["manifest"]) else []

    def fetch_manifest(self, entry: dict) -> dict:
        if entry.get("source") == "web":
            try:
                return self.web_json(entry["manifest"])
            except (requests.RequestException, ValueError):
                traceback.print_exc()
                saved = load_json(PACKS_CACHE / entry["manifest"], None)
                if saved:
                    return saved
            # Манифест сборки в облаке лежит рядом с её файлами
            return self.fetch_json(entry.get("base", "") + "manifest.json")
        return self.fetch_json(entry["manifest"])

    def cloud_alive(self) -> bool:
        # Облако хоста по Radmin — быстрый источник файлов; не отвечает за 2 секунды — качаем из интернета
        if self._cloud_alive is None:
            try:
                self._cloud_alive = cloud.get(f"{relay.CLOUD}/packs.json", timeout=(2, 5)).ok
            except requests.RequestException:
                self._cloud_alive = False
        return self._cloud_alive

    def build_version(self, pack: dict, game_dir: Path) -> dict:
        # Склеиваем ванильную версию и профиль Fabric в одну самостоятельную версию
        mc, loader = pack["minecraft"], pack["fabric_loader"]
        self.status(f"Подготовка версии {pack['version_id']}…")
        entry = next(v for v in web.get(MOJANG_MANIFEST, timeout=20).json()["versions"] if v["id"] == mc)
        vanilla = web.get(entry["url"], timeout=20).json()
        fabric = web.get(f"https://meta.fabricmc.net/v2/versions/loader/{mc}/{loader}/profile/json", timeout=20).json()

        game_args = [str(game_dir) if a == "${game_directory}" else a for a in vanilla["arguments"]["game"]]
        game_args += fabric.get("arguments", {}).get("game", [])

        version = dict(vanilla)
        version.update(
            id=pack["version_id"],
            type="modified",
            mainClass=fabric["mainClass"],
            libraries=fabric["libraries"] + vanilla["libraries"],
            arguments={
                "game": game_args,
                "jvm": vanilla["arguments"]["jvm"] + fabric.get("arguments", {}).get("jvm", []),
            },
        )
        version.pop("inheritsFrom", None)
        return version

    def install_version(self, pack: dict, game_dir: Path) -> None:
        state_file = game_dir / "launcher_state.json"
        state = load_json(state_file, {})
        game_dir.mkdir(parents=True, exist_ok=True)
        ensure_server(game_dir / "servers.dat", pack["server_name"], pack["server"])

        version_id = pack["version_id"]
        key = f"v5|{version_id}|{pack['minecraft']}|{pack['fabric_loader']}|{game_dir}"
        json_path = self.mc_dir / "versions" / version_id / f"{version_id}.json"
        if state.get("version_key") == key and json_path.exists():
            return
        save_json(json_path, self.build_version(pack, game_dir))
        state["version_key"] = key
        save_json(state_file, state)

    def upload_skin(self, nick: str, png: Path, slim: bool) -> str:
        data = png.read_bytes()
        if not data.startswith(b"\x89PNG\r\n\x1a\n"):
            raise RuntimeError("Скин должен быть картинкой PNG.")
        message = self.upload_texture(nick, data, "skin", slim)
        return f"{message} для {nick}. В игре он появится через несколько минут, после перезахода."

    def nick_token(self, nick: str) -> str:
        """Токен ника: создаётся при первой загрузке скина или сборки и хранится только у этого игрока."""
        if not re.fullmatch(r"[A-Za-z0-9_]{3,16}", nick or ""):
            raise RuntimeError("Ник должен быть из 3–16 латинских букв, цифр или _ — как в TLauncher.")
        state = load_json(self.state_file, {})
        token = state.setdefault("skin_tokens", {}).setdefault(nick.lower(), secrets.token_urlsafe(24))
        save_json(self.state_file, state)
        return token

    def upload_texture(self, nick: str, data: bytes, kind: str, slim: bool = False) -> str:
        # kind: skin или cape; плащ с пустыми data — убрать плащ
        token = self.nick_token(nick)
        state = load_json(self.state_file, {})

        self.status("Загружаю скин…" if kind == "skin" else "Загружаю плащ…")
        headers = {"X-Nick": nick, "X-Token": token, "X-Model": "slim" if slim else "default",
                   "Content-Type": "image/png"}
        try:
            r = relay.post("/upload" if kind == "skin" else "/cape", data=data, headers=headers, timeout=30)
        except requests.RequestException as e:
            raise RuntimeError("Нет связи с сервером скинов. Проверь интернет.\n\n" + str(e)) from None
        try:
            message = r.json()["message"]
        except (ValueError, KeyError):
            message = f"Ошибка сервера ({r.status_code})"
        if r.status_code != 200:
            raise RuntimeError(message)
        state["skin_nick"] = nick
        save_json(self.state_file, state)
        return message

    def download(self, url: str, dest: Path, expected_sha1: str, session=cloud) -> None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(dest.name + ".part")
        with session.get(url, stream=True, timeout=30) as r:
            r.raise_for_status()
            with tmp.open("wb") as f:
                for chunk in r.iter_content(1 << 16):
                    f.write(chunk)
        if sha1(tmp) != expected_sha1:
            tmp.unlink()
            raise RuntimeError(f"Файл {dest.name} скачался с ошибкой, попробуй ещё раз.")
        tmp.replace(dest)

    def sync_pack(self, pack: dict, game_dir: Path) -> None:
        """Приводит файлы сборки в папке игры к облаку.

        Файл качается, если его нет или в облаке вышла новая версия; правки игрока
        в конфигах живут до следующего обновления этого файла в облаке. Файлы из
        "once" (например, options.txt) кладутся только если их ещё нет.
        """
        name = pack["name"]
        state_file = game_dir / "launcher_state.json"
        state = load_json(state_file, {})
        recorded = state.get("files", {})
        # Пути, которые ставил лаунчер, в том числе в старом формате состояния
        managed = (set(recorded) | {f"mods/{n}" for n in state.get("managed_mods", [])}
                   | {f"resourcepacks/{n}" for n in state.get("managed_resourcepacks", [])})
        once = set(pack["once"])
        wanted = {f["path"] for f in pack["files"]}

        # Убираем только то, что раньше ставил лаунчер; свои файлы игрока не трогаем
        for rel in sorted(managed - wanted):
            path = inside(game_dir, rel)
            if path and path.exists():
                self.status(f"{name}: удаляю {path.name}")
                path.unlink()
            recorded.pop(rel, None)

        self.status(f"{name}: проверяю файлы…")
        todo = []
        for f in pack["files"]:
            rel, path = f["path"], inside(game_dir, f["path"])
            if path is None:
                continue
            if rel in once:
                if not path.exists():
                    todo.append(f)
            elif recorded.get(rel) == f["sha1"] and path.exists():
                continue
            elif path.exists() and path.stat().st_size == f["size"] and sha1(path) == f["sha1"]:
                recorded[rel] = f["sha1"]
            else:
                todo.append(f)

        total = sum(f["size"] for f in todo) or 1
        count = {"files": 0, "bytes": 0}

        def finished(f: dict) -> None:
            if f["path"] not in once:
                recorded[f["path"]] = f["sha1"]
            count["files"] += 1
            count["bytes"] += f["size"]
            self.status(f"{name}: скачиваю файлы ({count['files']}/{len(todo)}, "
                        f"{count['bytes'] / 2**20:.0f} из {total / 2**20:.0f} МБ)")
            self.progress(count["bytes"] / total)
            if count["files"] % 100 == 0:
                # Чтобы после обрыва связи продолжить с того же места
                state["files"] = recorded
                save_json(state_file, state)

        # Пути считаем до загрузки: resolve() не должен видеть папки, которые как раз создают потоки
        targets = [(f, inside(game_dir, f["path"])) for f in todo]
        quote = lambda f: requests.utils.quote(pack["base"] + f["path"])
        try:
            if pack.get("source") != "web":
                # Сборки друзей качаются с их ПК, сборки из облачного списка — из облака
                root = pack.get("root") or relay.CLOUD
                self.fetch_all([(f, dest, f"{root}/{quote(f)}", cloud) for f, dest in targets], finished)
            else:
                if targets and self.cloud_alive():
                    targets = self.fetch_all([(f, dest, f"{relay.CLOUD}/{quote(f)}", cloud) for f, dest in targets],
                                             finished, keep_going=True)
                self.fetch_all([(f, dest, f.get("url") or PACKS_WEB + f["asset"], web)
                                for f, dest in targets if "bundle" not in f], finished)
                bundles = {}
                for f, dest in targets:
                    if "bundle" in f:
                        bundles.setdefault(f["bundle"], []).append((f, dest))
                self.fetch_bundles(bundles, finished)
        except BaseException:
            state["files"] = recorded
            save_json(state_file, state)
            raise

        state["files"] = recorded
        state.pop("managed_mods", None)
        state.pop("managed_resourcepacks", None)
        save_json(state_file, state)

        options = game_dir / "options.txt"
        if not options.exists():
            options.write_text("lang:ru_ru\n", "utf-8")

    def fetch_all(self, jobs: list, finished, keep_going: bool = False) -> list:
        """Качает [(файл, куда, ссылка, сессия)] в несколько потоков. keep_going — не прерываться
        на ошибках, а вернуть то, что не скачалось (чтобы взять это из другого источника)."""
        failed = []
        with ThreadPoolExecutor(DOWNLOAD_THREADS) as pool:
            running = {pool.submit(self.download, url, dest, f["sha1"], session): (f, dest)
                       for f, dest, url, session in jobs}
            try:
                for job in as_completed(running):
                    try:
                        job.result()
                    except (requests.RequestException, RuntimeError, OSError):
                        if not keep_going:
                            raise
                        failed.append(running[job])
                        continue
                    finished(running[job][0])
            except BaseException:
                pool.shutdown(cancel_futures=True)
                raise
        return failed

    def fetch_bundles(self, bundles: dict, finished) -> None:
        """Мелкие файлы сборки лежат на GitHub архивами: из архива берём только нужные файлы."""
        def unpack(name: str, items: list) -> list:
            r = web.get(PACKS_WEB + name, timeout=60)
            r.raise_for_status()
            with zipfile.ZipFile(io.BytesIO(r.content)) as z:
                for f, dest in items:
                    data = z.read(f["path"])
                    if hashlib.sha1(data).hexdigest() != f["sha1"]:
                        raise RuntimeError(f"Файл {dest.name} скачался с ошибкой, попробуй ещё раз.")
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    tmp = dest.with_name(dest.name + ".part")
                    tmp.write_bytes(data)
                    tmp.replace(dest)
            return items

        with ThreadPoolExecutor(DOWNLOAD_THREADS) as pool:
            running = [pool.submit(unpack, name, items) for name, items in bundles.items()]
            try:
                for job in as_completed(running):
                    for f, _ in job.result():
                        finished(f)
            except BaseException:
                pool.shutdown(cancel_futures=True)
                raise

    def enable_packs(self, game_dir: Path, names: list) -> None:
        """Включает ресурспаки сервера в options.txt в нужном порядке.

        Делается только когда набор паков с облака поменялся, чтобы игрок
        мог сам выключить пак и лаунчер не включал его обратно каждый раз.
        """
        state_file = game_dir / "launcher_state.json"
        state = load_json(state_file, {})
        ours = [f"file/{n}" for n in names]
        if state.get("enabled_packs") == ours:
            return
        previous = set(state.get("enabled_packs", [])) | set(ours)

        options = game_dir / "options.txt"
        lines = options.read_text("utf-8").splitlines() if options.exists() else []
        current = ["vanilla", "fabric"]
        for line in lines:
            if line.startswith("resourcePacks:"):
                try:
                    current = json.loads(line.split(":", 1)[1])
                except ValueError:
                    pass
        # Свои паки игрока оставляем, наши ставим сверху (в конце списка — выше приоритет)
        packs_line = "resourcePacks:" + json.dumps([p for p in current if p not in previous] + ours)
        lines = [l for l in lines if not l.startswith("resourcePacks:")] + [packs_line]
        options.write_text("\n".join(lines) + "\n", "utf-8")

        state["enabled_packs"] = ours
        save_json(state_file, state)

    def open_tlauncher(self) -> bool:
        exe = tlauncher_exe()
        if exe:
            subprocess.Popen([str(exe)], cwd=str(exe.parent))
            return True
        return False

    def friend_dir(self, entry: dict) -> Path:
        return self.mc_dir / "friends" / custom_packs.slug(f"{entry['host']}-{entry['id']}")

    def install_shared(self, entry: dict) -> str:
        """Сборка сервера друга из Radmin: качается с его ПК и каждый раз подгоняется под его сервер."""
        name, host = entry["name"], entry["host"]
        self.status(f"{name}: получаю список файлов у {host}…")
        try:
            r = cloud.get(share.url(entry["ip"], entry["id"]) + "/manifest", timeout=120)
            r.raise_for_status()
            files = r.json()["files"]
        except (requests.RequestException, ValueError, KeyError) as e:
            raise RuntimeError(f"Не удалось получить сборку «{name}» у {host}.\n\nПроверь, что Radmin VPN "
                               f"включён и сервер ещё запущен.\n\n{e}") from None
        version_id = custom_packs.safe_name(f"{name} ({host})")
        game_dir = self.friend_dir(entry)
        game_dir.mkdir(parents=True, exist_ok=True)
        (game_dir / ".mineserc_pack").write_text(version_id, "utf-8")  # по нему разбор вылетов узнаёт сборку
        ensure_server(game_dir / "servers.dat", name, f"{entry['ip']}:{entry['port']}")

        # Версия в TLauncher: пересобираем, только если хост сменил Minecraft или загрузчик
        state_file = game_dir / "launcher_state.json"
        state = load_json(state_file, {})
        key = f"{entry['minecraft']}|{entry['loader']}|{entry['loader_version']}|{version_id}"
        json_path = self.mc_dir / "versions" / version_id / f"{version_id}.json"
        if state.get("version_key") != key or not json_path.exists():
            self.status(f"{name}: ставлю {loader_title(entry) if entry['loader'] != 'vanilla' else 'Minecraft ' + entry['minecraft']}…")
            version = custom_packs.build_version(version_id, entry["minecraft"], entry["loader"],
                                                 entry["loader_version"], game_dir, self.mc_dir, self.status)
            save_json(json_path, version)
            state["version_key"] = key
            save_json(state_file, state)

        self.sync_pack({"name": name, "files": files, "once": [], "base": "",
                        "root": share.url(entry["ip"], entry["id"]) + "/files"}, game_dir)
        setup_skin_loader(game_dir)
        return version_id

    def run(self, open_tl: bool = True, pack_ids: list = None) -> str:
        ids = pack_ids or selected_packs()
        versions, notes = [], []
        memory = []  # ГБ для каждой запускаемой сборки — TLauncher получит наибольшую
        dirs = []

        # Свои серверы: сборка уже стоит, добавляем сервер в список серверов игры
        for s in shared_servers():
            if f"own:{s['id']}" in ids:
                ensure_server(Path(s["game_dir"]) / "servers.dat", s["name"], f"localhost:{s['port']}")
                versions.append(s["version_id"])
                memory.append(pack_memory(f"own:{s['id']}", s["game_dir"]))
                remember_dir(f"own:{s['id']}", s["game_dir"])
                dirs.append(s["game_dir"])
        # Серверы друзей из Radmin
        friends = [i for i in ids if i.startswith("radmin:")]
        if friends:
            found = {e["key"]: e for e in friend_servers()}
            for key in friends:
                if key in found:
                    versions.append(self.install_shared(found[key]))
                    memory.append(pack_memory(key, self.friend_dir(found[key])))
                    remember_dir(key, self.friend_dir(found[key]))
                    dirs.append(self.friend_dir(found[key]))

        chosen = []
        if any(":" not in i for i in ids):
            try:
                chosen = [e for e in self.fetch_packs() if e["id"] in ids]
            except RuntimeError:
                if not versions:
                    raise
                notes.append("Список наших сборок недоступен, они не обновлены.")
        if not chosen and not versions:
            raise RuntimeError("Не выбрано ни одной сборки. Отметь нужные карточки на вкладке «Наши серверы».")

        for entry in chosen:
            self.status(f"{entry['name']}: получаю список файлов…")
            pack = normalize_pack(entry, self.fetch_manifest(entry))
            game_dir = self.mc_dir / pack["game_dir"]
            self.install_version(pack, game_dir)
            setup_skin_loader(game_dir)
            self.sync_pack(pack, game_dir)
            if pack["enable"]:
                self.enable_packs(game_dir, pack["enable"])
            versions.append(pack["version_id"])
            memory.append(pack_memory(entry["id"], game_dir))
            remember_dir(entry["id"], game_dir)
            dirs.append(game_dir)
            if pack.get("note"):
                notes.append(pack["note"])

        for game_dir in dirs:
            prepare_workdir(game_dir, self.mc_dir)
        self.progress(1)
        if open_tl and memory:
            notes.append(apply_memory(max(memory)))
        which = f"версию «{versions[0]}»" if len(versions) == 1 else "нужную версию: «" + "» или «".join(versions) + "»"
        where = "В TLauncher" if open_tl and self.open_tlauncher() else "Открой TLauncher и"
        return f"Готово! {where} выбери {which} и нажми «Войти в игру».{' ' + ' '.join(notes) if notes else ''}"


# ---------- Память для игры ----------
#
# TLauncher дописывает свой -Xmx в самый конец команды запуска, поэтому память из файла версии
# он перебивает. Остаётся его общая настройка minecraft.memory.ram3 (МБ): лаунчер ставит туда
# память запускаемой сборки прямо перед тем, как открыть TLauncher.

def total_ram_gb() -> int:
    class MemoryStatus(ctypes.Structure):
        _fields_ = [("length", ctypes.c_ulong), ("load", ctypes.c_ulong), ("total", ctypes.c_ulonglong),
                    ("avail", ctypes.c_ulonglong), ("total_page", ctypes.c_ulonglong), ("avail_page", ctypes.c_ulonglong),
                    ("total_virtual", ctypes.c_ulonglong), ("avail_virtual", ctypes.c_ulonglong),
                    ("avail_extended", ctypes.c_ulonglong)]
    status = MemoryStatus(length=ctypes.sizeof(MemoryStatus))
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
        return 8
    return max(1, round(status.total / 2 ** 30))


def max_game_memory() -> int:
    # Windows и браузеру нужно хотя бы 2–3 ГБ, иначе игра начнёт тормозить из-за файла подкачки
    total = total_ram_gb()
    return max(2, total - (3 if total > 8 else 2))


def recommended_memory(mods: int, size: int = 0) -> int:
    """Сколько ГБ советовать сборке: по числу модов (или по размеру, пока она не скачана)."""
    if not mods and size:
        mods = 200 if size > 500 * 2 ** 20 else 60
    need = 2 if mods == 0 else 4 if mods <= 60 else 6 if mods <= 180 else 8
    return min(need, max_game_memory())


def count_mods(game_dir) -> int:
    folder = Path(game_dir) / "mods" if game_dir else None
    return len(list(folder.glob("*.jar"))) if folder and folder.is_dir() else 0


def pack_memory(key: str, game_dir=None, size: int = 0) -> int:
    chosen = load_json(SETTINGS_FILE, {}).get("memory", {}).get(key)
    return int(chosen) if chosen else recommended_memory(count_mods(game_dir), size)


def remember_dir(key: str, game_dir) -> None:
    # Папка сборки по id карточки — чтобы окно памяти знало, сколько в ней модов
    settings = load_json(SETTINGS_FILE, {})
    dirs = settings.setdefault("pack_dirs", {})
    if dirs.get(key) != str(game_dir):
        dirs[key] = str(game_dir)
        save_json(SETTINGS_FILE, settings)


def tlauncher_running() -> bool:
    found = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    def check(hwnd, _):
        length = ctypes.windll.user32.GetWindowTextLengthW(hwnd)
        if length and ctypes.windll.user32.IsWindowVisible(hwnd):
            buf = ctypes.create_unicode_buffer(length + 1)
            ctypes.windll.user32.GetWindowTextW(hwnd, buf, length + 1)
            if buf.value.startswith("TLauncher"):
                found.append(hwnd)
        return True

    ctypes.windll.user32.EnumWindows(check, 0)
    return bool(found)


def set_tlauncher_memory(gb: int) -> bool:
    """Меняет только строку памяти в настройках TLauncher. False — TLauncher ещё не запускался."""
    try:
        text = TL_PROPERTIES.read_text("utf-8", errors="surrogateescape")
    except OSError:
        return False
    line = f"minecraft.memory.ram3={gb * 1024}"
    if re.search(r"^minecraft\.memory\.ram3=.*$", text, re.M):
        text = re.sub(r"^minecraft\.memory\.ram3=.*$", line, text, count=1, flags=re.M)
    else:
        text = text.rstrip("\r\n") + ("\r\n" if "\r\n" in text else "\n") + line + "\n"
    TL_PROPERTIES.write_text(text, "utf-8", errors="surrogateescape")
    return True


def apply_memory(gb: int) -> str:
    """Ставит память в TLauncher; текст для сообщения игроку."""
    if tlauncher_running():
        return (f"TLauncher уже открыт — память ({gb} ГБ) не поменять. Закрой его и нажми «Играть» ещё раз "
                "или выставь память в настройках TLauncher.")
    set_tlauncher_memory(gb)
    return f"Памяти для игры: {gb} ГБ."


def custom_profiles() -> list:
    return load_json(SETTINGS_FILE, {}).get("custom_profiles", [])


def save_custom_profiles(profiles: list) -> None:
    settings = load_json(SETTINGS_FILE, {})
    settings["custom_profiles"] = profiles
    save_json(SETTINGS_FILE, settings)


def hosted_servers() -> list:
    return load_json(SETTINGS_FILE, {}).get("servers", [])


def save_hosted_servers(servers: list) -> None:
    settings = load_json(SETTINGS_FILE, {})
    settings["servers"] = servers
    save_json(SETTINGS_FILE, settings)


def shared_servers() -> list:
    """Свои серверы так, как их раздаёт share.py: сборка — профиль, из которого собран сервер."""
    profiles = {p["version_id"]: p for p in custom_profiles()}
    result = []
    for s in hosted_servers():
        profile = profiles.get(s.get("pack"))
        if not profile:
            continue
        port = server_host.read_properties(Path(s["path"]) / "server.properties").get("server-port") or "25565"
        result.append({"id": Path(s["path"]).name, "name": s["name"], "minecraft": s.get("minecraft", ""),
                       "loader": s.get("loader", ""), "loader_version": s.get("loader_version", ""),
                       "port": int(port), "game_dir": profile["game_dir"], "version_id": profile["version_id"]})
    return result


def radmin_exe():
    for base in (os.environ.get("ProgramFiles(x86)"), os.environ.get("ProgramFiles")):
        if base and (Path(base) / "Radmin VPN" / "RvRvpnGui.exe").exists():
            return Path(base) / "Radmin VPN" / "RvRvpnGui.exe"
    return None


def radmin_state() -> dict:
    # on — у компьютера есть адрес Radmin 26.x.x.x, то есть Radmin VPN подключён
    return {"installed": radmin_exe() is not None, "on": server_host.radmin_ip() is not None}


def loader_title(s: dict) -> str:
    return f"{custom_packs.LOADER_NAMES.get(s.get('loader'), s.get('loader', ''))} {s.get('minecraft', '')}".strip()


def friend_hosts() -> list:
    # Адреса, добавленные вручную: на случай, если поиск по сети Radmin не дошёл
    return load_json(SETTINGS_FILE, {}).get("friend_hosts", [])


def friend_servers() -> list:
    """Серверы друзей, запущенные через лаунчер и видные по Radmin. key — id карточки."""
    own = server_host.radmin_ip()
    hosts = share.find_hosts() | {h for h in friend_hosts() if h != own}
    found = []
    threads = [threading.Thread(target=lambda h=h: found.extend(share.host_servers(cloud, h))) for h in hosts]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    for entry in found:
        entry["key"] = f"radmin:{entry['ip']}:{entry['id']}"
    return sorted(found, key=lambda e: (e["host"].lower(), e["name"].lower()))


def start_sharing() -> None:
    """Запускает фоновую раздачу своих сборок друзьям и автовыключение пустых серверов.

    Если помощник уже работает, второй процесс сам закроется.
    """
    if not hosted_servers():
        return
    if getattr(sys, "frozen", False):
        args = [sys.executable, "--share"]
    else:
        args = [sys.executable, str(Path(__file__).resolve()), "--share"]
    env = {**os.environ, "PYINSTALLER_RESET_ENVIRONMENT": "1"}
    subprocess.Popen(args, env=env, cwd=str(SETTINGS_FILE.parent), close_fds=True,
                     creationflags=subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP)


def option(key: str):
    return load_json(SETTINGS_FILE, {}).get("options", {}).get(key, OPTIONS[key])


def my_nick() -> str:
    state = load_json(minecraft_dir() / DEFAULT_PACK["game_dir"] / "launcher_state.json", {})
    return state.get("skin_nick") or tlauncher_nick(minecraft_dir()) or ""


def server_changes(before: dict, cards: list, me: str) -> list:
    """Что изменилось с прошлого опроса серверов: [(заголовок, текст)] для уведомлений.

    before — {id: (включён, {ники})} с прошлого раза; обновляется на месте.
    """
    events = []
    first = not before
    for c in cards:
        status = c.get("status")
        names = set(status.get("names", [])) if status else set()
        was = before.get(c["id"])
        before[c["id"]] = (status is not None, names)
        if first or not status:
            continue
        if (not was or not was[0]) and c.get("kind") != "own":
            events.append((f"Сервер «{c['name']}» запущен", "Можно заходить — открой лаунчер и нажми «Играть»."))
        elif was:
            joined = sorted(n for n in names - was[1] if n.lower() != me.lower())
            if joined:
                who = ", ".join(joined)
                events.append((f"{who} на «{c['name']}»", f"{who} {'зашёл' if len(joined) == 1 else 'зашли'} на сервер."))
    return events


def hosted_port(record: dict) -> int:
    port = server_host.read_properties(Path(record["path"]) / "server.properties").get("server-port") or "25565"
    return int(port) if port.isdigit() else 25565


def run_sharing() -> None:
    if not hosted_servers():
        return
    state = load_json(minecraft_dir() / DEFAULT_PACK["game_dir"] / "launcher_state.json", {})
    nick = state.get("skin_nick") or tlauncher_nick(minecraft_dir()) or socket.gethostname()
    host = share.Host(shared_servers, nick, SETTINGS_FILE.parent / "share_cache.json")
    auto_stop = server_admin.AutoStop()
    share.serve(host, lambda: any(server_status(f"127.0.0.1:{hosted_port(s)}", 2) for s in hosted_servers()),
                lambda: auto_stop.tick(hosted_servers(), lambda port: server_status(f"127.0.0.1:{port}", 2)))


def build_id() -> str:
    # Начало хэша exe: по нему видно, какая сборка лаунчера у игрока
    return sha1(Path(sys.executable))[:10] if getattr(sys, "frozen", False) else "dev"


def skin_b64(path: Path = SKIN_FILE):
    try:
        return base64.b64encode(path.read_bytes()).decode() if path.exists() else None
    except OSError:
        return None


def valid_cape_size(w: int, h: int) -> bool:
    # 64x32 и HD кратно 64, плюс самый старый формат 22x17 — как в skins-worker
    return (w % 64 == 0 and 64 <= w <= 1024 and h == w // 2) or (w, h) == (22, 17)


class Api:
    """Методы, которые вызывает интерфейс (ui/app.js) через pywebview.api.

    Каждый вызов из JS идёт в своём потоке, поэтому долгие методы просто работают
    до конца, а ход работы шлют в интерфейс событиями app.emit().
    """

    def __init__(self):
        self._window = None
        self._mc_dir = minecraft_dir()
        self._plan = {}
        self._booted = False
        self._waiting_tl = False
        self._skin_path = None  # выбранный, но ещё не загруженный скин
        self._cape = None  # то же для плаща: путь к PNG или "" — убрать плащ
        self._crash = None  # разбор последнего вылета Minecraft
        self._tray = None  # значок в трее (tray.Tray), создаёт run_gui
        self._status_before = {}  # прошлый опрос серверов — для уведомлений
        self._tray_hint = False  # подсказку «я в трее» показываем один раз за запуск
        self._cards = []  # последний опрос серверов: из него «кто на каком сервере»
        self._last_pack = ""  # что запускал игрок — друзья увидят «играет в …»

    def _emit(self, event: str, data=None) -> None:
        try:
            self._window.run_js(f"app.emit({json.dumps(event)}, {json.dumps(data, ensure_ascii=False)})")
        except Exception:
            pass  # окно уже закрыто

    def _status(self, scope: str):
        def status(text):
            print(f"[{scope}] {text}")  # в лог: по нему видно, на каком шаге случилась ошибка
            self._emit("status", {"scope": scope, "text": text})
        return status

    def _progress(self, scope: str):
        last = [-1.0]

        def report(value):
            # Не заваливаем интерфейс сотнями событий на мелких файлах
            if value >= 1 or abs(value - last[0]) >= 0.005:
                last[0] = value
                self._emit("progress", {"scope": scope, "value": value})
        return report

    def _pick(self, kind, file_types=()):
        picked = self._window.create_file_dialog(kind, file_types=file_types)
        return picked[0] if picked else None

    # ---- Окно и общее

    def init(self) -> dict:
        state = load_json(self._mc_dir / DEFAULT_PACK["game_dir"] / "launcher_state.json", {})
        settings = load_json(SETTINGS_FILE, {})
        # Лаунчер обновился: один раз покажем список обновлений (на новой установке настройки пустые)
        updated = bool(settings) and settings.get("seen_version") != APP_VERSION
        if settings.get("seen_version") != APP_VERSION:
            settings["seen_version"] = APP_VERSION
            save_json(SETTINGS_FILE, settings)
        return {"nick": state.get("skin_nick") or tlauncher_nick(self._mc_dir), "skin": skin_b64(),
                "cape": skin_b64(CAPE_FILE),
                "skin_model": settings.get("skin_model", "default"), "updated": updated,
                "options": {k: option(k) for k in OPTIONS},
                "loaders": [[l, custom_packs.LOADER_NAMES[l]] for l in custom_packs.LOADERS],
                "version": APP_VERSION, "build": build_id(), "cloud_url": CLOUD_URL, "mc_dir": str(self._mc_dir),
                "settings_dir": str(SETTINGS_FILE.parent), "repo": REPO_URL,
                "eula_url": server_host.EULA_URL, "skins_site": SKINS_SITE, "tlauncher": tlauncher_exe() is not None}

    def last_crash(self):
        """Свежий вылет Minecraft, которого игрок ещё не видел: разбор для окна или None."""
        settings = load_json(SETTINGS_FILE, {})
        seen = settings.get("crash_seen")
        found = crashes.candidates(self._mc_dir, seen or time.time())
        settings["crash_seen"] = max([time.time()] + [p.stat().st_mtime for p in found])
        save_json(SETTINGS_FILE, settings)
        if seen is None or not found:
            return None  # при первом запуске старые вылеты не показываем
        names = {str(self._mc_dir / DEFAULT_PACK["game_dir"]): DEFAULT_PACK["name"]}
        names.update({p["game_dir"]: p["name"] for p in custom_profiles()})
        self._crash = crashes.analyze(found[0], self._mc_dir, names)
        return {k: v for k, v in self._crash.items() if k != "text"}

    def send_crash(self) -> str:
        c = self._crash
        if not c:
            raise RuntimeError("Нет вылета, о котором можно отправить отчёт.")
        text = (f"Сборка: {c['pack']}\nКогда: {c['time']}\nЧто делала игра: {c['where'] or '?'}\n"
                f"Разбор лаунчера: {' '.join(c['hints'])}\nФайл: {c['file']}\n\n{c['text']}")
        return reports.send("game", f"{c['pack']}: {c['title']}", text,
                            reports.signature("game", c["pack"], c["title"], *c["mods"]), auto=False)

    def open_crash(self) -> None:
        if self._crash:
            os.startfile(self._crash["file"])

    def boot(self) -> None:
        # Интерфейс готов: проверяем версию лаунчера и запускаем индикатор серверов
        if self._booted:
            return
        self._booted = True
        threading.Thread(target=self._watch_servers, daemon=True).start()
        friends.Presence(self._about_me).start()
        # Сервер мог быть запущен и без лаунчера (start.bat): раздача сама дождётся его или закроется
        start_sharing()
        if not tlauncher_exe():
            self._watch_tlauncher()  # игрок может поставить TLauncher и без нашей кнопки
        if self_update(self._status("boot")):
            self._window.destroy()
            return
        self._emit("ready")

    def install_tlauncher(self) -> None:
        # Открываем официальный сайт; установку заметит _wait_tlauncher
        webbrowser.open(TLAUNCHER_SITE)
        self._watch_tlauncher()

    def _watch_tlauncher(self) -> None:
        if not self._waiting_tl:
            self._waiting_tl = True
            threading.Thread(target=self._wait_tlauncher, daemon=True).start()

    def _wait_tlauncher(self) -> None:
        deadline = time.time() + 60 * 60
        while time.time() < deadline and not tlauncher_exe():
            time.sleep(3)
        self._waiting_tl = False
        if tlauncher_exe():
            # Установщик мог выбрать другую папку игры
            self._mc_dir = minecraft_dir()
            self._emit("tlauncher", {"installed": True})

    def minimize(self) -> None:
        self._window.minimize()

    def close(self) -> None:
        if self._tray and option("tray"):
            self._window.hide()
            if not self._tray_hint:
                self._tray_hint = True
                self._tray.notify("MineForFriends свернулся в трей",
                                  "Пришлю уведомление, когда друг запустит сервер. Выход — правой кнопкой по значку.")
        else:
            self.quit_app()

    def show_window(self) -> None:
        self._window.show()
        self._window.restore()

    def quit_app(self) -> None:
        if self._tray:
            self._tray.stop()
        self._window.destroy()

    def set_option(self, key: str, value: bool) -> None:
        if key not in OPTIONS:
            raise RuntimeError("Неизвестная настройка.")
        settings = load_json(SETTINGS_FILE, {})
        settings.setdefault("options", {})[key] = bool(value)
        save_json(SETTINGS_FILE, settings)

    def open_url(self, url: str) -> None:
        webbrowser.open(url)

    def releases(self) -> list:
        return release_notes()

    def open_folder(self, path: str) -> None:
        if Path(path).exists():
            os.startfile(path)

    # ---- Наши серверы

    def _watch_servers(self) -> None:
        while True:
            data = self._servers()
            self._emit("servers", data)
            self._cards = data["packs"]
            try:
                self._emit("friends", self._friends())
            except Exception:
                traceback.print_exc()  # без списка друзей лаунчер работает как раньше
            events = server_changes(self._status_before, data["packs"], my_nick())
            if self._tray and option("notify"):
                for title, text in events[:3]:  # больше трёх разом — уже шум
                    self._tray.notify(title, text)
            time.sleep(STATUS_REFRESH / 1000)

    def _servers(self) -> dict:
        # Индикатор: сборки из облака, свои серверы и серверы друзей в Radmin, какие из них включены
        cards = []
        try:
            for e in Updater(lambda s: None, lambda v: None).fetch_packs(max_age=PACKS_LIST_TTL):
                cards.append({"id": e["id"], "name": e["name"], "description": e.get("description", ""),
                              "size": e.get("size"), "address": e.get("server") or DEFAULT_PACK["server"]})
            cloud_ok = True
        except Exception:
            cloud_ok = False
        for s in shared_servers():
            cards.append({"id": f"own:{s['id']}", "name": s["name"], "kind": "own", "address": f"127.0.0.1:{s['port']}",
                          "description": f"Твой сервер · {loader_title(s)}"})
        for e in friend_servers():
            cards.append({"id": e["key"], "name": e["name"], "kind": "friend", "address": f"{e['ip']}:{e['port']}",
                          "description": f"Сервер {e['host']} · {loader_title(e)}"})
        results = {}
        threads = [threading.Thread(target=lambda c=c: results.__setitem__(c["id"], server_status(c["address"])))
                   for c in cards]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        for c in cards:
            c["status"] = results.get(c["id"])
            c["memory"] = pack_memory(c["id"], self._memory_dir(c["id"]), c.get("size") or 0)
        return {"cloud": cloud_ok, "packs": cards, "selected": selected_packs(), "radmin": radmin_state()}

    # ---- Друзья в сети и проверка связи

    def _about_me(self):
        # Что видят друзья по Radmin. Выключено в «О лаунчере» — не отвечаем вовсе
        if not option("presence"):
            return None
        nick = my_nick()
        if not nick:
            return None
        title = friends.minecraft_window()
        hosting = [c["name"] for c in self._cards if c.get("kind") == "own" and c.get("status")]
        return {"v": 1, "nick": nick, "app": APP_VERSION, "state": "playing" if title else "launcher",
                "pack": self._last_pack if title else "", "mode": friends.game_mode(title) if title else "",
                "hosting": hosting[:3]}

    def _friends(self) -> list:
        """Друзья в сети: ответившие лаунчеры плюс ники с серверов (у кого лаунчер закрыт)."""
        me = my_nick().lower()
        people = {}
        for f in friends.ask(friend_hosts()):
            nick = f.get("nick") or ""
            if not NICK_RE.match(nick) or nick.lower() == me:
                continue
            hosting = [str(h)[:60] for h in f.get("hosting") or [] if isinstance(h, str)][:3]
            people[nick.lower()] = {"nick": nick, "ip": f["ip"], "launcher": True,
                                    "playing": f.get("state") == "playing", "pack": str(f.get("pack") or "")[:80],
                                    "mode": str(f.get("mode") or "")[:30], "hosting": hosting,
                                    "app": str(f.get("app") or "")[:12], "server": "", "card": "", "address": ""}
        for c in self._cards:
            for nick in (c.get("status") or {}).get("names", []):
                if nick.lower() == me:
                    continue
                person = people.setdefault(nick.lower(), {
                    "nick": nick, "ip": "", "launcher": False, "pack": "", "mode": "", "hosting": [], "app": ""})
                person.update(playing=True, server=c["name"], card=c["id"], address=c["address"])
        return sorted(people.values(), key=lambda p: (not p["server"], not p["playing"], p["nick"].lower()))

    def friends_online(self) -> list:
        return self._friends()

    def check_connection(self, address: str) -> dict:
        address = (address or "").strip()
        if not address:
            raise RuntimeError("Впиши адрес сервера, например 26.12.34.56:25565.")
        return friends.check(address, radmin_state(), lambda a: server_status(a, 5))

    def open_radmin(self) -> None:
        exe = radmin_exe()
        if exe:
            subprocess.Popen([str(exe)], cwd=str(exe.parent))
        else:
            webbrowser.open(RADMIN_SITE)

    def refresh_servers(self) -> None:
        self._emit("servers", self._servers())

    def friend_hosts(self) -> list:
        return friend_hosts()

    def save_friend_hosts(self, hosts: list) -> None:
        clean = []
        for h in hosts:
            h = h.strip().split(":")[0]
            if not h:
                continue
            if not re.fullmatch(r"(\d{1,3})\.(\d{1,3})\.(\d{1,3})\.(\d{1,3})", h) or any(int(p) > 255 for p in h.split(".")):
                raise RuntimeError(f"«{h}» — не похоже на адрес. Нужен адрес Radmin вида 26.123.45.67.")
            if h not in clean:
                clean.append(h)
        settings = load_json(SETTINGS_FILE, {})
        settings["friend_hosts"] = clean
        save_json(SETTINGS_FILE, settings)

    def set_selected(self, ids: list) -> None:
        save_selected_packs(ids)

    def play(self) -> str:
        chosen = [c["name"] for c in self._cards if c["id"] in selected_packs()]
        self._last_pack = ", ".join(chosen[:2])
        message = Updater(self._status("play"), self._progress("play")).run()
        # С треем лаунчер прячется и продолжает присылать уведомления, без него — закрывается
        threading.Timer(6, self.close).start()
        return message

    # ---- Свои сборки

    def pick_folder(self):
        return self._pick(webview.FileDialog.FOLDER)

    def pick_archive(self):
        return self._pick(webview.FileDialog.OPEN, ("Сборка (*.zip;*.mrpack)", "Все файлы (*.*)"))

    def analyze(self, path: str) -> dict:
        self._plan = {}
        result = custom_packs.analyze(Path(path), status=self._status("custom"))
        self._plan = result
        kinds = {"curseforge": "CurseForge", "modrinth": "Modrinth", "multimc": "MultiMC/Prism", "folder": "папка с модами"}
        parts = [kinds[result["format"]], f"модов: {result['mods']}"]
        if result["downloads"]:
            parts.append(f"докачать файлов: {len(result['downloads'])}")
        if result["missing"]:
            parts.append(f"не хватает зависимостей: {len(result['missing'])} (найду на Modrinth)")
        info = " · ".join(parts) + ("\n" + " ".join(result["notes"]) if result["notes"] else "")
        return {"name": result["name"], "minecraft": result["minecraft"], "mc_source": result.get("mc_source", ""),
                "releases": result.get("releases") or [], "loader": result["loader"],
                "loader_version": result.get("loader_version"), "info": info}

    def fit_count(self, mc: str) -> dict:
        specs = self._plan.get("mod_specs") or []
        fits = custom_packs.fit_count(specs, mc) if specs and mc else len(specs)
        return {"fits": fits, "total": len(specs)}

    def loader_versions(self, loader: str, mc: str):
        try:
            return custom_packs.loader_versions(loader, mc)
        except Exception:
            return None  # нет сети — останется только «авто»

    def install_custom(self, name: str, mc: str, loader: str, loader_version) -> dict:
        chosen = {**self._plan, "name": name, "minecraft": mc, "loader": loader, "loader_version": loader_version}
        res = custom_packs.install(chosen, self._mc_dir, status=self._status("custom"), progress=self._progress("custom"))
        if loader != "vanilla":
            setup_skin_loader(Path(res["game_dir"]))
        profiles = [p for p in custom_profiles() if p["version_id"] != res["version_id"]] + [
            {k: res[k] for k in ("name", "version_id", "game_dir", "minecraft", "loader", "loader_version")}]
        save_custom_profiles(profiles)
        text = f"В TLauncher выбери версию «{res['version_id']}» и нажми «Войти в игру»."
        if chosen["mods"] > 150:
            text += "\n\nСборка большая — выдели в настройках TLauncher не меньше 6 ГБ памяти."
        if res["notes"]:
            text += "\n\n" + "\n".join(res["notes"])
        return {"version_id": res["version_id"], "text": text}

    # ---- Память для игры
    # key — id карточки на «Наших серверах» или custom:<версия> для своей сборки

    def _memory_dir(self, key: str):
        if key.startswith("custom:"):
            profile = next((p for p in custom_profiles() if p["version_id"] == key[7:]), None)
            return profile and profile["game_dir"]
        if key.startswith("own:"):
            shared = next((s for s in shared_servers() if f"own:{s['id']}" == key), None)
            if shared:
                return shared["game_dir"]
        if key == DEFAULT_PACK["id"]:
            return self._mc_dir / DEFAULT_PACK["game_dir"]
        return load_json(SETTINGS_FILE, {}).get("pack_dirs", {}).get(key)

    def memory_info(self, key: str, size: int = 0) -> dict:
        mods = count_mods(self._memory_dir(key))
        recommended = recommended_memory(mods, size or 0)
        chosen = load_json(SETTINGS_FILE, {}).get("memory", {}).get(key)
        return {"gb": int(chosen) if chosen else recommended, "auto": not chosen, "recommended": recommended,
                "mods": mods, "total": total_ram_gb(), "max": max(max_game_memory(), int(chosen or 0))}

    def set_memory(self, key: str, gb) -> None:
        settings = load_json(SETTINGS_FILE, {})
        memory = settings.setdefault("memory", {})
        if gb is None:
            memory.pop(key, None)  # снова «как советует лаунчер»
        else:
            gb = int(gb)
            if not 1 <= gb <= total_ram_gb():
                raise RuntimeError(f"Памяти можно дать от 1 до {total_ram_gb()} ГБ.")
            memory[key] = gb
        save_json(SETTINGS_FILE, settings)

    def play_custom(self, version_id: str) -> str:
        profile = next((p for p in custom_profiles() if p["version_id"] == version_id), None)
        if not profile:
            raise RuntimeError("Сборка не найдена.")
        note = apply_memory(pack_memory(f"custom:{version_id}", profile["game_dir"]))
        prepare_workdir(profile["game_dir"], minecraft_dir())
        self._last_pack = profile["name"]
        where = "В TLauncher" if Updater(lambda s: None, lambda v: None).open_tlauncher() else "Открой TLauncher и"
        return f"{where} выбери версию «{version_id}» и нажми «Войти в игру». {note}"

    # ---- Рецепты: поделиться своей сборкой и поставить сборку друга

    def share_scan(self, version_id: str) -> dict:
        profile = next((p for p in custom_profiles() if p["version_id"] == version_id), None)
        if not profile:
            raise RuntimeError("Сборка не найдена.")
        scanned = recipes.scan(Path(profile["game_dir"]), self._status("custom"))
        self._share = (profile, scanned)
        # Сборку, поставленную из рецепта друга, предлагаем под исходным названием
        name = profile.get("recipe_name") or profile["name"]
        return {"name": name, "mods": len(scanned["mods"]), "missing": scanned["missing"],
                "zip_files": scanned["zip_files"], "zip_size": scanned["zip_size"], "skipped": scanned["skipped"],
                "id": recipes.recipe_id(name)}

    def share_submit(self, name: str, description: str, nick: str) -> str:
        if not getattr(self, "_share", None):
            raise RuntimeError("Сначала выбери сборку.")
        if not name.strip():
            raise RuntimeError("Впиши название сборки.")
        profile, scanned = self._share
        token = Updater(lambda s: None, lambda v: None).nick_token(nick.strip())
        message = recipes.submit(profile, scanned, name, description, nick.strip(), token)
        self._share = None
        return message

    def recipe_list(self) -> list:
        installed = {p.get("recipe"): p for p in custom_profiles() if p.get("recipe")}
        items = recipes.listing()
        for r in items:
            p = installed.get(r["id"])
            r["installed"] = bool(p)
            r["outdated"] = bool(p) and p.get("recipe_sha") != r["sha"]
            r["loader_name"] = custom_packs.LOADER_NAMES.get(r["loader"], r["loader"])
        return items

    def install_recipe(self, recipe_id: str, sha: str) -> dict:
        res, recipe = recipes.install({"id": recipe_id, "sha": sha}, self._mc_dir,
                                      status=self._status("custom"), progress=self._progress("custom"))
        if res["loader"] != "vanilla":
            setup_skin_loader(Path(res["game_dir"]))
        fields = {k: res[k] for k in ("name", "version_id", "game_dir", "minecraft", "loader", "loader_version")}
        fields.update(recipe=recipe["id"], recipe_sha=sha, recipe_name=recipe["name"])
        save_custom_profiles([p for p in custom_profiles() if p["version_id"] != res["version_id"]] + [fields])
        text = f"Сборка «{res['version_id']}» готова. Нажми ▶ у неё в «Моих сборках» — лаунчер выставит память и откроет TLauncher."
        if res["notes"]:
            text += "\n\n" + "\n".join(res["notes"])
        return {"version_id": res["version_id"], "text": text}

    def profile_list(self) -> list:
        return [{**p, "loader_name": custom_packs.LOADER_NAMES.get(p["loader"], p["loader"])} for p in custom_profiles()]

    # ---- Обновление модов своей сборки

    def _profile(self, version_id: str) -> dict:
        profile = next((p for p in custom_profiles() if p["version_id"] == version_id), None)
        if not profile:
            raise RuntimeError("Сборка не найдена.")
        return profile

    def mod_updates(self, version_id: str) -> dict:
        p = self._profile(version_id)
        found = mod_updates.check(Path(p["game_dir"]), p["loader"], p["minecraft"], self._status("custom"))
        self._mod_updates = (version_id, found["updates"])
        return found

    def apply_mod_updates(self, version_id: str, picked: list) -> str:
        p = self._profile(version_id)
        if not getattr(self, "_mod_updates", None) or self._mod_updates[0] != version_id:
            raise RuntimeError("Сначала проверь обновления.")
        if friends.minecraft_window():
            raise RuntimeError("Закрой Minecraft: пока игра запущена, Windows не даёт заменить файлы модов.")
        chosen = [u for i, u in enumerate(self._mod_updates[1]) if i in set(picked)]
        if not chosen:
            raise RuntimeError("Отметь хотя бы один мод.")
        res = mod_updates.apply(Path(p["game_dir"]), chosen, p["loader"], p["minecraft"],
                                self._status("custom"), self._progress("custom"))
        self._mod_updates = None
        lines = [f"Обновлено модов: {len(res['updated'])}."]
        if res["added"]:
            lines.append("Докачаны новые зависимости: " + ", ".join(res["added"][:6]))
        if res["failed"]:
            lines.append("Не получилось: " + ", ".join(res["failed"][:6]))
        if res["not_found"]:
            lines.append("Не нашёл зависимости: " + ", ".join(res["not_found"][:4]))
        if res["backup"]:
            lines.append(f"Старые версии лежат в папке сборки (кнопка с папкой): mods_backup/{Path(res['backup']).name}. "
                         "Если что-то сломалось, верни их в папку mods.")
        if any(s.get("pack") == p["name"] for s in hosted_servers()):
            lines.append("У тебя есть сервер из этой сборки — на нём моды остались старыми. "
                         "Если друзей не пускает на сервер, собери сервер заново.")
        if p.get("recipe"):
            lines.append("Это сборка друга: если он выпустит обновление и ты нажмёшь «Обновить», моды вернутся к его версиям.")
        return "\n\n".join(lines)

    def remove_profile(self, version_id: str) -> None:
        profile = next((p for p in custom_profiles() if p["version_id"] == version_id), None)
        if profile:
            custom_packs.remove(profile, self._mc_dir)
            save_custom_profiles([p for p in custom_profiles() if p["version_id"] != version_id])

    # ---- Сервер

    def _server(self, path: str) -> dict:
        record = next((s for s in hosted_servers() if s["path"] == path), None)
        if not record:
            raise RuntimeError("Сервер не найден в списке.")
        return record

    def server_list(self) -> list:
        ip = server_host.radmin_ip() or "localhost"
        servers = []
        for s in hosted_servers():
            port = server_host.read_properties(Path(s["path"]) / "server.properties").get("server-port", "?")
            status = server_status(f"127.0.0.1:{port}", 1) if str(port).isdigit() else None
            servers.append({"name": s["name"], "path": s["path"], "minecraft": s.get("minecraft", ""),
                            "loader_name": custom_packs.LOADER_NAMES.get(s.get("loader"), s.get("loader", "")),
                            "address": f"{ip}:{port}", "online": status is not None,
                            "players": f"{status['online']}/{status['max']}" if status else ""})
        return servers

    def _online(self, path: str) -> bool:
        port = server_host.read_properties(Path(path) / "server.properties").get("server-port", "25565")
        return server_status(f"127.0.0.1:{port}", 2) is not None

    def host_server(self, pack_name: str):
        profile = next((p for p in custom_profiles() if p["name"] == pack_name), None)
        if not profile:
            raise RuntimeError("Сначала добавь сборку во вкладке «Свои сборки».")
        folder = self._pick(webview.FileDialog.FOLDER)
        if not folder:
            return None
        self._progress("host")(0)
        taken = {int(server_host.read_properties(Path(s["path"]) / "server.properties").get("server-port", 0) or 0)
                 for s in hosted_servers()}
        # Порт основного сервера MineSerc тоже занят
        taken.add(int(DEFAULT_PACK["server"].rsplit(":", 1)[1]))
        record = server_host.build(profile, Path(folder), self._mc_dir, taken,
                                   status=self._status("host"), progress=self._progress("host"))
        server_host.accept_eula(Path(record["path"]))
        save_hosted_servers(hosted_servers() + [record])
        return {"name": record["name"], "path": record["path"]}

    def server_settings(self, path: str) -> dict:
        record = self._server(path)
        props = {**server_host.DEFAULTS, **server_host.read_properties(Path(path) / "server.properties")}
        fields = []
        for key, label, kind, choices in server_host.SETTINGS:
            value = props.get(key, "")
            field = {"key": key, "label": label, "kind": kind, "value": value.lower() == "true" if kind == "bool" else value}
            if kind == "choice":
                field["choices"] = list(choices.items())
            fields.append(field)
        ip = server_host.radmin_ip()
        hint = (f"Друзья подключаются по адресу {ip}:{props.get('server-port')}" if ip else
                "Radmin VPN не найден: друзья подключаются по твоему адресу и этому порту")
        return {"name": record["name"], "fields": fields, "ram": record.get("ram", 4),
                "auto_stop": record.get("auto_stop", 0), "hint": hint}

    def save_server_settings(self, path: str, values: dict) -> None:
        record = self._server(path)
        changes = {}
        for key, label, kind, _ in server_host.SETTINGS:
            value = values.get(key)
            if kind == "bool":
                value = "true" if value else "false"
            elif kind == "int":
                value = str(value).strip()
                if not value.isdigit():
                    raise RuntimeError(f"«{label}» — должно быть числом.")
            changes[key] = str(value)
        changes["query.port"] = changes["server-port"]
        # Пиратам (TLauncher) мешает проверка подписи чата
        changes["enforce-secure-profile"] = changes["online-mode"]
        ram = str(values.get("ram", "")).strip()
        if not ram.isdigit() or int(ram) < 1:
            raise RuntimeError("Память — целое число гигабайт.")
        auto_stop = str(values.get("auto_stop", 0)).strip() or "0"
        if not auto_stop.isdigit():
            raise RuntimeError("Автовыключение — число минут (0 — не выключать).")
        server_host.write_properties(Path(path) / "server.properties", changes)
        record["ram"] = int(ram)
        record["auto_stop"] = int(auto_stop)
        server_host.write_start(record)
        save_hosted_servers([record if s["path"] == path else s for s in hosted_servers()])

    def start_server(self, path: str) -> str:
        record = self._server(path)
        if self._online(path):
            raise RuntimeError("Сервер уже запущен.")
        server_admin.ensure_rcon(Path(path))
        saved = server_admin.backup(Path(path), False, self._status("host"))
        server_host.start(record)
        start_sharing()
        return ((f"Бэкап мира: {saved.name}. " if saved else "")
                + f"Сервер «{record['name']}» запускается в отдельном окне. Друзья в Radmin увидят его в лаунчере "
                f"на вкладке «Наши серверы». Если Windows спросит про доступ MineForFriends к сети — разреши.")

    def stop_server(self, path: str) -> str:
        self._server(path)
        server_admin.rcon(Path(path), "stop")
        return "Сервер сохраняет мир и выключается."

    def restart_server(self, path: str) -> str:
        self.stop_server(path)
        status = self._status("host")
        status("Жду, пока сервер выключится…")
        for _ in range(90):
            time.sleep(2)
            if not self._online(path):
                break
        else:
            raise RuntimeError("Сервер не выключился за 3 минуты. Проверь окно его консоли.")
        time.sleep(3)  # окно консоли ещё дописывает мир и закрывается
        return self.start_server(path)

    def server_players(self, path: str) -> dict:
        self._server(path)
        online = self._online(path)
        return {**server_admin.players(Path(path), online), "running": online}

    def server_player_action(self, path: str, action: str, name: str = "") -> None:
        self._server(path)
        server_admin.player_action(Path(path), self._online(path), action, name.strip())

    def server_load(self, path: str) -> dict:
        record = self._server(path)
        if not getattr(self, "_monitor", None):
            self._monitor = server_admin.Monitor()
        result = self._monitor.sample(Path(path), record.get("loader", ""), int(record.get("ram") or 4))
        port = server_host.read_properties(Path(path) / "server.properties").get("server-port", "25565")
        status = server_status(f"127.0.0.1:{port}", 1) if result["running"] else None
        result["players"] = status["online"] if status else None
        return result

    def backup_server(self, path: str) -> str:
        self._server(path)
        saved = server_admin.backup(Path(path), self._online(path), self._status("host"))
        if not saved:
            return "Мира ещё нет: он появится после первого запуска сервера."
        return f"Бэкап готов: {saved}"

    def server_console(self, path: str, pos: int = -1) -> dict:
        self._server(path)
        return server_admin.log_tail(Path(path), int(pos))

    def server_command(self, path: str, command: str) -> str:
        self._server(path)
        command = command.strip().lstrip("/")
        if not command:
            return ""
        return server_admin.rcon(Path(path), command)[0]

    def server_backups(self, path: str) -> list:
        self._server(path)
        return server_admin.backups(Path(path))

    def restore_backup(self, path: str, name: str) -> str:
        self._server(path)
        if self._online(path):
            raise RuntimeError("Сначала останови сервер — мир нельзя подменить, пока он запущен.")
        before = server_admin.restore(Path(path), name, self._status("host"))
        return (f"Мир восстановлен из «{name}»."
                + (f" Мир, каким он был до этого, сохранён в «{before.name}»." if before else ""))

    def server_stats(self, path: str) -> list:
        self._server(path)
        return server_admin.player_stats(Path(path))

    # ---- Скриншоты

    def _shot(self, path: str) -> Path:
        # Только файлы из папок скриншотов игры — путь приходит из интерфейса
        p = Path(path)
        if p.parent not in screenshots.folders(self._mc_dir) or not p.is_file():
            raise RuntimeError("Скриншот не найден — возможно, его уже удалили.")
        return p

    def screenshot_list(self) -> list:
        names = {str(self._mc_dir / DEFAULT_PACK["game_dir"]): DEFAULT_PACK["name"]}
        names.update({p["game_dir"]: p["name"] for p in custom_profiles()})
        threading.Thread(target=screenshots.clean_cache, args=(SETTINGS_FILE.parent / "thumbs",), daemon=True).start()
        return screenshots.find(self._mc_dir, names)

    def screenshot_thumb(self, path: str):
        try:
            return screenshots.thumbnail(self._shot(path), SETTINGS_FILE.parent / "thumbs")
        except (OSError, ValueError):
            return None  # битый файл — в галерее будет пустая плитка

    def screenshot_full(self, path: str) -> str:
        try:
            return screenshots.full(self._shot(path))
        except (OSError, ValueError) as e:
            raise RuntimeError(f"Не удалось открыть картинку: {e}") from None

    def screenshot_copy(self, path: str) -> None:
        screenshots.copy_image(self._shot(path))

    def screenshot_show(self, path: str) -> None:
        subprocess.Popen(["explorer", "/select,", str(self._shot(path))])

    def forget_server(self, path: str) -> None:
        save_hosted_servers([s for s in hosted_servers() if s["path"] != path])

    # ---- Скин

    def pick_skin(self):
        # Только выбор и проверка файла: игрок сначала смотрит предпросмотр, потом загружает
        png = self._pick(webview.FileDialog.OPEN, ("Скин PNG (*.png)",))
        if not png:
            return None
        data = Path(png).read_bytes()
        size = png_size(data)
        if not size or not valid_skin_size(*size):
            raise RuntimeError("Это не похоже на скин: нужен PNG 64×64 (или старый 64×32, или HD со стороной, кратной 64).")
        if len(data) > MAX_SKIN:
            raise RuntimeError("Файл скина больше 1 МБ.")
        self._skin_path = Path(png)
        return {"skin": base64.b64encode(data).decode(), "name": self._skin_path.name}

    def pick_cape(self):
        png = self._pick(webview.FileDialog.OPEN, ("Плащ PNG (*.png)",))
        if not png:
            return None
        data = Path(png).read_bytes()
        size = png_size(data)
        if not size or not valid_cape_size(*size):
            raise RuntimeError("Это не похоже на плащ: нужен PNG 64×32 (или HD со сторонами, кратными 64×32).")
        if len(data) > MAX_SKIN:
            raise RuntimeError("Файл плаща больше 1 МБ.")
        self._cape = png
        return {"cape": base64.b64encode(data).decode(), "name": Path(png).name}

    def remove_cape(self):
        # Плащ уберётся с сервера по кнопке «Загрузить на сервер», вместе со скином
        self._cape = ""

    def upload_skin(self, nick: str, slim: bool) -> dict:
        if not self._skin_path and self._cape is None:
            raise RuntimeError("Сначала выбери PNG со скином или плащом.")
        updater = Updater(self._status("skin"), lambda v: None)
        SKIN_FILE.parent.mkdir(parents=True, exist_ok=True)
        done = []
        if self._skin_path:
            done.append(updater.upload_skin(nick, self._skin_path, slim))
            # Копия для аватарки в шапке и предпросмотра без облака
            shutil.copyfile(self._skin_path, SKIN_FILE)
            settings = load_json(SETTINGS_FILE, {})
            settings["skin_model"] = "slim" if slim else "default"
            save_json(SETTINGS_FILE, settings)
            self._skin_path = None
        if self._cape is not None:
            data = Path(self._cape).read_bytes() if self._cape else b""
            message = updater.upload_texture(nick, data, "cape")
            if data:
                CAPE_FILE.write_bytes(data)
            else:
                CAPE_FILE.unlink(missing_ok=True)
            self._cape = None
            done.append(message if done else f"{message} для {nick}. В игре это будет видно через несколько минут, после перезахода.")
        return {"message": " ".join(done), "skin": skin_b64(), "cape": skin_b64(CAPE_FILE)}

    def server_skin(self, nick: str):
        """Скин ника на GitHub — так игрока видят другие.

        {"found": True, "skin", "model", "cape"}, {"found": False} — скина нет, None — GitHub недоступен.
        skin и cape — PNG в base64 или None (у ника только плащ / только скин).
        """
        if not NICK_RE.match(nick or ""):
            return {"found": False}

        def texture(name):
            png = web.get(f"{SKINS_ROOT}textures/{name}", timeout=8)
            png.raise_for_status()
            return base64.b64encode(png.content).decode()

        try:
            # Метка времени в запросе — чтобы не получить копию из кэша GitHub
            r = web.get(f"{SKINS_ROOT}{nick}.json", params={"t": int(time.time())}, timeout=8)
            if r.status_code == 404:
                return {"found": False}
            r.raise_for_status()
            profile = r.json()
            model, skin = next(iter(profile.get("skins", {}).items()), ("default", None))
            return {"found": True, "skin": skin and texture(skin), "model": model,
                    "cape": profile.get("cape") and texture(profile["cape"])}
        except (requests.RequestException, ValueError, AttributeError):
            return None

    # ---- Отчёты об ошибках

    def send_report(self, text: str) -> str:
        # Игрок сам нажал «Отправить отчёт» под ошибкой или в «О лаунчере»
        first = text.strip().splitlines()[0] if text.strip() else "Отчёт игрока"
        return reports.send("manual", first, text, reports.signature("manual", first), auto=False)

    def report_js(self, message: str, stack: str) -> None:
        print(f"Ошибка интерфейса: {message}\n{stack}")
        threading.Thread(target=reports.send, args=("js", message, f"{message}\n\n{stack}",
                                                    reports.signature("js", message)), daemon=True).start()


def _guarded(method):
    # Понятные ошибки (RuntimeError) показываем игроку как есть, остальное — сбой: отчёт уходит сам
    @functools.wraps(method)
    def call(self, *args, **kwargs):
        try:
            return method(self, *args, **kwargs)
        except RuntimeError as e:
            print(f"{method.__name__}: {e}")
            raise
        except requests.RequestException as e:
            # Сеть, а не поломка лаунчера: объясняем, что делать
            reports.crash(e)
            host = urllib.parse.urlsplit(e.request.url).hostname if e.request is not None else None
            where = f" с {host}" if host else ""
            raise RuntimeError(f"Не получилось скачать{where}: связь оборвалась.\n\n"
                               "Попробуй ещё раз. Если повторяется — провайдер мешает этому сайту: "
                               "лаунчер сам пойдёт в обход через облако хоста, когда облако запущено "
                               "и ты в сети Radmin.") from None
        except Exception as e:
            reports.crash(e)
            raise RuntimeError(f"В лаунчере что-то сломалось ({type(e).__name__}: {e}).\n\n"
                               "Отчёт об ошибке отправляется автору.") from None
    return call


for _name, _method in list(vars(Api).items()):
    if callable(_method) and not _name.startswith("_"):
        setattr(Api, _name, _guarded(_method))


def run_gui() -> None:
    api = Api()
    api._window = webview.create_window(
        APP_NAME, url=str(RESOURCES / "ui" / "index.html"), js_api=api, width=1000, height=660,
        resizable=False, frameless=True, easy_drag=False, background_color="#14171c")
    try:
        import tray  # pystray нужен только окну, фоновому --share он ни к чему
        api._tray = tray.Tray(APP_NAME, RESOURCES / "icon.ico", api.show_window, api.quit_app)
        api._tray.start()
    except Exception as e:
        print(f"Трей недоступен: {e}")  # без трея крестик просто закрывает лаунчер
        api._tray = None
    webview.start(http_server=True, icon=str(RESOURCES / "icon.ico"))
    if api._tray:
        api._tray.stop()


if __name__ == "__main__":
    # Отладка: launcher.py --cli [--no-open] [--packs id1,id2]  |  launcher.py --skin <ник> <файл.png> [--slim]
    # --share — фоновая раздача сборок своих серверов друзьям (запускает сам лаунчер)
    migrate_settings()
    sharing = len(sys.argv) >= 2 and sys.argv[1] == "--share"
    reports.setup(SETTINGS_FILE.parent, APP_VERSION, build_id(), "share" if sharing else "launcher")
    if sharing:
        run_sharing()
    elif len(sys.argv) >= 4 and sys.argv[1] == "--skin":
        print(Updater(print, lambda v: None).upload_skin(sys.argv[2], Path(sys.argv[3]), "--slim" in sys.argv))
    elif len(sys.argv) >= 2 and sys.argv[1] == "--cli":
        ids = sys.argv[sys.argv.index("--packs") + 1].split(",") if "--packs" in sys.argv else None
        print(Updater(print, lambda v: None).run(open_tl="--no-open" not in sys.argv, pack_ids=ids))
    else:
        run_gui()
