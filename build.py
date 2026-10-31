"""Собирает dist\\MineForFriends.exe.

Версия берётся из APP_VERSION в launcher.py и вместе с названием и автором попадает в
«Свойства → Подробно» exe: файлы без этих сведений антивирусы оценивают строже.

Загрузчик (bootloader\\runw.exe) собран из исходников PyInstaller той же версии компилятором
MinGW-w64: готовый загрузчик из пакета PyInstaller многие антивирусы ловят по сигнатуре,
потому что им же пакуют вредоносы. Пересобрать после обновления PyInstaller:
    скачать исходники pyinstaller-<версия>.tar.gz с PyPI, в папке bootloader:
    python ./waf all --gcc --target-arch=64bit  (gcc из MinGW-w64 в PATH)
    и скопировать PyInstaller\\bootloader\\Windows-64bit-intel\\runw.exe сюда в bootloader\\

    .venv\\Scripts\\python.exe build.py
"""
import re
import shutil
import subprocess
import sys
from pathlib import Path

import PyInstaller

HERE = Path(__file__).resolve().parent
BUILD = HERE / "build"
OWN_BOOTLOADER = HERE / "bootloader" / "runw.exe"
PYI_BOOTLOADER = Path(PyInstaller.__file__).parent / "bootloader" / "Windows-64bit-intel" / "runw.exe"


def use_own_bootloader() -> None:
    # PyInstaller берёт загрузчик из своей папки: подменяем его своим, оригинал — в runw.orig.exe
    if not OWN_BOOTLOADER.exists():
        sys.exit("Нет bootloader\\runw.exe — собери загрузчик (см. описание в build.py)")
    backup = PYI_BOOTLOADER.with_name("runw.orig.exe")
    if not backup.exists():
        shutil.copyfile(PYI_BOOTLOADER, backup)
    shutil.copyfile(OWN_BOOTLOADER, PYI_BOOTLOADER)


def app_version() -> str:
    text = (HERE / "launcher.py").read_text("utf-8")
    return re.search(r'^APP_VERSION = "(.+)"', text, re.M).group(1)


def version_info(version: str) -> str:
    # Windows хочет четыре числа: «0.8» → 0.8.0.0, «0.5.5 beta» → 0.5.5.0
    numbers = [int(n) for n in re.findall(r"\d+", version)[:4]]
    numbers += [0] * (4 - len(numbers))
    nums = ", ".join(map(str, numbers))
    strings = {
        "CompanyName": "TEJKA",
        "FileDescription": "MineForFriends — лаунчер Minecraft для друзей",
        "FileVersion": version,
        "InternalName": "MineForFriends",
        "LegalCopyright": "© TEJKA",
        "OriginalFilename": "MineForFriends.exe",
        "ProductName": "MineForFriends by TEJKA",
        "ProductVersion": version,
    }
    entries = ",\n          ".join(f"StringStruct({k!r}, {v!r})" for k, v in strings.items())
    return f"""VSVersionInfo(
  ffi=FixedFileInfo(filevers=({nums}), prodvers=({nums}), mask=0x3f, flags=0x0, OS=0x40004,
                    fileType=0x1, subtype=0x0, date=(0, 0)),
  kids=[
    StringFileInfo([StringTable('041904B0', [
          {entries}])]),
    VarFileInfo([VarStruct('Translation', [0x0419, 1200])])
  ]
)
"""


def main() -> None:
    version = app_version()
    BUILD.mkdir(exist_ok=True)
    info = BUILD / "version_info.txt"
    info.write_text(version_info(version), "utf-8")
    use_own_bootloader()
    subprocess.run([
        sys.executable, "-m", "PyInstaller", "--noconfirm", "--onefile", "--windowed", "--name", "MineForFriends",
        "--icon", str(HERE / "icon.ico"), "--version-file", str(info),
        "--add-data", f"{HERE / 'icon.ico'};.", "--add-data", f"{HERE / 'ui'};ui",
        # pystray выбирает бэкенд при запуске — PyInstaller сам его не увидит
        "--hidden-import", "pystray._win32",
        "--exclude-module", "minecraft_launcher_lib", "--exclude-module", "tkinter",
        "--distpath", str(HERE / "dist"), "--workpath", str(BUILD), "--specpath", str(BUILD),
        str(HERE / "launcher.py"),
    ], check=True, cwd=HERE)
    print(f"Готово: dist\\MineForFriends.exe, версия {version}")


if __name__ == "__main__":
    main()
