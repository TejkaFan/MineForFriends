"""Скриншоты Minecraft из всех сборок в одном месте: список, миниатюры, копирование в буфер обмена.

Игра кладёт скриншоты (F2) в папку screenshots своей папки игры: у основной сборки, у
«Своих сборок» (custom/<имя>), у сборок друзей (friends/<хост-сервер>) и у самого TLauncher.
"""
import base64
import ctypes
import hashlib
import io
import time
from ctypes import wintypes
from pathlib import Path

from PIL import Image

from crashes import pack_name

THUMB_WIDTH = 320
EXTENSIONS = (".png", ".jpg", ".jpeg")


def folders(mc_dir: Path) -> list:
    found = [mc_dir / "screenshots"]
    for pattern in ("*/screenshots", "*/*/screenshots"):
        found += [p for p in mc_dir.glob(pattern) if p.is_dir()]
    return [p for p in found if p.is_dir()]


def find(mc_dir: Path, names: dict) -> list:
    """Все скриншоты, новые сверху: {path, name, pack, time, folder}."""
    names = {k.lower(): v for k, v in names.items()}
    items = []
    for folder in folders(mc_dir):
        pack = pack_name(mc_dir, folder / "x.png", names)
        for p in folder.iterdir():
            if p.suffix.lower() in EXTENSIONS and p.is_file():
                items.append((p.stat().st_mtime, p, pack))
    items.sort(key=lambda i: i[0], reverse=True)
    return [{"path": str(p), "name": p.name, "pack": pack,
             "time": time.strftime("%d.%m.%Y %H:%M", time.localtime(mtime))} for mtime, p, pack in items]


def thumbnail(path: Path, cache: Path) -> str:
    """Уменьшенная копия JPEG в base64; готовые миниатюры лежат в cache."""
    stat = path.stat()
    key = hashlib.sha1(f"{path}|{stat.st_mtime}|{stat.st_size}".encode()).hexdigest()
    cached = cache / f"{key}.jpg"
    if not cached.exists():
        cache.mkdir(parents=True, exist_ok=True)
        with Image.open(path) as img:
            img = img.convert("RGB")
            img.thumbnail((THUMB_WIDTH, THUMB_WIDTH))
            img.save(cached, "JPEG", quality=80)
    return base64.b64encode(cached.read_bytes()).decode()


def full(path: Path) -> str:
    """Картинка для просмотра: JPEG не больше 1920 по ширине, чтобы не гонять мегабайты PNG в окно."""
    with Image.open(path) as img:
        img = img.convert("RGB")
        img.thumbnail((1920, 1920))
        buf = io.BytesIO()
        img.save(buf, "JPEG", quality=90)
    return base64.b64encode(buf.getvalue()).decode()


def clean_cache(cache: Path, keep: int = 2000) -> None:
    if cache.is_dir():
        files = sorted(cache.glob("*.jpg"), key=lambda p: p.stat().st_mtime, reverse=True)
        for p in files[keep:]:
            p.unlink(missing_ok=True)


# ---------- Буфер обмена ----------

CF_DIB = 8
GMEM_MOVEABLE = 0x0002


def copy_image(path: Path) -> None:
    """Кладёт картинку в буфер обмена — дальше Ctrl+V в Telegram, Discord, Paint."""
    with Image.open(path) as img:
        buf = io.BytesIO()
        img.convert("RGB").save(buf, "BMP")
    dib = buf.getvalue()[14:]  # без заголовка файла BMP

    user32, kernel32 = ctypes.windll.user32, ctypes.windll.kernel32
    kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
    kernel32.GlobalAlloc.argtypes = (wintypes.UINT, ctypes.c_size_t)
    kernel32.GlobalLock.restype = wintypes.LPVOID
    kernel32.GlobalLock.argtypes = (wintypes.HGLOBAL,)
    kernel32.GlobalUnlock.argtypes = (wintypes.HGLOBAL,)
    user32.SetClipboardData.argtypes = (wintypes.UINT, wintypes.HANDLE)
    user32.SetClipboardData.restype = wintypes.HANDLE

    handle = kernel32.GlobalAlloc(GMEM_MOVEABLE, len(dib))
    if not handle:
        raise RuntimeError("Не хватило памяти для буфера обмена.")
    ptr = kernel32.GlobalLock(handle)
    ctypes.memmove(ptr, dib, len(dib))
    kernel32.GlobalUnlock(handle)
    for _ in range(10):  # буфер может держать другая программа
        if user32.OpenClipboard(None):
            break
        time.sleep(0.1)
    else:
        raise RuntimeError("Буфер обмена занят другой программой, попробуй ещё раз.")
    try:
        user32.EmptyClipboard()
        if not user32.SetClipboardData(CF_DIB, handle):
            raise RuntimeError("Не удалось положить картинку в буфер обмена.")
    finally:
        user32.CloseClipboard()
