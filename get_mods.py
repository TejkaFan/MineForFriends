"""Скачивает моды с Modrinth под Fabric 26.3 вместе с обязательными зависимостями.

    python get_mods.py <папка> <slug> [<slug> ...]
Проверяет sha512, уже скачанные файлы с верным хэшем пропускает.
"""
import hashlib
import json
import sys
import urllib.parse
from pathlib import Path

import requests

API = "https://api.modrinth.com/v2"
GAME = "26.3"
s = requests.Session()
s.headers["User-Agent"] = "MineSerc-server-setup"


def latest_version(project: str) -> dict:
    params = {"game_versions": json.dumps([GAME]), "loaders": json.dumps(["fabric"])}
    versions = s.get(f"{API}/project/{project}/version", params=params, timeout=30).json()
    if not versions:
        raise SystemExit(f"{project}: нет версии под Fabric {GAME}")
    return versions[0]


def download(dest: Path, project: str, seen: set) -> None:
    if project in seen:
        return
    seen.add(project)
    v = latest_version(project)
    slug = s.get(f"{API}/project/{project}", timeout=30).json()["slug"]
    f = next((x for x in v["files"] if x["primary"]), v["files"][0])
    path = dest / f["filename"]
    if path.exists() and hashlib.sha512(path.read_bytes()).hexdigest() == f["hashes"]["sha512"]:
        print(f"= {slug} {v['version_number']} ({f['filename']})")
    else:
        data = s.get(f["url"], timeout=120).content
        if hashlib.sha512(data).hexdigest() != f["hashes"]["sha512"]:
            raise SystemExit(f"{slug}: хэш не совпал")
        path.write_bytes(data)
        print(f"+ {slug} {v['version_number']} [{v['version_type']}] ({f['filename']})")
    for dep in v["dependencies"]:
        if dep["dependency_type"] == "required" and dep.get("project_id"):
            download(dest, dep["project_id"], seen)


def main() -> None:
    dest = Path(sys.argv[1])
    dest.mkdir(parents=True, exist_ok=True)
    seen: set = set()
    for slug in sys.argv[2:]:
        download(dest, slug, seen)


if __name__ == "__main__":
    main()
