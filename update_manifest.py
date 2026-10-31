"""Пересобирает манифесты облака launcher-cloud.

- manifest.json — основная сборка MineSerc (моды из mods/, ресурспаки из resourcepacks/);
- packs/<id>/manifest.json — дополнительные сборки: все файлы папки packs/<id>/,
  настройки берутся из packs/<id>/pack.json;
- packs.json — список всех сборок для кнопки «Сборки…» в лаунчере.

Запускать после любых изменений в облаке:
    D:\\MineSerc-Fabric\\launcher\\.venv\\Scripts\\python.exe D:\\MineSerc-Fabric\\launcher\\update_manifest.py
"""
import hashlib
import json
import shutil
from datetime import datetime
from pathlib import Path

CLOUD = Path(__file__).resolve().parent.parent / "launcher-cloud"
MANIFEST = CLOUD / "manifest.json"
PACKS_DIR = CLOUD / "packs"
PACKS_LIST = CLOUD / "packs.json"
LAUNCHER_INFO = CLOUD / "launcher.json"

# Настройки основной сборки, которые читает лаунчер
SETTINGS = {
    "name": "MineSerc 26.3",
    "description": "Наш основной сервер: мебель, карты, скины, ресурспаки.",
    "minecraft": "26.3",
    "fabric_loader": "0.20.1",
    "server": "26.199.180.115:25565",
    "server_name": "MineSerc",
    "version_id": "MineSerc 26.3",
    "game_dir": "mineserc",
}

# Порядок ресурспаков: от низшего приоритета к высшему (последний перекрывает остальные).
# Паки, которых нет в списке, встают выше всех по алфавиту.
PACK_ORDER = [
    "faithful-32x.zip",
    "fast-better-grass.zip",
    "enchantment-outlines.zip",
    "new-glowing-ores.zip",
    "fancy-crops.zip",
]


def sha1(path: Path) -> str:
    h = hashlib.sha1()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def entry(path: Path) -> dict:
    return {"file": path.name, "sha1": sha1(path), "size": path.stat().st_size}


def pack_rank(path: Path):
    name = path.name
    return (0, PACK_ORDER.index(name), "") if name in PACK_ORDER else (1, 0, name.lower())


def write_manifest(path: Path, manifest: dict) -> bool:
    # "updated" меняется только при реальных изменениях
    old = json.loads(path.read_text("utf-8")) if path.exists() else {}
    changed = {k: v for k, v in old.items() if k != "updated"} != manifest
    manifest["updated"] = datetime.now().isoformat(timespec="seconds") if changed else old.get("updated")
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), "utf-8")
    return changed


def main_pack() -> dict:
    mods = [entry(p) for p in sorted((CLOUD / "mods").glob("*.jar"), key=lambda p: p.name.lower())]
    packs = [entry(p) for p in sorted((CLOUD / "resourcepacks").glob("*.zip"), key=pack_rank)]
    changed = write_manifest(MANIFEST, {**SETTINGS, "mods": mods, "resourcepacks": packs})

    print(f"[mineserc] {len(mods)} модов{'' if changed else ' (без изменений)'}:")
    for m in mods:
        print(f"  {m['file']}")
    print(f"[mineserc] {len(packs)} ресурспаков (снизу вверх по приоритету):")
    for p in packs:
        print(f"  {p['file']}")
    size = sum(m["size"] for m in mods + packs)
    return {"id": "mineserc", "name": SETTINGS["name"], "description": SETTINGS["description"],
            "manifest": "manifest.json", "base": "", "size": size, "server": SETTINGS["server"]}


def extra_pack(folder: Path) -> dict:
    settings = json.loads((folder / "pack.json").read_text("utf-8"))
    skip = {folder / "pack.json", folder / "manifest.json"}
    files = []
    for p in sorted(folder.rglob("*"), key=lambda p: p.as_posix().lower()):
        if p.is_file() and p not in skip:
            files.append({"path": p.relative_to(folder).as_posix(), "sha1": sha1(p), "size": p.stat().st_size})
    changed = write_manifest(folder / "manifest.json", {**settings, "files": files})

    size = sum(f["size"] for f in files)
    mods = sum(1 for f in files if f["path"].startswith("mods/"))
    print(f"[{folder.name}] {len(files)} файлов, {mods} модов, {size / 2**20:.0f} МБ{'' if changed else ' (без изменений)'}")
    return {"id": folder.name, "name": settings["name"], "description": settings.get("description", ""),
            "manifest": f"packs/{folder.name}/manifest.json", "base": f"packs/{folder.name}/", "size": size,
            "server": settings["server"]}


def main() -> None:
    packs = [main_pack()]
    if PACKS_DIR.exists():
        packs += [extra_pack(d) for d in sorted(PACKS_DIR.iterdir()) if (d / "pack.json").exists()]
    PACKS_LIST.write_text(json.dumps({"packs": packs}, ensure_ascii=False, indent=2), "utf-8")
    print(f"Сборок в packs.json: {len(packs)}")

    # По нему лаунчеры друзей сами обновляются до свежего MineForFriends.exe
    exe = CLOUD / "MineForFriends.exe"
    if exe.exists():
        info = {"sha1": sha1(exe), "size": exe.stat().st_size}
        LAUNCHER_INFO.write_text(json.dumps(info), "utf-8")
        # Лаунчеры до переименования качают обновление под старым именем
        legacy = CLOUD / "MineSerc.exe"
        if not legacy.exists() or sha1(legacy) != info["sha1"]:
            shutil.copyfile(exe, legacy)
        print(f"launcher.json: MineForFriends.exe {info['sha1'][:10]}…")


if __name__ == "__main__":
    main()
