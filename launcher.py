"""MineForFriends by TEJKA — лаунчер-помощник для игры с друзьями через TLauncher.

Для каждой отмеченной сборки (карточки «Наши серверы») создаёт в TLauncher свою версию
(Fabric, сервер в «Сетевой игре», отдельная папка игры), синхронизирует её файлы
с облака на ПК хоста (через Radmin) и открывает TLauncher.
Ещё загружает скин игрока в облако; в игре его показывает мод CustomSkinLoader.
Интерфейс — HTML-страница из папки ui/ в окне pywebview (WebView2).
"""
import base64
import hashlib
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
import webbrowser
from pathlib import Path

import requests
import webview

import custom_packs
import server_host

APP_NAME = "MineForFriends by TEJKA"
APP_VERSION = "0.5.1 beta"
# Короткое имя без пробелов: папка настроек, имя exe, загрузчик скинов
APP_ID = "MineForFriends"
LAUNCHER_EXE = f"{APP_ID}.exe"
# Прежнее имя приложения: забираем его настройки и чистим его следы
OLD_APP_ID = "MineSerc"
CLOUD_URL = "http://26.199.180.115:8518"
# Каталог скинов: там можно скачать PNG и выбрать его кнопкой «Выбрать скин»
SKINS_SITE = "https://www.minecraftskins.com/"
REPO_URL = "https://github.com/TejkaFan/MineForFriends"
APPDATA = Path(os.environ.get("APPDATA", Path.home()))
TL_PROPERTIES = APPDATA / ".tlauncher" / "tlauncher-2.0.properties"
SETTINGS_FILE = APPDATA / APP_ID / "settings.json"
OLD_SETTINGS_FILE = APPDATA / OLD_APP_ID / "settings.json"
# Копия последнего загруженного скина — для аватарки в шапке
SKIN_FILE = SETTINGS_FILE.parent / "skin.png"
MOJANG_MANIFEST = "https://piston-meta.mojang.com/mc/game/version_manifest_v2.json"

# Основная сборка; старый манифест облака не содержит этих полей
DEFAULT_PACK = {
    "id": "mineserc", "name": "MineSerc 26.3", "manifest": "manifest.json", "base": "",
    "version_id": "MineSerc 26.3", "game_dir": "mineserc", "server_name": "MineSerc",
    "server": "26.199.180.115:25565",
}
# Как часто обновлять индикатор серверов, мс
STATUS_REFRESH = 30_000
# Описание свежего MineForFriends.exe в облаке ({"sha1", "size"}), пишет update_manifest.py
LAUNCHER_INFO = "launcher.json"
# Последний релиз на GitHub: основной источник обновлений лаунчера, работает и без Radmin
GITHUB_LATEST = "https://api.github.com/repos/TejkaFan/MineForFriends/releases/latest"
# Внутри exe ресурсы лежат во временной папке PyInstaller
RESOURCES = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))

# К облаку ходим напрямую: системный прокси (VPN-клиенты) не видит сеть Radmin
cloud = requests.Session()
cloud.trust_env = False
web = requests.Session()


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
        return json.loads(path.read_text("utf-8"))
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
    return {"online": players.get("online", 0), "max": players.get("max", 0),
            "version": (data.get("version") or {}).get("name", "")}


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


def selected_packs() -> list:
    return load_json(SETTINGS_FILE, {}).get("packs") or [DEFAULT_PACK["id"]]


def save_selected_packs(ids: list) -> None:
    settings = load_json(SETTINGS_FILE, {})
    settings["packs"] = ids
    save_json(SETTINGS_FILE, settings)


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


def latest_launcher():
    """Где взять свежий MineForFriends.exe: (сессия, ссылка, алгоритм хэша, хэш) или None.

    Сначала последний релиз на GitHub, а если GitHub недоступен или в релизе нет exe —
    облако хоста (launcher.json).
    """
    try:
        r = web.get(GITHUB_LATEST, timeout=8, headers={"Accept": "application/vnd.github+json"})
        r.raise_for_status()
        asset = next((a for a in r.json().get("assets", []) if a.get("name") == LAUNCHER_EXE), None)
        algo, _, digest = str((asset or {}).get("digest") or "").partition(":")
        if algo == "sha256" and digest:
            return web, asset["browser_download_url"], "sha256", digest
    except (requests.RequestException, ValueError, KeyError):
        traceback.print_exc()
    try:
        info = cloud.get(f"{CLOUD_URL}/{LAUNCHER_INFO}", timeout=5).json()
        if info.get("sha1"):
            return cloud, f"{CLOUD_URL}/{LAUNCHER_EXE}", "sha1", info["sha1"]
    except (requests.RequestException, ValueError):
        traceback.print_exc()
    return None


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
    latest = latest_launcher()
    if not latest:
        return False
    session, url, algo, digest = latest
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
            return False
        exe.replace(old)
        try:
            part.replace(exe)
        except OSError:
            old.replace(exe)
            raise
    except (requests.RequestException, ValueError, OSError):
        # Обрыв загрузки или нет прав на папку — работаем на текущей версии
        traceback.print_exc()
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

    def fetch_json(self, path: str):
        try:
            r = cloud.get(f"{CLOUD_URL}/{path}", timeout=8)
            r.raise_for_status()
            return r.json()
        except requests.RequestException as e:
            raise RuntimeError(
                "Нет связи с сервером.\n\nПроверь, что Radmin VPN запущен и ты в нашей сети, "
                "а хост включил сервер.\n\n" + str(e)
            ) from None

    def fetch_packs(self) -> list:
        self.status("Связь с сервером…")
        try:
            r = cloud.get(f"{CLOUD_URL}/packs.json", timeout=8)
            if r.status_code == 404:
                return [dict(DEFAULT_PACK)]  # облако без списка сборок
            r.raise_for_status()
            return r.json()["packs"]
        except requests.RequestException:
            return [dict(DEFAULT_PACK)] if self.fetch_json(DEFAULT_PACK["manifest"]) else []

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

    def setup_skin_loader(self, game_dir: Path) -> None:
        # Наше облако первым в списке CustomSkinLoader, остальные его настройки не трогаем
        path = game_dir / "CustomSkinLoader" / "CustomSkinLoader.json"
        config = load_json(path, {})
        ours = {"name": APP_ID, "type": "CustomSkinAPI", "root": f"{CLOUD_URL}/skins/"}
        loadlist = config.get("loadlist") or []
        if loadlist and loadlist[0] == ours:
            return
        config["loadlist"] = [ours] + [p for p in loadlist if p.get("name") not in (APP_ID, OLD_APP_ID)]
        save_json(path, config)

    def upload_skin(self, nick: str, png: Path, slim: bool) -> str:
        if not re.fullmatch(r"[A-Za-z0-9_]{3,16}", nick):
            raise RuntimeError("Ник должен быть из 3–16 латинских букв, цифр или _ — как в TLauncher.")
        data = png.read_bytes()
        if not data.startswith(b"\x89PNG\r\n\x1a\n"):
            raise RuntimeError("Скин должен быть картинкой PNG.")
        # Токен ника: создаётся при первой загрузке и хранится только у этого игрока
        state = load_json(self.state_file, {})
        tokens = state.setdefault("skin_tokens", {})
        token = tokens.setdefault(nick.lower(), secrets.token_urlsafe(24))
        save_json(self.state_file, state)

        self.status("Загружаю скин…")
        headers = {"X-Nick": nick, "X-Token": token, "X-Model": "slim" if slim else "default",
                   "Content-Type": "image/png"}
        try:
            r = cloud.post(f"{CLOUD_URL}/skins/upload", data=data, headers=headers, timeout=15)
        except requests.RequestException as e:
            raise RuntimeError("Нет связи с сервером. Проверь Radmin VPN.\n\n" + str(e)) from None
        try:
            message = r.json()["message"]
        except (ValueError, KeyError):
            message = f"Ошибка сервера ({r.status_code})"
        if r.status_code != 200:
            raise RuntimeError(message)
        state["skin_nick"] = nick
        save_json(self.state_file, state)
        return f"{message} для {nick}. Другие увидят его после перезахода на сервер."

    def download(self, url_path: str, dest: Path, expected_sha1: str) -> None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(dest.name + ".part")
        with cloud.get(f"{CLOUD_URL}/{requests.utils.quote(url_path)}", stream=True, timeout=30) as r:
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
        done = 0
        for i, f in enumerate(todo, 1):
            self.status(f"{name}: скачиваю {Path(f['path']).name} ({i}/{len(todo)}, {done / 2**20:.0f} из {total / 2**20:.0f} МБ)")
            self.download(pack["base"] + f["path"], inside(game_dir, f["path"]), f["sha1"])
            if f["path"] not in once:
                recorded[f["path"]] = f["sha1"]
            done += f["size"]
            self.progress(done / total)
            if i % 100 == 0:
                # Чтобы после обрыва связи продолжить с того же места
                state["files"] = recorded
                save_json(state_file, state)

        state["files"] = recorded
        state.pop("managed_mods", None)
        state.pop("managed_resourcepacks", None)
        save_json(state_file, state)

        options = game_dir / "options.txt"
        if not options.exists():
            options.write_text("lang:ru_ru\n", "utf-8")

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
        exe = self.mc_dir / "TLauncher.exe"
        if exe.exists():
            subprocess.Popen([str(exe)], cwd=str(self.mc_dir))
            return True
        return False

    def run(self, open_tl: bool = True, pack_ids: list = None) -> str:
        ids = pack_ids or selected_packs()
        chosen = [e for e in self.fetch_packs() if e["id"] in ids]
        if not chosen:
            raise RuntimeError("Не выбрано ни одной сборки. Отметь нужные карточки на вкладке «Наши серверы».")

        versions, notes = [], []
        for entry in chosen:
            self.status(f"{entry['name']}: получаю список файлов…")
            pack = normalize_pack(entry, self.fetch_json(entry["manifest"]))
            game_dir = self.mc_dir / pack["game_dir"]
            self.install_version(pack, game_dir)
            self.setup_skin_loader(game_dir)
            self.sync_pack(pack, game_dir)
            if pack["enable"]:
                self.enable_packs(game_dir, pack["enable"])
            versions.append(pack["version_id"])
            if pack.get("note"):
                notes.append(pack["note"])

        self.progress(1)
        which = f"версию «{versions[0]}»" if len(versions) == 1 else "нужную версию: «" + "» или «".join(versions) + "»"
        where = "В TLauncher" if open_tl and self.open_tlauncher() else "Открой TLauncher и"
        return f"Готово! {where} выбери {which} и нажми «Войти в игру».{' ' + ' '.join(notes) if notes else ''}"


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


def build_id() -> str:
    # Начало хэша exe: по нему видно, какая сборка лаунчера у игрока
    return sha1(Path(sys.executable))[:10] if getattr(sys, "frozen", False) else "dev"


def skin_b64():
    try:
        return base64.b64encode(SKIN_FILE.read_bytes()).decode() if SKIN_FILE.exists() else None
    except OSError:
        return None


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

    def _emit(self, event: str, data=None) -> None:
        try:
            self._window.run_js(f"app.emit({json.dumps(event)}, {json.dumps(data, ensure_ascii=False)})")
        except Exception:
            pass  # окно уже закрыто

    def _status(self, scope: str):
        return lambda text: self._emit("status", {"scope": scope, "text": text})

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
        return {"nick": state.get("skin_nick") or tlauncher_nick(self._mc_dir), "skin": skin_b64(),
                "loaders": [[l, custom_packs.LOADER_NAMES[l]] for l in custom_packs.LOADERS],
                "version": APP_VERSION, "build": build_id(), "cloud_url": CLOUD_URL, "mc_dir": str(self._mc_dir),
                "settings_dir": str(SETTINGS_FILE.parent), "repo": REPO_URL,
                "eula_url": server_host.EULA_URL, "skins_site": SKINS_SITE}

    def boot(self) -> None:
        # Интерфейс готов: проверяем версию лаунчера и запускаем индикатор серверов
        if self._booted:
            return
        self._booted = True
        threading.Thread(target=self._watch_servers, daemon=True).start()
        if self_update(self._status("boot")):
            self._window.destroy()
            return
        self._emit("ready")

    def minimize(self) -> None:
        self._window.minimize()

    def close(self) -> None:
        self._window.destroy()

    def open_url(self, url: str) -> None:
        webbrowser.open(url)

    def open_folder(self, path: str) -> None:
        if Path(path).exists():
            os.startfile(path)

    # ---- Наши серверы

    def _watch_servers(self) -> None:
        while True:
            self._emit("servers", self._servers())
            time.sleep(STATUS_REFRESH / 1000)

    def _servers(self) -> dict:
        # Индикатор: какой сервер сейчас включён и доступно ли облако со сборками
        try:
            entries = Updater(lambda s: None, lambda v: None).fetch_packs()
        except Exception:
            return {"cloud": False, "packs": [], "selected": selected_packs()}
        results = {}
        threads = [threading.Thread(target=lambda e=e: results.__setitem__(
            e["id"], server_status(e.get("server") or DEFAULT_PACK["server"]))) for e in entries]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        packs = [{"id": e["id"], "name": e["name"], "description": e.get("description", ""), "size": e.get("size"),
                  "status": results.get(e["id"])} for e in entries]
        return {"cloud": True, "packs": packs, "selected": selected_packs()}

    def set_selected(self, ids: list) -> None:
        save_selected_packs(ids)

    def play(self) -> str:
        message = Updater(self._status("play"), self._progress("play")).run()
        threading.Timer(6, self._window.destroy).start()
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
        profiles = [p for p in custom_profiles() if p["version_id"] != res["version_id"]] + [
            {k: res[k] for k in ("name", "version_id", "game_dir", "minecraft", "loader", "loader_version")}]
        save_custom_profiles(profiles)
        text = f"В TLauncher выбери версию «{res['version_id']}» и нажми «Войти в игру»."
        if chosen["mods"] > 150:
            text += "\n\nСборка большая — выдели в настройках TLauncher не меньше 6 ГБ памяти."
        if res["notes"]:
            text += "\n\n" + "\n".join(res["notes"])
        return {"version_id": res["version_id"], "text": text}

    def profile_list(self) -> list:
        return [{**p, "loader_name": custom_packs.LOADER_NAMES.get(p["loader"], p["loader"])} for p in custom_profiles()]

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
            servers.append({"name": s["name"], "path": s["path"], "minecraft": s.get("minecraft", ""),
                            "loader_name": custom_packs.LOADER_NAMES.get(s.get("loader"), s.get("loader", "")),
                            "address": f"{ip}:{port}"})
        return servers

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
        return {"name": record["name"], "fields": fields, "ram": record.get("ram", 4), "hint": hint}

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
        server_host.write_properties(Path(path) / "server.properties", changes)
        record["ram"] = int(ram)
        server_host.write_start(record)
        save_hosted_servers([record if s["path"] == path else s for s in hosted_servers()])

    def start_server(self, path: str) -> str:
        record = self._server(path)
        server_host.start(record)
        return f"Сервер «{record['name']}» запускается в отдельном окне."

    def forget_server(self, path: str) -> None:
        save_hosted_servers([s for s in hosted_servers() if s["path"] != path])

    # ---- Скин

    def upload_skin(self, nick: str, slim: bool):
        png = self._pick(webview.FileDialog.OPEN, ("Скин PNG (*.png)",))
        if not png:
            return None
        message = Updater(self._status("skin"), lambda v: None).upload_skin(nick, Path(png), slim)
        # Копия для аватарки в шапке лаунчера
        SKIN_FILE.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(png, SKIN_FILE)
        return {"message": message, "skin": skin_b64()}


def run_gui() -> None:
    api = Api()
    api._window = webview.create_window(
        APP_NAME, url=str(RESOURCES / "ui" / "index.html"), js_api=api, width=1000, height=660,
        resizable=False, frameless=True, easy_drag=False, background_color="#14171c")
    webview.start(http_server=True, icon=str(RESOURCES / "icon.ico"))


if __name__ == "__main__":
    # Отладка: launcher.py --cli [--no-open] [--packs id1,id2]  |  launcher.py --skin <ник> <файл.png> [--slim]
    migrate_settings()
    if len(sys.argv) >= 4 and sys.argv[1] == "--skin":
        print(Updater(print, lambda v: None).upload_skin(sys.argv[2], Path(sys.argv[3]), "--slim" in sys.argv))
    elif len(sys.argv) >= 2 and sys.argv[1] == "--cli":
        ids = sys.argv[sys.argv.index("--packs") + 1].split(",") if "--packs" in sys.argv else None
        print(Updater(print, lambda v: None).run(open_tl="--no-open" not in sys.argv, pack_ids=ids))
    else:
        run_gui()
