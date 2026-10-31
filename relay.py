"""Запросы к Worker (скины, отчёты, сборки друзей) с запасным путём через облако хоста.

У части провайдеров соединение с Cloudflare обрывается посреди отправки: маленькая страница
открывается, а скин или отчёт — уже нет (ConnectionResetError 10054). Тогда лаунчер шлёт тот же
запрос облаку хоста по Radmin (этот трафик провайдер не видит), а облако пересылает его в Worker.
Если запрос не прошёл напрямую, до конца запуска лаунчер сразу идёт через облако.
"""
import requests

WORKER = "https://mineforfriends-skins.inkiandmelon.workers.dev"
CLOUD_RELAY = "http://26.199.180.115:8518/relay"
# Обрыв, зависание или отказ соединения — то, что лечит обход через облако
BLOCKED = (requests.ConnectionError, requests.Timeout, requests.exceptions.ChunkedEncodingError)

web = requests.Session()
web.headers["User-Agent"] = "MineForFriends-launcher (github.com/TejkaFan)"
state = {"via_cloud": False}


def request(method: str, path: str, timeout: float, **kw) -> requests.Response:
    """path — маршрут Worker, например "/upload". Ответ Worker, напрямую или через облако."""
    if not state["via_cloud"]:
        try:
            return web.request(method, WORKER + path, timeout=timeout, **kw)
        except BLOCKED as e:
            direct_error = e
    else:
        direct_error = None
    try:
        r = web.request(method, CLOUD_RELAY + path, timeout=timeout, **kw)
    except requests.RequestException:
        # Облако тоже недоступно (хост выключил ПК или Radmin) — покажем исходную ошибку
        if direct_error:
            raise direct_error
        state["via_cloud"] = False
        return web.request(method, WORKER + path, timeout=timeout, **kw)
    if r.status_code == 404 and r.headers.get("X-MFF-Relay") != "1":
        # Облако старой версии, без пересылки
        if direct_error:
            raise direct_error
        state["via_cloud"] = False
        return web.request(method, WORKER + path, timeout=timeout, **kw)
    state["via_cloud"] = True
    return r


def post(path: str, timeout: float, **kw) -> requests.Response:
    return request("POST", path, timeout, **kw)


def get(path: str, timeout: float, **kw) -> requests.Response:
    return request("GET", path, timeout, **kw)
