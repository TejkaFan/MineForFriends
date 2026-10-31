"""Свои сборки игрока: папка или zip → версия в TLauncher.

Лаунчер разбирает сборку (CurseForge, Modrinth .mrpack, MultiMC/Prism или просто
папка с модами), определяет версию Minecraft и загрузчик, докачивает с официальных
сайтов то, чего не хватает (моды из манифеста, зависимости модов с Modrinth, сам
загрузчик) и создаёт в TLauncher версию со своей папкой игры.
"""
import hashlib
import io
import json
import os
import re
import shutil
import subprocess
import tempfile
import tomllib
import zipfile
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path, PurePosixPath

import requests

LOADERS = ("vanilla", "fabric", "quilt", "forge", "neoforge")
LOADER_NAMES = {"vanilla": "Без модов", "fabric": "Fabric", "quilt": "Quilt", "forge": "Forge", "neoforge": "NeoForge"}

MOJANG_MANIFEST = "https://piston-meta.mojang.com/mc/game/version_manifest_v2.json"
FABRIC_META = "https://meta.fabricmc.net/v2/versions/loader"
QUILT_META = "https://meta.quiltmc.org/v3/versions/loader"
FORGE_PROMOS = "https://files.minecraftforge.net/net/minecraftforge/forge/promotions_slim.json"
FORGE_MAVEN = "https://maven.minecraftforge.net/net/minecraftforge/forge"
NEOFORGE_MAVEN = "https://maven.neoforged.net/releases/net/neoforged/neoforge"
MODRINTH = "https://api.modrinth.com/v2"
# Проект на Modrinth мода, который показывает скины игроков с нашего GitHub
SKIN_LOADER = "customskinloader"
CURSEFORGE_FILE = "https://www.curseforge.com/api/v1/mods/{project}/files/{file}/download"

# Что не переносим из сборки в папку игры: служебные файлы лаунчеров, логи, кэши
SKIP_TOP = {
    ".fabric", ".mixin.out", ".replay_cache", ".curseclient", ".cache", "logs", "crash-reports",
    "downloads", "replay_recordings", "replay_videos", "screenshots", "versions", "libraries",
    "assets", "natives", "manifest.json", "minecraftinstance.json", "modrinth.index.json",
    "mmc-pack.json", "instance.cfg", "modlist.html", "export.log", "usercache.json",
    "usernamecache.json", "launcher_state.json", "launcher_profiles.json", "tlauncherprofiles.json",
}
# Метки, по которым ищем корень сборки внутри папки или архива
MARKERS = ("manifest.json", "minecraftinstance.json", "modrinth.index.json", "mmc-pack.json", "mods")
# Эти «зависимости» даёт сам загрузчик или игра
BUILTIN = {"minecraft", "java", "fabricloader", "fabric-loader", "quilt_loader", "quilt_base",
           "forge", "neoforge", "fml", "javafml", "lowcodefml", "mixinextras"}
MC_TOKEN = re.compile(r"(?<![\d.])(1\.\d{1,2}(?:\.\d{1,2})?|[2-9]\d\.\d{1,2}(?:\.\d{1,2})?)(?![\d.])")

web = requests.Session()
web.headers["User-Agent"] = "MineForFriends-launcher (github.com/TejkaFan)"


def get_json(url: str, **kw):
    r = web.get(url, timeout=30, **kw)
    r.raise_for_status()
    return r.json()


def safe_name(text: str) -> str:
    # Имя версии TLauncher — это имя папки, убираем запрещённые в Windows символы
    return re.sub(r'[\\/:*?"<>|]+', " ", text).strip(" .") or "Сборка"


TRANSLIT = dict(zip("абвгдеёжзийклмнопрстуфхцчшщъыьэюя",
                    "a b v g d e e zh z i y k l m n o p r s t u f h ts ch sh sch - y - e yu ya".split()))


def slug(text: str) -> str:
    text = "".join(TRANSLIT.get(c, c) for c in text.lower())
    return re.sub(r"[^a-z0-9_]+", "-", text).strip("-") or "pack"


def mc_hints(spec: str) -> list:
    """Версии игры из условия зависимости с весом: точные важнее нижних границ, верхние не считаем."""
    hints = []
    for m in re.finditer(r"([<>=~^]*)\s*" + MC_TOKEN.pattern, spec):
        op, version = m.group(1), m.group(2)
        if "<" in op:
            continue
        hints += [version] * (1 if ">" in op else 3)
    return hints


# ---------- Источник: папка или zip ----------

class Source:
    """Папка или архив сборки с единым доступом к файлам по относительным путям."""

    def __init__(self, path: Path):
        self.path = path
        self.zip = zipfile.ZipFile(path) if path.is_file() else None
        if self.zip:
            self.files = {n: n for n in self.zip.namelist() if not n.endswith("/")}
        else:
            self.files = {p.relative_to(path).as_posix(): p for p in path.rglob("*") if p.is_file()}
        self.root = self.find_root()

    def find_root(self) -> str:
        # Сборку часто кладут во вложенную папку: ищем самый верхний уровень с метками
        candidates = Counter()
        for rel in self.files:
            parts = PurePosixPath(rel).parts
            for i, part in enumerate(parts):
                if part.lower() in MARKERS and (part.lower() == "mods" or i == len(parts) - 1):
                    candidates["/".join(parts[:i])] += 1
                    break
        if not candidates:
            return ""
        return min(candidates, key=lambda r: (r.count("/") if r else -1, r))

    def rel(self, name: str) -> str:
        return f"{self.root}/{name}" if self.root else name

    def exists(self, name: str) -> bool:
        return self.rel(name) in self.files

    def read(self, name: str) -> bytes:
        item = self.files[self.rel(name)]
        return self.zip.read(item) if self.zip else item.read_bytes()

    def json(self, name: str):
        try:
            return json.loads(self.read(name).decode("utf-8-sig"))
        except (KeyError, ValueError):
            return None

    def listing(self, base: str = ""):
        """(путь внутри base, полный ключ) для всех файлов под root/base."""
        prefix = self.rel(base).rstrip("/") + "/" if self.rel(base) else ""
        for rel, item in self.files.items():
            if rel.startswith(prefix):
                yield rel[len(prefix):], item

    def size(self, item) -> int:
        return self.zip.getinfo(item).file_size if self.zip else item.stat().st_size

    def copy(self, item, dest: Path) -> None:
        dest.parent.mkdir(parents=True, exist_ok=True)
        if self.zip:
            with self.zip.open(item) as src, dest.open("wb") as out:
                shutil.copyfileobj(src, out, 1 << 20)
        else:
            shutil.copyfile(item, dest)


# ---------- Разбор модов ----------

def mod_meta(data: bytes, depth: int = 0) -> dict:
    """Что известно о моде из его jar: загрузчик, id, зависимости, намёки на версию игры."""
    # mc — намёки на версию игры, specs — условия «minecraft» из метаданных для проверки версий
    info = {"loader": None, "ids": set(), "requires": set(), "mc": [], "specs": [], "nested": []}
    try:
        jar = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile:
        return info
    names = set(jar.namelist())

    def read_json(name):
        try:
            return json.loads(jar.read(name).decode("utf-8-sig"), strict=False)
        except (KeyError, ValueError, UnicodeDecodeError):
            return None

    # Многие jar несут метаданные сразу для нескольких загрузчиков: Fabric важнее Quilt, оба — Forge
    if "fabric.mod.json" not in names and "quilt.mod.json" in names and (q := read_json("quilt.mod.json")):
        loader = q.get("quilt_loader", {})
        info["loader"] = "quilt"
        info["ids"].add(loader.get("id", ""))
        info["ids"].update(p if isinstance(p, str) else p.get("id", "") for p in loader.get("provides", []))
        for dep in loader.get("depends", []):
            dep_id = dep if isinstance(dep, str) else dep.get("id", "")
            if isinstance(dep, dict) and (dep.get("optional") or dep.get("unless")):
                continue  # «unless» — нужен, только если нет замены; не гадаем
            if dep_id == "minecraft" and isinstance(dep, dict):
                info["mc"] += mc_hints(json.dumps(dep.get("versions", "")))
                info["specs"].append(("semver", dep.get("versions", "*")))
            info["requires"].add(dep_id)
    if "fabric.mod.json" in names and (f := read_json("fabric.mod.json")):
        info["loader"] = info["loader"] or "fabric"
        info["ids"].add(f.get("id", ""))
        info["ids"].update(f.get("provides", []))
        for dep_id, spec in (f.get("depends") or {}).items():
            if dep_id == "minecraft":
                info["mc"] += mc_hints(json.dumps(spec))
                info["specs"].append(("semver", spec))
            info["requires"].add(dep_id)
        info["nested"] += [j.get("file") for j in f.get("jars", []) if isinstance(j, dict)]
    for toml_name, loader in (("META-INF/neoforge.mods.toml", "neoforge"), ("META-INF/mods.toml", "forge")):
        if toml_name not in names or info["loader"]:
            continue
        try:
            t = tomllib.loads(jar.read(toml_name).decode("utf-8", "replace"))
        except (tomllib.TOMLDecodeError, ValueError):
            continue
        mods = [m.get("modId", "") for m in t.get("mods", []) if isinstance(m, dict)]
        info["ids"].update(mods)
        for deps in (t.get("dependencies") or {}).values():
            for dep in deps if isinstance(deps, list) else []:
                dep_id = dep.get("modId", "")
                if dep_id == "neoforge":
                    loader = "neoforge"
                if dep_id == "minecraft":
                    # Диапазон Maven «[1.20.1,1.21)»: первая версия — нижняя граница
                    found = MC_TOKEN.findall(str(dep.get("versionRange", "")))
                    info["mc"] += found[:1] * (3 if len(found) == 1 else 1)
                    info["specs"].append(("maven", str(dep.get("versionRange", ""))))
                required = dep.get("mandatory", dep.get("type", "required") == "required")
                if required is True or required == "required":
                    info["requires"].add(dep_id)
        info["loader"] = info["loader"] or loader
        if "META-INF/jarjar/metadata.json" in names and (jj := read_json("META-INF/jarjar/metadata.json")):
            info["nested"] += [j.get("path") for j in jj.get("jars", []) if isinstance(j, dict)]
    # Вложенные библиотеки тоже дают id (например, модули Fabric API)
    if depth < 2:
        for name in info["nested"]:
            if name in names:
                inner = mod_meta(jar.read(name), depth + 1)
                info["ids"] |= inner["ids"]
    info["ids"].discard("")
    info["requires"].discard("")
    return info


# ---------- Анализ ----------

def vtuple(text: str):
    nums = re.match(r"\d+(?:\.\d+)*", text.strip())
    return tuple(int(x) for x in nums.group().split(".")) if nums else None


def semver_ok(spec, version: tuple) -> bool:
    """Условие Fabric/Quilt: строка (пробел — «и»), список (любой из), «*», «1.20.x», «~1.20.1»."""
    if isinstance(spec, list):
        return not spec or any(semver_ok(s, version) for s in spec)
    if isinstance(spec, dict):  # Quilt: {"any": [...]} / {"all": [...]}
        if "all" in spec:
            return all(semver_ok(s, version) for s in spec["all"])
        return semver_ok(spec.get("any", "*"), version)
    for term in str(spec).split():
        m = re.match(r"(>=|<=|>|<|=|~|\^)?(.+)", term)
        op, raw = m.group(1) or "=", m.group(2)
        if raw in ("*", "x", "X"):
            continue
        if re.search(r"\.[xX*]$", raw):  # «1.20.x» — любая из ветки
            prefix = vtuple(raw[:-2])
            if prefix is not None and version[:len(prefix)] != prefix:
                return False
            continue
        target = vtuple(raw)
        if target is None:
            continue
        pad = max(len(target), len(version))
        v, t = version + (0,) * (pad - len(version)), target + (0,) * (pad - len(target))
        ok = {"=": v == t, ">=": v >= t, ">": v > t, "<=": v <= t, "<": v < t,
              "~": v >= t and v[:2] == t[:2], "^": v >= t and v[:1] == t[:1]}[op]
        if not ok:
            return False
    return True


def maven_ok(spec: str, version: tuple) -> bool:
    """Диапазон Forge «[1.20.1,1.21)», «[1.20.1]», «[1.20,)»; голая версия — без ограничений."""
    ranges = re.findall(r"([\[(])([^\[\]()]*)([\])])", spec)
    if not ranges:
        return True
    for lo_br, body, hi_br in ranges:
        lo, _, hi = body.partition(",") if "," in body else (body, None, body)
        lo_t, hi_t = vtuple(lo) if lo.strip() else None, vtuple(hi) if hi and hi.strip() else None
        if lo_t and (version < lo_t or (lo_br == "(" and version == lo_t)):
            continue
        if hi_t and (version > hi_t or (hi_br == ")" and version == hi_t)):
            continue
        return True
    return False


def spec_ok(kind: str, spec, version: str) -> bool:
    v = vtuple(version)
    if v is None:
        return True
    try:
        return semver_ok(spec, v) if kind == "semver" else maven_ok(spec, v)
    except (KeyError, AttributeError, TypeError):
        return True  # непонятное условие версию не исключает


def fit_count(mod_specs: list, version: str) -> int:
    """Сколько модов (по их условиям «minecraft») запустятся на этой версии."""
    return sum(all(spec_ok(k, s, version) for k, s in specs) for specs in mod_specs)


def pick_version(metas: list, votes: Counter, releases: list):
    """Версия, на которой запустится больше всего модов; при равенстве — та, что чаще
    упоминается в модах, затем новее. Возвращает (версия, подходит модов, модов с условием)."""
    with_specs = [m for m in metas if m["specs"]]
    if not releases:
        return (votes.most_common(1)[0][0] if votes else None), 0, len(with_specs)
    best = None
    for rank, version in enumerate(releases):  # releases — от новых к старым
        fits = fit_count([m["specs"] for m in with_specs], version)
        key = (fits, votes.get(version, 0), -rank)
        if best is None or key > best[0]:
            best = (key, version, fits)
    return best[1], best[2], len(with_specs)


_releases = []


def release_versions() -> list:
    """Релизы Minecraft от новых к старым (список Mojang запрашиваем один раз)."""
    if not _releases:
        try:
            _releases.extend(v["id"] for v in get_json(MOJANG_MANIFEST)["versions"] if v["type"] == "release")
        except (requests.RequestException, ValueError, KeyError):
            pass
    return list(_releases)


def parse_loader(text: str):
    """«fabric-0.18.4», «forge-47.2.0», «neoforge-21.1.1», «fabric-0.18.4-1.20.1» → (loader, версия)."""
    m = re.match(r"(fabric|quilt|neoforge|forge)-(.+)", text or "")
    if not m:
        return None, None
    loader, version = m.groups()
    if loader in ("fabric", "quilt"):
        version = re.sub(r"-\d+\.\d+(\.\d+)?$", "", version)  # хвост с версией игры
    elif loader == "forge" and re.match(r"\d+\.\d+(\.\d+)?-", version):
        version = version.split("-", 1)[1]  # «1.20.1-47.2.0» → «47.2.0»
    return loader, version


def analyze(path: Path, status=print) -> dict:
    """Разбирает сборку; ничего не пишет на диск. Результат идёт в окно подтверждения и в install()."""
    status("Разбираю сборку…")
    src = Source(path)
    result = {"source": str(path), "name": path.stem if path.is_file() else path.name, "format": "folder",
              "minecraft": None, "loader": None, "loader_version": None,
              "copy_base": "", "downloads": [], "notes": [], "mods": 0}

    cf = src.json("manifest.json")
    cf = cf if isinstance(cf, dict) and cf.get("manifestType") == "minecraftModpack" else None
    cfi = src.json("minecraftinstance.json")
    mr = src.json("modrinth.index.json")
    mmc = src.json("mmc-pack.json")

    if mr:
        result["format"] = "modrinth"
        result["name"] = mr.get("name") or result["name"]
        deps = mr.get("dependencies", {})
        result["minecraft"] = deps.get("minecraft")
        for key, loader in (("fabric-loader", "fabric"), ("quilt-loader", "quilt"), ("neoforge", "neoforge"), ("forge", "forge")):
            if key in deps:
                result["loader"], result["loader_version"] = loader, deps[key]
        for f in mr.get("files", []):
            if (f.get("env") or {}).get("client") == "unsupported" or not f.get("downloads"):
                continue
            result["downloads"].append({"path": f["path"], "url": f["downloads"][0],
                                        "sha1": f.get("hashes", {}).get("sha1"), "size": f.get("fileSize", 0)})
        result["copy_base"] = ["overrides", "client-overrides"]
    elif cf or cfi:
        result["format"] = "curseforge"
        if cf:
            result["name"] = cf.get("name") or result["name"]
            result["minecraft"] = cf.get("minecraft", {}).get("version")
            loaders = cf.get("minecraft", {}).get("modLoaders", [])
            primary = next((l for l in loaders if l.get("primary")), loaders[0] if loaders else {})
            result["loader"], result["loader_version"] = parse_loader(primary.get("id", ""))
        if cfi:
            result["name"] = cfi.get("name") or result["name"]
            result["minecraft"] = result["minecraft"] or cfi.get("gameVersion")
            base = cfi.get("baseModLoader") or {}
            if not result["loader"] and base.get("name"):
                result["loader"], result["loader_version"] = parse_loader(base["name"])
        # Установленная сборка CurseForge: файлы уже на месте, архив-экспорт: всё в overrides
        overrides = (cf or {}).get("overrides", "overrides")
        exported = any(True for _ in src.listing(overrides))
        result["copy_base"] = [overrides] if exported else [""]
        # Папка файла по классу проекта CurseForge: моды, ресурспаки, шейдеры
        folders = {6: "mods", 12: "resourcepacks", 6552: "shaderpacks"}
        known = {}
        for addon in (cfi or {}).get("installedAddons", []):
            f = addon.get("installedFile") or {}
            if f.get("id"):
                known[f["id"]] = (f, folders.get(addon.get("categoryClassID"), "mods"))
        for f in (cf or {}).get("files", []):
            if not f.get("required", True):
                continue
            info, folder = known.get(f.get("fileID"), ({}, "mods"))
            name = info.get("fileName")
            if name and any(src.exists(f"{base}{folder}/{name}{off}") for base in ("", f"{overrides}/") for off in ("", ".disabled")):
                continue  # уже в сборке (или выключен игроком в CurseForge)
            url = info.get("downloadUrl") or CURSEFORGE_FILE.format(project=f["projectID"], file=f["fileID"])
            result["downloads"].append({"path": f"{folder}/{name}" if name else None, "url": url, "sha1": None,
                                        "size": info.get("fileLength", 0)})
    elif mmc:
        result["format"] = "multimc"
        for c in mmc.get("components", []):
            uid, version = c.get("uid"), c.get("version")
            if uid == "net.minecraft":
                result["minecraft"] = version
            loader = {"net.fabricmc.fabric-loader": "fabric", "org.quiltmc.quilt-loader": "quilt",
                      "net.minecraftforge": "forge", "net.neoforged": "neoforge"}.get(uid)
            if loader:
                result["loader"], result["loader_version"] = loader, version
        game = ".minecraft" if any(True for _ in src.listing(".minecraft")) else "minecraft"
        result["copy_base"] = [game]
    else:
        result["copy_base"] = [""]

    # Моды сборки: загрузчик, версия игры и недостающие зависимости
    status("Проверяю моды…")
    metas, votes_loader, votes_mc = [], Counter(), Counter()
    for base in result["copy_base"]:
        prefix = f"{base}/mods" if base else "mods"
        for rel, item in list(src.listing(prefix)):
            if "/" in rel or not rel.endswith(".jar"):
                continue
            data = src.zip.read(item) if src.zip else item.read_bytes()
            meta = mod_meta(data)
            metas.append(meta)
            if meta["loader"]:
                votes_loader[meta["loader"]] += 1
            votes_mc.update(meta["mc"])
            # Версия в имени файла («…-1.20.1.jar») — почти всегда точная
            for v in MC_TOKEN.findall(rel):
                votes_mc[v] += 2
    result["mods"] = len(metas) + sum(1 for d in result["downloads"] if (d["path"] or "mods/").startswith("mods/"))

    releases = release_versions()
    if result["minecraft"]:
        result["mc_source"] = {"curseforge": "из описания сборки CurseForge", "modrinth": "из описания сборки Modrinth",
                               "multimc": "из описания сборки MultiMC"}.get(result["format"], "из описания сборки")
    elif metas:
        version, fits, total = pick_version(metas, votes_mc, releases)
        result["minecraft"] = version
        result["mc_source"] = (f"по модам: подходит {fits} из {total}" if total
                               else "по названиям модов — проверь её")
        if total and fits < total:
            result["notes"].append(f"Ни на одной версии не подходят все моды ({fits} из {total}).")
    else:
        result["minecraft"] = releases[0] if releases else None
        result["mc_source"] = "модов нет — взята последняя версия"
    if not result["loader"]:
        result["loader"] = votes_loader.most_common(1)[0][0] if votes_loader else "vanilla"

    result["missing"] = missing_deps(metas)
    result["releases"] = releases  # для ручного выбора версии
    result["mod_specs"] = [m["specs"] for m in metas if m["specs"]]
    return result


def missing_deps(metas: list) -> dict:
    """{id недостающего мода: кому он нужен}."""
    provided = set(BUILTIN)
    for meta in metas:
        provided |= meta["ids"]
    missing = {}
    for meta in metas:
        for dep in meta["requires"] - provided:
            missing.setdefault(dep, sorted(meta["ids"])[0] if meta["ids"] else "?")
    return missing


# ---------- Загрузчики ----------

def latest_loader(loader: str, mc: str) -> str:
    if loader == "fabric":
        versions = get_json(f"{FABRIC_META}/{mc}")
        return next((v["loader"]["version"] for v in versions if v["loader"].get("stable")), versions[0]["loader"]["version"])
    if loader == "quilt":
        return get_json(f"{QUILT_META}/{mc}")[0]["loader"]["version"]
    if loader == "forge":
        promos = get_json(FORGE_PROMOS)["promos"]
        version = promos.get(f"{mc}-recommended") or promos.get(f"{mc}-latest")
        if not version:
            raise RuntimeError(f"Forge для Minecraft {mc} не найден.")
        return version
    if loader == "neoforge":
        xml = web.get(f"{NEOFORGE_MAVEN}/maven-metadata.xml", timeout=30).text
        parts = mc.split(".")
        prefix = f"{parts[1]}.{parts[2] if len(parts) > 2 else 0}." if parts[0] == "1" else f"{mc}."
        versions = [v for v in re.findall(r"<version>([^<]+)</version>", xml) if v.startswith(prefix)]
        stable = [v for v in versions if "beta" not in v and "alpha" not in v]
        if not versions:
            raise RuntimeError(f"NeoForge для Minecraft {mc} не найден.")
        return (stable or versions)[-1]
    return ""


def loader_versions(loader: str, mc: str) -> list:
    """Все версии загрузчика под эту версию игры, от новых к старым (для ручного выбора)."""
    if loader in ("fabric", "quilt"):
        meta = FABRIC_META if loader == "fabric" else QUILT_META
        return [v["loader"]["version"] for v in get_json(f"{meta}/{mc}")]
    if loader == "forge":
        xml = web.get(f"{FORGE_MAVEN}/maven-metadata.xml", timeout=30).text
        return [v.split("-", 1)[1] for v in reversed(re.findall(r"<version>([^<]+)</version>", xml)) if v.startswith(f"{mc}-")]
    if loader == "neoforge":
        xml = web.get(f"{NEOFORGE_MAVEN}/maven-metadata.xml", timeout=30).text
        parts = mc.split(".")
        prefix = f"{parts[1]}.{parts[2] if len(parts) > 2 else 0}." if parts[0] == "1" else f"{mc}."
        return [v for v in reversed(re.findall(r"<version>([^<]+)</version>", xml)) if v.startswith(prefix)]
    return []


def java_candidates(mc_dir: Path) -> list:
    """Все найденные java.exe: своя у TLauncher, рантаймы игры, системные."""
    appdata = Path(os.environ.get("APPDATA", Path.home()))
    candidates = []
    if os.environ.get("JAVA_HOME"):
        candidates.append(Path(os.environ["JAVA_HOME"]) / "bin" / "java.exe")
    candidates += sorted((appdata / ".tlauncher").glob("starter/jre_default/*/bin/java.exe"), reverse=True)
    candidates += sorted(mc_dir.glob("runtime/*/windows*/*/bin/java.exe"), reverse=True)
    for base in (os.environ.get("ProgramFiles", r"C:\Program Files"),):
        for vendor in ("Eclipse Adoptium", "Java", "Microsoft", "Zulu", "BellSoft"):
            candidates += sorted(Path(base, vendor).glob("*/bin/java.exe"), reverse=True)
    if which := shutil.which("java"):
        candidates.append(Path(which))
    seen, result = set(), []
    for c in candidates:
        key = str(c).lower()
        if key not in seen and c.exists():
            seen.add(key)
            result.append(c)
    return result


def java_major(java: Path):
    """Основная версия Java (8, 17, 21, 25…) по файлу release рядом с ней, иначе по имени папки."""
    home = java.parent.parent
    try:
        text = (home / "release").read_text("utf-8", errors="ignore")
        version = re.search(r'JAVA_VERSION="([^"]+)"', text).group(1)
    except (OSError, AttributeError):
        m = re.search(r"(?:jdk|jre)-?(\d+(?:\.\d+)*)", str(home).lower())
        if not m:
            return None
        version = m.group(1)
    parts = version.split(".")
    return int(parts[1]) if parts[0] == "1" and len(parts) > 1 else int(parts[0])


def java_full(java: Path) -> bool:
    """Урезанные Java (как у самого TLauncher) без jdk.zipfs не запускают Fabric и Forge."""
    try:
        text = (java.parent.parent / "release").read_text("utf-8", errors="ignore")
    except OSError:
        return True
    modules = re.search(r'^MODULES="([^"]*)"', text, re.M)
    return not modules or "jdk.zipfs" in modules.group(1)


def find_java(mc_dir: Path, need: int = 0):
    """Полная Java для установщика или сервера: подходящая по версии, ближайшая к нужной."""
    found = [(java_major(c), c) for c in java_candidates(mc_dir) if java_full(c)]
    if not need:
        return found[0][1] if found else None
    fit = sorted((v, str(c), c) for v, c in found if v and v >= need)
    return fit[0][2] if fit else None


def run_forge_installer(loader: str, mc: str, version: str, mc_dir: Path, status) -> str:
    """Ставит Forge/NeoForge официальным установщиком в папку TLauncher; возвращает id его версии."""
    java = find_java(mc_dir)
    if not java:
        raise RuntimeError("Для Forge нужна Java. Запусти любую версию игры в TLauncher один раз и попробуй снова.")
    if loader == "forge":
        full = f"{mc}-{version}"
        url = f"{FORGE_MAVEN}/{full}/forge-{full}-installer.jar"
        version_id = f"{mc}-forge-{version}"
    else:
        url = f"{NEOFORGE_MAVEN}/{version}/neoforge-{version}-installer.jar"
        version_id = f"neoforge-{version}"
    if (mc_dir / "versions" / version_id / f"{version_id}.json").exists():
        return version_id

    profiles = mc_dir / "launcher_profiles.json"
    if not profiles.exists():
        # Установщик Forge отказывается ставить без этого файла официального лаунчера
        profiles.write_text(json.dumps({"profiles": {}}), "utf-8")
    status(f"Скачиваю установщик {LOADER_NAMES[loader]} {version}…")
    with tempfile.TemporaryDirectory() as tmp:
        installer = Path(tmp) / "installer.jar"
        r = web.get(url, timeout=60)
        r.raise_for_status()
        installer.write_bytes(r.content)
        status(f"Ставлю {LOADER_NAMES[loader]} {version} (может занять пару минут)…")
        flags = 0x08000000 if os.name == "nt" else 0  # без чёрного окна консоли
        log = ""
        for option in ("--installClient", "--install-client"):
            p = subprocess.run([str(java), "-jar", str(installer), option, str(mc_dir)], cwd=tmp,
                               capture_output=True, text=True, errors="replace", creationflags=flags)
            log = p.stdout + p.stderr
            if p.returncode == 0 and (mc_dir / "versions" / version_id).exists():
                return version_id
            if "not a recognized option" not in log and "UnrecognizedOption" not in log:
                break
    tail = "\n".join(log.strip().splitlines()[-8:])
    raise RuntimeError(f"Установщик {LOADER_NAMES[loader]} завершился с ошибкой:\n\n{tail}")


def build_version(version_id: str, mc: str, loader: str, loader_version: str, game_dir: Path, mc_dir: Path, status) -> dict:
    """Самостоятельная версия TLauncher: ванилла + профиль загрузчика, своя папка игры."""
    status(f"Готовлю версию {version_id}…")
    entry = next((v for v in get_json(MOJANG_MANIFEST)["versions"] if v["id"] == mc), None)
    if not entry:
        raise RuntimeError(f"Версия Minecraft {mc} не найдена у Mojang.")
    vanilla = get_json(entry["url"])
    if loader == "fabric":
        profile = get_json(f"{FABRIC_META}/{mc}/{loader_version}/profile/json")
    elif loader == "quilt":
        profile = get_json(f"{QUILT_META}/{mc}/{loader_version}/profile/json")
    elif loader in ("forge", "neoforge"):
        installed = run_forge_installer(loader, mc, loader_version, mc_dir, status)
        profile = json.loads((mc_dir / "versions" / installed / f"{installed}.json").read_text("utf-8"))
    else:
        profile = {}

    version = dict(vanilla)
    version.update(id=version_id, type="modified")
    version.pop("inheritsFrom", None)
    if profile.get("mainClass"):
        version["mainClass"] = profile["mainClass"]
    version["libraries"] = profile.get("libraries", []) + vanilla["libraries"]
    if "arguments" in vanilla:
        game = [str(game_dir) if a == "${game_directory}" else a for a in vanilla["arguments"]["game"]]
        version["arguments"] = {
            "game": game + profile.get("arguments", {}).get("game", []),
            "jvm": vanilla["arguments"].get("jvm", []) + profile.get("arguments", {}).get("jvm", []),
        }
    else:
        # Старые версии (до 1.13): аргументы одной строкой
        args = profile.get("minecraftArguments") or vanilla["minecraftArguments"]
        version["minecraftArguments"] = args.replace("${game_directory}", f'"{game_dir}"')
    return version


# ---------- Установка ----------

def download_file(url: str, dest: Path, expected_sha1=None, into_folder=False) -> Path:
    with web.get(url, stream=True, timeout=60) as r:
        r.raise_for_status()
        if into_folder:
            # Имя файла с CurseForge узнаём только после переадресации
            name = requests.utils.unquote(PurePosixPath(r.url.split("?")[0]).name)
            # Без описания сборки zip почти всегда ресурспак, а не мод
            dest = (dest.parent / "resourcepacks" if name.endswith(".zip") else dest) / name
        dest.parent.mkdir(parents=True, exist_ok=True)
        tmp = dest.with_name(dest.name + ".part")
        h = hashlib.sha1()
        with tmp.open("wb") as f:
            for chunk in r.iter_content(1 << 16):
                f.write(chunk)
                h.update(chunk)
    if expected_sha1 and h.hexdigest() != expected_sha1:
        tmp.unlink()
        raise RuntimeError(f"{dest.name} скачался с ошибкой")
    tmp.replace(dest)
    return dest


def modrinth_file(project: str, loader: str, mc: str):
    """Подходящий файл мода с Modrinth: (url, имя, sha1, обязательные зависимости) или None."""
    loaders = [loader] + (["fabric"] if loader == "quilt" else [])
    try:
        versions = get_json(f"{MODRINTH}/project/{project}/version",
                            params={"loaders": json.dumps(loaders), "game_versions": json.dumps([mc])})
    except requests.RequestException:
        return None
    if not versions:
        return None
    v = versions[0]
    f = next((x for x in v["files"] if x.get("primary")), v["files"][0])
    deps = [d["project_id"] for d in v.get("dependencies", []) if d.get("dependency_type") == "required" and d.get("project_id")]
    return f["url"], f["filename"], f["hashes"].get("sha1"), deps


def ensure_skin_loader(game_dir: Path, loader: str, mc: str, status):
    """Докачивает мод CustomSkinLoader, если его нет в сборке: через него видны скины игроков.

    Возвращает текст для заметок, если поставить не вышло, иначе None.
    """
    mods = game_dir / "mods"
    if any(SKIN_LOADER in p.name.lower() for p in mods.glob("*.jar")):
        return None
    status("Скачиваю мод скинов CustomSkinLoader…")
    found = modrinth_file(SKIN_LOADER, loader, mc)
    if not found:
        return f"CustomSkinLoader для Minecraft {mc} не нашёлся — скины игроков в этой сборке видны не будут."
    url, name, sha, _ = found
    try:
        download_file(url, mods / name, sha)
    except (requests.RequestException, RuntimeError, OSError):
        return "CustomSkinLoader не скачался — скины игроков в этой сборке видны не будут. Переустанови сборку позже."
    return None


def resolve_missing(missing: dict, loader: str, mc: str, status) -> tuple:
    """Ищет недостающие зависимости на Modrinth; возвращает (загрузки, не найденные)."""
    downloads, not_found, seen = [], [], set()
    queue = list(missing.items())
    while queue:
        dep, needed_by = queue.pop(0)
        project = "fabric-api" if dep == "fabric" or re.fullmatch(r"fabric(-[a-z-]+)?-v\d+|fabric-api", dep) else dep
        if project in seen:
            continue
        seen.add(project)
        status(f"Ищу мод {project} на Modrinth…")
        # id мода обычно совпадает с адресом проекта, иногда с дефисами вместо _
        found = modrinth_file(project, loader, mc) or (
            modrinth_file(project.replace("_", "-"), loader, mc) if "_" in project else None)
        if not found:
            not_found.append(f"{dep} (нужен для {needed_by})")
            continue
        url, name, sha, deps = found
        downloads.append({"path": f"mods/{name}", "url": url, "sha1": sha, "size": 0})
        queue += [(d, project) for d in deps]
    return downloads, not_found


def install(plan: dict, mc_dir: Path, status, progress) -> dict:
    """Ставит разобранную сборку. plan — результат analyze() с правками игрока (name, minecraft, loader, loader_version)."""
    mc, loader = plan["minecraft"], plan["loader"]
    if not mc:
        raise RuntimeError("Укажи версию Minecraft.")
    version_id, game_dir = free_place(safe_name(plan["name"]), mc_dir)
    loader_version = plan.get("loader_version") or (latest_loader(loader, mc) if loader != "vanilla" else "")

    # Файлы сборки
    src = Source(Path(plan["source"]))
    items = []
    for base in plan["copy_base"]:
        for rel, item in src.listing(base):
            top = rel.split("/", 1)[0].lower()
            if base == "" and top in SKIP_TOP:
                continue
            if base == "" and plan["format"] in ("modrinth",) and top in ("overrides", "client-overrides"):
                continue
            items.append((rel, item))
    total = sum(src.size(i) for _, i in items) or 1
    done = 0
    game_dir.mkdir(parents=True, exist_ok=True)
    for n, (rel, item) in enumerate(items, 1):
        dest = (game_dir / rel).resolve()
        if game_dir.resolve() not in dest.parents:
            continue
        if n % 50 == 1:
            status(f"Копирую файлы сборки ({n}/{len(items)})…")
        src.copy(item, dest)
        done += src.size(item)
        progress(0.4 * done / total)

    # Недостающие файлы: сначала моды из манифеста, потом зависимости итогового набора модов
    failed = fetch_all(plan["downloads"], game_dir, status, lambda v: progress(0.4 + 0.3 * v))
    not_found = []
    if loader != "vanilla":
        status("Проверяю зависимости модов…")
        metas = [mod_meta(p.read_bytes()) for p in (game_dir / "mods").glob("*.jar")]
        extra, not_found = resolve_missing(missing_deps(metas), loader, mc, status)
        failed += fetch_all(extra, game_dir, status, lambda v: progress(0.7 + 0.1 * v))
        skin_note = ensure_skin_loader(game_dir, loader, mc, status)
    else:
        skin_note = "В сборке без модов скины игроков не видны: для них нужен мод CustomSkinLoader."

    # Версия в TLauncher
    version = build_version(version_id, mc, loader, loader_version, game_dir, mc_dir, status)
    path = mc_dir / "versions" / version_id / f"{version_id}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(version, ensure_ascii=False, indent=2), "utf-8")
    options = game_dir / "options.txt"
    if not options.exists():
        options.write_text("lang:ru_ru\n", "utf-8")
    progress(1)

    notes = []
    if not_found:
        notes.append("Не нашёл на Modrinth: " + ", ".join(not_found[:8]) + (" и др." if len(not_found) > 8 else ""))
    if failed:
        notes.append("Не скачались: " + ", ".join(failed[:8]) + (" и др." if len(failed) > 8 else ""))
    if skin_note:
        notes.append(skin_note)
    return {"name": version_id, "version_id": version_id, "game_dir": str(game_dir), "minecraft": mc,
            "loader": loader, "loader_version": loader_version, "notes": notes}


def free_place(name: str, mc_dir: Path) -> tuple:
    """Имя версии и папка игры, не задевающие чужое: повторная установка той же сборки
    обновляет её на месте, а одноимённая чужая версия или папка получает номер."""
    for n in range(1, 100):
        version_id = name if n == 1 else f"{name} ({n})"
        game_dir = mc_dir / "custom" / (slug(name) if n == 1 else f"{slug(name)}-{n}")
        marker = game_dir / ".mineserc_pack"
        version_json = mc_dir / "versions" / version_id / f"{version_id}.json"
        ours = marker.exists() and marker.read_text("utf-8").strip() == version_id
        dir_free = ours or not game_dir.exists() or not any(game_dir.iterdir())
        version_free = ours or not version_json.exists()
        if dir_free and version_free:
            game_dir.mkdir(parents=True, exist_ok=True)
            marker.write_text(version_id, "utf-8")
            return version_id, game_dir
    raise RuntimeError("Слишком много сборок с таким именем.")


def fetch_all(downloads: list, game_dir: Path, status, progress) -> list:
    """Качает файлы в папку игры по 6 сразу; возвращает имена тех, что не скачались."""
    failed = []
    with ThreadPoolExecutor(6) as pool:
        jobs = {}
        for d in downloads:
            if d["path"]:
                dest = (game_dir / d["path"]).resolve()
                if dest.exists() or game_dir.resolve() not in dest.parents:
                    continue
                jobs[pool.submit(download_file, d["url"], dest, d.get("sha1"))] = d
            else:
                jobs[pool.submit(download_file, d["url"], game_dir / "mods", None, True)] = d
        for n, job in enumerate(as_completed(jobs), 1):
            status(f"Скачиваю файлы ({n}/{len(jobs)})…")
            progress(n / len(jobs))
            try:
                job.result()
            except (requests.RequestException, RuntimeError, OSError):
                failed.append(PurePosixPath(jobs[job]["path"] or jobs[job]["url"]).name)
    return failed


def remove(profile: dict, mc_dir: Path) -> None:
    """Убирает версию из TLauncher; папку игры (миры, настройки) не трогаем."""
    folder = mc_dir / "versions" / profile["version_id"]
    if folder.exists():
        shutil.rmtree(folder)
