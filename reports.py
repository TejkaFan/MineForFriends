"""Лог лаунчера и отчёты об ошибках автору.

Всё, что лаунчер пишет в stdout/stderr, идёт в launcher.log в папке настроек (с временем).
Отчёт — описание ошибки и хвост лога — уходит через Cloudflare Worker (skins-worker/) в закрытый
репозиторий на GitHub задачей (issue). Путь к профилю Windows в отчёте заменяется на ~.
Непредвиденные сбои отправляются сами (одна и та же ошибка — не чаще раза в сутки),
понятные ошибки вроде «нет интернета» — только если игрок нажал «Отправить отчёт».
"""
import hashlib
import json
import os
import platform
import re
import sys
import threading
import time
import traceback
from pathlib import Path

import requests

REPORT_URL = "https://mineforfriends-skins.inkiandmelon.workers.dev/report"
LOG_LIMIT = 512 * 1024
LOG_TAIL = 150
REPEAT_AFTER = 24 * 60 * 60

config = {"dir": None, "version": "?", "build": "?"}


class LogWriter:
    """Пишет в файл (и в консоль, если она есть), добавляя время в начало каждой строки."""

    def __init__(self, file, console):
        self.file, self.console = file, console
        self.fresh = True
        self.lock = threading.Lock()

    def write(self, text: str) -> int:
        with self.lock:
            if self.console:
                try:
                    self.console.write(text)
                except (OSError, ValueError):
                    pass
            for part in text.splitlines(keepends=True):
                if self.fresh:
                    self.file.write(time.strftime("%H:%M:%S "))
                self.file.write(part)
                self.fresh = part.endswith("\n")
            self.file.flush()
        return len(text)

    def flush(self) -> None:
        pass


def log_file() -> Path:
    return config["dir"] / "launcher.log"


def setup(settings_dir: Path, version: str, build: str, name: str = "launcher") -> None:
    """Включает лог и автоматические отчёты о сбоях. name — какой процесс пишет (лаунчер или раздача)."""
    config.update(dir=settings_dir, version=version, build=build)
    settings_dir.mkdir(parents=True, exist_ok=True)
    path = log_file() if name == "launcher" else settings_dir / f"{name}.log"
    try:
        if path.exists() and path.stat().st_size > LOG_LIMIT:
            path.replace(path.with_suffix(".old.log"))
        f = path.open("a", encoding="utf-8", errors="replace")
    except OSError:
        return
    writer = LogWriter(f, sys.stdout)
    sys.stdout = sys.stderr = writer
    print(f"===== {time.strftime('%Y-%m-%d')} MineForFriends {version} ({build}), {platform.platform()}, "
          f"{' '.join(sys.argv[1:]) or 'окно'}")
    sys.excepthook = lambda t, e, tb: crash(e, wait=True)
    threading.excepthook = lambda a: crash(a.exc_value) if a.exc_type is not SystemExit else None


def scrub(text: str) -> str:
    # Имя пользователя Windows из путей не уходит
    home = str(Path.home())
    user = os.environ.get("USERNAME") or Path.home().name
    text = text.replace(home, "~").replace(home.replace("\\", "/"), "~")
    if user:
        text = re.sub(r"(?i)([\\/]Users[\\/])" + re.escape(user), r"\1<user>", text)
    return text


def log_tail(lines: int = LOG_TAIL) -> str:
    try:
        return "".join(log_file().read_text("utf-8", errors="replace").splitlines(keepends=True)[-lines:])
    except (OSError, TypeError):
        return ""


def signature(*parts) -> str:
    return hashlib.sha1("|".join(map(str, parts)).encode()).hexdigest()[:12]


def exc_signature(e: BaseException) -> str:
    # Тип ошибки и места в нашем коде (без номеров строк — они меняются от версии к версии)
    frames = [f"{Path(f.filename).name}:{f.name}" for f in traceback.extract_tb(e.__traceback__)]
    return signature(type(e).__name__, *frames[-6:])


def sent_recently(sig: str) -> bool:
    path = config["dir"] / "reports.json"
    try:
        sent = json.loads(path.read_text("utf-8"))
    except (OSError, ValueError, TypeError):
        sent = {}
    now = time.time()
    sent = {k: v for k, v in sent.items() if now - v < REPEAT_AFTER}
    recent = sig in sent
    sent[sig] = sent.get(sig, now)
    try:
        path.write_text(json.dumps(sent), "utf-8")
    except (OSError, TypeError):
        pass
    return recent


def send(kind: str, title: str, text: str, sig: str, auto: bool = True) -> str:
    """Отправляет отчёт; возвращает ответ для игрока. auto — не повторять ту же ошибку чаще раза в сутки."""
    if config["dir"] is None or (auto and sent_recently(sig)):
        return "Отчёт об этой ошибке уже отправлен."
    tail = log_tail()
    payload = {"kind": kind, "title": scrub(title)[:100], "signature": sig, "version": config["version"],
               "build": config["build"], "os": platform.platform(),
               "text": scrub(f"{text.strip()}\n\n----- последние строки лога -----\n{tail}")[-60000:]}
    try:
        r = requests.post(REPORT_URL, json=payload, timeout=15)
        message = r.json().get("message") or f"Ошибка сервера ({r.status_code})"
        print(f"Отчёт [{sig}]: {r.status_code} {message}")
        return message
    except (requests.RequestException, ValueError) as e:
        print(f"Отчёт [{sig}] не отправлен: {e}")
        return "Не удалось отправить отчёт: нет связи с сервером."


def crash(e: BaseException, wait: bool = False) -> None:
    """Непредвиденная ошибка: в лог и автору. wait — отправить до выхода (процесс сейчас упадёт)."""
    text = "".join(traceback.format_exception(type(e), e, e.__traceback__))
    print(text, end="")
    job = threading.Thread(target=send, args=("crash", f"{type(e).__name__}: {e}", text, exc_signature(e)))
    job.start()
    if wait:
        job.join(20)
