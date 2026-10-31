"""Разбор вылетов Minecraft: находит свежий отчёт о вылете и объясняет его по-человечески.

Смотрим в папке игры и в папках сборок (mineserc, custom/<сборка>, friends/<сервер>):
- crash-reports/crash-*.txt — отчёт игры о вылете;
- hs_err_pid*.log — вылет самой Java (чаще всего драйвер видеокарты или память);
- logs/latest.log — только если в нём ошибка загрузки модов (Fabric/Forge не создают crash-report,
  когда не хватает зависимостей).
"""
import re
import time
from pathlib import Path

# Где искать относительно папки игры: в ней самой и на 1–2 уровня глубже (папки сборок)
PATTERNS = [f"{prefix}{name}" for prefix in ("", "*/", "*/*/")
            for name in ("crash-reports/crash-*.txt", "hs_err_pid*.log", "logs/latest.log")]
# Вылеты старше этого не показываем: игрок их уже видел или забыл
MAX_AGE = 3 * 24 * 60 * 60
HEAD_LINES = 250

LOG_MARKERS = ("Incompatible mods found!", "Missing or unsupported mandatory dependencies",
               "Mod loading has failed", "---- Minecraft Crash Report ----")

DESCRIPTIONS = {
    "Initializing game": "запуск игры",
    "Rendering screen": "отрисовка меню",
    "Rendering overlay": "отрисовка экрана загрузки",
    "Unexpected error": "игра",
    "Ticking entity": "обработка существа в мире",
    "Ticking block entity": "обработка блока в мире",
    "Exception in server tick loop": "работа мира",
    "Exception ticking world": "работа мира",
    "Mod loading error has occurred": "загрузка модов",
    "Loading screen debug info": "загрузка игры",
    "Rendering item": "отрисовка предмета",
    "Exception generating new chunk": "генерация мира",
    "Watching Server": "зависание сервера мира",
}

DRIVERS = {"atio6axx": "AMD", "atioglxx": "AMD", "amdxx": "AMD", "nvoglv": "NVIDIA", "nvwgf": "NVIDIA",
           "ig9icd": "Intel", "ig7icd": "Intel", "ig75icd": "Intel", "igxelpicd": "Intel", "ig8icd": "Intel"}


def candidates(mc_dir: Path, since: float) -> list:
    """Файлы вылетов новее since, свежие первыми."""
    found = []
    for pattern in PATTERNS:
        for path in mc_dir.glob(pattern):
            try:
                mtime = path.stat().st_mtime
            except OSError:
                continue
            if mtime <= since or time.time() - mtime > MAX_AGE:
                continue
            if path.name == "latest.log" and not log_failed(path):
                continue
            found.append((mtime, path))
    return [p for _, p in sorted(found, reverse=True)]


def read(path: Path) -> str:
    try:
        return path.read_text("utf-8", errors="replace")
    except OSError:
        return ""


def log_failed(path: Path) -> bool:
    text = read(path)
    return any(m in text for m in LOG_MARKERS)


def pack_name(mc_dir: Path, path: Path, names: dict) -> str:
    """Название сборки по папке, где лежит файл вылета; names — {папка игры: название} от лаунчера."""
    game_dir = path.parent.parent if path.parent.name in ("crash-reports", "logs") else path.parent
    if str(game_dir).lower() in names:
        return names[str(game_dir).lower()]
    marker = game_dir / ".mineserc_pack"
    if marker.exists():
        return read(marker).strip() or game_dir.name
    if game_dir == mc_dir:
        return "Minecraft"
    return game_dir.name


def unique(items):
    return list(dict.fromkeys(i for i in items if i))


def analyze(path: Path, mc_dir: Path, names: dict = None) -> dict:
    """{title, where, hints: [...], mods: [...], file, pack, time, text} — что случилось и что делать."""
    text = read(path)
    hints, mods = [], []
    kind = "java" if path.name.startswith("hs_err") else "log" if path.name == "latest.log" else "report"

    m = re.search(r"^Description: (.+)$", text, re.M)
    where = DESCRIPTIONS.get(m.group(1).strip(), m.group(1).strip()) if m else ""
    exc = re.search(r"^((?:[\w$]+\.)+[\w$]*(?:Exception|Error)[\w$]*)(?:: (.*))?$", text, re.M)
    title = exc.group(1).rsplit(".", 1)[-1] if exc else ""

    # Моды-виновники
    for block in re.findall(r"Suspected Mods?:\s*\n?((?:\s+.+\n?)+?)(?:\n\S|\Z)", text):
        mods += [line.strip().split(",")[0] for line in block.splitlines()
                 if line.strip() and not line.strip().startswith(("Issue tracker", "None"))]
    mods += re.findall(r"provided by '([\w-]+)'", text)
    mods += re.findall(r"Mixin \[([\w.-]+?)\.mixins?[\w.-]*\.json:", text)
    mods += re.findall(r"from mod ([\w-]+)", text)
    mods = unique(m for m in mods if m not in ("minecraft", "java", "fabricloader", "forge", "neoforge"))
    # «Farmer's Delight (farmersdelight)» и «farmersdelight» — один и тот же мод
    mods = [m for m in mods if not any(f"({m})" in other for other in mods)][:5]

    # Нехватка зависимостей (Fabric и Forge пишут это в latest.log)
    missing = re.findall(r"requires (?:version [^ ]+ (?:or later )?of |any version of )?(?:mod )?'?([\w ._-]+?)'?"
                         r"(?: \([\w-]+\))?(?:,| which| but)", text)
    missing += re.findall(r"^\s*- Install ([\w-]+),", text, re.M)
    missing += [f"{a} (нужен моду {b})" for a, b in re.findall(r"Mod ID: '([\w-]+)', Requested by: '([\w-]+)'", text)]
    missing = unique(m.strip() for m in missing)[:6]

    if re.search(r"OutOfMemoryError|Java heap space|GC overhead limit|Out of Memory Error|insufficient memory", text):
        title = title or "OutOfMemoryError"
        hints.append("Игре не хватило памяти. В TLauncher открой настройки и выдели больше памяти: "
                     "для сборок с модами нужно 6–8 ГБ (но не больше половины памяти компьютера).")
    driver = next((name for dll, name in DRIVERS.items()
                   if re.search(rf"\[{dll}\w*\.dll", text, re.I)), None)
    if driver or re.search(r"Pixel format not accelerated|GLFW error 65542|does not appear to support OpenGL"
                           r"|Could not create context|OpenGL 3\.\d+ is required", text):
        hints.append(f"Похоже на проблему с видеодрайвером{f' {driver}' if driver else ''}: "
                     "обнови драйвер видеокарты с сайта производителя и перезагрузи компьютер.")
    if "UnsupportedClassVersionError" in text:
        hints.append("Сборке нужна более новая Java. В TLauncher → Настройки выбери Java 21 "
                     "(для Minecraft 1.20.5 и новее) или Java 17 (1.18–1.20.4).")
    if missing or "Incompatible mods found!" in text or "Missing or unsupported mandatory dependencies" in text:
        title = "не хватает модов"
        hints.append("Не хватает модов или их версии не подходят друг к другу"
                     + (f": {', '.join(missing)}" if missing else "") + ". Нажми «Играть» ещё раз — лаунчер "
                     "докачает сборку; если сборка с сервера друга — попроси хоста перезапустить сервер.")
    elif mods:
        hints.append(f"Скорее всего, вылет из-за мода {', '.join(mods)}. Попробуй убрать его из папки mods "
                     "или обновить; если это наша сборка — отправь отчёт автору.")
    if not hints:
        hints.append("Причину сразу не понять — отправь отчёт автору, он разберётся.")

    if kind == "java":
        title = title or "вылет Java"
    title = title or "вылет"
    lines = text.splitlines()
    excerpt = "\n".join(lines[-HEAD_LINES:] if kind == "log" else lines[:HEAD_LINES])
    return {"title": title, "where": where, "hints": hints, "mods": mods, "file": str(path),
            "pack": pack_name(mc_dir, path, {k.lower(): v for k, v in (names or {}).items()}), "time": time.strftime("%d.%m %H:%M", time.localtime(path.stat().st_mtime)),
            "text": excerpt}
