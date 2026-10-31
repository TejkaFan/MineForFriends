"""Сервер из своей сборки: вкладка «Сервер» лаунчера.

Из папки игры сборки (её создала вкладка «Свои сборки») собирается сервер в выбранной
папке: моды без клиентских, конфиги и датапаки, серверный загрузчик, подходящая Java
и start.bat. Настройки сервера правятся в окне лаунчера и пишутся в server.properties.
"""
import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import tempfile
import zipfile
from pathlib import Path

import requests

import custom_packs
from custom_packs import FABRIC_META, FORGE_MAVEN, LOADER_NAMES, MOJANG_MANIFEST, MODRINTH, NEOFORGE_MAVEN, get_json, web

# Что переносим из папки игры на сервер; остальное (ресурспаки, шейдеры, миры, кэши) — клиентское
SERVER_DIRS = ("mods", "config", "defaultconfigs", "kubejs", "scripts", "datapacks", "global_packs",
               "global_datapacks", "openloader", "data", "patchouli_books", "moonlight-global-datapacks", "paxi")
JAVA_RUNTIMES = "https://launchermeta.mojang.com/v1/products/java-runtime/2ec0cc96c44e5a76b9c8b7c39df7210883d12871/all.json"
QUILT_INSTALLER = "https://maven.quiltmc.org/repository/release/org/quiltmc/quilt-installer"
EULA_URL = "https://aka.ms/MinecraftEULA"

# Настройки в окне: ключ server.properties, подпись, тип, варианты
DIFFICULTY = {"peaceful": "Мирная", "easy": "Лёгкая", "normal": "Нормальная", "hard": "Сложная"}
GAMEMODE = {"survival": "Выживание", "creative": "Творческий", "adventure": "Приключение", "spectator": "Наблюдатель"}
SETTINGS = [
    ("motd", "Название в списке серверов", "str", None),
    ("server-port", "Порт", "int", None),
    ("max-players", "Игроков максимум", "int", None),
    ("difficulty", "Сложность", "choice", DIFFICULTY),
    ("gamemode", "Режим игры", "choice", GAMEMODE),
    ("view-distance", "Прорисовка (чанков)", "int", None),
    ("simulation-distance", "Симуляция (чанков)", "int", None),
    ("spawn-protection", "Защита спавна (блоков)", "int", None),
    ("level-name", "Папка мира", "str", None),
    ("level-seed", "Сид мира", "str", None),
    ("online-mode", "Только лицензия (без пиратов)", "bool", None),
    ("pvp", "PvP", "bool", None),
    ("hardcore", "Хардкор", "bool", None),
    ("allow-flight", "Разрешить полёт (нужно многим модам)", "bool", None),
    ("white-list", "Белый список", "bool", None),
    ("enable-command-block", "Командные блоки", "bool", None),
]
DEFAULTS = {
    "motd": "Сервер на сборке", "server-port": "25565", "max-players": "20", "difficulty": "normal",
    "gamemode": "survival", "view-distance": "10", "simulation-distance": "8", "spawn-protection": "0",
    "level-name": "world", "level-seed": "", "online-mode": "false", "enforce-secure-profile": "false",
    "pvp": "true", "hardcore": "false", "allow-flight": "true", "white-list": "false",
    "enable-command-block": "false",
}


# ---------- server.properties ----------

def read_properties(path: Path) -> dict:
    props = {}
    if path.exists():
        for line in path.read_text("utf-8", errors="replace").splitlines():
            if line.strip() and not line.lstrip().startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                props[key.strip()] = value.replace("\\:", ":").replace("\\=", "=").replace("\\\\", "\\")
    return props


def write_properties(path: Path, changes: dict) -> None:
    """Меняет только переданные ключи, остальные строки файла (и порядок) оставляет как были."""
    lines = path.read_text("utf-8", errors="replace").splitlines() if path.exists() else ["#Minecraft server properties"]
    left = dict(changes)
    for i, line in enumerate(lines):
        if "=" in line and not line.lstrip().startswith("#"):
            key = line.split("=", 1)[0].strip()
            if key in left:
                lines[i] = f"{key}={escape(left.pop(key))}"
    lines += [f"{k}={escape(v)}" for k, v in left.items()]
    # Без BOM: сервер читает файл как есть
    path.write_text("\n".join(lines) + "\n", "utf-8")


def escape(value) -> str:
    return str(value).replace("\\", "\\\\").replace(":", "\\:").replace("=", "\\=")


# ---------- Java ----------

def required_java(mc: str) -> tuple:
    """(версия Java, компонент рантайма Mojang) для этой версии игры."""
    entry = next((v for v in get_json(MOJANG_MANIFEST)["versions"] if v["id"] == mc), None)
    java = (get_json(entry["url"]) if entry else {}).get("javaVersion") or {}
    return java.get("majorVersion", 8), java.get("component", "jre-legacy")


def download_runtime(component: str, dest: Path, status) -> Path:
    """Официальная Java от Mojang (та же, что ставит лаунчер игры) в папку сервера."""
    runtimes = get_json(JAVA_RUNTIMES)["windows-x64"].get(component) or []
    if not runtimes:
        raise RuntimeError(f"Не нашёл Java «{component}» у Mojang.")
    files = get_json(runtimes[0]["manifest"]["url"])["files"]
    items = [(p, f) for p, f in files.items() if f["type"] == "file"]
    for n, (path, f) in enumerate(items, 1):
        if n % 25 == 1:
            status(f"Скачиваю Java для сервера ({n}/{len(items)})…")
        target = dest / path
        target.parent.mkdir(parents=True, exist_ok=True)
        raw = f["downloads"]["raw"]
        data = web.get(raw["url"], timeout=60).content
        if hashlib.sha1(data).hexdigest() != raw["sha1"]:
            raise RuntimeError(f"Java: файл {path} скачался с ошибкой.")
        target.write_bytes(data)
    return dest / "bin" / "java.exe"


# ---------- Моды ----------

def client_only_by_modrinth(jars: list) -> set:
    """Моды, которые Modrinth помечает как ненужные на сервере (server_side: unsupported)."""
    hashes = {}
    for jar in jars:
        hashes[hashlib.sha1(jar.read_bytes()).hexdigest()] = jar
    try:
        r = web.post(f"{MODRINTH}/version_files", json={"hashes": list(hashes), "algorithm": "sha1"}, timeout=60)
        r.raise_for_status()
        versions = r.json()
        projects = {v["project_id"] for v in versions.values()}
        info = {}
        ids = list(projects)
        for i in range(0, len(ids), 100):
            chunk = ids[i:i + 100]
            for p in get_json(f"{MODRINTH}/projects", params={"ids": json.dumps(chunk)}):
                info[p["id"]] = p
    except (requests.RequestException, ValueError, KeyError):
        return set()  # нет сети — переносим всё, клиентские Fabric-моды сервер сам пропустит
    return {hashes[h] for h, v in versions.items()
            if info.get(v["project_id"], {}).get("server_side") == "unsupported"}


def client_only_by_meta(jar: Path) -> bool:
    try:
        with zipfile.ZipFile(jar) as z:
            names = set(z.namelist())
            if "fabric.mod.json" in names:
                meta = json.loads(z.read("fabric.mod.json").decode("utf-8-sig"), strict=False)
                return meta.get("environment") == "client"
            if "META-INF/neoforge.mods.toml" in names or "META-INF/mods.toml" in names:
                text = z.read("META-INF/neoforge.mods.toml" if "META-INF/neoforge.mods.toml" in names else "META-INF/mods.toml")
                return bool(re.search(rb"clientSideOnly\s*=\s*true", text))
    except (zipfile.BadZipFile, ValueError, OSError, KeyError):
        pass
    return False


def keep_dependencies(jars: list, skip: set) -> set:
    """Возвращает на сервер «клиентские» моды, без которых не запустятся серверные."""
    metas = {j: custom_packs.mod_meta(j.read_bytes()) for j in jars}
    skip = set(skip)
    while True:
        provided = set(custom_packs.BUILTIN)
        for j in jars:
            if j not in skip:
                provided |= metas[j]["ids"]
        needed = set()
        for j in jars:
            if j not in skip:
                needed |= metas[j]["requires"] - provided
        back = {j for j in skip if metas[j]["ids"] & needed}
        if not back:
            return skip
        skip -= back


# ---------- Серверный загрузчик ----------

def install_loader(loader: str, mc: str, version: str, server: Path, java: Path, status) -> list:
    """Ставит серверную часть загрузчика; возвращает аргументы запуска после java и памяти."""
    if loader == "vanilla":
        status("Скачиваю сервер Minecraft…")
        entry = next(v for v in get_json(MOJANG_MANIFEST)["versions"] if v["id"] == mc)
        url = get_json(entry["url"])["downloads"]["server"]["url"]
        (server / "server.jar").write_bytes(web.get(url, timeout=120).content)
        return ["-jar", "server.jar"]
    if loader == "fabric":
        status(f"Скачиваю сервер Fabric {version}…")
        installers = get_json(f"{FABRIC_META.rsplit('/', 1)[0]}/installer")
        installer = next((i["version"] for i in installers if i.get("stable")), installers[0]["version"])
        r = web.get(f"{FABRIC_META}/{mc}/{version}/{installer}/server/jar", timeout=120)
        r.raise_for_status()
        (server / "fabric-server-launch.jar").write_bytes(r.content)
        return ["-jar", "fabric-server-launch.jar"]

    # Quilt, Forge и NeoForge ставятся своими официальными установщиками
    if loader == "quilt":
        latest = re.findall(r"<version>([^<]+)</version>", web.get(f"{QUILT_INSTALLER}/maven-metadata.xml", timeout=30).text)[-1]
        url = f"{QUILT_INSTALLER}/{latest}/quilt-installer-{latest}.jar"
        commands = [["install", "server", mc, version, "--download-server", f"--install-dir={server}"]]
    elif loader == "forge":
        url = f"{FORGE_MAVEN}/{mc}-{version}/forge-{mc}-{version}-installer.jar"
        commands = [["--installServer", str(server)], ["--install-server", str(server)]]
    else:
        url = f"{NEOFORGE_MAVEN}/{version}/neoforge-{version}-installer.jar"
        commands = [["--installServer", str(server)], ["--install-server", str(server)]]
    status(f"Ставлю сервер {LOADER_NAMES[loader]} {version} (может занять пару минут)…")
    with tempfile.TemporaryDirectory() as tmp:
        installer = Path(tmp) / "installer.jar"
        r = web.get(url, timeout=120)
        r.raise_for_status()
        installer.write_bytes(r.content)
        log = ""
        for args in commands:
            p = subprocess.run([str(java), "-jar", str(installer), *args], cwd=str(server), capture_output=True,
                               text=True, errors="replace", creationflags=0x08000000 if os.name == "nt" else 0)
            log = p.stdout + p.stderr
            if p.returncode == 0:
                break
            if "not a recognized option" not in log and "UnrecognizedOption" not in log:
                tail = "\n".join(log.strip().splitlines()[-8:])
                raise RuntimeError(f"Установщик {LOADER_NAMES[loader]} завершился с ошибкой:\n\n{tail}")
    for leftover in server.glob("*installer.jar.log"):
        leftover.unlink(missing_ok=True)

    if loader == "quilt":
        return ["-jar", "quilt-server-launch.jar"]
    # Forge 1.17+ и NeoForge запускаются через файл аргументов, старый Forge — через jar
    args_file = next(server.glob("libraries/net/*/*/*/win_args.txt"), None)
    if args_file:
        return [f"@{args_file.relative_to(server).as_posix()}"]
    jar = next((j for j in server.glob("forge-*.jar") if "installer" not in j.name), None)
    if jar:
        return ["-jar", jar.name]
    raise RuntimeError("Сервер Forge установился, но не нашёл, чем его запускать.")


# ---------- Сборка сервера ----------

def radmin_ip():
    # Адрес для друзей: Radmin VPN раздаёт адреса 26.x.x.x
    try:
        ips = socket.gethostbyname_ex(socket.gethostname())[2]
    except OSError:
        return None
    return next((ip for ip in ips if ip.startswith("26.")), None)


def free_port(taken: set) -> int:
    port = 25565
    while port in taken:
        port += 1
    return port


def build(profile: dict, folder: Path, mc_dir: Path, taken_ports: set, status, progress) -> dict:
    """Собирает сервер из установленной сборки в новой подпапке folder. Возвращает запись сервера."""
    game_dir = Path(profile["game_dir"])
    mc, loader, version = profile["minecraft"], profile["loader"], profile.get("loader_version") or ""
    name = profile["name"]
    server = folder / f"{custom_packs.slug(name)}-server"
    n = 2
    while server.exists() and any(server.iterdir()):
        server = folder / f"{custom_packs.slug(name)}-server-{n}"
        n += 1
    server.mkdir(parents=True, exist_ok=True)

    # Java нужной версии: найденная на ПК или официальная от Mojang
    status("Подбираю Java…")
    need, component = required_java(mc)
    java = custom_packs.find_java(mc_dir, need)
    if not java:
        java = download_runtime(component, server / "runtime", status)
    progress(0.1)

    # Файлы сборки без клиентских
    files = [p for d in SERVER_DIRS if (game_dir / d).is_dir() for p in (game_dir / d).rglob("*") if p.is_file()]
    jars = [p for p in files if p.parent == game_dir / "mods" and p.suffix == ".jar"]
    status("Проверяю, какие моды нужны только игроку…")
    skip = {j for j in jars if client_only_by_meta(j)} | client_only_by_modrinth(jars)
    skip = keep_dependencies(jars, skip)
    for i, path in enumerate(files, 1):
        if path in skip:
            continue
        dest = server / path.relative_to(game_dir)
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(path, dest)
        if i % 100 == 0:
            status(f"Копирую файлы сборки ({i}/{len(files)})…")
            progress(0.1 + 0.5 * i / len(files))
    progress(0.6)

    launch = install_loader(loader, mc, version, server, java, status)
    progress(0.95)

    port = free_port(taken_ports)
    props = {**DEFAULTS, "motd": name, "server-port": str(port), "query.port": str(port)}
    packs = initial_packs(server, loader)
    if packs:
        props["initial-enabled-packs"] = ",".join(packs)
    write_properties(server / "server.properties", props)
    record = {"name": name, "path": str(server), "pack": profile["version_id"], "minecraft": mc,
              "loader": loader, "loader_version": version, "java": str(java), "launch": launch,
              "ram": 6 if len(jars) > 150 else 4, "skipped": sorted(p.name for p in skip)}
    write_start(record)
    progress(1)
    return record


def initial_packs(server: Path, loader: str) -> list:
    """Датапаки, которые сервер сам не включит в новом мире: из OpenLoader и пак CristelLib.
    Без них в мире не появятся структуры и правки сборки."""
    extra = []
    for folder in (server / "config" / "openloader" / "data", server / "openloader" / "data"):
        if folder.is_dir():
            extra += [f"data/{p.name}" for p in sorted(folder.iterdir())]
    if any(server.glob("mods/cristellib*.jar")):
        extra.append("Runtime Pack")
    if not extra:
        return []
    base = ["vanilla"] + ({"fabric": ["fabric"], "quilt": ["fabric"], "forge": ["mod_resources"],
                           "neoforge": ["mod_resources"]}.get(loader, []))
    return base + extra


def accept_eula(server: Path) -> None:
    (server / "eula.txt").write_text(
        f"# EULA Minecraft ({EULA_URL}) принята хостом в MineForFriends by TEJKA\neula=true\n", "utf-8")


def write_start(record: dict) -> None:
    server = Path(record["path"])
    ram = int(record.get("ram") or 4)
    xms = max(1, ram // 2)
    args = " ".join(f'"{a}"' if " " in a else a for a in record["launch"])
    # Кодовая страница UTF-8, чтобы русские пути и названия не ломались в консоли
    text = (f"@echo off\r\nchcp 65001 >nul\r\ntitle Сервер {record['name']}\r\ncd /d \"%~dp0\"\r\n"
            f"\"{record['java']}\" -Xms{xms}G -Xmx{ram}G {args} nogui\r\npause\r\n")
    (server / "start.bat").write_bytes(text.encode("utf-8"))


def start(record: dict) -> None:
    """Запускает сервер в отдельном окне консоли."""
    server = Path(record["path"])
    if not (server / "eula.txt").exists():
        raise RuntimeError("Сначала прими EULA Minecraft.")
    subprocess.Popen(["cmd", "/c", "start", "", str(server / "start.bat")], cwd=str(server))
