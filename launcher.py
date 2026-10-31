"""Обновлятор MineSerc для TLauncher.

Для каждой выбранной сборки (кнопка «Сборки…») создаёт в TLauncher свою версию
(Fabric, сервер в «Сетевой игре», отдельная папка игры), синхронизирует её файлы
с облака на ПК хоста (через Radmin) и открывает TLauncher.
Ещё загружает скин игрока в облако; в игре его показывает мод CustomSkinLoader.
"""
import hashlib
import json
import os
import re
import secrets
import socket
import subprocess
import sys
import threading
import time
import traceback
import webbrowser
from pathlib import Path

import requests

APP_NAME = "MineSerc"
CLOUD_URL = "http://26.199.180.115:8518"
# Каталог скинов: там можно скачать PNG и выбрать его кнопкой «Выбрать скин»
SKINS_SITE = "https://www.minecraftskins.com/"
APPDATA = Path(os.environ.get("APPDATA", Path.home()))
TL_PROPERTIES = APPDATA / ".tlauncher" / "tlauncher-2.0.properties"
SETTINGS_FILE = APPDATA / APP_NAME / "settings.json"
MOJANG_MANIFEST = "https://piston-meta.mojang.com/mc/game/version_manifest_v2.json"

# Основная сборка; старый манифест облака не содержит этих полей
DEFAULT_PACK = {
    "id": "mineserc", "name": "MineSerc 26.3", "manifest": "manifest.json", "base": "",
    "version_id": "MineSerc 26.3", "game_dir": "mineserc", "server_name": "MineSerc",
    "server": "26.199.180.115:25565",
}
# Как часто обновлять индикатор серверов, мс
STATUS_REFRESH = 30_000
# Описание свежего MineSerc.exe в облаке ({"sha1", "size"}), пишет update_manifest.py
LAUNCHER_INFO = "launcher.json"
# Внутри exe ресурсы лежат во временной папке PyInstaller
RESOURCES = Path(getattr(sys, "_MEIPASS", Path(__file__).resolve().parent))

# К облаку ходим напрямую: системный прокси (VPN-клиенты) не видит сеть Radmin
cloud = requests.Session()
cloud.trust_env = False
web = requests.Session()


def load_json(path: Path, default):
    try:
        return json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        return default


def save_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")


def sha1(path: Path) -> str:
    h = hashlib.sha1()
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


def self_update(status) -> bool:
    """Если в облаке новый MineSerc.exe — ставит его на место текущего и запускает.

    Запущенный exe Windows не даёт перезаписать, но даёт переименовать: старый
    уезжает в MineSerc.old.exe и удаляется при следующем запуске.
    True — новая версия запущена, этот экземпляр нужно закрыть.
    """
    if not getattr(sys, "frozen", False):
        return False
    exe = Path(sys.executable)
    old = exe.with_name(exe.stem + ".old.exe")
    try:
        old.unlink(missing_ok=True)
    except OSError:
        pass  # прошлый экземпляр ещё закрывается
    try:
        info = cloud.get(f"{CLOUD_URL}/{LAUNCHER_INFO}", timeout=5).json()
        if not info.get("sha1") or sha1(exe) == info["sha1"]:
            return False
        status("Скачиваю новую версию лаунчера…")
        part = exe.with_name(exe.name + ".part")
        with cloud.get(f"{CLOUD_URL}/MineSerc.exe", stream=True, timeout=30) as r:
            r.raise_for_status()
            with part.open("wb") as f:
                for chunk in r.iter_content(1 << 16):
                    f.write(chunk)
        if sha1(part) != info["sha1"]:
            part.unlink()
            return False
        exe.replace(old)
        try:
            part.replace(exe)
        except OSError:
            old.replace(exe)
            raise
    except (requests.RequestException, ValueError, OSError):
        # Нет облака или нет прав на папку — работаем на текущей версии
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
        ours = {"name": APP_NAME, "type": "CustomSkinAPI", "root": f"{CLOUD_URL}/skins/"}
        loadlist = config.get("loadlist") or []
        if loadlist and loadlist[0] == ours:
            return
        config["loadlist"] = [ours] + [p for p in loadlist if p.get("name") != APP_NAME]
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
            raise RuntimeError("Не выбрано ни одной сборки. Нажми «Сборки…» и отметь нужные.")

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


def run_gui() -> None:
    import tkinter as tk
    from tkinter import filedialog, messagebox, ttk

    root = tk.Tk()
    root.title(APP_NAME)
    root.resizable(False, False)
    try:
        root.iconbitmap(str(RESOURCES / "icon.ico"))
    except tk.TclError:
        pass
    frm = ttk.Frame(root, padding=16)
    frm.grid()

    ttk.Label(frm, text="MineSerc", font=("Segoe UI", 18, "bold")).grid(column=0, row=0, pady=(0, 4))
    ttk.Label(frm, text="Обновление модов и версии для TLauncher").grid(column=0, row=1, pady=(0, 8))
    servers = ttk.LabelFrame(frm, text="Серверы", padding=8)
    servers.grid(column=0, row=2, pady=(0, 10), sticky="we")
    servers.columnconfigure(1, weight=1)
    ttk.Label(servers, text="Проверяю…", foreground="gray").grid(column=0, row=0, sticky="w")
    bar = ttk.Progressbar(frm, length=340, maximum=1.0)
    bar.grid(column=0, row=3, pady=(4, 4))
    status_var = tk.StringVar(value="Нажми «Обновить и открыть TLauncher»")
    ttk.Label(frm, textvariable=status_var, wraplength=340).grid(column=0, row=4, sticky="w")
    actions = ttk.Frame(frm)
    actions.grid(column=0, row=5, pady=(12, 0), sticky="we")
    actions.columnconfigure(0, weight=1)
    go = ttk.Button(actions, text="Обновить и открыть TLauncher")
    go.grid(column=0, row=0, sticky="we")
    packs_btn = ttk.Button(actions, text="Сборки…")
    packs_btn.grid(column=1, row=0, padx=(6, 0))

    # Скин: ник как в TLauncher, тип модели и выбор PNG
    skin = ttk.LabelFrame(frm, text="Скин на сервере", padding=8)
    skin.grid(column=0, row=6, pady=(14, 0), sticky="we")
    skin.columnconfigure(2, weight=1)
    probe = Updater(lambda s: None, lambda v: None)
    nick_var = tk.StringVar(value=load_json(probe.state_file, {}).get("skin_nick") or tlauncher_nick(probe.mc_dir))
    slim_var = tk.BooleanVar(value=False)
    ttk.Label(skin, text="Ник:").grid(column=0, row=0, sticky="w")
    ttk.Entry(skin, textvariable=nick_var, width=24).grid(column=1, row=0, columnspan=2, sticky="we", padx=(6, 0))
    ttk.Radiobutton(skin, text="Обычная модель", variable=slim_var, value=False).grid(column=0, row=1, columnspan=2, sticky="w", pady=(6, 0))
    ttk.Radiobutton(skin, text="Тонкие руки (Алекс)", variable=slim_var, value=True).grid(column=2, row=1, sticky="w", pady=(6, 0))
    buttons = ttk.Frame(skin)
    buttons.grid(column=0, row=2, columnspan=3, pady=(8, 0), sticky="we")
    buttons.columnconfigure((0, 1), weight=1, uniform="skin")
    pick = ttk.Button(buttons, text="Выбрать скин (PNG)…")
    pick.grid(column=0, row=0, sticky="we", padx=(0, 3))
    site = ttk.Button(buttons, text="Найти скин на сайте", command=lambda: webbrowser.open(SKINS_SITE))
    site.grid(column=1, row=0, sticky="we", padx=(3, 0))

    def ui(fn, *a):
        root.after(0, fn, *a)

    def show_servers(entries, statuses):
        # Индикатор: какой сервер сейчас включён и доступно ли облако со сборками
        for w in servers.winfo_children():
            w.destroy()
        if entries is None:
            ttk.Label(servers, text="●", foreground="#c62828").grid(column=0, row=0, sticky="w")
            ttk.Label(servers, text="Облако недоступно — сборки сейчас не загрузить.\nПроверь Radmin VPN или подожди, пока хост включит сервер.",
                      wraplength=310).grid(column=1, row=0, sticky="w", padx=(6, 0))
            return
        chosen = set(selected_packs())
        for i, (e, st) in enumerate(zip(entries, statuses)):
            name = e["name"] + ("" if e["id"] in chosen else " (не выбрана)")
            if st:
                color, text = "#2e7d32", f"включён · игроков {st['online']}/{st['max']}"
            else:
                color, text = "#9e9e9e", "выключен"
            ttk.Label(servers, text="●", foreground=color).grid(column=0, row=i, sticky="w")
            ttk.Label(servers, text=name).grid(column=1, row=i, sticky="w", padx=(6, 12))
            ttk.Label(servers, text=text, foreground=color).grid(column=2, row=i, sticky="e")
        ttk.Label(servers, text="Облако доступно — все сборки можно загрузить", foreground="gray").grid(
            column=0, row=len(entries), columnspan=3, sticky="w", pady=(6, 0))

    def status_worker(again):
        try:
            entries = Updater(lambda s: None, lambda v: None).fetch_packs()
        except Exception:
            entries = None
        statuses = []
        if entries is not None:
            results = {}
            threads = [threading.Thread(target=lambda e=e: results.__setitem__(e["id"], server_status(e.get("server") or DEFAULT_PACK["server"])))
                       for e in entries]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            statuses = [results.get(e["id"]) for e in entries]
        ui(show_servers, entries, statuses)
        if again:
            ui(root.after, STATUS_REFRESH, refresh_servers)

    def refresh_servers(again=True):
        threading.Thread(target=status_worker, args=(again,), daemon=True).start()

    def worker():
        upd = Updater(lambda s: ui(status_var.set, s), lambda v: ui(bar.configure, {"value": v}))
        try:
            ui(status_var.set, upd.run())
            ui(root.after, 6000, root.destroy)
        except Exception as e:
            traceback.print_exc()
            ui(messagebox.showerror, APP_NAME, str(e))
            ui(status_var.set, "Ошибка, попробуй ещё раз")
            ui(go.state, ["!disabled"])

    def on_go():
        go.state(["disabled"])
        threading.Thread(target=worker, daemon=True).start()

    def show_packs(entries):
        # Окно выбора сборок: отмеченные устанавливаются и обновляются кнопкой «Обновить»
        win = tk.Toplevel(root)
        win.title("Сборки")
        win.resizable(False, False)
        win.transient(root)
        win.grab_set()
        box = ttk.Frame(win, padding=16)
        box.grid()
        ttk.Label(box, text="Отметь сборки, которые нужно установить и обновлять:").grid(column=0, row=0, sticky="w", pady=(0, 8))
        chosen = set(selected_packs())
        checks = {}
        for i, e in enumerate(entries):
            checks[e["id"]] = tk.BooleanVar(value=e["id"] in chosen)
            size = f" — {e['size'] / 2**20:.0f} МБ" if e.get("size") else ""
            ttk.Checkbutton(box, text=e["name"] + size, variable=checks[e["id"]]).grid(column=0, row=1 + 2 * i, sticky="w")
            if e.get("description"):
                ttk.Label(box, text=e["description"], foreground="gray", wraplength=320).grid(
                    column=0, row=2 + 2 * i, sticky="w", padx=(22, 0), pady=(0, 6))

        def save():
            ids = [pid for pid, var in checks.items() if var.get()]
            if not ids:
                messagebox.showwarning(APP_NAME, "Отметь хотя бы одну сборку.", parent=win)
                return
            save_selected_packs(ids)
            names = ", ".join(e["name"] for e in entries if e["id"] in ids)
            status_var.set(f"Выбрано: {names}. Нажми «Обновить и открыть TLauncher».")
            win.destroy()
            refresh_servers(again=False)

        ttk.Button(box, text="Сохранить", command=save).grid(column=0, row=1 + 2 * len(entries), sticky="e", pady=(10, 0))

    def packs_worker():
        upd = Updater(lambda s: ui(status_var.set, s), lambda v: None)
        try:
            entries = upd.fetch_packs()
            ui(status_var.set, "Выбери сборки")
            ui(show_packs, entries)
        except Exception as e:
            traceback.print_exc()
            ui(messagebox.showerror, APP_NAME, str(e))
            ui(status_var.set, "Не удалось получить список сборок")
        ui(packs_btn.state, ["!disabled"])

    def on_packs():
        packs_btn.state(["disabled"])
        threading.Thread(target=packs_worker, daemon=True).start()

    def skin_worker(nick, png, slim):
        upd = Updater(lambda s: ui(status_var.set, s), lambda v: None)
        try:
            ui(status_var.set, upd.upload_skin(nick, png, slim))
        except Exception as e:
            traceback.print_exc()
            ui(messagebox.showerror, APP_NAME, str(e))
            ui(status_var.set, "Скин не загружен")
        ui(pick.state, ["!disabled"])

    def on_pick():
        nick = nick_var.get().strip()
        if not nick:
            messagebox.showwarning(APP_NAME, "Впиши ник, под которым играешь в TLauncher.")
            return
        png = filedialog.askopenfilename(title="Выбери скин", filetypes=[("Скин PNG", "*.png")])
        if png:
            pick.state(["disabled"])
            threading.Thread(target=skin_worker, args=(nick, Path(png), slim_var.get()), daemon=True).start()

    go.configure(command=on_go)
    packs_btn.configure(command=on_packs)
    pick.configure(command=on_pick)

    def self_update_worker():
        # Пока проверяем версию лаунчера, кнопки сборок неактивны
        if self_update(lambda s: ui(status_var.set, s)):
            ui(root.destroy)
            return
        ui(go.state, ["!disabled"])
        ui(packs_btn.state, ["!disabled"])

    go.state(["disabled"])
    packs_btn.state(["disabled"])
    threading.Thread(target=self_update_worker, daemon=True).start()
    refresh_servers()
    root.mainloop()


if __name__ == "__main__":
    # Отладка: launcher.py --cli [--no-open] [--packs id1,id2]  |  launcher.py --skin <ник> <файл.png> [--slim]
    if len(sys.argv) >= 4 and sys.argv[1] == "--skin":
        print(Updater(print, lambda v: None).upload_skin(sys.argv[2], Path(sys.argv[3]), "--slim" in sys.argv))
    elif len(sys.argv) >= 2 and sys.argv[1] == "--cli":
        ids = sys.argv[sys.argv.index("--packs") + 1].split(",") if "--packs" in sys.argv else None
        print(Updater(print, lambda v: None).run(open_tl="--no-open" not in sys.argv, pack_ids=ids))
    else:
        run_gui()
