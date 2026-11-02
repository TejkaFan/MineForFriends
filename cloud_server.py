"""Облако MineForFriends by TEJKA: раздаёт папку launcher-cloud (сборки, моды, ресурспаки, скины) для лаунчера друзей.

Работает из трея (cloud_tray.py, сам запускается при входе в Windows) или вручную:
    python cloud_server.py
Порт 8518; если облако уже запущено, второй экземпляр сразу закрывается.

Скины (для мода CustomSkinLoader, тип CustomSkinAPI):
    GET  /skins/<ник>.json        профиль {"username", "skins": {"default"|"slim": <хэш>}}
    GET  /skins/textures/<хэш>    PNG скина
    POST /skins/upload            тело — PNG; заголовки X-Nick, X-Token, X-Model (default|slim)

Пересылка в Worker (для друзей, у которых провайдер обрывает соединение с Cloudflare):
    POST /relay/upload | /relay/cape | /relay/report | /relay/recipe, GET /relay/recipes
И в сайты, откуда лаунчер качает загрузчики, моды и обновления (только из FETCH_HOSTS):
    GET|POST /relay-url?url=<адрес>   переадресации не выполняются, а возвращаются лаунчеру
Только из сети Radmin (26.x.x.x) и с этого компьютера.
Первый, кто загрузил скин на ник, задаёт его токен; дальше менять скин этого ника можно
только с тем же токеном. Сбросить ник: удалить его строку из skin_tokens.json.
"""
import functools
import hashlib
import json
import re
import shutil
import socket
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PORT = 8518
# Папка MineForFriends: рядом лежат launcher (код) и launcher-cloud (то, что раздаём)
ROOT = Path(__file__).resolve().parent.parent
CLOUD = ROOT / "launcher-cloud"
SKINS = CLOUD / "skins"
TEXTURES = SKINS / "textures"
# Лежит вне раздаваемой папки, чтобы его нельзя было скачать
TOKENS = ROOT / "skin_tokens.json"
# Пауза из трея: облако держит порт, но на все запросы отвечает «на паузе»
state = {"paused": False}

NICK_RE = re.compile(r"^[A-Za-z0-9_]{3,16}$")
MAX_SKIN = 1 << 20
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
skins_lock = threading.Lock()

# Пересылка в Worker: только эти маршруты и заголовки
WORKER = "https://mineforfriends-skins.inkiandmelon.workers.dev"
RELAY_ROUTES = {("POST", "/upload"), ("POST", "/cape"), ("POST", "/report"), ("POST", "/recipe"), ("GET", "/recipes")}
RELAY_HEADERS = ("X-Nick", "X-Token", "X-Model", "Content-Type")
RELAY_MAX = 8 << 20
RELAY_REPLY_HEADERS = ("Content-Type", "Location", "Content-Disposition", "ETag", "Last-Modified")
# Сайты загрузчиков, модов и обновлений лаунчера; переадресации CurseForge ведут на forgecdn
FETCH_HOSTS = {
    "meta.fabricmc.net", "maven.fabricmc.net", "meta.quiltmc.org", "maven.quiltmc.org",
    "files.minecraftforge.net", "maven.minecraftforge.net", "maven.neoforged.net",
    "api.modrinth.com", "cdn.modrinth.com", "www.curseforge.com", "edge.forgecdn.net", "mediafilez.forgecdn.net",
    "piston-meta.mojang.com", "piston-data.mojang.com", "launchermeta.mojang.com", "libraries.minecraft.net",
    "api.github.com", "github.com", "raw.githubusercontent.com", "objects.githubusercontent.com",
    "release-assets.githubusercontent.com",
}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    # Переадресацию выполняет лаунчер: так он снова проверит, разрешён ли новый сайт
    def redirect_request(self, *args, **kwargs):
        return None


relay_opener = urllib.request.build_opener(NoRedirect)


def png_size(data: bytes):
    # Размер из заголовка IHDR, без сторонних библиотек
    if not data.startswith(PNG_SIGNATURE) or data[12:16] != b"IHDR":
        return None
    return int.from_bytes(data[16:20], "big"), int.from_bytes(data[20:24], "big")


def valid_skin_size(w: int, h: int) -> bool:
    # 64x64, старый 64x32 и HD-скины кратно 64 (до 1024)
    return w % 64 == 0 and 64 <= w <= 1024 and h in (w, w // 2)


def save_skin(nick: str, token: str, model: str, data: bytes) -> None:
    key = nick.lower()
    with skins_lock:
        tokens = json.loads(TOKENS.read_text("utf-8")) if TOKENS.exists() else {}
        token_hash = hashlib.sha256(token.encode()).hexdigest()
        if tokens.get(key, token_hash) != token_hash:
            raise PermissionError("Этот ник уже занят другим игроком. Если это твой ник — попроси хоста сбросить его.")

        TEXTURES.mkdir(parents=True, exist_ok=True)
        texture = hashlib.sha256(data).hexdigest()
        (TEXTURES / texture).write_bytes(data)

        profile_path = SKINS / f"{key}.json"
        old = json.loads(profile_path.read_text("utf-8")) if profile_path.exists() else {}
        profile = {"username": nick, "skins": {model: texture}}
        profile_path.write_text(json.dumps(profile), "utf-8")

        # Старую текстуру удаляем, если на неё больше никто не ссылается
        for old_texture in set(old.get("skins", {}).values()) - {texture}:
            used = any(old_texture in p.read_text("utf-8") for p in SKINS.glob("*.json"))
            if not used:
                (TEXTURES / old_texture).unlink(missing_ok=True)

        tokens[key] = token_hash
        TOKENS.write_text(json.dumps(tokens, indent=2), "utf-8")


class Handler(SimpleHTTPRequestHandler):
    # Без листинга папок: отдаём только файлы
    def list_directory(self, path):
        self.send_error(404)
        return None

    def translate_path(self, path):
        # Профили скинов хранятся в нижнем регистре, а игра спрашивает ник как есть
        if path.startswith("/skins/") and path.endswith(".json"):
            path = path.lower()
        return super().translate_path(path)

    def end_headers(self):
        # Скины меняются, пусть игра не держит старые профили
        if self.path.startswith("/skins/"):
            self.send_header("Cache-Control", "no-cache")
        super().end_headers()

    # ---- Пересылка в Worker для друзей, у которых провайдер обрывает соединение с Cloudflare

    def relay(self, method: str) -> bool:
        route, _, query = self.path.partition("?")
        if route == "/relay-url":
            url = urllib.parse.parse_qs(query).get("url", [""])[0]
            parts = urllib.parse.urlsplit(url)
            if parts.scheme != "https" or parts.hostname not in FETCH_HOSTS:
                self.reply(404, "Этот сайт облако не пересылает")
                return True
            headers = {k: self.headers[k] for k in ("Accept", "Content-Type") if self.headers.get(k)}
            headers["User-Agent"] = self.headers.get("User-Agent") or "MineForFriends-cloud-relay"
            return self.forward(method, url, headers, parts.hostname)
        if not route.startswith("/relay/"):
            return False
        target = route[len("/relay"):]
        if (method, target) not in RELAY_ROUTES:
            self.reply(404, "Нет такого адреса")
            return True
        headers = {k: self.headers[k] for k in RELAY_HEADERS if self.headers.get(k)}
        headers["User-Agent"] = "MineForFriends-cloud-relay"
        return self.forward(method, WORKER + target, headers, "сервера скинов")

    def forward(self, method: str, url: str, headers: dict, site: str) -> bool:
        # Только своим: сеть Radmin и этот компьютер. Иначе облако стало бы открытым прокси
        if not (self.client_address[0].startswith("26.") or self.client_address[0] == "127.0.0.1"):
            self.reply(403, "Пересылка только для друзей по Radmin")
            return True
        length = int(self.headers.get("Content-Length") or 0)
        if length > RELAY_MAX:
            self.reply(413, "Слишком большой запрос")
            return True
        body = self.rfile.read(length) if length else None
        request = urllib.request.Request(url, data=body, headers=headers, method=method)
        try:
            r = relay_opener.open(request, timeout=120)
        except urllib.error.HTTPError as e:
            r = e  # 3xx (переадресацию отдаём лаунчеру), 4xx, 5xx — ответ сайта как есть
        except OSError as e:
            self.reply(502, f"Облако хоста не достучалось до {site}: {e}")
            return True
        with r:
            size = r.headers.get("Content-Length")
            data = None if size else r.read()
            self.send_response(r.status)
            for k in RELAY_REPLY_HEADERS:
                if r.headers.get(k):
                    self.send_header(k, r.headers[k])
            self.send_header("Content-Length", size or str(len(data)))
            self.send_header("X-MFF-Relay", "1")
            self.end_headers()
            if data is None:
                # Большие файлы (моды, загрузчики) передаём кусками, не держа целиком в памяти
                shutil.copyfileobj(r, self.wfile, 1 << 16)
            else:
                self.wfile.write(data)
        self.log_message("пересылка %s %s -> %s", method, url.split("?")[0], r.status)
        return True

    def paused(self) -> bool:
        if state["paused"]:
            self.reply(503, "Облако хоста на паузе. Попроси хоста включить его.")
        return state["paused"]

    def do_HEAD(self):
        if not self.paused():
            super().do_HEAD()

    def do_GET(self):
        if not self.paused() and not self.relay("GET"):
            super().do_GET()

    def do_POST(self):
        if self.paused() or self.relay("POST"):
            return
        if self.path != "/skins/upload":
            self.send_error(404)
            return
        nick = self.headers.get("X-Nick", "")
        token = self.headers.get("X-Token", "")
        model = self.headers.get("X-Model", "default")
        length = int(self.headers.get("Content-Length") or 0)

        if not NICK_RE.match(nick):
            return self.reply(400, "Ник должен быть из 3–16 латинских букв, цифр или _")
        if len(token) < 16 or model not in ("default", "slim"):
            return self.reply(400, "Неверный запрос")
        if not 0 < length <= MAX_SKIN:
            return self.reply(413, "Файл скина слишком большой (максимум 1 МБ)")
        data = self.rfile.read(length)
        size = png_size(data)
        if not size or not valid_skin_size(*size):
            return self.reply(400, "Это не похоже на скин: нужен PNG 64×64 (или 64×32, или HD кратно 64)")
        try:
            save_skin(nick, token, model, data)
        except PermissionError as e:
            return self.reply(403, str(e))
        self.log_message("скин обновлён: %s (%s)", nick, model)
        self.reply(200, "Скин загружен")

    def reply(self, code: int, message: str) -> None:
        body = json.dumps({"ok": code == 200, "message": message}, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *args):
        sys.stdout.write(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {self.address_string()} {fmt % args}\n")
        sys.stdout.flush()


class CloudServer(ThreadingHTTPServer):
    # На Windows SO_REUSEADDR позволяет второму облаку занять тот же порт;
    # с эксклюзивным портом второй экземпляр просто не запустится
    allow_reuse_address = False

    def server_bind(self):
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def serve() -> CloudServer:
    """Занимает порт и возвращает сервер; OSError — порт занят (облако уже запущено)."""
    return CloudServer(("0.0.0.0", PORT), functools.partial(Handler, directory=str(CLOUD)))


def main() -> None:
    try:
        httpd = serve()
    except OSError:
        print(f"Порт {PORT} уже занят — облако уже запущено, второе не нужно.")
        return
    with httpd:
        print(f"Облако модов: {CLOUD} на порту {PORT}")
        httpd.serve_forever()


if __name__ == "__main__":
    main()
