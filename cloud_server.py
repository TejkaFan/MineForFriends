"""Раздаёт папку launcher-cloud (манифест и моды) для лаунчера друзей.

Запускается из start.bat вместе с сервером Minecraft. Порт 8518.
"""
import functools
import sys
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PORT = 8518
CLOUD = Path(__file__).resolve().parent.parent / "launcher-cloud"


class Handler(SimpleHTTPRequestHandler):
    # Без листинга папок: отдаём только файлы
    def list_directory(self, path):
        self.send_error(404)
        return None

    def log_message(self, fmt, *args):
        sys.stdout.write(f"{self.address_string()} {fmt % args}\n")
        sys.stdout.flush()


def main() -> None:
    handler = functools.partial(Handler, directory=str(CLOUD))
    with ThreadingHTTPServer(("0.0.0.0", PORT), handler) as httpd:
        print(f"Облако модов: {CLOUD} на порту {PORT}")
        httpd.serve_forever()


if __name__ == "__main__":
    main()
