"""Рецепты сборок: сборка как список модов с Modrinth плюс небольшой архив конфигов.

Поделиться: лаунчер находит моды, ресурспаки и шейдеры сборки на Modrinth по хэшу файла,
конфиги (config, kubejs и т. п.) упаковывает в zip и отправляет в Worker. Тот открывает
Pull Request в репозитории скинов, и сборка появится у всех, когда автор лаунчера его примет.
Файлы модов на наш GitHub не попадают: друзья качают их с Modrinth, так их нельзя подменить
и не нарушаются лицензии модов. Моды, которых на Modrinth нет, в рецепт не входят.
"""
import base64
import hashlib
import io
import json
import re
import shutil
import tempfile
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import requests

import custom_packs

WORKER = "https://mineforfriends-skins.inkiandmelon.workers.dev"
RAW = "https://raw.githubusercontent.com/TejkaFan/MineForFriends-skins/main/recipes/"
CONTENT_DIRS = ("mods", "resourcepacks", "shaderpacks")
# Что из папки игры уходит в архив конфигов. Миры, скриншоты, логи и options.txt — личное
CONFIG_DIRS = ("config", "defaultconfigs", "kubejs", "scripts", "global_packs", "openloader", "paxi")
MAX_ZIP = 4 << 20
MAX_CONFIG_FILE = 1 << 20
MODRINTH_CDN = "https://cdn.modrinth.com/data/"
PATH_RE = re.compile(r"^(mods|resourcepacks|shaderpacks)/[^/\\]{1,200}$")


def recipe_id(name: str) -> str:
    return custom_packs.slug(name).replace("_", "-")[:40].strip("-") or "pack"


def file_sha1(path: Path) -> str:
    h = hashlib.sha1()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------- Поделиться ----------

def scan(game_dir: Path, status) -> dict:
    """Что пойдёт в рецепт: {mods: [...], missing: [имена], zip: bytes, zip_files, zip_size}."""
    files = []
    for folder in CONTENT_DIRS:
        d = game_dir / folder
        if d.is_dir():
            files += [p for p in d.iterdir() if p.is_file() and p.suffix.lower() in (".jar", ".zip")]
    status(f"Считаю хэши файлов ({len(files)})…")
    with ThreadPoolExecutor(6) as pool:
        hashes = dict(zip(pool.map(file_sha1, files), files))

    status("Ищу моды на Modrinth…")
    found = {}
    keys = list(hashes)
    for i in range(0, len(keys), 300):
        try:
            r = custom_packs.web.post(f"{custom_packs.MODRINTH}/version_files",
                                      json={"hashes": keys[i:i + 300], "algorithm": "sha1"}, timeout=30)
            r.raise_for_status()
            found.update(r.json())
        except (requests.RequestException, ValueError) as e:
            raise RuntimeError(f"Modrinth не отвечает — попробуй позже.\n\n{e}") from None
    projects = {v["project_id"] for v in found.values()}
    titles = {}
    ids = sorted(projects)
    for i in range(0, len(ids), 100):
        try:
            for p in custom_packs.get_json(f"{custom_packs.MODRINTH}/projects", params={"ids": json.dumps(ids[i:i + 100])}):
                titles[p["id"]] = p.get("title", "")
        except (requests.RequestException, ValueError):
            pass  # без названий рецепт всё равно рабочий

    mods, missing = [], []
    for sha, path in sorted(hashes.items(), key=lambda kv: kv[1].name.lower()):
        rel = f"{path.parent.name}/{path.name}"
        version = found.get(sha)
        file = next((f for f in (version or {}).get("files", []) if f.get("hashes", {}).get("sha1") == sha), None)
        if not file or not file.get("url", "").startswith(MODRINTH_CDN):
            missing.append(rel)
            continue
        mods.append({"path": rel, "url": file["url"], "sha1": sha, "size": file.get("size", 0),
                     "title": titles.get(version["project_id"], ""), "project": version["project_id"]})

    status("Упаковываю конфиги…")
    buf = io.BytesIO()
    count, skipped = 0, []
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        for folder in CONFIG_DIRS:
            for p in sorted((game_dir / folder).rglob("*")) if (game_dir / folder).is_dir() else []:
                if not p.is_file():
                    continue
                if p.stat().st_size > MAX_CONFIG_FILE:
                    skipped.append(p.relative_to(game_dir).as_posix())
                    continue
                z.write(p, p.relative_to(game_dir).as_posix())
                count += 1
    data = buf.getvalue()
    if len(data) > MAX_ZIP:
        raise RuntimeError(f"Конфиги сборки весят {len(data) / 2 ** 20:.1f} МБ в архиве — больше 4 МБ не отправить. "
                           "Обычно раздувает папка config с кэшами модов.")
    return {"mods": mods, "missing": missing, "zip": data if count else b"", "zip_files": count,
            "zip_size": len(data) if count else 0, "skipped": skipped}


def submit(profile: dict, scanned: dict, name: str, description: str, nick: str, token: str) -> str:
    recipe = {"id": recipe_id(name), "name": name.strip(), "description": description.strip(),
              "minecraft": profile["minecraft"], "loader": profile["loader"],
              "loader_version": profile.get("loader_version") or "", "mods": scanned["mods"],
              "missing": scanned["missing"]}
    body = json.dumps(recipe, ensure_ascii=False).encode()
    if scanned["zip"]:
        body += b"\n" + base64.b64encode(scanned["zip"])
    try:
        r = custom_packs.web.post(f"{WORKER}/recipe", data=body, timeout=120,
                                  headers={"X-Nick": nick, "X-Token": token, "Content-Type": "text/plain; charset=utf-8"})
    except requests.RequestException as e:
        raise RuntimeError(f"Нет связи с сервером сборок. Проверь интернет.\n\n{e}") from None
    try:
        message = r.json()["message"]
    except (ValueError, KeyError):
        message = f"Ошибка сервера ({r.status_code})"
    if r.status_code != 200:
        raise RuntimeError(message)
    return message


# ---------- Сборки друзей ----------

def fetch(recipe_ref: dict) -> dict:
    # sha в адресе — чтобы не получить старую копию из кэша GitHub после обновления
    r = custom_packs.web.get(f"{RAW}{recipe_ref['id']}/recipe.json", params={"v": recipe_ref["sha"][:12]}, timeout=15)
    r.raise_for_status()
    return r.json()


def listing() -> list:
    """Принятые рецепты: [{id, name, author, description, minecraft, loader, mods, missing, files, updated}]."""
    try:
        r = custom_packs.web.get(f"{WORKER}/recipes", timeout=15)
        r.raise_for_status()
        refs = r.json()["recipes"]
    except (requests.RequestException, ValueError, KeyError) as e:
        raise RuntimeError(f"Не удалось получить список сборок друзей. Проверь интернет.\n\n{e}") from None
    with ThreadPoolExecutor(6) as pool:
        results = list(pool.map(lambda ref: _safe_fetch(ref), refs))
    items = []
    for ref, rec in zip(refs, results):
        if rec:
            items.append({**{k: rec.get(k) for k in ("id", "name", "author", "description", "minecraft", "loader",
                                                     "loader_version", "updated")},
                          "mods": len(rec.get("mods", [])), "missing": len(rec.get("missing", [])),
                          "files": bool(rec.get("files")), "sha": ref["sha"]})
    return sorted(items, key=lambda x: x.get("updated") or "", reverse=True)


def _safe_fetch(ref):
    try:
        return fetch(ref)
    except (requests.RequestException, ValueError):
        return None


def check(recipe: dict) -> None:
    # Рецепт уже прошёл проверку автора лаунчера, но ставим только то, что точно безопасно
    for m in recipe.get("mods", []):
        if not PATH_RE.match(str(m.get("path"))) or not str(m.get("url", "")).startswith(MODRINTH_CDN) \
                or not re.fullmatch(r"[0-9a-f]{40}", str(m.get("sha1"))):
            raise RuntimeError(f"В рецепте странный файл: {m.get('path')}")
    if recipe.get("loader") not in custom_packs.LOADER_NAMES:
        raise RuntimeError("В рецепте неизвестный загрузчик.")


def install(ref: dict, mc_dir: Path, status, progress) -> tuple:
    """Ставит сборку по рецепту как «Свою сборку». Возвращает (результат custom_packs.install, рецепт)."""
    status("Скачиваю рецепт…")
    recipe = fetch(ref)
    check(recipe)
    tmp = Path(tempfile.mkdtemp(prefix="mff-recipe-"))
    try:
        archive = tmp / "files.zip"
        if recipe.get("files"):
            status("Скачиваю конфиги…")
            r = custom_packs.web.get(f"{RAW}{recipe['id']}/files.zip", params={"v": ref["sha"][:12]}, timeout=60)
            r.raise_for_status()
            archive.write_bytes(r.content)
            with zipfile.ZipFile(archive) as z:
                for n in z.namelist():
                    parts = Path(n).parts
                    if not parts or parts[0] not in CONFIG_DIRS or ".." in parts or Path(n).is_absolute():
                        raise RuntimeError(f"В архиве конфигов лишний файл: {n}")
        else:
            zipfile.ZipFile(archive, "w").close()

        # Обновление: моды, которых больше нет в рецепте, убираем (свои заметки игрока не трогаем)
        # Имя с автором: своя сборка игрока с тем же названием останется нетронутой
        title = f"{recipe['name']} ({recipe.get('author') or 'друг'})"
        version_id, game_dir = custom_packs.free_place(custom_packs.safe_name(title), mc_dir)
        keep = {Path(m["path"]).name for m in recipe["mods"] if m["path"].startswith("mods/")}
        for jar in (game_dir / "mods").glob("*.jar") if (game_dir / "mods").is_dir() else []:
            if jar.name not in keep and custom_packs.SKIN_LOADER not in jar.name.lower():
                jar.unlink()

        plan = {"source": str(archive), "name": title, "format": "recipe", "minecraft": recipe["minecraft"],
                "loader": recipe["loader"], "loader_version": recipe.get("loader_version") or None,
                "copy_base": [""], "notes": [], "mods": len(recipe["mods"]),
                "downloads": [{"path": m["path"], "url": m["url"], "sha1": m["sha1"], "size": m.get("size", 0)}
                              for m in recipe["mods"]]}
        result = custom_packs.install(plan, mc_dir, status, progress)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    if recipe.get("missing"):
        result["notes"].insert(0, "Автор сборки добавил моды не с Modrinth, их тут нет: "
                               + ", ".join(Path(x).name for x in recipe["missing"][:6])
                               + (" и др." if len(recipe["missing"]) > 6 else ""))
    return result, recipe
