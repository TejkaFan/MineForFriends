"""Облако MineForFriends в трее: раздаёт сборки, скины и обход для друзей без окна консоли.

Значок в трее: включить, поставить на паузу (порт остаётся занят, друзьям отвечаем «на паузе»),
выключить (порт свободен). При первом запуске ставит себя в автозапуск Windows (для текущего
пользователя, без прав администратора); пункт меню снимает его. Работает с пониженным
приоритетом, чтобы не отнимать процессор у серверов Minecraft. Журнал — cloud.log рядом с облаком.

    .venv\\Scripts\\pythonw.exe cloud_tray.py
"""
import ctypes
import json
import os
import sys
import threading
import time
import traceback
import winreg
from pathlib import Path

HERE = Path(__file__).resolve().parent
LOG = HERE.parent / "cloud.log"


def open_log() -> None:
    # У pythonw нет консоли: всё, что облако пишет, уходит в файл. Старый журнал — в cloud.old.log.
    # Открываем до остальных импортов, иначе ошибка при запуске Windows пропадёт бесследно
    if LOG.exists() and LOG.stat().st_size > 2 << 20:
        LOG.replace(LOG.with_name("cloud.old.log"))
    sys.stdout = sys.stderr = LOG.open("a", encoding="utf-8", buffering=1)


if __name__ == "__main__":
    open_log()

import pystray  # noqa: E402
from PIL import Image, ImageEnhance  # noqa: E402

import cloud_server  # noqa: E402

SETTINGS = cloud_server.ROOT / "cloud_tray.json"
RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
RUN_NAME = "MineForFriends-Cloud"
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
# Без типов ctypes обрезает псевдодескриптор процесса (-1) до 32 бит, и приоритет не меняется
kernel32.GetCurrentProcess.restype = ctypes.c_void_p
kernel32.SetPriorityClass.argtypes = (ctypes.c_void_p, ctypes.c_uint32)


def log(text: str) -> None:
    print(f"{time.strftime('%Y-%m-%d %H:%M:%S')} {text}")


def first_instance() -> bool:
    kernel32.CreateMutexW(None, False, "Local\\MineForFriends-Cloud-Tray")
    return ctypes.get_last_error() != 183  # ERROR_ALREADY_EXISTS


def autostart_command() -> str:
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    return f'"{pythonw}" "{HERE / "cloud_tray.py"}"'


def autostart() -> bool:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            return winreg.QueryValueEx(key, RUN_NAME)[0] == autostart_command()
    except OSError:
        return False


def set_autostart(on: bool) -> None:
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE) as key:
        if on:
            winreg.SetValueEx(key, RUN_NAME, 0, winreg.REG_SZ, autostart_command())
        else:
            try:
                winreg.DeleteValue(key, RUN_NAME)
            except FileNotFoundError:
                pass


class Tray:
    def __init__(self):
        self.httpd = None
        base = Image.open(HERE / "icon.ico").convert("RGBA").resize((64, 64))
        gray = ImageEnhance.Color(base).enhance(0)
        self.images = {"on": base, "paused": ImageEnhance.Brightness(gray).enhance(1.1),
                       "off": ImageEnhance.Brightness(gray).enhance(0.55)}
        self.icon = pystray.Icon(RUN_NAME, self.images["off"], "Облако MineForFriends", menu=pystray.Menu(
            pystray.MenuItem(lambda _: self.title(), None, enabled=False),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Включено", lambda: self.switch("on"), checked=lambda _: self.mode() == "on", radio=True),
            pystray.MenuItem("Пауза", lambda: self.switch("paused"), checked=lambda _: self.mode() == "paused", radio=True),
            pystray.MenuItem("Выключено", lambda: self.switch("off"), checked=lambda _: self.mode() == "off", radio=True),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Запускать при входе в Windows", self.toggle_autostart, checked=lambda _: autostart()),
            pystray.MenuItem("Открыть папку облака", lambda: os.startfile(cloud_server.CLOUD)),
            pystray.MenuItem("Журнал", lambda: os.startfile(LOG)),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Выход", self.quit),
        ))

    def mode(self) -> str:
        if not self.httpd:
            return "off"
        return "paused" if cloud_server.state["paused"] else "on"

    def title(self) -> str:
        return {"on": f"Облако работает · порт {cloud_server.PORT}", "paused": "Облако на паузе",
                "off": "Облако выключено"}[self.mode()]

    def switch(self, mode: str) -> None:
        if mode == "off":
            if self.httpd:
                self.httpd.shutdown()
                self.httpd.server_close()
                self.httpd = None
        elif not self.httpd:
            try:
                self.httpd = cloud_server.serve()
            except OSError:
                log(f"порт {cloud_server.PORT} занят")
                self.icon.notify(f"Порт {cloud_server.PORT} занят: облако уже запущено в другом окне "
                                 "(например, из start.bat сервера). Закрой его и включи облако здесь.",
                                 "Облако MineForFriends")
                self.refresh()
                return
            threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        cloud_server.state["paused"] = mode == "paused"
        log(f"облако: {self.title()}")
        self.refresh()

    def refresh(self) -> None:
        self.icon.icon = self.images[self.mode()]
        self.icon.title = f"MineForFriends · {self.title()}"
        self.icon.update_menu()

    def toggle_autostart(self) -> None:
        set_autostart(not autostart())
        self.icon.update_menu()

    def quit(self) -> None:
        self.switch("off")
        self.icon.stop()

    def run(self) -> None:
        def ready(icon):
            icon.visible = True
            # Первый запуск: в автозапуск. Дальше решает пункт меню
            settings = json.loads(SETTINGS.read_text("utf-8")) if SETTINGS.exists() else {}
            if not settings.get("autostart_asked"):
                set_autostart(True)
                SETTINGS.write_text(json.dumps({"autostart_asked": True}), "utf-8")
            elif registered() and not autostart():
                set_autostart(True)  # папку перенесли: обновляем путь в автозапуске
            self.switch("on")

        self.icon.run(setup=ready)


def registered() -> bool:
    # Стоит ли значок в автозапуске вообще (возможно, со старым путём)
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            return bool(winreg.QueryValueEx(key, RUN_NAME)[0])
    except OSError:
        return False


def main() -> None:
    if not first_instance():
        log("значок облака уже запущен")
        return
    # Облако почти всё время ждёт запросов; пониженный приоритет — чтобы не мешать серверам игры
    kernel32.SetPriorityClass(kernel32.GetCurrentProcess(), 0x4000)  # BELOW_NORMAL_PRIORITY_CLASS
    log(f"запуск, облако: {cloud_server.CLOUD}")
    # Сразу после входа в Windows панель задач может быть ещё не готова к значку: пробуем несколько раз
    for attempt in range(5):
        tray = Tray()
        try:
            tray.run()
            return
        except Exception:
            log(f"значок не запустился (попытка {attempt + 1}):\n{traceback.format_exc()}")
            tray.switch("off")
            time.sleep(15)


if __name__ == "__main__":
    try:
        main()
    except Exception:
        log(f"облако упало:\n{traceback.format_exc()}")
