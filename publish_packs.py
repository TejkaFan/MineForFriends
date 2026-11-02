"""Выкладывает сборки облака в интернет, чтобы лаунчер ставил их и без Radmin.

Файлы, которые есть на Modrinth (по sha1), лаунчер качает прямо оттуда. Остальное (конфиги,
шейдеры, моды с CurseForge) лежит в служебном предрелизе `packs` репозитория MineForFriends:
крупные файлы — по одному (f-<sha1>), мелкие — в 32 архивах на сборку (<id>-NN-<хэш>.zip,
файл попадает в архив по хэшу пути: правка одного конфига меняет только его архив).
Там же манифесты <id>.json и список packs.json. Имена файлов зависят от содержимого, поэтому
выкладывается только изменившееся, а то, на что манифесты больше не ссылаются, удаляется.

Запускать после изменений в облаке (сам пересобирает манифесты облака):
    .venv\\Scripts\\python.exe publish_packs.py
"""
import hashlib
import json
import subprocess
import sys
import zipfile
import zlib
from pathlib import Path

import requests

import update_manifest

REPO = "TejkaFan/MineForFriends"
TAG = "packs"
CLOUD = update_manifest.CLOUD
STAGE = CLOUD.parent / "packs-publish"
# Что уже известно про Modrinth: sha1 → ссылка или null (нет там)
CACHE = CLOUD.parent / "packs_modrinth.json"
BUNDLES = 32
SINGLE = 4 << 20  # файлы крупнее — отдельными файлами релиза
MODRINTH = "https://api.modrinth.com/v2/version_files"
web = requests.Session()
web.headers["User-Agent"] = "MineForFriends-launcher (github.com/TejkaFan)"


def gh(*args, capture=False) -> str:
    r = subprocess.run(["gh", *args, "-R", REPO], check=True, text=True, encoding="utf-8",
                       stdout=subprocess.PIPE if capture else None)
    return r.stdout if capture else ""


def pack_files(manifest: dict) -> list:
    # Основная сборка хранится в старом формате: отдельные списки модов и ресурспаков
    if "files" in manifest:
        return manifest["files"]
    return ([{"path": f"mods/{m['file']}", "sha1": m["sha1"], "size": m["size"]} for m in manifest["mods"]]
            + [{"path": f"resourcepacks/{p['file']}", "sha1": p["sha1"], "size": p["size"]} for p in manifest["resourcepacks"]])


def modrinth_links(files: list) -> dict:
    cache = json.loads(CACHE.read_text("utf-8")) if CACHE.exists() else {}
    ask = sorted({f["sha1"] for f in files if f["path"].endswith((".jar", ".zip")) and not cache.get(f["sha1"])})
    for i in range(0, len(ask), 200):
        part = ask[i:i + 200]
        r = web.post(MODRINTH, json={"hashes": part, "algorithm": "sha1"}, timeout=60)
        r.raise_for_status()
        found = r.json()
        for h in part:
            version = found.get(h)
            cache[h] = next((f["url"] for f in version["files"] if f["hashes"].get("sha1") == h), None) if version else None
    CACHE.write_text(json.dumps(cache, indent=1), "utf-8")
    return cache


def bundle_name(pack_id: str, number: int, members: list) -> str:
    digest = hashlib.sha1("".join(f"{f['path']} {f['sha1']}\n" for f in sorted(members, key=lambda f: f["path"])).encode())
    return f"{pack_id}-{number:02d}-{digest.hexdigest()[:12]}.zip"


def build(pack: dict, links: dict, uploads: dict) -> dict:
    """Манифест сборки для интернета; в uploads — файлы, которые должны лежать в релизе."""
    manifest = json.loads((CLOUD / pack["manifest"]).read_text("utf-8"))
    files = pack_files(manifest)
    source = CLOUD / pack["base"]
    groups = {}
    for f in files:
        if links.get(f["sha1"]):
            f["url"] = links[f["sha1"]]
        elif f["size"] > SINGLE:
            f["asset"] = f"f-{f['sha1']}"
            uploads[f["asset"]] = ("file", source / f["path"])
        else:
            groups.setdefault(zlib.crc32(f["path"].encode()) % BUNDLES, []).append(f)
    for number, members in groups.items():
        name = bundle_name(pack["id"], number, members)
        for f in members:
            f["bundle"] = name
        uploads[name] = ("zip", [(source / f["path"], f["path"]) for f in members])

    out = {k: v for k, v in manifest.items() if k not in ("mods", "resourcepacks", "files")}
    out["files"] = files
    if "resourcepacks" in manifest:
        out["enable_resourcepacks"] = [p["file"] for p in manifest["resourcepacks"]]
    stats = {k: sum(1 for f in files if k in f) for k in ("url", "asset", "bundle")}
    own = sum(f["size"] for f in files if "url" not in f) / 2**20
    print(f"[{pack['id']}] Modrinth: {stats['url']}, своих: {stats['asset']} отдельно + {stats['bundle']} в "
          f"{len(groups)} архивах ({own:.0f} МБ)")
    return out


def stage(name: str, what) -> Path:
    path = STAGE / name
    if path.exists():
        return path
    kind, data = what
    if kind == "file":
        path.write_bytes(data.read_bytes())
    else:
        with zipfile.ZipFile(path.with_suffix(".part"), "w", zipfile.ZIP_DEFLATED) as z:
            for src, arc in data:
                z.write(src, arc)
        path.with_suffix(".part").replace(path)
    return path


def main() -> None:
    update_manifest.main()
    STAGE.mkdir(exist_ok=True)
    packs = json.loads((CLOUD / "packs.json").read_text("utf-8"))["packs"]
    links = modrinth_links([f for p in packs for f in pack_files(json.loads((CLOUD / p["manifest"]).read_text("utf-8")))])

    uploads, manifests, listing = {}, {}, []
    for pack in packs:
        manifests[f"{pack['id']}.json"] = build(pack, links, uploads)
        listing.append({**pack, "manifest": f"{pack['id']}.json"})

    try:
        existing = {a["name"] for a in json.loads(gh("release", "view", TAG, "--json", "assets", capture=True))["assets"]}
    except subprocess.CalledProcessError:
        gh("release", "create", TAG, "--prerelease", "--title", "Данные сборок (служебный)",
           "--notes", "Служебный релиз: файлы сборок для лаунчера MineForFriends. Скачивать вручную не нужно.")
        existing = set()

    new = [stage(name, what) for name, what in sorted(uploads.items()) if name not in existing]
    for i in range(0, len(new), 20):
        gh("release", "upload", TAG, *map(str, new[i:i + 20]))
    print(f"Выложено новых файлов: {len(new)} ({sum(p.stat().st_size for p in new) / 2**20:.1f} МБ)")

    # Манифесты — после файлов, packs.json — последним: лаунчер не увидит ссылок на ещё не выложенное
    for name, data in manifests.items():
        (STAGE / name).write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), "utf-8")
    (STAGE / "packs.json").write_text(json.dumps({"packs": listing}, ensure_ascii=False, indent=2), "utf-8")
    gh("release", "upload", TAG, "--clobber", *[str(STAGE / n) for n in manifests], str(STAGE / "packs.json"))

    keep = set(uploads) | set(manifests) | {"packs.json"}
    for name in sorted(existing - keep):
        gh("release", "delete-asset", TAG, name, "--yes")
    for path in STAGE.iterdir():
        if path.name not in keep:
            path.unlink()
    print(f"Удалено старых файлов: {len(existing - keep)}. Готово.")


if __name__ == "__main__":
    sys.exit(main())
