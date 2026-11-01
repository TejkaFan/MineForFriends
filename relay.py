"""Запросы в интернет с запасным путём через облако хоста.

У части провайдеров соединение с Cloudflare (и не только) обрывается посреди передачи: маленькая
страница открывается, а скин, отчёт, загрузчик Fabric или мод — уже нет (ConnectionResetError 10054,
SSL EOF). Облако хоста по Radmin (этот трафик провайдер не видит) выполняет такие запросы само.

    request/post/get — запросы к Worker (скины, отчёты, сборки друзей), маршрут вида "/upload".
        Пока облако хоста включено, идут через него; выключено или на паузе — напрямую
    protect(session) — сессия requests, которая для сайтов из FETCH_HOSTS сама уходит в обход
"""
import socket
import time
import urllib.parse

import requests
from requests.adapters import HTTPAdapter

from cloud_server import FETCH_HOSTS

WORKER = "https://mineforfriends-skins.inkiandmelon.workers.dev"
HOST_IP = "26.199.180.115"


def own_ips() -> set:
    try:
        return set(socket.gethostbyname_ex(socket.gethostname())[2])
    except OSError:
        return set()


# На ПК хоста свой Radmin-адрес изнутри не отвечает: к своему облаку идём через 127.0.0.1
CLOUD = f"http://{'127.0.0.1' if HOST_IP in own_ips() else HOST_IP}:8518"
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
# Облако по Radmin отвечает за доли секунды; не ответило за это время — выключено
CLOUD_CONNECT = 2
# Облако не ответило — столько секунд шлём напрямую, потом снова пробуем облако
CLOUD_RETRY = 300
state = {"cloud_down_until": 0.0}
blocked_hosts = set()


def request(method: str, path: str, timeout: float, **kw) -> requests.Response:
    """path — маршрут Worker, например "/upload". Ответ Worker: через облако хоста, если оно включено, иначе напрямую."""
    if time.monotonic() >= state["cloud_down_until"]:
        try:
            r = cloud.request(method, CLOUD_RELAY + path, timeout=(CLOUD_CONNECT, timeout), **kw)
            # Без отметки X-MFF-Relay ответило не пересылкой: облако на паузе, старой версии или не дошло до Worker
            if r.headers.get("X-MFF-Relay") == "1":
                return r
        except requests.RequestException:
            pass
        state["cloud_down_until"] = time.monotonic() + CLOUD_RETRY
    return web.request(method, WORKER + path, timeout=timeout, **kw)


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
