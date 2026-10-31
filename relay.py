"""Запросы в интернет с запасным путём через облако хоста.

У части провайдеров соединение с Cloudflare (и не только) обрывается посреди передачи: маленькая
страница открывается, а скин, отчёт, загрузчик Fabric или мод — уже нет (ConnectionResetError 10054,
SSL EOF). Тогда лаунчер шлёт тот же запрос облаку хоста по Radmin (этот трафик провайдер не видит),
а облако выполняет его само. После первого обрыва лаунчер до конца запуска сразу идёт через облако.

    request/post/get — запросы к Worker (скины, отчёты, сборки друзей), маршрут вида "/upload"
    protect(session) — сессия requests, которая для сайтов из FETCH_HOSTS сама уходит в обход
"""
import urllib.parse

import requests
from requests.adapters import HTTPAdapter

from cloud_server import FETCH_HOSTS

WORKER = "https://mineforfriends-skins.inkiandmelon.workers.dev"
CLOUD = "http://26.199.180.115:8518"
CLOUD_RELAY = CLOUD + "/relay"
CLOUD_FETCH = CLOUD + "/relay-url"
# Обрыв, зависание или отказ соединения — то, что лечит обход через облако
BLOCKED = (requests.ConnectionError, requests.Timeout, requests.exceptions.ChunkedEncodingError)

web = requests.Session()
web.headers["User-Agent"] = "MineForFriends-launcher (github.com/TejkaFan)"
# К облаку ходим напрямую: системный прокси (VPN-клиенты) не видит сеть Radmin
cloud = requests.Session()
cloud.trust_env = False
cloud.headers["User-Agent"] = web.headers["User-Agent"]
state = {"via_cloud": False}
blocked_hosts = set()


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
        r = cloud.request(method, CLOUD_RELAY + path, timeout=timeout, **kw)
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


class CloudFallback(HTTPAdapter):
    """Если соединение с сайтом из FETCH_HOSTS оборвалось, тот же запрос выполняет облако хоста."""

    def send(self, request, stream=False, timeout=None, verify=True, cert=None, proxies=None):
        host = urllib.parse.urlsplit(request.url).hostname
        if host not in FETCH_HOSTS:
            return super().send(request, stream, timeout, verify, cert, proxies)
        error = None
        if host not in blocked_hosts:
            try:
                r = super().send(request, True, timeout, verify, cert, proxies)
                # Ответ читаем сразу: провайдер рвёт связь посреди передачи, а не при подключении
                r.content
                return r
            except BLOCKED as e:
                error = e
        via = request.copy()
        via.prepare_url(CLOUD_FETCH, {"url": request.url})
        try:
            r = super().send(via, True, timeout, verify, cert, {})
            r.content
            if r.headers.get("X-MFF-Relay") != "1":
                raise requests.ConnectionError("облако старой версии, без пересылки")
        except requests.RequestException:
            # Облако недоступно или старое — покажем исходную ошибку, а если связь была в обход, попробуем напрямую
            if error:
                raise error
            blocked_hosts.discard(host)
            return self.send(request, stream, timeout, verify, cert, proxies)
        blocked_hosts.add(host)
        # Для вызывающего кода это ответ самого сайта: имя файла и переадресации считаются от его адреса
        r.url, r.request = request.url, request
        return r


def protect(session: requests.Session) -> requests.Session:
    session.mount("https://", CloudFallback())
    return session
