"""Проверка доступа к сайтам, откуда лаунчер качает и куда отправляет файлы, и к облаку хоста.

Часть провайдеров (особенно в России) не закрывает сайт целиком, а обрывает соединение,
когда передано около 16 КБ: страница открывается, а мод или скин — уже нет. Поэтому каждый
сайт проверяется запросом крупнее этого порога, и результат различает «работает»,
«обрывается» и «недоступен». Проверка идёт напрямую, без обхода через облако хоста.
"""
import time
from concurrent.futures import ThreadPoolExecutor

import requests

import relay

# Сколько байт нужно получить, чтобы считать, что сайт не обрывает связь
ENOUGH = 200 << 10
TIME_LIMIT = 12
UA = {"User-Agent": "MineForFriends-launcher (github.com/TejkaFan)"}

# (название, для чего нужен, как проверять, адрес)
SITES = [
    ("GitHub", "обновления лаунчера и файлы сборок",
     "get", "https://github.com/TejkaFan/MineForFriends/releases/download/packs/homestead.json"),
    ("Modrinth: файлы", "моды сборок",
     "get", "https://cdn.modrinth.com/data/P7dR8mSH/versions/aLxYjsiv/fabric-api-0.92.8%2B1.20.1.jar"),
    ("Modrinth: поиск", "версии модов и их обновления",
     "get", "https://api.modrinth.com/v2/tag/game_version"),
    ("Сервер скинов", "загрузка скинов и плащей, отчёты об ошибках, сборки друзей",
     "post", f"{relay.WORKER}/report"),
    ("Fabric", "загрузчик Fabric для сборок",
     "get", "https://meta.fabricmc.net/v2/versions/game"),
    ("Mojang", "версии Minecraft",
     "get", "https://piston-meta.mojang.com/mc/game/version_manifest_v2.json"),
]


def probe(method: str, url: str) -> dict:
    """{state: ok|cut|fail, text}: ok — передано больше порога, cut — связь оборвалась на середине."""
    web = requests.Session()
    web.headers.update(UA)
    started = time.monotonic()
    got, slow, answered = 0, False, False
    try:
        if method == "post":
            # Тело крупнее порога: на неверный отчёт сервер отвечает ошибкой, но только если дочитал его
            web.post(url, data=b"{" + b" " * ENOUGH, headers={"Content-Type": "application/json"},
                     timeout=(8, TIME_LIMIT))
            return {"state": "ok", "text": f"отправка {ENOUGH >> 10} КБ прошла за "
                                           f"{(time.monotonic() - started) * 1000:.0f} мс"}
        with web.get(url, stream=True, timeout=(8, TIME_LIMIT)) as r:
            if r.status_code >= 400:
                return {"state": "fail", "text": f"сайт ответил ошибкой {r.status_code}"}
            answered = True
            for chunk in r.iter_content(1024):
                got += len(chunk)
                if got >= ENOUGH:
                    break
                if time.monotonic() - started > TIME_LIMIT:
                    slow = True
                    break
    except requests.RequestException as e:
        # Соединение было и оборвалось на середине — это и есть блокировка «после 16 КБ».
        # Сброс (10054, SSL EOF) после подключения — тоже она; отказ и молчание — сайт недоступен
        reset = any(s in str(e) for s in ("Connection aborted", "10054", "reset", "EOF occurred"))
        if got or answered or reset:
            return {"state": "cut", "text": "связь обрывается" + (f" после {got >> 10} КБ" if got else " при отправке")}
        kind = "не отвечает (превышено время ожидания)" if isinstance(e, requests.Timeout) else "не открывается"
        return {"state": "fail", "text": kind}
    finally:
        web.close()
    seconds = time.monotonic() - started
    if slow:
        return {"state": "cut", "text": f"за {TIME_LIMIT} с пришло только {got >> 10} КБ — связь режут"}
    if got < ENOUGH:
        return {"state": "ok", "text": f"ответ {got >> 10} КБ за {seconds * 1000:.0f} мс"}
    return {"state": "ok", "text": f"{got / seconds / 1024:.0f} КБ/с"}


def cloud_status() -> dict:
    """Облако хоста по Radmin: {state: on|paused|off, ms}."""
    started = time.monotonic()
    try:
        r = relay.cloud.get(f"{relay.CLOUD}/packs.json", timeout=(2, 5))
    except requests.RequestException:
        return {"state": "off", "ms": None}
    ms = round((time.monotonic() - started) * 1000)
    if r.status_code == 503:
        return {"state": "paused", "ms": ms}
    return {"state": "on" if r.ok else "off", "ms": ms}


def check() -> dict:
    """Все сайты разом (до ~15 с) и облако; summary — что это значит для игрока."""
    with ThreadPoolExecutor(len(SITES) + 1) as pool:
        jobs = [pool.submit(probe, method, url) for _, _, method, url in SITES]
        cloud = pool.submit(cloud_status)
        results = [{"title": title, "purpose": purpose, **job.result()}
                   for (title, purpose, _, _), job in zip(SITES, jobs)]
        cloud = cloud.result()
    cloud_text = {"on": f"работает, отвечает за {cloud['ms']} мс",
                  "paused": "на паузе — хост поставил его на паузу",
                  "off": "недоступно — хост выключил облако или ты не в сети Radmin"}[cloud["state"]]
    results.append({"title": "Облако хоста (Radmin)", "purpose": "обход блокировок, быстрые загрузки сборок",
                    "state": "ok" if cloud["state"] == "on" else "fail", "text": cloud_text})

    bad = [r["title"] for r in results[:-1] if r["state"] != "ok"]
    if not bad:
        summary = "Все сайты работают напрямую — лаунчеру ничего не мешает."
    elif cloud["state"] == "on":
        summary = (f"Напрямую не работает: {', '.join(bad)}. Лаунчер обходит это через облако хоста — "
                   "пока оно включено и ты в сети Radmin, всё будет скачиваться и отправляться.")
    else:
        summary = (f"Напрямую не работает: {', '.join(bad)}, а облако хоста сейчас недоступно. "
                   "Попроси хоста включить облако (и проверь, что ты в сети Radmin) или включи VPN.")
    return {"results": results, "cloud": cloud, "summary": summary}


def report_text(result: dict) -> str:
    lines = ["Проверка доступа к сайтам", ""]
    for r in result["results"]:
        lines.append(f"{'OK ' if r['state'] == 'ok' else 'ОБРЫВ' if r['state'] == 'cut' else 'НЕТ'}  "
                     f"{r['title']}: {r['text']}")
    return "\n".join(lines + ["", result["summary"]])
