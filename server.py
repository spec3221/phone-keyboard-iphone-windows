#!/usr/bin/env python3
"""Phone-to-PC keyboard bridge. Run this on the computer."""
from __future__ import annotations

import argparse
import asyncio
import hmac
import logging
import platform
import secrets
import socket
from io import BytesIO
from pathlib import Path
from typing import Any

import qrcode
import psutil
from aiohttp import web
# Windows uses the native SendInput API below and does not need pynput.
# pynput is imported only for non-Windows fallback platforms.
if platform.system() == "Windows":
    Key = KeyCode = Controller = None
    keyboard = None
else:
    try:
        from pynput.keyboard import Controller, Key, KeyCode
        keyboard = Controller()
    except Exception as exc:
        raise RuntimeError("Для этой ОС нужен pynput. На Windows он не требуется.") from exc

ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "static"
ICONS = ROOT / "icons"

KEY_MAP = {} if platform.system() == "Windows" else {
    "enter": Key.enter,
    "tab": Key.tab,
    "backspace": Key.backspace,
    "delete": Key.delete,
    "esc": Key.esc,
    "left": Key.left,
    "right": Key.right,
    "up": Key.up,
    "down": Key.down,
    "home": Key.home,
    "end": Key.end,
    "pageup": Key.page_up,
    "pagedown": Key.page_down,
    "insert": Key.insert,
    "capslock": Key.caps_lock,
    "numlock": Key.num_lock,
    "scrolllock": Key.scroll_lock,
    "printscreen": Key.print_screen,
    "pause": Key.pause,
    "menu": Key.menu,
    "space": Key.space,
    "ctrl": Key.ctrl,
    "shift": Key.shift,
    "alt": Key.alt,
    "cmd": Key.cmd,
    "win": Key.cmd,
}
if platform.system() != "Windows":
    for _function_number in range(1, 13):
        KEY_MAP[f"f{_function_number}"] = getattr(Key, f"f{_function_number}")
    for _digit in range(10):
        KEY_MAP[f"numpad{_digit}"] = KeyCode.from_vk(0x60 + _digit)
    KEY_MAP.update({
        "numpad_multiply": KeyCode.from_vk(0x6A),
        "numpad_add": KeyCode.from_vk(0x6B),
        "numpad_separator": KeyCode.from_vk(0x6C),
        "numpad_subtract": KeyCode.from_vk(0x6D),
        "numpad_decimal": KeyCode.from_vk(0x6E),
        "numpad_divide": KeyCode.from_vk(0x6F),
    })

# Windows virtual-key codes. On Windows we use SendInput directly and batch
# key-down/key-up events in one native call; this is quicker and more reliable
# than synthesizing separate pynput events for every tap.
WINDOWS_VK = {
    "enter": 0x0D, "tab": 0x09, "backspace": 0x08, "delete": 0x2E,
    "esc": 0x1B, "left": 0x25, "up": 0x26, "right": 0x27, "down": 0x28,
    "home": 0x24, "end": 0x23, "pageup": 0x21, "pagedown": 0x22,
    "insert": 0x2D, "capslock": 0x14, "numlock": 0x90,
    "scrolllock": 0x91, "printscreen": 0x2C, "pause": 0x13,
    "menu": 0x5D, "space": 0x20, "ctrl": 0x11, "shift": 0x10,
    "alt": 0x12, "win": 0x5B, "cmd": 0x5B,
    "numpad_multiply": 0x6A, "numpad_add": 0x6B,
    "numpad_separator": 0x6C, "numpad_subtract": 0x6D,
    "numpad_decimal": 0x6E, "numpad_divide": 0x6F,
}
for _i in range(1, 13):
    WINDOWS_VK[f"f{_i}"] = 0x70 + _i - 1
for _i in range(10):
    WINDOWS_VK[f"numpad{_i}"] = 0x60 + _i
WINDOWS_EXTENDED_VK = {
    0x21, 0x22, 0x23, 0x24, 0x25, 0x26, 0x27, 0x28,
    0x2C, 0x2D, 0x2E, 0x5B, 0x5C, 0x5D, 0x6F,
}
WINDOWS_PUNCTUATION_VK = {
    "\`": 0xC0, "-": 0xBD, "=": 0xBB, "[": 0xDB, "]": 0xDD,
    "\\": 0xDC, ";": 0xBA, "'": 0xDE, ",": 0xBC,
    ".": 0xBE, "/": 0xBF,
}


def lan_addresses() -> list[tuple[str, str]]:
    """Return active LAN IPv4 addresses, preferring Wi-Fi/Ethernet over VPN tunnels."""
    excluded = (
        "vpn", "tun", "tap", "wireguard", "wintun", "happ", "tailscale",
        "zerotier", "hamachi", "virtual", "vmware", "vbox", "loopback",
        "docker", "wsl", "bluetooth", "утп", "туннель",
    )
    preferred = (
        ("wi-fi", 0), ("wifi", 0), ("wlan", 1), ("wireless", 1),
        ("беспровод", 1), ("ethernet", 2), ("ethernet", 2), ("eth", 3),
    )
    found: list[tuple[int, str, str]] = []
    try:
        addresses = psutil.net_if_addrs()
        stats = psutil.net_if_stats()
        for interface, items in addresses.items():
            lowered = interface.casefold()
            if any(marker in lowered for marker in excluded):
                continue
            status = stats.get(interface)
            if status is not None and not status.isup:
                continue
            score = 5
            for marker, value in preferred:
                if marker in lowered:
                    score = value
                    break
            for item in items:
                if item.family != socket.AF_INET:
                    continue
                address = item.address
                if address.startswith(("127.", "169.254.")):
                    continue
                found.append((score, interface, address))
    except Exception as exc:
        logging.warning("Не удалось прочитать сетевые интерфейсы: %s", exc)

    # Stable order: Wi-Fi first, then Ethernet, then other non-VPN interfaces.
    found.sort(key=lambda row: (row[0], row[1].casefold(), row[2]))
    result: list[tuple[str, str]] = []
    seen: set[str] = set()
    for _, interface, address in found:
        if address not in seen:
            result.append((interface, address))
            seen.add(address)
    return result


def lan_ip() -> str:
    """Choose the most likely physical LAN address, not a VPN/tunnel address."""
    candidates = lan_addresses()
    if candidates:
        return candidates[0][1]

    # Fallback for uncommon systems where interface enumeration is unavailable.
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("192.0.2.1", 80))
        address = sock.getsockname()[0]
        return address if not address.startswith("127.") else "127.0.0.1"
    except OSError:
        return "127.0.0.1"
    finally:
        sock.close()


def resolve_hotkey_key(name: str) -> Any:
    # On macOS, the conventional Ctrl shortcuts are Command shortcuts.
    if name == "ctrl" and platform.system() == "Darwin":
        return Key.cmd
    return key_object(name)


def key_object(name: str) -> Any:
    """Resolve a named key; on Windows use physical VK codes for printable keys.

    VK codes follow the key's physical position, so the active Windows layout
    (English, Russian, etc.) decides which character appears on screen.
    """
    if name in KEY_MAP:
        return KEY_MAP[name]
    if isinstance(name, str) and len(name) == 1 and name.isprintable():
        if platform.system() == "Windows":
            punctuation_vks = {
                "\`": 0xC0, "-": 0xBD, "=": 0xBB,
                "[": 0xDB, "]": 0xDD, "\\": 0xDC,
                ";": 0xBA, "'": 0xDE, ",": 0xBC,
                ".": 0xBE, "/": 0xBF,
            }
            vk = punctuation_vks.get(name)
            if vk is None and name.isalpha() and name.isascii():
                vk = ord(name.upper())
            elif vk is None and name.isdigit():
                vk = ord(name)
            if vk is not None:
                return KeyCode.from_vk(vk)
        return name
    raise ValueError("Неизвестная клавиша")


def windows_vk_for_name(name: str) -> tuple[int, bool]:
    """Return (VK code, extended-key flag) for a supported keyboard key."""
    vk = WINDOWS_VK.get(name)
    if vk is None and isinstance(name, str) and len(name) == 1:
        if name.isascii() and name.isalpha():
            vk = ord(name.upper())
        elif name.isascii() and name.isdigit():
            vk = ord(name)
        else:
            vk = WINDOWS_PUNCTUATION_VK.get(name)
    if vk is None:
        raise ValueError(f"Неизвестная клавиша: {name}")
    return vk, vk in WINDOWS_EXTENDED_VK


def send_windows_vk_events(events: list[tuple[int, bool, bool]]) -> None:
    """Send a batch of (vk, key_up, extended) events using one SendInput call."""
    if not events:
        return
    ctypes, wintypes, INPUT, KEYBDINPUT, send_input = _windows_sendinput_api()
    INPUT_KEYBOARD = 1
    KEYEVENTF_KEYUP = 0x0002
    KEYEVENTF_EXTENDEDKEY = 0x0001
    built = []
    for vk, is_up, extended in events:
        flags = (KEYEVENTF_KEYUP if is_up else 0)
        if extended:
            flags |= KEYEVENTF_EXTENDEDKEY
        built.append(INPUT(type=INPUT_KEYBOARD, ki=KEYBDINPUT(vk, 0, flags, 0, 0)))

    # SendInput can theoretically accept fewer items than requested. Resume
    # from the first unsent item instead of silently losing trailing keys.
    offset = 0
    while offset < len(built):
        remaining = built[offset:]
        array = (INPUT * len(remaining))(*remaining)
        sent = int(send_input(len(remaining), array, ctypes.sizeof(INPUT)))
        if sent <= 0:
            error = ctypes.get_last_error()
            raise OSError(error, f"Windows SendInput не принял клавиши (код {error})")
        offset += sent


def send_key(name: str, count: int = 1) -> None:
    if not isinstance(count, int) or isinstance(count, bool) or not 1 <= count <= 100:
        raise ValueError("Недопустимое количество повторов")
    if platform.system() == "Windows":
        vk, extended = windows_vk_for_name(name)
        events: list[tuple[int, bool, bool]] = []
        for _ in range(count):
            events.append((vk, False, extended))
            events.append((vk, True, extended))
        send_windows_vk_events(events)
        return
    key = key_object(name)
    for _ in range(count):
        keyboard.press(key)
        keyboard.release(key)


def _windows_sendinput_api():
    """Build and cache ctypes types once.

    ctypes keeps SendInput.argtypes on the function object. Recreating INPUT on
    each text event causes: "expected LP_INPUT instance instead of pointer to
    INPUT". A single cached structure type prevents that mismatch.
    """
    global _SENDINPUT_API
    if _SENDINPUT_API is not None:
        return _SENDINPUT_API

    import ctypes
    from ctypes import wintypes

    class KEYBDINPUT(ctypes.Structure):
        _fields_ = [
            ("wVk", ctypes.c_uint16),
            ("wScan", ctypes.c_uint16),
            ("dwFlags", ctypes.c_uint32),
            ("time", ctypes.c_uint32),
            ("dwExtraInfo", ctypes.c_size_t),
        ]

    class MOUSEINPUT(ctypes.Structure):
        _fields_ = [
            ("dx", ctypes.c_int32),
            ("dy", ctypes.c_int32),
            ("mouseData", ctypes.c_uint32),
            ("dwFlags", ctypes.c_uint32),
            ("time", ctypes.c_uint32),
            ("dwExtraInfo", ctypes.c_size_t),
        ]

    class HARDWAREINPUT(ctypes.Structure):
        _fields_ = [
            ("uMsg", ctypes.c_uint32),
            ("wParamL", ctypes.c_uint16),
            ("wParamH", ctypes.c_uint16),
        ]

    # INPUT's union must include MOUSEINPUT too: on 64-bit Windows it is
    # larger than KEYBDINPUT, and SendInput rejects an undersized cbSize.
    class INPUT_UNION(ctypes.Union):
        _fields_ = [
            ("mi", MOUSEINPUT),
            ("ki", KEYBDINPUT),
            ("hi", HARDWAREINPUT),
        ]

    class INPUT(ctypes.Structure):
        _anonymous_ = ("u",)
        _fields_ = [("type", ctypes.c_uint32), ("u", INPUT_UNION)]

    send_input = ctypes.WinDLL("user32", use_last_error=True).SendInput
    send_input.argtypes = (ctypes.c_uint32, ctypes.POINTER(INPUT), ctypes.c_int)
    send_input.restype = ctypes.c_uint32
    _SENDINPUT_API = (ctypes, wintypes, INPUT, KEYBDINPUT, send_input)
    return _SENDINPUT_API


_SENDINPUT_API = None


def type_unicode_windows(value: str) -> None:
    """Inject UTF-16 text on Windows, independent of the active keyboard layout."""
    if platform.system() != "Windows":
        keyboard.type(value)
        return
    ctypes, wintypes, INPUT, KEYBDINPUT, send_input = _windows_sendinput_api()
    events = []
    encoded = value.encode("utf-16-le", "surrogatepass")
    for offset in range(0, len(encoded), 2):
        unit = int.from_bytes(encoded[offset:offset + 2], "little")
        events.append(INPUT(type=1, ki=KEYBDINPUT(0, unit, 0x0004, 0, 0)))
        events.append(INPUT(type=1, ki=KEYBDINPUT(0, unit, 0x0004 | 0x0002, 0, 0)))
    if events:
        offset = 0
        while offset < len(events):
            remaining = events[offset:]
            array = (INPUT * len(remaining))(*remaining)
            sent = int(send_input(len(remaining), array, ctypes.sizeof(INPUT)))
            if sent <= 0:
                error = ctypes.get_last_error()
                raise OSError(error, f"Windows не приняла текст (код {error})")
            offset += sent


def type_text(value: str) -> None:
    """Type text while translating newline and tab to real key presses."""
    chunk: list[str] = []

    def flush() -> None:
        if not chunk:
            return
        text = "".join(chunk)
        if platform.system() == "Windows":
            type_unicode_windows(text)
        else:
            keyboard.type(text)
        chunk.clear()

    for char in value:
        if char in ("\n", "\r", "\t"):
            flush()
            send_key("tab" if char == "\t" else "enter")
        else:
            chunk.append(char)
    flush()


def send_hotkey(names: list[str]) -> None:
    known = WINDOWS_VK if platform.system() == "Windows" else KEY_MAP
    valid = lambda name: isinstance(name, str) and (name in known or (len(name) == 1 and name.isprintable()))
    if not 2 <= len(names) <= 4 or any(not valid(name) for name in names):
        raise ValueError("Недопустимое сочетание клавиш")
    if platform.system() == "Windows":
        resolved = [windows_vk_for_name(name) for name in names]
        events: list[tuple[int, bool, bool]] = []
        # Press modifiers and the main key in order, then release in reverse.
        for vk, extended in resolved:
            events.append((vk, False, extended))
        for vk, extended in reversed(resolved):
            events.append((vk, True, extended))
        send_windows_vk_events(events)
        return
    pressed = []
    try:
        for name in names:
            key = resolve_hotkey_key(name)
            keyboard.press(key)
            pressed.append(key)
    finally:
        for key in reversed(pressed):
            keyboard.release(key)


async def index(_: web.Request) -> web.FileResponse:
    return web.FileResponse(STATIC / "index.html")


async def manifest(_: web.Request) -> web.FileResponse:
    return web.FileResponse(STATIC / "manifest.webmanifest")


async def service_worker(_: web.Request) -> web.FileResponse:
    response = web.FileResponse(STATIC / "sw.js")
    response.headers["Service-Worker-Allowed"] = "/"
    return response


async def pairing_qr(request: web.Request) -> web.Response:
    image = qrcode.make(request.app["pair_url"])
    buffer = BytesIO()
    image.save(buffer, format="PNG")
    return web.Response(body=buffer.getvalue(), content_type="image/png", headers={"Cache-Control": "no-store"})


async def websocket_handler(request: web.Request) -> web.WebSocketResponse:
    supplied = request.query.get("token", "")
    expected = request.app["token"]
    if not hmac.compare_digest(supplied, expected):
        raise web.HTTPForbidden(text="Неверный код подключения")

    ws = web.WebSocketResponse(heartbeat=20, max_msg_size=4096, compress=False)
    await ws.prepare(request)
    # Disable Nagle's algorithm: tiny key events should be sent immediately.
    try:
        transport_socket = request.transport.get_extra_info("socket")
        if transport_socket is not None:
            transport_socket.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    except (AttributeError, OSError):
        pass

    logging.info("Телефон подключился")
    await ws.send_json({"type": "connected"})

    try:
        async for message in ws:
            if message.type != web.WSMsgType.TEXT:
                continue
            command_id = None
            try:
                data = message.json()
                if not isinstance(data, dict):
                    raise ValueError("Неверный формат команды")
                kind = data.get("type")
                if kind == "ping":
                    await ws.send_json({"type": "pong", "sent": data.get("sent")})
                    continue

                raw_command_id = data.get("id")
                if raw_command_id is not None and (not isinstance(raw_command_id, str) or not 1 <= len(raw_command_id) <= 120):
                    raise ValueError("Неверный ID команды")
                command_id = raw_command_id
                if command_id is not None:
                    previous = request.app["seen_commands"].get(command_id)
                    if previous is not None:
                        await ws.send_json(previous)
                        continue

                if kind == "text":
                    value = data.get("text", "")
                    if not isinstance(value, str) or len(value) > 1000:
                        raise ValueError("Слишком длинный текст")
                    if value:
                        type_text(value)
                elif kind == "key":
                    name = data.get("key", "")
                    count = data.get("count", 1)
                    if not isinstance(name, str) or not isinstance(count, int) or isinstance(count, bool):
                        raise ValueError("Неверный параметр клавиши")
                    send_key(name, count)
                elif kind == "hotkey":
                    names = data.get("keys", [])
                    if not isinstance(names, list) or not all(isinstance(x, str) for x in names):
                        raise ValueError("Неверное сочетание клавиш")
                    send_hotkey(names)
                else:
                    raise ValueError("Неизвестная команда")

                response = {"type": "ack", "id": command_id}
            except Exception as exc:  # Keep the socket alive and report each rejected command.
                logging.warning("Команда не выполнена: %s", exc)
                response = {"type": "error", "id": command_id, "message": str(exc)[:180]}

            if command_id is not None:
                cache = request.app["seen_commands"]
                cache[command_id] = response
                cache.move_to_end(command_id)
                while len(cache) > 4096:
                    cache.popitem(last=False)
            await ws.send_json(response)
    finally:
        logging.info("Телефон отключился")
    return ws


def make_app(token: str, pair_url: str) -> web.Application:
    app = web.Application(client_max_size=8192)
    app["token"] = token
    app["pair_url"] = pair_url
    from collections import OrderedDict
    app["seen_commands"] = OrderedDict()
    app.router.add_get("/", index)
    app.router.add_get("/manifest.webmanifest", manifest)
    app.router.add_get("/sw.js", service_worker)
    app.router.add_get("/pair.png", pairing_qr)
    app.router.add_get("/ws", websocket_handler)
    app.router.add_static("/static/", STATIC, show_index=False)
    app.router.add_static("/icons/", ICONS, show_index=False)
    return app


def main() -> None:
    parser = argparse.ArgumentParser(description="Клавиатура телефона для компьютера")
    parser.add_argument("--port", type=int, default=8765, help="Порт (по умолчанию: 8765)")
    args = parser.parse_args()
    token = secrets.token_urlsafe(12)
    candidates = lan_addresses()
    host = candidates[0][1] if candidates else lan_ip()

    print("\n=== Клавиатура телефона → компьютер ===")
    print("iPhone / Android и компьютер должны быть в одной Wi-Fi сети.\n")
    phone_url = f"http://{host}:{args.port}/?token={token}"
    pc_url = f"http://127.0.0.1:{args.port}/?token={token}"
    selected_name = candidates[0][0] if candidates else "сетевой интерфейс"
    print(f"Ссылка для iPhone / Android (выбран {selected_name}): {phone_url}")
    if len(candidates) > 1:
        print("\nДругие адреса, если первый не откроется:")
        for interface, address in candidates[1:]:
            print(f"  {interface}: http://{address}:{args.port}/?token={token}")
    print(f"\nПроверка на самом компьютере: {pc_url}")
    print("На iPhone открой Safari и ВРУЧНУЮ введи полный адрес http://.../?token=...")
    print("Не открывай HTTP-ссылку через Камеру/QR: iOS может заблокировать такой переход.")
    print("Если Windows спросит о брандмауэре, разреши доступ для частных сетей.")
    print("\nНе сообщай ссылку посторонним. Для остановки нажми Ctrl+C.\n")
    if platform.system() == "Darwin":
        print("macOS: при первом запуске разрешите Python управлять компьютером в настройках Универсальный доступ.")
    elif platform.system() == "Linux":
        print("Linux: для эмуляции клавиш обычно нужен X11; в Wayland поддержка может быть ограничена.")

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s: %(message)s")
    web.run_app(make_app(token, phone_url), host="0.0.0.0", port=args.port, print=None, access_log=None)


if __name__ == "__main__":
    main()
