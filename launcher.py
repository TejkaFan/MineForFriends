"""Обновлятор MineSerc для TLauncher.

Создаёт в TLauncher версию «MineSerc 26.3» (Fabric, сервер в «Сетевой игре»),
синхронизирует моды и ресурспаки с облака на ПК хоста (через Radmin) и открывает TLauncher.
"""
import hashlib
import json
import os
import subprocess
import sys
import threading
import traceback
from pathlib import Path

import requests

APP_NAME = "MineSerc"
CLOUD_URL = "http://26.199.180.115:8518"
VERSION_ID = "MineSerc 26.3"
APPDATA = Path(os.environ.get("APPDATA", Path.home()))
TL_PROPERTIES = APPDATA / ".tlauncher" / "tlauncher-2.0.properties"
MOJANG_MANIFEST = "https://piston-meta.mojang.com/mc/game/version_manifest_v2.json"

# К облаку ходим напрямую: системный прокси (VPN-клиенты) не видит сеть Radmin
cloud = requests.Session()
cloud.trust_env = False
web = requests.Session()


def load_json(path: Path, default):
    try:
        return json.loads(path.read_text("utf-8"))
    except (OSError, ValueError):
        return default


def save_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), "utf-8")


def sha1(path: Path) -> str:
    h = hashlib.sha1()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def nbt_string(key: str, value: str) -> bytes:
    k, v = key.encode(), value.encode()
    return b"\x08" + len(k).to_bytes(2, "big") + k + len(v).to_bytes(2, "big") + v


def ensure_server(path: Path, name: str, ip: str) -> None:
    """Добавляет сервер первым в servers.dat (несжатый NBT), если его там ещё нет."""
    entry = nbt_string("name", name) + nbt_string("ip", ip) + b"\x00"
    head = b"\x0a\x00\x00\x09\x00\x07servers"
    data = path.read_bytes() if path.exists() else b""

    if not data:
        path.write_bytes(head + b"\x0a" + (1).to_bytes(4, "big") + entry + b"\x00")
        return
    if nbt_string("ip", ip) in data:
        return
    if data.startswith(head + b"\x0a"):
        pos = len(head) + 1
        count = int.from_bytes(data[pos:pos + 4], "big")
        path.write_bytes(data[:pos] + (count + 1).to_bytes(4, "big") + entry + data[pos + 4:])
    elif data.startswith(head + b"\x00\x00\x00\x00\x00"):
        # Пустой список
        rest = data[len(head) + 5:]
        path.write_bytes(head + b"\x0a" + (1).to_bytes(4, "big") + entry + rest)
    # Иначе формат неожиданный — файл не трогаем


def minecraft_dir() -> Path:
    # Папка игры из настроек TLauncher, иначе стандартная
    try:
        for line in TL_PROPERTIES.read_text("utf-8", errors="ignore").splitlines():
            if line.startswith("minecraft.gamedir="):
                value = line.split("=", 1)[1].replace("\\:", ":").replace("\\\\", "\\").strip()
                if value:
                    return Path(value)
    except OSError:
        pass
    return APPDATA / ".minecraft"


class Updater:
    def __init__(self, status, progress):
        self.status = status
        self.progress = progress
        self.mc_dir = minecraft_dir()
        self.game_dir = self.mc_dir / "mineserc"
        self.state_file = self.game_dir / "launcher_state.json"

    def fetch_manifest(self) -> dict:
        self.status("Связь с сервером…")
        try:
            r = cloud.get(f"{CLOUD_URL}/manifest.json", timeout=8)
            r.raise_for_status()
            return r.json()
        except requests.RequestException as e:
            raise RuntimeError(
                "Нет связи с сервером.\n\nПроверь, что Radmin VPN запущен и ты в нашей сети, "
                "а хост включил сервер.\n\n" + str(e)
            ) from None

    def build_version(self, mc: str, loader: str) -> dict:
        # Склеиваем ванильный 26.3 и профиль Fabric в одну самостоятельную версию
        self.status(f"Подготовка версии {VERSION_ID}…")
        entry = next(v for v in web.get(MOJANG_MANIFEST, timeout=20).json()["versions"] if v["id"] == mc)
        vanilla = web.get(entry["url"], timeout=20).json()
        fabric = web.get(f"https://meta.fabricmc.net/v2/versions/loader/{mc}/{loader}/profile/json", timeout=20).json()

        game_args = [str(self.game_dir) if a == "${game_directory}" else a for a in vanilla["arguments"]["game"]]
        game_args += fabric.get("arguments", {}).get("game", [])

        version = dict(vanilla)
        version.update(
            id=VERSION_ID,
            type="modified",
            mainClass=fabric["mainClass"],
            libraries=fabric["libraries"] + vanilla["libraries"],
            arguments={
                "game": game_args,
                "jvm": vanilla["arguments"]["jvm"] + fabric.get("arguments", {}).get("jvm", []),
            },
        )
        version.pop("inheritsFrom", None)
        return version

    def install_version(self, manifest: dict) -> None:
        state = load_json(self.state_file, {})
        self.game_dir.mkdir(parents=True, exist_ok=True)
        ensure_server(self.game_dir / "servers.dat", "MineSerc", manifest["server"])

        key = f"v4|{manifest['minecraft']}|{manifest['fabric_loader']}|{self.game_dir}"
        json_path = self.mc_dir / "versions" / VERSION_ID / f"{VERSION_ID}.json"
        if state.get("version_key") == key and json_path.exists():
            return
        version = self.build_version(manifest["minecraft"], manifest["fabric_loader"])
        save_json(json_path, version)

        options = self.game_dir / "options.txt"
        if not options.exists():
            options.write_text("lang:ru_ru\n", "utf-8")

        state["version_key"] = key
        save_json(self.state_file, state)

    def sync_files(self, items: list, folder: str, what: str) -> None:
        """Скачивает файлы из облака в game_dir/<folder> (одноимённая папка в облаке)."""
        target = self.game_dir / folder
        target.mkdir(parents=True, exist_ok=True)
        state = load_json(self.state_file, {})
        wanted = {m["file"] for m in items}

        # Убираем только файлы, которые раньше ставил лаунчер; свои файлы игрока не трогаем
        for name in state.get(f"managed_{folder}", []):
            if name not in wanted and (target / name).exists():
                self.status(f"Удаляю старый {what} {name}")
                (target / name).unlink()

        for i, m in enumerate(items, 1):
            self.progress(i / max(len(items), 1))
            path = target / m["file"]
            if path.exists() and sha1(path) == m["sha1"]:
                continue
            self.status(f"Скачиваю {what} {m['file']} ({i}/{len(items)})")
            tmp = path.with_name(path.name + ".part")
            with cloud.get(f"{CLOUD_URL}/{folder}/{requests.utils.quote(m['file'])}", stream=True, timeout=30) as r:
                r.raise_for_status()
                with tmp.open("wb") as f:
                    for chunk in r.iter_content(1 << 16):
                        f.write(chunk)
            if sha1(tmp) != m["sha1"]:
                tmp.unlink()
                raise RuntimeError(f"Файл {m['file']} скачался с ошибкой, попробуй ещё раз.")
            tmp.replace(path)

        state[f"managed_{folder}"] = sorted(wanted)
        save_json(self.state_file, state)

    def enable_packs(self, packs: list) -> None:
        """Включает ресурспаки сервера в options.txt в нужном порядке.

        Делается только когда набор паков с облака поменялся, чтобы игрок
        мог сам выключить пак и лаунчер не включал его обратно каждый раз.
        """
        state = load_json(self.state_file, {})
        ours = [f"file/{p['file']}" for p in packs]
        if state.get("enabled_packs") == ours:
            return
        previous = set(state.get("enabled_packs", [])) | set(ours)

        options = self.game_dir / "options.txt"
        lines = options.read_text("utf-8").splitlines() if options.exists() else []
        current = ["vanilla", "fabric"]
        for line in lines:
            if line.startswith("resourcePacks:"):
                try:
                    current = json.loads(line.split(":", 1)[1])
                except ValueError:
                    pass
        # Свои паки игрока оставляем, наши ставим сверху (в конце списка — выше приоритет)
        packs_line = "resourcePacks:" + json.dumps([p for p in current if p not in previous] + ours)
        lines = [l for l in lines if not l.startswith("resourcePacks:")] + [packs_line]
        options.write_text("\n".join(lines) + "\n", "utf-8")

        state["enabled_packs"] = ours
        save_json(self.state_file, state)

    def open_tlauncher(self) -> bool:
        exe = self.mc_dir / "TLauncher.exe"
        if exe.exists():
            subprocess.Popen([str(exe)], cwd=str(self.mc_dir))
            return True
        return False

    def run(self, open_tl: bool = True) -> str:
        manifest = self.fetch_manifest()
        self.install_version(manifest)
        self.sync_files(manifest["mods"], "mods", "мод")
        packs = manifest.get("resourcepacks", [])
        self.sync_files(packs, "resourcepacks", "ресурспак")
        self.enable_packs(packs)
        self.progress(1)
        if open_tl and self.open_tlauncher():
            return f"Готово! В TLauncher выбери версию «{VERSION_ID}» и нажми «Войти в игру»."
        return f"Готово! Открой TLauncher, выбери версию «{VERSION_ID}» и нажми «Войти в игру»."


def run_gui() -> None:
    import tkinter as tk
    from tkinter import messagebox, ttk

    root = tk.Tk()
    root.title(APP_NAME)
    root.resizable(False, False)
    frm = ttk.Frame(root, padding=16)
    frm.grid()

    ttk.Label(frm, text="MineSerc", font=("Segoe UI", 18, "bold")).grid(column=0, row=0, pady=(0, 4))
    ttk.Label(frm, text="Обновление модов и версии для TLauncher").grid(column=0, row=1, pady=(0, 8))
    bar = ttk.Progressbar(frm, length=340, maximum=1.0)
    bar.grid(column=0, row=2, pady=(4, 4))
    status_var = tk.StringVar(value="Нажми «Обновить и открыть TLauncher»")
    ttk.Label(frm, textvariable=status_var, wraplength=340).grid(column=0, row=3, sticky="w")
    go = ttk.Button(frm, text="Обновить и открыть TLauncher")
    go.grid(column=0, row=4, pady=(12, 0), sticky="we")

    def ui(fn, *a):
        root.after(0, fn, *a)

    def worker():
        upd = Updater(lambda s: ui(status_var.set, s), lambda v: ui(bar.configure, {"value": v}))
        try:
            ui(status_var.set, upd.run())
            ui(root.after, 6000, root.destroy)
        except Exception as e:
            traceback.print_exc()
            ui(messagebox.showerror, APP_NAME, str(e))
            ui(status_var.set, "Ошибка, попробуй ещё раз")
            ui(go.state, ["!disabled"])

    def on_go():
        go.state(["disabled"])
        threading.Thread(target=worker, daemon=True).start()

    go.configure(command=on_go)
    root.mainloop()


if __name__ == "__main__":
    # Отладка: launcher.py --cli [--no-open]
    if len(sys.argv) >= 2 and sys.argv[1] == "--cli":
        print(Updater(print, lambda v: None).run(open_tl="--no-open" not in sys.argv))
    else:
        run_gui()
