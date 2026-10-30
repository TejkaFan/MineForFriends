"""Пересобирает launcher-cloud/manifest.json по содержимому launcher-cloud/mods и launcher-cloud/resourcepacks.

Запускать после того, как добавил/убрал .jar в mods или .zip в resourcepacks:
    D:\\MineSerc-Fabric\\launcher\\.venv\\Scripts\\python.exe D:\\MineSerc-Fabric\\launcher\\update_manifest.py
"""
import hashlib
import json
from datetime import datetime
from pathlib import Path

CLOUD = Path(__file__).resolve().parent.parent / "launcher-cloud"
MANIFEST = CLOUD / "manifest.json"

# Настройки сборки, которые читает лаунчер
SETTINGS = {
    "minecraft": "26.3",
    "fabric_loader": "0.20.1",
    "server": "26.199.180.115:25565",
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


def main() -> None:
    mods = [entry(p) for p in sorted((CLOUD / "mods").glob("*.jar"), key=lambda p: p.name.lower())]
    packs = [entry(p) for p in sorted((CLOUD / "resourcepacks").glob("*.zip"), key=pack_rank)]

    old = json.loads(MANIFEST.read_text("utf-8")) if MANIFEST.exists() else {}
    manifest = {**SETTINGS, "mods": mods, "resourcepacks": packs}
    changed = {k: v for k, v in old.items() if k != "updated"} != manifest
    manifest["updated"] = datetime.now().isoformat(timespec="seconds") if changed else old.get("updated")

    MANIFEST.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), "utf-8")
    print(f"{len(mods)} модов в манифесте{'' if changed else ' (без изменений)'}:")
    for m in mods:
        print(f"  {m['file']}")
    print(f"{len(packs)} ресурспаков (снизу вверх по приоритету):")
    for p in packs:
        print(f"  {p['file']}")


if __name__ == "__main__":
    main()
