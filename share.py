"""Свои серверы друзьям по Radmin: раздача сборки с ПК хоста и поиск таких серверов.

Хост: пока его сервер запущен, фоновый процесс (MineForFriends.exe --share) отдаёт по HTTP
список файлов клиентской сборки и сами файлы, а на UDP отвечает на поиск.
Игрок: лаунчер шлёт широковещательный запрос в сеть Radmin, у ответивших хостов берёт
список серверов и ставит сборку так же, как наши сборки из облака.
"""
import hashlib
import json
import shutil
import socket
import threading
import time
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote, unquote

import requests

import server_host

PORT = 25590
ASK, ANSWER = b"MineForFriends?", b"MineForFriends!"
RADMIN_BROADCAST = "26.255.255.255"
# Папки клиентской сборки, которые получают игроки: всё серверное плюс ресурспаки и шейдеры
CLIENT_DIRS = server_host.SERVER_DIRS + ("resourcepacks", "shaderpacks")
# Раздача ждёт запуска сервера и закрывается, когда он выключен
WAIT_START = 10 * 60
WAIT_STOP = 3 * 60
# Список файлов пересчитывается не чаще раза в минуту
MANIFEST_TTL = 60


def url(ip: str, server_id: str) -> str:
    return f"http://{ip}:{PORT}/{quote(server_id)}"


# ---------- Хост ----------

class Host:
    """Что раздаём: servers() — [{id, name, minecraft, loader, loader_version, port, game_dir}]."""

    def __init__(self, servers, nick: str, cache_file: Path):
        self.servers = servers
        self.nick = nick
        self.cache_file = cache_file
        try:
            self.cache = json.loads(cache_file.read_text("utf-8"))
        except (OSError, ValueError):
            self.cache = {}
        self.manifests = {}
        self.lock = threading.Lock()

    def listing(self) -> dict:
        keys = ("id", "name", "minecraft", "loader", "loader_version", "port")
        return {"host": self.nick, "servers": [{k: s.get(k, "") for k in keys} for s in self.servers()]}

    def file_sha1(self, path: Path) -> str:
        # Хэш пересчитываем, только если файл поменялся: сборка может весить гигабайт
        st = path.stat()
        cached = self.cache.get(str(path))
        if cached and cached[0] == st.st_size and cached[1] == st.st_mtime_ns:
            return cached[2]
        h = hashlib.sha1()
        with path.open("rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        self.cache[str(path)] = [st.st_size, st.st_mtime_ns, h.hexdigest()]
        return h.hexdigest()

    def manifest(self, server_id: str):
        """(список файлов, {путь: файл на диске}) или (None, None), если такого сервера нет."""
        with self.lock:
            cached = self.manifests.get(server_id)
            if cached and time.time() - cached[0] < MANIFEST_TTL:
                return cached[1], cached[2]
            entry = next((s for s in self.servers() if s["id"] == server_id), None)
            if not entry:
                return None, None
            game_dir = Path(entry["game_dir"])
            files, index = [], {}
            for d in CLIENT_DIRS:
                if not (game_dir / d).is_dir():
                    continue
                for path in sorted((game_dir / d).rglob("*")):
                    if path.is_file() and path.suffix != ".part":
                        rel = path.relative_to(game_dir).as_posix()
                        files.append({"path": rel, "size": path.stat().st_size, "sha1": self.file_sha1(path)})
                        index[rel] = path
            try:
                self.cache_file.write_text(json.dumps(self.cache), "utf-8")
            except OSError:
                pass
            self.manifests[server_id] = (time.time(), files, index)
            return files, index


class Handler(BaseHTTPRequestHandler):
    # GET /servers | /<id>/manifest | /<id>/files/<путь>; отдаём только файлы из списка сборки
    def do_GET(self):
        host = self.server.host
        try:
            if self.path == "/servers":
                return self.send_json(host.listing())
            parts = self.path.lstrip("/").split("/", 2)
            if len(parts) >= 2:
                files, index = host.manifest(unquote(parts[0]))
                if files is not None and parts[1] == "manifest":
                    return self.send_json({"files": files})
                if files is not None and parts[1] == "files" and len(parts) == 3:
                    path = index.get(unquote(parts[2]))
                    if path and path.is_file():
                        return self.send_file(path)
            self.send_error(404)
        except (ConnectionError, OSError):
            pass  # игрок оборвал загрузку

    def send_json(self, data) -> None:
        body = json.dumps(data, ensure_ascii=False).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def send_file(self, path: Path) -> None:
        self.send_response(200)
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Length", str(path.stat().st_size))
        self.end_headers()
        with path.open("rb") as f:
            shutil.copyfileobj(f, self.wfile, 1 << 16)

    def log_message(self, fmt, *args):
        pass


class Server(ThreadingHTTPServer):
    # Второй экземпляр раздачи не должен занять тот же порт (на Windows reuse это разрешает)
    allow_reuse_address = False
    daemon_threads = True


def answer(udp: socket.socket) -> None:
    while True:
        try:
            data, addr = udp.recvfrom(64)
            if data == ASK:
                udp.sendto(ANSWER, addr)
        except OSError:
            time.sleep(1)


def serve(host: Host, any_online) -> None:
    """Раздаёт сборки, пока запущен хоть один сервер. Если раздача уже идёт — сразу выходит."""
    try:
        http = Server(("0.0.0.0", PORT), Handler)
    except OSError:
        return
    http.host = host
    try:
        udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        udp.bind(("0.0.0.0", PORT))
        threading.Thread(target=answer, args=(udp,), daemon=True).start()
    except OSError:
        traceback.print_exc()  # без поиска друзья всё равно добавят сервер по адресу
    threading.Thread(target=http.serve_forever, daemon=True).start()
    seen, last = False, time.time()
    while True:
        time.sleep(20)
        if any_online():
            seen, last = True, time.time()
        elif time.time() - last > (WAIT_STOP if seen else WAIT_START):
            break
    http.shutdown()


# ---------- Игрок ----------

def find_hosts(timeout: float = 1.5) -> set:
    """Адреса хостов в сети Radmin, у которых идёт раздача (кроме своего)."""
    ip = server_host.radmin_ip()
    if not ip:
        return set()
    hosts = set()
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            s.bind((ip, 0))
            s.sendto(ASK, (RADMIN_BROADCAST, PORT))
            deadline = time.time() + timeout
            while (left := deadline - time.time()) > 0:
                s.settimeout(left)
                try:
                    data, addr = s.recvfrom(64)
                except socket.timeout:
                    break
                if data == ANSWER:
                    hosts.add(addr[0])
    except OSError:
        traceback.print_exc()
    hosts.discard(ip)
    return hosts


def host_servers(session: requests.Session, ip: str) -> list:
    """Серверы хоста: [{id, name, minecraft, loader, loader_version, port, ip, host}]; [] если не ответил."""
    try:
        r = session.get(f"http://{ip}:{PORT}/servers", timeout=3)
        r.raise_for_status()
        data = r.json()
        return [{**s, "ip": ip, "host": data.get("host") or ip} for s in data["servers"]]
    except (requests.RequestException, ValueError, KeyError, TypeError):
        return []
