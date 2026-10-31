"""Раздаёт папку launcher-cloud (сборки, моды, ресурспаки, скины) для лаунчера друзей.

Запускается из start.bat любого из серверов (MineSerc, Homestead). Порт 8518;
если облако уже запущено, второй экземпляр сразу закрывается.

Скины (для мода CustomSkinLoader, тип CustomSkinAPI):
    GET  /skins/<ник>.json        профиль {"username", "skins": {"default"|"slim": <хэш>}}
    GET  /skins/textures/<хэш>    PNG скина
    POST /skins/upload            тело — PNG; заголовки X-Nick, X-Token, X-Model (default|slim)
Первый, кто загрузил скин на ник, задаёт его токен; дальше менять скин этого ника можно
только с тем же токеном. Сбросить ник: удалить его строку из skin_tokens.json.
"""
import functools
import hashlib
import json
import re
import socket
import sys
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PORT = 8518
CLOUD = Path(__file__).resolve().parent.parent / "launcher-cloud"
SKINS = CLOUD / "skins"
TEXTURES = SKINS / "textures"
# Лежит вне раздаваемой папки, чтобы его нельзя было скачать
TOKENS = Path(__file__).resolve().parent / "skin_tokens.json"

NICK_RE = re.compile(r"^[A-Za-z0-9_]{3,16}$")
MAX_SKIN = 1 << 20
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
skins_lock = threading.Lock()


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

    def do_POST(self):
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
        sys.stdout.write(f"{self.address_string()} {fmt % args}\n")
        sys.stdout.flush()


class CloudServer(ThreadingHTTPServer):
    # На Windows SO_REUSEADDR позволяет второму облаку занять тот же порт;
    # с эксклюзивным портом второй экземпляр просто не запустится
    allow_reuse_address = False

    def server_bind(self):
        if hasattr(socket, "SO_EXCLUSIVEADDRUSE"):
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


def main() -> None:
    handler = functools.partial(Handler, directory=str(CLOUD))
    try:
        httpd = CloudServer(("0.0.0.0", PORT), handler)
    except OSError:
        print(f"Порт {PORT} уже занят — облако уже запущено, второе не нужно.")
        return
    with httpd:
        print(f"Облако модов: {CLOUD} на порту {PORT}")
        httpd.serve_forever()


if __name__ == "__main__":
    main()
