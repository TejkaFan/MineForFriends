"""Значок лаунчера в трее и всплывающие уведомления Windows (через pystray)."""
import threading

import pystray
from PIL import Image


class Tray:
    def __init__(self, title: str, icon_path, on_open, on_quit):
        menu = pystray.Menu(
            # default — срабатывает по щелчку на значке
            pystray.MenuItem("Открыть", lambda: on_open(), default=True),
            pystray.MenuItem("Выход", lambda: on_quit()),
        )
        self.icon = pystray.Icon("MineForFriends", Image.open(icon_path), title, menu)
        self.ready = threading.Event()

    def start(self) -> None:
        def setup(icon):
            icon.visible = True
            self.ready.set()
        threading.Thread(target=self.icon.run, kwargs={"setup": setup}, daemon=True).start()

    def notify(self, title: str, text: str) -> None:
        if self.ready.wait(5):
            try:
                self.icon.notify(text, title)
            except Exception as e:
                print(f"Уведомление не показано: {e}")

    def stop(self) -> None:
        try:
            self.icon.stop()
        except Exception:
            pass
