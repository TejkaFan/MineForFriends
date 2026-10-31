"""Обновление модов своей сборки с Modrinth.

Лаунчер узнаёт каждый мод по хэшу файла, спрашивает у Modrinth самую новую версию этого же
мода для загрузчика и версии Minecraft сборки и показывает, что можно обновить. Старые файлы
не удаляются, а переезжают в mods_backup/<дата> рядом с папкой mods, чтобы обновление можно было откатить.
"""
import datetime
import json
import shutil
from pathlib import Path

import requests

import custom_packs
from recipes import MODRINTH_CDN, file_sha1

# Каким загрузчиком можно запускать моды: Quilt понимает и моды Fabric
LOADERS = {"fabric": ["fabric"], "quilt": ["quilt", "fabric"], "forge": ["forge"], "neoforge": ["neoforge"]}
# Вне папки mods: загрузчик и раздача друзьям не должны видеть старые файлы
BACKUP_DIR = "mods_backup"
BACKUPS_KEEP = 3


def _post(path: str, body: dict) -> dict:
    r = custom_packs.web.post(f"{custom_packs.MODRINTH}/{path}", json=body, timeout=40)
    r.raise_for_status()
    return r.json()


def check(game_dir: Path, loader: str, mc: str, status) -> dict:
    """Что можно обновить: {updates: [...], mods, found}. Моды не с Modrinth пропускаются."""
    if loader not in LOADERS:
        raise RuntimeError("В сборке без загрузчика нет модов.")
    jars = sorted((game_dir / "mods").glob("*.jar")) if (game_dir / "mods").is_dir() else []
    status(f"Считаю хэши модов ({len(jars)})…")
    hashes = {file_sha1(p): p for p in jars}
    keys = list(hashes)
    current, latest = {}, {}
    status("Ищу обновления на Modrinth…")
    try:
        for i in range(0, len(keys), 300):
            part = keys[i:i + 300]
            current.update(_post("version_files", {"hashes": part, "algorithm": "sha1"}))
            latest.update(_post("version_files/update", {"hashes": part, "algorithm": "sha1",
                                                         "loaders": LOADERS[loader], "game_versions": [mc]}))
    except (requests.RequestException, ValueError) as e:
        raise RuntimeError(f"Modrinth не отвечает — попробуй позже.\n\n{e}") from None

    updates = []
    for sha, path in hashes.items():
        cur, new = current.get(sha), latest.get(sha)
        # Новая версия должна быть того же мода и выйти позже текущей (иначе это не обновление)
        if not cur or not new or new["id"] == cur["id"] or new.get("project_id") != cur.get("project_id") \
                or new.get("date_published", "") <= cur.get("date_published", ""):
            continue
        files = new.get("files") or []
        file = next((f for f in files if f.get("primary")), files[0] if files else None)
        if not file or not str(file.get("url", "")).startswith(MODRINTH_CDN) or not file.get("filename", "").endswith(".jar"):
            continue
        updates.append({"file": path.name, "project": new["project_id"], "title": "",
                        "from": cur.get("version_number", ""), "to": new.get("version_number", ""),
                        "type": new.get("version_type", "release"), "url": file["url"],
                        "sha1": file.get("hashes", {}).get("sha1"), "filename": file["filename"],
                        "size": file.get("size", 0)})

    ids = sorted({u["project"] for u in updates})
    titles = {}
    for i in range(0, len(ids), 100):
        try:
            for p in custom_packs.get_json(f"{custom_packs.MODRINTH}/projects",
                                           params={"ids": json.dumps(ids[i:i + 100])}):
                titles[p["id"]] = p.get("title", "")
        except (requests.RequestException, ValueError):
            pass  # без названий покажем имена файлов
    for u in updates:
        u["title"] = titles.get(u["project"]) or Path(u["file"]).stem
    updates.sort(key=lambda u: u["title"].lower())
    return {"updates": updates, "mods": len(jars), "found": len(current)}


def apply(game_dir: Path, updates: list, loader: str, mc: str, status, progress) -> dict:
    """Ставит выбранные обновления. Старые файлы — в mods_backup/<дата>. Потом докачивает зависимости."""
    mods = game_dir / "mods"
    backup = game_dir / BACKUP_DIR / datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
    done, failed = [], []
    for n, u in enumerate(updates, 1):
        status(f"Обновляю {u['title']} ({n}/{len(updates)})…")
        progress(0.8 * (n - 1) / max(1, len(updates)))
        old = mods / u["file"]
        name = Path(u["filename"]).name
        if not old.is_file() or not u["url"].startswith(MODRINTH_CDN) or not name.endswith(".jar"):
            failed.append(u["title"])
            continue
        try:
            # Качаем рядом под временным именем: имя новой версии может совпасть со старым
            tmp = custom_packs.download_file(u["url"], mods / f"{name}.new", u.get("sha1"))
        except (requests.RequestException, RuntimeError, OSError):
            failed.append(u["title"])
            continue
        backup.mkdir(parents=True, exist_ok=True)
        shutil.move(str(old), str(backup / old.name))
        tmp.replace(mods / name)
        done.append(f"{u['title']} {u['from']} → {u['to']}")

    # У новых версий могут появиться новые зависимости
    status("Проверяю зависимости модов…")
    metas = [custom_packs.mod_meta(p.read_bytes()) for p in mods.glob("*.jar")]
    extra, not_found = custom_packs.resolve_missing(custom_packs.missing_deps(metas), loader, mc, status)
    failed += custom_packs.fetch_all(extra, game_dir, status, lambda v: progress(0.8 + 0.2 * v))
    progress(1)

    # Старые бэкапы чистим, последние три оставляем
    olds = sorted(d for d in (game_dir / BACKUP_DIR).iterdir() if d.is_dir()) if (game_dir / BACKUP_DIR).is_dir() else []
    for d in olds[:-BACKUPS_KEEP]:
        shutil.rmtree(d, ignore_errors=True)
    return {"updated": done, "failed": failed, "added": [Path(d["path"]).name for d in extra],
            "not_found": not_found, "backup": str(backup) if done else ""}
