"""Управление запущенным сервером из лаунчера: команды по RCON, игроки, бэкапы мира.

Сервер работает в своём окне консоли, поэтому команды (stop, list, whitelist, op) лаунчер
шлёт по RCON — протоколу удалённой консоли Minecraft. RCON включается в server.properties
со случайным паролем перед каждым запуском из лаунчера (если ещё не включён).
Когда сервер выключен, белый список, операторы и баны правятся прямо в whitelist.json, ops.json
и banned-players.json.
"""
import ctypes
import datetime
import gzip
import hashlib
import json
import os
import re
import secrets
import shutil
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
            "ops": sorted(p["name"] for p in read_list(server, "ops.json") if p.get("name")),
            "banned": sorted(p["name"] for p in read_list(server, "banned-players.json") if p.get("name"))}


def player_action(server: Path, online: bool, action: str, name: str = "") -> None:
    if action not in ("whitelist_on", "whitelist_off") and not NICK_RE.match(name):
        raise RuntimeError("Ник — 3–16 латинских букв, цифр или _.")
    if online:
        command = {"whitelist_add": f"whitelist add {name}", "whitelist_remove": f"whitelist remove {name}",
                   "op": f"op {name}", "deop": f"deop {name}", "kick": f"kick {name}",
                   "ban": f"ban {name}", "pardon": f"pardon {name}",
                   "whitelist_on": "whitelist on", "whitelist_off": "whitelist off"}[action]
        rcon(server, command)
        return
    if action == "kick":
        raise RuntimeError("Сервер выключен.")
    if action in ("whitelist_on", "whitelist_off"):
        on = "true" if action == "whitelist_on" else "false"
        write_properties(server / "server.properties", {"white-list": on, "enforce-whitelist": on})
        return
    if action.startswith("whitelist"):
        file, entry = "whitelist.json", {"uuid": offline_uuid(name), "name": name}
    elif action in ("ban", "pardon"):
        created = datetime.datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %z")
        file, entry = "banned-players.json", {"uuid": offline_uuid(name), "name": name, "created": created,
                                              "source": "Server", "expires": "forever",
                                              "reason": "Banned by an operator."}
    else:
        file, entry = "ops.json", {"uuid": offline_uuid(name), "name": name, "level": 4, "bypassesPlayerLimit": False}
    items = [p for p in read_list(server, file) if p.get("name", "").lower() != name.lower()]
    if action in ("whitelist_add", "op", "ban"):
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


def backups(server: Path) -> list:
    folder = server / "backups"
    items = sorted(folder.glob("*.zip"), key=lambda p: p.stat().st_mtime, reverse=True) if folder.is_dir() else []
    return [{"name": p.name, "size": p.stat().st_size,
             "time": time.strftime("%d.%m.%Y %H:%M", time.localtime(p.stat().st_mtime))} for p in items]


def restore(server: Path, name: str, status) -> Path:
    """Возвращает мир из бэкапа. Сервер должен быть выключен.

    Сначала распаковка во временную папку, потом бэкап текущего мира (он может удалить
    самый старый архив — тот, из которого восстанавливаем, уже распакован), потом подмена.
    Возвращает архив с миром, каким он был до восстановления.
    """
    zip_path = server / "backups" / name
    if "/" in name or "\\" in name or not zip_path.is_file():
        raise RuntimeError("Бэкап не найден.")
    world_name = read_properties(server / "server.properties").get("level-name") or "world"
    world = server / world_name
    tmp = server / f"{world_name}.restoring"
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir()
    status("Распаковываю бэкап…")
    try:
        with zipfile.ZipFile(zip_path) as z:
            for item in z.infolist():
                # В архиве пути вида world/region/r.0.0.mca; чужое и выходы за папку пропускаем
                parts = Path(item.filename).parts
                if len(parts) < 2 or parts[0] != world_name or ".." in parts or item.is_dir():
                    continue
                dest = tmp.joinpath(*parts[1:])
                dest.parent.mkdir(parents=True, exist_ok=True)
                with z.open(item) as src, dest.open("wb") as out:
                    shutil.copyfileobj(src, out)
    except (zipfile.BadZipFile, OSError) as e:
        shutil.rmtree(tmp, ignore_errors=True)
        raise RuntimeError(f"Архив бэкапа повреждён: {e}") from None
    if not any(tmp.iterdir()):
        shutil.rmtree(tmp, ignore_errors=True)
        raise RuntimeError(f"В архиве нет мира «{world_name}».")
    before = backup(server, False, status)
    if world.exists():
        shutil.rmtree(world)
    tmp.rename(world)
    return before


# ---------- Консоль ----------

def log_tail(server: Path, pos: int = -1) -> dict:
    """Новые строки logs/latest.log начиная с pos (-1 — последние ~64 КБ). Сервер начал новый лог — читаем сначала."""
    path = server / "logs" / "latest.log"
    try:
        size = path.stat().st_size
        with path.open("rb") as f:
            if pos < 0 or pos > size:
                pos = max(0, size - 65536) if pos < 0 else 0
            f.seek(pos)
            data = f.read(1 << 20)
    except OSError:
        return {"text": "", "pos": 0}
    if pos and data and b"\n" not in data:
        return {"text": "", "pos": pos}  # строка ещё дописывается
    end = data.rfind(b"\n") + 1 if data else 0
    return {"text": data[:end].decode("utf-8", errors="replace"), "pos": pos + end}


# ---------- Статистика игроков ----------

LOG_LINE = re.compile(r"^\[(?:(\d{1,2})([A-Za-z]{3})(\d{4}) )?(\d\d):(\d\d):(\d\d)")
JOIN_LEAVE = re.compile(r": (\w{3,16}) (joined|left) the game\s*$")
MONTHS = {m: i for i, m in enumerate(("Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug", "Sep", "Oct", "Nov", "Dec"), 1)}


def _log_files(server: Path) -> list:
    """(путь, дата начала) по порядку. Архивы называются по дате (2026-10-31-1.log.gz)."""
    folder = server / "logs"
    files = []
    for p in folder.glob("*.log.gz") if folder.is_dir() else []:
        m = re.match(r"(\d{4})-(\d\d)-(\d\d)-(\d+)\.log\.gz$", p.name)
        if m:
            files.append(((int(m[1]), int(m[2]), int(m[3]), int(m[4])), p))
    files = [(p, datetime.date(*key[:3])) for key, p in sorted(files)]
    latest = folder / "latest.log"
    if latest.exists():
        files.append((latest, None))
    return files


def player_stats(server: Path) -> list:
    """Сколько каждый наиграл на сервере — по логам входов и выходов. Сортировка по времени игры."""
    stats = {}

    def close(name, start, end):
        s = stats.setdefault(name.lower(), {"name": name, "seconds": 0, "sessions": 0, "last": None})
        s["name"] = name
        s["seconds"] += max(0, (end - start).total_seconds())
        s["sessions"] += 1
        if not s["last"] or end > s["last"]:
            s["last"] = end

    for path, day in _log_files(server):
        try:
            raw = gzip.open(path).read() if path.suffix == ".gz" else path.read_bytes()
        except OSError:
            continue
        lines = raw.decode("utf-8", errors="replace").splitlines()
        if day is None:
            # latest.log: последняя строка записана в момент изменения файла
            day = datetime.date.fromtimestamp(path.stat().st_mtime)
            last = next((LOG_LINE.match(line) for line in reversed(lines) if LOG_LINE.match(line)), None)
            if last and not last[1]:
                end_time = datetime.time(int(last[4]), int(last[5]), int(last[6]))
                if end_time > datetime.datetime.fromtimestamp(path.stat().st_mtime).time():
                    day -= datetime.timedelta(days=1)
                # Начало файла могло быть раньше полуночи — отмотаем дни по переходам через 00:00
                prev, wraps = None, 0
                for line in lines:
                    m = LOG_LINE.match(line)
                    if m and not m[1]:
                        t = (int(m[4]), int(m[5]), int(m[6]))
                        if prev and t < prev:
                            wraps += 1
                        prev = t
                day -= datetime.timedelta(days=wraps)
        online, prev, now = {}, None, None
        for line in lines:
            m = LOG_LINE.match(line)
            if not m:
                continue
            if m[1]:  # формат Forge: [31Oct2026 12:34:56.789]
                now = datetime.datetime(int(m[3]), MONTHS.get(m[2], 1), int(m[1]), int(m[4]), int(m[5]), int(m[6]))
            else:
                t = (int(m[4]), int(m[5]), int(m[6]))
                if prev and t < prev:
                    day += datetime.timedelta(days=1)
                prev = t
                now = datetime.datetime.combine(day, datetime.time(*t))
            event = JOIN_LEAVE.search(line)
            if event and event[2] == "joined":
                online[event[1]] = now
            elif event and event[1] in online:
                close(event[1], online.pop(event[1]), now)
            elif "Stopping server" in line:
                for name, start in online.items():
                    close(name, start, now)
                online.clear()
        # Лог оборвался (вылет или сервер ещё работает) — считаем до последней строки
        for name, start in online.items():
            if now:
                close(name, start, now)
    result = sorted(stats.values(), key=lambda s: -s["seconds"])
    for s in result:
        s["last"] = s["last"].strftime("%d.%m.%Y %H:%M") if s["last"] else ""
        s["seconds"] = int(s["seconds"])
    return result


# ---------- Нагрузка ----------

# Forge: «Mean tick time: 1.2 ms», ванилла: «Average time per tick: 1.2ms», NeoForge: «20.0 TPS (1.2 ms/tick)»
TICK_RE = re.compile(r"(?:Mean tick time|Average time per tick):\s*([\d.,]+)\s*ms|\(([\d.,]+) ms/tick\)", re.I)
LAG_RE = re.compile(r"Can't keep up!.*?Running (\d+)ms or (\d+) ticks behind")


class _MemoryCounters(ctypes.Structure):
    _fields_ = [("cb", ctypes.c_ulong), ("PageFaultCount", ctypes.c_ulong)] + \
               [(n, ctypes.c_size_t) for n in ("PeakWorkingSetSize", "WorkingSetSize", "QuotaPeakPagedPoolUsage",
                                               "QuotaPagedPoolUsage", "QuotaPeakNonPagedPoolUsage",
                                               "QuotaNonPagedPoolUsage", "PagefileUsage", "PeakPagefileUsage")]


def port_pid(port: int):
    """PID процесса, который слушает TCP-порт (у сервера Minecraft это java.exe)."""
    iphlpapi = ctypes.windll.iphlpapi
    size = ctypes.c_ulong(0)
    iphlpapi.GetExtendedTcpTable(None, ctypes.byref(size), False, 2, 3, 0)  # AF_INET, TCP_TABLE_OWNER_PID_LISTENER
    buf = ctypes.create_string_buffer(size.value + 4096)
    size.value = len(buf)
    if iphlpapi.GetExtendedTcpTable(buf, ctypes.byref(size), False, 2, 3, 0) != 0:
        return None
    count = struct.unpack_from("<I", buf, 0)[0]
    for i in range(count):
        # MIB_TCPROW_OWNER_PID: состояние, адрес, порт (в сетевом порядке), удалённые адрес и порт, pid
        _, _, lport, _, _, pid = struct.unpack_from("<6I", buf, 4 + i * 24)
        if socket.ntohs(lport & 0xFFFF) == port:
            return pid
    return None


def process_usage(pid: int):
    """(байт памяти, секунд процессорного времени) процесса или None."""
    k32, psapi = ctypes.windll.kernel32, ctypes.windll.psapi
    k32.OpenProcess.restype = ctypes.c_void_p
    h = k32.OpenProcess(0x1000, False, pid)  # PROCESS_QUERY_LIMITED_INFORMATION
    if not h:
        return None
    h = ctypes.c_void_p(h)
    try:
        mem = _MemoryCounters()
        mem.cb = ctypes.sizeof(mem)
        times = [ctypes.c_ulonglong() for _ in range(4)]
        if not psapi.GetProcessMemoryInfo(h, ctypes.byref(mem), mem.cb) or \
                not k32.GetProcessTimes(h, *[ctypes.byref(x) for x in times]):
            return None
        return mem.WorkingSetSize, (times[2].value + times[3].value) / 1e7
    finally:
        k32.CloseHandle(h)


def lag_warnings(server: Path, minutes: int = 5) -> int:
    """Сколько раз за последние минуты сервер писал «Can't keep up!» — работает на любой версии."""
    path = server / "logs" / "latest.log"
    try:
        with path.open("rb") as f:
            f.seek(max(0, path.stat().st_size - 256 * 1024))
            lines = f.read().decode("utf-8", errors="replace").splitlines()
    except OSError:
        return 0
    now = datetime.datetime.now()
    count = 0
    for line in lines:
        m = LOG_LINE.match(line)
        if not m or not LAG_RE.search(line):
            continue
        t = now.replace(hour=int(m[4]), minute=int(m[5]), second=int(m[6]), microsecond=0)
        if t > now:
            t -= datetime.timedelta(days=1)
        if (now - t).total_seconds() <= minutes * 60:
            count += 1
    return count


class Monitor:
    """Замеры нагрузки сервера: TPS и время тика (по RCON), память и процессор java.exe."""

    def __init__(self):
        self.cpu = {}       # pid -> (время замера, секунды процессора)
        self.tps_cmd = {}   # путь сервера -> команда, которая отдаёт время тика ("" — такой нет)

    def tick_time(self, server: Path, loader: str):
        """Среднее время тика в мс или None, если сервер его не сообщает."""
        key = str(server)
        if key not in self.tps_cmd:
            # Forge и NeoForge: «forge tps»; ванилла и Fabric с 1.20.3: «tick query»
            candidates = {"forge": ["forge tps"], "neoforge": ["neoforge tps", "forge tps"]}.get(loader, [])
            self.tps_cmd[key] = ""
            for command in candidates + ["tick query"]:
                if TICK_RE.search(rcon(server, command)[0]):
                    self.tps_cmd[key] = command
                    break
        if not self.tps_cmd[key]:
            return None
        # У Forge сначала строки по каждому миру, итог «Overall» — последний
        found = TICK_RE.findall(rcon(server, self.tps_cmd[key])[0])
        return float("".join(found[-1]).replace(",", ".")) if found else None

    def sample(self, server: Path, loader: str, ram_gb: int) -> dict:
        port = int(read_properties(server / "server.properties").get("server-port") or 25565)
        pid = port_pid(port)
        result = {"running": bool(pid), "memory": None, "memory_max": ram_gb * 1024 ** 3, "cpu": None,
                  "mspt": None, "tps": None, "lag": lag_warnings(server)}
        if not pid:
            self.tps_cmd.pop(str(server), None)  # после перезапуска сервер мог смениться
            return result
        usage = process_usage(pid)
        if usage:
            result["memory"] = usage[0]
            now = time.monotonic()
            before = self.cpu.get(pid)
            self.cpu[pid] = (now, usage[1])
            if before and now > before[0]:
                share = (usage[1] - before[1]) / (now - before[0]) / (os.cpu_count() or 1)
                result["cpu"] = round(min(100.0, 100 * share), 1)
        try:
            mspt = self.tick_time(server, loader)
        except RuntimeError:
            mspt = None
        if mspt is not None:
            result["mspt"] = round(mspt, 1)
            result["tps"] = round(min(20.0, 1000 / mspt) if mspt > 0 else 20.0, 1)
        return result


# ---------- Автовыключение ----------

class AutoStop:
    """Выключает сервер, на котором никого нет дольше заданного времени (auto_stop — минуты, 0 — никогда)."""

    def __init__(self):
        self.empty_since = {}

    def tick(self, servers: list, status) -> None:
        # servers: записи из настроек; status(port) — опрос сервера или None, если выключен
        now = time.time()
        for s in servers:
            minutes = int(s.get("auto_stop") or 0)
            path = Path(s["path"])
            port = read_properties(path / "server.properties").get("server-port") or "25565"
            state = status(int(port)) if minutes else None
            if not state or state["online"]:
                self.empty_since.pop(s["path"], None)
                continue
            since = self.empty_since.setdefault(s["path"], now)
            if now - since >= minutes * 60:
                self.empty_since.pop(s["path"], None)
                try:
                    rcon(path, "say Сервер пуст и выключается", "stop")
                    print(f"Автовыключение: {s['name']} (пуст {minutes} мин)")
                except RuntimeError as e:
                    print(f"Автовыключение {s['name']} не удалось: {e}")
