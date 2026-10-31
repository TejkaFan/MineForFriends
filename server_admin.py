"""Управление запущенным сервером из лаунчера: команды по RCON, игроки, бэкапы мира.

Сервер работает в своём окне консоли, поэтому команды (stop, list, whitelist, op) лаунчер
шлёт по RCON — протоколу удалённой консоли Minecraft. RCON включается в server.properties
со случайным паролем перед каждым запуском из лаунчера (если ещё не включён).
Когда сервер выключен, белый список и операторы правятся прямо в whitelist.json и ops.json.
"""
import hashlib
import json
import re
import secrets
import socket
import struct
import time
import uuid
import zipfile
from pathlib import Path

from server_host import read_properties, write_properties

BACKUPS_KEEP = 7
NICK_RE = re.compile(r"^[A-Za-z0-9_]{3,16}$")


# ---------- RCON ----------

def ensure_rcon(server: Path) -> None:
    """Включает RCON (только для этого ПК: пароль знает лишь лаунчер). Вызывать, пока сервер выключен."""
    props = read_properties(server / "server.properties")
    if props.get("enable-rcon") == "true" and props.get("rcon.password"):
        return
    port = int(props.get("server-port") or 25565)
    write_properties(server / "server.properties", {
        "enable-rcon": "true", "rcon.port": str(port + 10), "rcon.password": secrets.token_urlsafe(18),
        "broadcast-rcon-to-ops": "false"})


def rcon(server: Path, *commands: str) -> list:
    """Выполняет команды на запущенном сервере, возвращает ответы."""
    props = read_properties(server / "server.properties")
    if props.get("enable-rcon") != "true" or not props.get("rcon.password"):
        raise RuntimeError("Сервер запущен старой версией лаунчера и не принимает команды. "
                           "Останови его в окне консоли командой stop и запусти снова из лаунчера.")
    port = int(props.get("rcon.port") or 25575)

    def send(sock, req_id, kind, body):
        data = struct.pack("<ii", req_id, kind) + body.encode("utf-8") + b"\x00\x00"
        sock.sendall(struct.pack("<i", len(data)) + data)

    def receive(sock):
        def exact(n):
            buf = b""
            while len(buf) < n:
                chunk = sock.recv(n - len(buf))
                if not chunk:
                    raise OSError("сервер закрыл соединение")
                buf += chunk
            return buf
        length = struct.unpack("<i", exact(4))[0]
        data = exact(length)
        req_id, _ = struct.unpack("<ii", data[:8])
        return req_id, data[8:-2].decode("utf-8", errors="replace")

    try:
        with socket.create_connection(("127.0.0.1", port), timeout=10) as sock:
            send(sock, 1, 3, props["rcon.password"])
            if receive(sock)[0] == -1:
                raise RuntimeError("Сервер не принял пароль RCON.")
            answers = []
            for n, command in enumerate(commands, 2):
                send(sock, n, 2, command)
                answers.append(receive(sock)[1])
            return answers
    except OSError as e:
        raise RuntimeError(f"Сервер не отвечает на команды — он запущен? ({e})") from None


# ---------- Игроки ----------

def offline_uuid(name: str) -> str:
    # Так сервер в режиме без лицензии (TLauncher) считает UUID игрока
    digest = bytearray(hashlib.md5(f"OfflinePlayer:{name}".encode()).digest())
    digest[6] = digest[6] & 0x0F | 0x30
    digest[8] = digest[8] & 0x3F | 0x80
    return str(uuid.UUID(bytes=bytes(digest)))


def read_list(server: Path, name: str) -> list:
    try:
        return json.loads((server / name).read_text("utf-8"))
    except (OSError, ValueError):
        return []


def players(server: Path, online: bool) -> dict:
    props = read_properties(server / "server.properties")
    now = []
    if online:
        answer = rcon(server, "list")[0]
        # «There are 2 of a max of 20 players online: Tejka, Friend»
        names = answer.split(":", 1)[1] if ":" in answer else ""
        now = [n.strip() for n in names.split(",") if n.strip()]
    return {"online": now, "whitelist_on": props.get("white-list") == "true",
            "whitelist": sorted(p["name"] for p in read_list(server, "whitelist.json") if p.get("name")),
            "ops": sorted(p["name"] for p in read_list(server, "ops.json") if p.get("name"))}


def player_action(server: Path, online: bool, action: str, name: str = "") -> None:
    if action not in ("whitelist_on", "whitelist_off") and not NICK_RE.match(name):
        raise RuntimeError("Ник — 3–16 латинских букв, цифр или _.")
    if online:
        command = {"whitelist_add": f"whitelist add {name}", "whitelist_remove": f"whitelist remove {name}",
                   "op": f"op {name}", "deop": f"deop {name}", "kick": f"kick {name}",
                   "whitelist_on": "whitelist on", "whitelist_off": "whitelist off"}[action]
        rcon(server, command)
        return
    if action == "kick":
        raise RuntimeError("Сервер выключен.")
    if action in ("whitelist_on", "whitelist_off"):
        on = "true" if action == "whitelist_on" else "false"
        write_properties(server / "server.properties", {"white-list": on, "enforce-whitelist": on})
        return
    file, entry = ("whitelist.json", {"uuid": offline_uuid(name), "name": name}) if action.startswith("whitelist") \
        else ("ops.json", {"uuid": offline_uuid(name), "name": name, "level": 4, "bypassesPlayerLimit": False})
    items = [p for p in read_list(server, file) if p.get("name", "").lower() != name.lower()]
    if action in ("whitelist_add", "op"):
        items.append(entry)
    (server / file).write_text(json.dumps(items, ensure_ascii=False, indent=2), "utf-8")


# ---------- Бэкапы ----------

def backup(server: Path, online: bool, status) -> Path:
    """Архивирует мир в backups/<мир>-<дата>.zip, хранит последние BACKUPS_KEEP. None — мира ещё нет."""
    world_name = read_properties(server / "server.properties").get("level-name") or "world"
    world = server / world_name
    if not world.is_dir():
        return None
    folder = server / "backups"
    folder.mkdir(exist_ok=True)
    dest = folder / f"{world_name}-{time.strftime('%Y-%m-%d_%H-%M-%S')}.zip"
    if online:
        # Сервер не должен писать в мир, пока его копируем
        rcon(server, "save-off", "save-all flush")
    try:
        files = [p for p in world.rglob("*") if p.is_file() and p.name != "session.lock"]
        tmp = dest.with_suffix(".part")
        with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED, compresslevel=1) as z:
            for n, path in enumerate(files, 1):
                if n % 200 == 1:
                    status(f"Бэкап мира: {n}/{len(files)} файлов…")
                try:
                    z.write(path, path.relative_to(server).as_posix())
                except OSError:
                    pass  # файл занят сервером — пропускаем, остальное важнее
        tmp.replace(dest)
    finally:
        if online:
            rcon(server, "save-on")
    for old in sorted(folder.glob(f"{world_name}-*.zip"))[:-BACKUPS_KEEP]:
        old.unlink(missing_ok=True)
    return dest
