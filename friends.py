"""Друзья в сети и проверка связи с сервером друга.

Каждый запущенный лаунчер слушает UDP-порт 25591 и на запрос из сети Radmin (26.x.x.x)
отвечает коротким JSON о себе: ник, в лаунчере он или в игре, какая сборка. Спрашивает
лаунчер широковещательно по Radmin и отдельно — по адресам друзей, добавленным вручную.
Ни облако, ни Worker тут не участвуют: о том, кто в сети, знают только сами друзья.
"""
import ctypes
import ctypes.wintypes
import json
import socket
import threading
import time

import server_host

PORT = 25591
ASK = b"MineForFriends:who?"
RADMIN_BROADCAST = "26.255.255.255"
MAX_REPLY = 1200


def from_radmin(ip: str) -> bool:
    # Отвечаем только своим по Radmin (и себе — для проверки), не всей домашней сети
    return ip.startswith("26.") or ip == "127.0.0.1"


class Presence:
    """Отвечает на «кто в сети?». about() — словарь о себе или None, если показывать себя не надо."""

    def __init__(self, about):
        self.about = about
        self.sock = None

    def start(self) -> bool:
        try:
            self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self.sock.bind(("0.0.0.0", PORT))
        except OSError:
            return False  # порт занят: значит, отвечает другой экземпляр лаунчера
        threading.Thread(target=self._loop, daemon=True).start()
        return True

    def _loop(self) -> None:
        while True:
            try:
                data, addr = self.sock.recvfrom(64)
                if data != ASK or not from_radmin(addr[0]):
                    continue
                me = self.about()
                if me:
                    self.sock.sendto(json.dumps(me, ensure_ascii=False).encode()[:MAX_REPLY], addr)
            except OSError:
                time.sleep(1)
            except Exception:
                time.sleep(1)  # about() не должен ронять ответчик


def ask(hosts=(), timeout: float = 1.2, local: bool = False) -> list:
    """Кто из друзей в сети: [{ip, nick, state, pack, mode, hosting, app}]. Себя не включает."""
    own = server_host.radmin_ip()
    if not own and not hosts and not local:
        return []
    found = {}
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
            s.bind((own or "0.0.0.0", 0))
            targets = ([RADMIN_BROADCAST] if own else []) + [h for h in hosts if h != own] + (["127.0.0.1"] if local else [])
            for target in targets:
                try:
                    s.sendto(ASK, (target, PORT))
                except OSError:
                    pass
            deadline = time.time() + timeout
            while time.time() < deadline:
                s.settimeout(max(0.05, deadline - time.time()))
                try:
                    data, addr = s.recvfrom(MAX_REPLY + 100)
                except (socket.timeout, OSError):
                    break
                if addr[0] == own and not local:
                    continue
                try:
                    info = json.loads(data.decode("utf-8"))
                except ValueError:
                    continue
                if isinstance(info, dict) and isinstance(info.get("nick"), str):
                    found[addr[0]] = {"ip": addr[0], **{k: info.get(k) for k in
                                      ("nick", "state", "pack", "mode", "hosting", "app")}}
    except OSError:
        pass
    return list(found.values())


# ---------- Окно игры ----------

def minecraft_window():
    """Заголовок окна Minecraft, если игра запущена: «Minecraft* 1.20.1 - Multiplayer (3rd-party Server)»."""
    user32 = ctypes.windll.user32
    found = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.wintypes.HWND, ctypes.wintypes.LPARAM)
    def each(hwnd, _):
        if user32.IsWindowVisible(hwnd):
            n = user32.GetWindowTextLengthW(hwnd)
            if n:
                buf = ctypes.create_unicode_buffer(n + 1)
                user32.GetWindowTextW(hwnd, buf, n + 1)
                if buf.value.startswith("Minecraft") and "Launcher" not in buf.value:
                    found.append(buf.value)
        return True

    user32.EnumWindows(each, 0)
    return found[0] if found else None


def game_mode(title: str) -> str:
    if "Multiplayer" in title or "Мультиплеер" in title or "Сетевая" in title:
        return "по сети"
    if "Singleplayer" in title or "Одиночная" in title:
        return "одиночная игра"
    return ""


# ---------- Проверка связи ----------

def ping(ip: str, timeout_ms: int = 1500):
    """Время ответа на ping в мс или None. Через IcmpSendEcho — без окна консоли."""
    iphlpapi = ctypes.windll.iphlpapi
    iphlpapi.IcmpCreateFile.restype = ctypes.c_void_p
    handle = iphlpapi.IcmpCreateFile()
    if not handle or handle == ctypes.c_void_p(-1).value:
        return None
    try:
        addr = int.from_bytes(socket.inet_aton(ip), "little")
        data = b"MineForFriends"
        reply = ctypes.create_string_buffer(64 + len(data) + 8)
        n = iphlpapi.IcmpSendEcho(ctypes.c_void_p(handle), ctypes.c_ulong(addr), data, len(data), None,
                                  reply, len(reply), timeout_ms)
        if not n:
            return None
        # ICMP_ECHO_REPLY: адрес, статус, время (мс)
        status, rtt = int.from_bytes(reply.raw[4:8], "little"), int.from_bytes(reply.raw[8:12], "little")
        return rtt if status == 0 else None
    except OSError:
        return None
    finally:
        iphlpapi.IcmpCloseHandle(ctypes.c_void_p(handle))


def tcp_check(ip: str, port: int, timeout: float = 4):
    """"ok", "refused" (ПК ответил, но порт закрыт) или "timeout" (ответа нет вообще)."""
    try:
        with socket.create_connection((ip, port), timeout=timeout):
            return "ok"
    except ConnectionRefusedError:
        return "refused"
    except OSError:
        return "timeout"


def check(address: str, radmin: dict, status) -> dict:
    """Проверка связи по шагам. status(address) — опрос сервера Minecraft или None.

    Ответ: {"steps": [{title, state: ok|warn|fail|skip, text}], "summary"}.
    """
    host, _, port = address.strip().rpartition(":")
    if not host:
        host, port = address.strip(), ""
    port = int(port) if port.isdigit() else 25565
    steps = []

    def step(title, state, text):
        steps.append({"title": title, "state": state, "text": text})

    def done(summary):
        for title in ("Компьютер друга", "Порт сервера", "Сервер Minecraft"):
            if not any(s["title"] == title for s in steps):
                step(title, "skip", "Не проверялось.")
        return {"steps": steps, "summary": summary, "address": f"{host}:{port}"}

    try:
        socket.inet_aton(host)
        is_ip = host.count(".") == 3
    except OSError:
        is_ip = False
    if not is_ip:
        try:
            host = socket.gethostbyname(host)
        except OSError:
            step("Адрес", "fail", f"Не нашёл компьютер «{host}». Проверь, что адрес написан правильно.")
            return done("Адрес не найден.")

    over_radmin = host.startswith("26.")
    if over_radmin:
        own = server_host.radmin_ip()
        if not radmin["installed"]:
            step("Radmin VPN", "fail", "Radmin VPN не установлен — без него до сервера друга не достучаться.")
            return done("Установи Radmin VPN и зайди в ту же сеть, что и друг.")
        if not own:
            step("Radmin VPN", "fail", "Radmin VPN выключен. Открой его и нажми кнопку включения.")
            return done("Включи Radmin VPN.")
        if own == host:
            step("Radmin VPN", "ok", f"Это твой собственный адрес ({own}).")
        else:
            step("Radmin VPN", "ok", f"Подключён, твой адрес {own}.")

    ms = ping(host)
    if ms is not None:
        step("Компьютер друга", "ok", f"Отвечает, задержка {ms} мс.")
    else:
        text = "Не отвечает на ping. Это ещё не беда: Windows часто запрещает ping, а Minecraft при этом работает."
        if over_radmin:
            text += " Но если дальше тоже ошибка — проверь, что друг в той же сети Radmin и у него горит зелёный значок."
        step("Компьютер друга", "warn", text)

    tcp = tcp_check(host, port)
    if tcp == "refused":
        step("Порт сервера", "fail", f"Компьютер друга ответил, но на порту {port} ничего не запущено.")
        return done("Сервер выключен или у него другой порт. Попроси друга запустить сервер и проверить адрес.")
    if tcp == "timeout":
        step("Порт сервера", "fail", f"Порт {port} не отвечает.")
        if ms is not None:
            return done("Компьютер друга в сети, но порт закрыт. Скорее всего, брандмауэр Windows у друга не пускает "
                        "Java: пусть друг разрешит Java (OpenJDK Platform binary) в брандмауэре для частных "
                        "и общественных сетей. Или сервер ещё не запущен.")
        return done("До компьютера друга не достучаться. Проверьте, что вы оба в одной сети Radmin и Radmin "
                    "включён у обоих, а у друга запущен сервер.")
    step("Порт сервера", "ok", f"Порт {port} открыт.")

    started = time.time()
    st = status(f"{host}:{port}")
    if not st:
        step("Сервер Minecraft", "fail", "Порт открыт, но Minecraft не ответил — сервер ещё запускается или это не Minecraft.")
        return done("Подожди минуту, пока сервер загрузится, и проверь снова.")
    took = int((time.time() - started) * 1000)
    who = f", играют: {', '.join(st['names'])}" if st.get("names") else ""
    step("Сервер Minecraft", "ok", f"Версия {st['version'] or '?'}, игроков {st['online']} из {st['max']}{who}. "
                                   f"Ответ за {took} мс.")
    return done("Связь в порядке — можно заходить.")
