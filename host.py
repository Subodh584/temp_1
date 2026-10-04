#!/usr/bin/env python3
"""LiteView host - run this on the computer you want to control.

The controlling computer just opens http://<this-computer-ip>:<port> in a browser.
"""
import argparse
import asyncio
import hashlib
import hmac
import io
import ipaddress
import json
import os
import secrets
import socket
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from aiohttp import WSMsgType, web
from PIL import Image
from pynput.keyboard import Controller as KeyboardController
from pynput.keyboard import Key, KeyCode
from pynput.mouse import Button
from pynput.mouse import Controller as MouseController

HERE = Path(__file__).resolve().parent

# Optional: thirdeye captures protected windows (WDA_EXCLUDEFROMCAPTURE).
# Try the installed wheel first; fall back to the local module_2 copy.
try:
    import eye3 as _eye3
except ImportError:
    try:
        sys.path.insert(0, str(HERE / "module_2" / "thirdeye" / "python"))
        import eye3 as _eye3
    except ImportError:
        _eye3 = None

# dxcam uses DXGI Desktop Duplication, which captures GPU-composited frames
# (including hardware-decoded video like Netflix) — something BitBlt cannot do.
# We use it alongside thirdeye: thirdeye clears WDA_EXCLUDEFROMCAPTURE, dxcam
# captures during that window.
try:
    import dxcam as _dxcam
except ImportError:
    _dxcam = None
PASSWORD_FILE = Path.home() / ".liteview_password"
LOG_FILE = Path.home() / ".liteview.log"

try:
    from module_2.thirdeye import ThirdEyeModule
except Exception:  # pragma: no cover - optional integration module
    ThirdEyeModule = None

# ---------------------------------------------------------------- capture-bypass integration
# capture-bypass (module_3) injects a persistent DLL into browser processes that
# calls SetWindowDisplayAffinity(WDA_NONE) every 500 ms, permanently clearing
# WDA_EXCLUDEFROMCAPTURE so DXGI can see protected windows (e.g. Netflix in Chrome).
# It handles Chrome's multi-process architecture by injecting all child PIDs.

def _get_protected_pids():
    """Return PIDs of all processes that own a WDA-protected window."""
    # Only available on Windows; returns empty set on other platforms.
    try:
        import ctypes
        import ctypes.wintypes
        user32 = ctypes.windll.user32
    except Exception:
        return set()

    protected_pids = set()

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.wintypes.HWND, ctypes.wintypes.LPARAM)
    def _cb(hwnd, _):
        affinity = ctypes.c_uint(0)
        user32.GetWindowDisplayAffinity(hwnd, ctypes.byref(affinity))
        if affinity.value != 0:
            pid = ctypes.c_uint(0)
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            if pid.value:
                protected_pids.add(pid.value)
        return True

    user32.EnumWindows(_cb, 0)
    return protected_pids


def _inject_capture_bypass(cb_dir: Path):
    """Inject payload_dll_persistent.dll into every process with a protected window."""
    cli = cb_dir / "capture_bypass_cli.exe"
    dll = cb_dir / "payload_dll_persistent.dll"
    if not cli.exists() or not dll.exists():
        return

    pids = _get_protected_pids()
    if not pids:
        print("[*] capture-bypass: no WDA-protected windows found at startup", flush=True)
        return

    ok = 0
    for pid in sorted(pids):
        try:
            r = subprocess.run(
                [str(cli), str(pid), str(dll)],
                capture_output=True, timeout=5,
            )
            if r.returncode == 0:
                ok += 1
        except Exception:
            pass

    if ok:
        print(f"[+] capture-bypass: injected persistent DLL into {ok}/{len(pids)} protected process(es)", flush=True)
    else:
        print(
            f"[!] capture-bypass: injection failed for {len(pids)} process(es) — "
            "run LiteView as Administrator to enable capture of protected windows",
            flush=True,
        )


# ---------------------------------------------------------------- screen capture

_capture = threading.local()  # thirdeye sessions are not thread-safe; keep one per thread

# Shared dxcam camera instance (DXGI Desktop Duplication).
_dxcam_camera = None
_dxcam_lock = threading.Lock()

# ThirdEye capture option: bypass_protection keeps thirdeye as a fallback even
# when capture-bypass hasn't injected yet (e.g. no Admin rights).
_TE_OPTS_BYPASS = None


def _init_capture(quality):
    global _dxcam_camera, _TE_OPTS_BYPASS
    if _TE_OPTS_BYPASS is None:
        _TE_OPTS_BYPASS = _eye3.ThirdEyeOptions(
            format=_eye3.ThirdeyeFormat.JPEG,
            quality=quality,
            bypass_protection=True,
        )
    if _dxcam is not None and _dxcam_camera is None:
        with _dxcam_lock:
            if _dxcam_camera is None:
                _dxcam_camera = _dxcam.create(output_color="RGB")


def grab_jpeg(max_width, quality, force):
    """Return the screen as JPEG bytes, or None if nothing changed since last grab."""
    global _dxcam_camera

    if not hasattr(_capture, "session"):
        _capture.session = _eye3.ThirdEyeSession()
        _capture.last_digest = None
        _init_capture(quality)

    jpeg = None

    if _dxcam_camera is not None:
        # capture-bypass has (hopefully) already cleared WDA_EXCLUDEFROMCAPTURE
        # via the persistent DLL, so DXGI can see GPU-composited frames including
        # hardware-decoded video (Netflix etc.) without any per-frame bypass delay.
        try:
            frame = _dxcam_camera.grab()
            if frame is not None:
                img = Image.fromarray(frame)
                if img.width > max_width:
                    img = img.resize(
                        (max_width, round(img.height * max_width / img.width)),
                        Image.BILINEAR,
                    )
                buf = io.BytesIO()
                img.save(buf, "JPEG", quality=quality)
                jpeg = buf.getvalue()
        except Exception as exc:
            print(f"[dxcam] capture error, disabling: {exc}", flush=True)
            _dxcam_camera = None

    if jpeg is None:
        # Fallback: thirdeye BitBlt with its own per-frame WDA bypass.
        # Works even if capture-bypass injection failed (no Admin) or dxcam is absent.
        jpeg = _capture.session.capture_to_buffer(_TE_OPTS_BYPASS)

    digest = hashlib.blake2b(jpeg, digest_size=16).digest()
    if digest == _capture.last_digest and not force:
        return None
    _capture.last_digest = digest
    return jpeg


# ---------------------------------------------------------------- input injection

# Browser KeyboardEvent.code -> pynput Key name. Looked up with getattr because
# some keys (insert, menu, print_screen) don't exist on every OS.
SPECIAL_CODES = {
    "Enter": "enter", "NumpadEnter": "enter", "Backspace": "backspace", "Tab": "tab",
    "Escape": "esc", "Space": "space", "CapsLock": "caps_lock",
    "ArrowUp": "up", "ArrowDown": "down", "ArrowLeft": "left", "ArrowRight": "right",
    "Delete": "delete", "Insert": "insert", "Home": "home", "End": "end",
    "PageUp": "page_up", "PageDown": "page_down",
    "ShiftLeft": "shift_l", "ShiftRight": "shift_r",
    "ControlLeft": "ctrl_l", "ControlRight": "ctrl_r",
    "AltLeft": "alt_l", "AltRight": "alt_r",
    "MetaLeft": "cmd_l", "MetaRight": "cmd_r",
    "ContextMenu": "menu", "PrintScreen": "print_screen",
    **{f"F{i}": f"f{i}" for i in range(1, 13)},
}
CHAR_CODES = {
    "Minus": "-", "Equal": "=", "BracketLeft": "[", "BracketRight": "]",
    "Backslash": "\\", "Semicolon": ";", "Quote": "'", "Backquote": "`",
    "Comma": ",", "Period": ".", "Slash": "/",
    "NumpadAdd": "+", "NumpadSubtract": "-", "NumpadMultiply": "*",
    "NumpadDivide": "/", "NumpadDecimal": ".",
}
BUTTONS = {0: Button.left, 1: Button.middle, 2: Button.right}


def code_to_key(code):
    # Using physical key codes (not typed characters) means held modifiers like
    # Shift/Ctrl are applied by the host OS, so presses and releases always match.
    if code in SPECIAL_CODES:
        return getattr(Key, SPECIAL_CODES[code], None)
    if code in CHAR_CODES:
        return KeyCode.from_char(CHAR_CODES[code])
    if code.startswith("Key") and len(code) == 4:
        return KeyCode.from_char(code[3].lower())
    if code.startswith("Digit"):
        return KeyCode.from_char(code[5:])
    if code.startswith("Numpad") and code[6:].isdigit():
        return KeyCode.from_char(code[6:])
    return None


class InputInjector:
    def __init__(self, monitor):
        self.monitor = monitor
        self.mouse = MouseController()
        self.keyboard = KeyboardController()
        self.held_keys = set()
        self.held_buttons = set()

    def _move(self, ev):
        m = self.monitor
        x = min(max(float(ev["x"]), 0.0), 1.0)
        y = min(max(float(ev["y"]), 0.0), 1.0)
        self.mouse.position = (m["left"] + round(x * (m["width"] - 1)),
                               m["top"] + round(y * (m["height"] - 1)))

    def handle(self, ev):
        t = ev.get("t")
        if t == "move":
            self._move(ev)
        elif t in ("down", "up"):
            button = BUTTONS.get(ev.get("b"))
            if button is None:
                return
            self._move(ev)
            if t == "down":
                self.mouse.press(button)
                self.held_buttons.add(button)
            else:
                self.mouse.release(button)
                self.held_buttons.discard(button)
        elif t == "wheel":
            self.mouse.scroll(int(ev.get("dx", 0)), int(ev.get("dy", 0)))
        elif t in ("kd", "ku"):
            key = code_to_key(str(ev.get("code", "")))
            if key is None:
                return
            if t == "kd":
                self.keyboard.press(key)
                self.held_keys.add(key)
            else:
                self.keyboard.release(key)
                self.held_keys.discard(key)
        elif t == "releaseall":
            self.release_all()

    def release_all(self):
        """Avoid stuck keys/buttons when the viewer loses focus or disconnects."""
        for key in list(self.held_keys):
            try:
                self.keyboard.release(key)
            except Exception:
                pass
        for button in list(self.held_buttons):
            try:
                self.mouse.release(button)
            except Exception:
                pass
        self.held_keys.clear()
        self.held_buttons.clear()


# ---------------------------------------------------------------- web server

async def index(request):
    return web.FileResponse(HERE / "viewer.html")


async def stream_frames(ws, app, acked):
    loop = asyncio.get_running_loop()
    interval = 1 / app["fps"]
    force = True
    third_eye = app.get("third_eye")
    while not ws.closed:
        started = loop.time()
        jpeg = await loop.run_in_executor(
            app["capture_pool"], grab_jpeg, app["max_width"], app["quality"], force)
        if jpeg:
            if third_eye is not None:
                third_eye.observe(jpeg)
                if third_eye.last_frame:
                    jpeg = third_eye.last_frame
            force = False
            acked.clear()
            await ws.send_bytes(jpeg)
            try:
                await asyncio.wait_for(acked.wait(), timeout=5)
            except asyncio.TimeoutError:
                force = True
        await asyncio.sleep(max(0.0, interval - (loop.time() - started)))


async def ws_handler(request):
    app = request.app
    peer = request.remote
    ws = web.WebSocketResponse(heartbeat=20, max_msg_size=64 * 1024)
    await ws.prepare(request)

    try:
        first = await ws.receive(timeout=30)
        auth = json.loads(first.data) if first.type == WSMsgType.TEXT else {}
    except (asyncio.TimeoutError, ValueError):
        auth = {}
    if not hmac.compare_digest(str(auth.get("pw", "")).encode(), app["password"].encode()):
        print(f"[!] Rejected {peer}: wrong password")
        await asyncio.sleep(1)
        await ws.send_str(json.dumps({"t": "error", "msg": "Wrong password"}))
        await ws.close(code=4001)
        return ws
    session = app["session"]
    old = session["ws"]
    if old is not None and not old.closed:
        print("[*] New viewer is taking over the existing session")
        try:
            await old.send_str(json.dumps({"t": "error", "msg": "Another viewer took over this session"}))
        except Exception:
            pass
        asyncio.create_task(old.close(code=4002))
    session["ws"] = ws
    print(f"[+] {peer} connected")
    third_eye = app.get("third_eye")
    if third_eye is not None:
        await ws.send_str(json.dumps({"t": "module", "module": "thirdeye", "enabled": True, "status": "tracking"}))
    await ws.send_str(json.dumps({"t": "ok", "capture": app["capture_backend"]}))
    acked = asyncio.Event()
    injector = app["injector"]
    streamer = asyncio.create_task(stream_frames(ws, app, acked))
    try:
        async for msg in ws:
            if msg.type != WSMsgType.TEXT:
                continue
            try:
                ev = json.loads(msg.data)
                if ev.get("t") == "ack":
                    acked.set()
                elif not app["view_only"]:
                    injector.handle(ev)
            except Exception as exc:
                print(f"[!] Bad input event {msg.data[:80]!r}: {exc}")
    finally:
        streamer.cancel()
        injector.release_all()
        if session["ws"] is ws:
            session["ws"] = None
        print(f"[-] {peer} disconnected")
    return ws


def load_password(cli_password):
    if cli_password:
        return cli_password
    if os.environ.get("LITEVIEW_PASSWORD"):
        return os.environ["LITEVIEW_PASSWORD"]
    if PASSWORD_FILE.exists():
        return PASSWORD_FILE.read_text().strip()
    password = secrets.token_urlsafe(9)
    PASSWORD_FILE.write_text(password + "\n")
    try:
        PASSWORD_FILE.chmod(0o600)
    except OSError:
        pass
    return password


def lan_ip():
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        try:
            s.connect(("10.255.255.255", 1))
            return s.getsockname()[0]
        except OSError:
            return "127.0.0.1"


TAILSCALE_NET = ipaddress.ip_network("100.64.0.0/10")


def tailscale_ip():
    """This computer's Tailscale IPv4 address, or None if Tailscale isn't connected."""
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
        try:
            s.connect(("100.100.100.100", 1))
            ip = s.getsockname()[0]
        except OSError:
            return None
    return ip if ipaddress.ip_address(ip) in TAILSCALE_NET else None


def wait_for_tailscale():
    ip = tailscale_ip()
    if ip is None:
        print("Waiting for Tailscale to connect...", flush=True)
    while ip is None:
        time.sleep(5)
        ip = tailscale_ip()
    return ip


def main():
    if sys.stdout is None:
        sys.stdout = sys.stderr = open(LOG_FILE, "a", buffering=1, encoding="utf-8")

    parser = argparse.ArgumentParser(description="LiteView host: share this screen and allow remote control.")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--password", help=f"access password (default: $LITEVIEW_PASSWORD, else saved in {PASSWORD_FILE})")
    parser.add_argument("--fps", type=float, default=15)
    parser.add_argument("--quality", type=int, default=60, help="JPEG quality 1-95 (lower = less bandwidth)")
    parser.add_argument("--max-width", type=int, default=1600, help="downscale frames wider than this")
    parser.add_argument("--view-only", action="store_true", help="share the screen but ignore mouse/keyboard")
    parser.add_argument("--tailscale-only", action="store_true",
                        help="only accept connections through Tailscale (waits for Tailscale if it isn't up yet)")
    parser.add_argument("--show-address", action="store_true",
                        help="print the addresses and password to connect with, then exit")
    parser.add_argument("--module", "--module_2", "--third-eye", "--thirdeye",
                        action="append", default=[],
                        help="enable optional LiteView modules, e.g. 'thirdeye'")
    args = parser.parse_args()

    password = load_password(args.password)

    if args.show_address:
        ts_ip = tailscale_ip()
        print(f"  From anywhere (Tailscale):   http://{ts_ip}:{args.port}" if ts_ip
              else "  Tailscale not connected - only reachable on this local network.")
        if not args.tailscale_only:
            print(f"  From the same network:       http://{lan_ip()}:{args.port}")
        print(f"  Password:                    {password}")
        return

    if _eye3 is None:
        sys.exit("thirdeye (eye3) is not installed. Run: pip install eye3")
    try:
        with _eye3.ThirdEyeSession() as probe:
            # Use JPEG for the probe too — PIL reads dimensions without full decode.
            test_jpeg = probe.capture_to_buffer(
                _eye3.ThirdEyeOptions(format=_eye3.ThirdeyeFormat.JPEG, quality=50)
            )
            img = Image.open(io.BytesIO(test_jpeg))
            monitor = {"left": 0, "top": 0, "width": img.width, "height": img.height}
    except Exception as exc:
        sys.exit(f"thirdeye failed to capture the screen: {exc}")

    # Inject capture-bypass persistent DLL into browser processes so DXGI can
    # see WDA-protected windows (Netflix, etc.) without per-frame bypass tricks.
    # Silently skips if the binaries aren't present or Admin rights are missing.
    _inject_capture_bypass(HERE / "capture-bypass")

    app = web.Application()
    app.update(
        password=password,
        fps=args.fps,
        quality=args.quality,
        max_width=args.max_width,
        view_only=args.view_only,
        session={"ws": None},
        injector=InputInjector(monitor),
        capture_pool=ThreadPoolExecutor(max_workers=1, thread_name_prefix="capture"),
        capture_backend="thirdeye",
    )
    app.router.add_get("/", index)
    app.router.add_get("/ws", ws_handler)

    for requested in args.module:
        requested_name = requested.strip().lower().replace("_", "-")
        if requested_name in {"thirdeye", "third-eye", "module-2", "module_2"}:
            if ThirdEyeModule is None:
                print("[!] ThirdEye module is unavailable in this checkout.")
                continue
            module = ThirdEyeModule()
            module.register(app, frame_provider=grab_jpeg)
            app["third_eye"] = module
            app.setdefault("modules", []).append(module.name)
            print("[+] ThirdEye module enabled")
        else:
            print(f"[!] Unknown module '{requested}' (supported: thirdeye)")

    if args.tailscale_only:
        ts_ip = wait_for_tailscale()
        bind_host = ts_ip
    else:
        ts_ip = tailscale_ip()
        bind_host = "0.0.0.0"

    print("LiteView host is running.")
    if ts_ip:
        print(f"  From anywhere (Tailscale):   http://{ts_ip}:{args.port}")
    else:
        print("  Tailscale not connected - only reachable on this local network.")
    if not args.tailscale_only:
        print(f"  From the same network:       http://{lan_ip()}:{args.port}")
    print(f"  Password:                    {app['password']}")
    print(f"  Screen:                      {monitor['width']}x{monitor['height']}"
          + ("  (view only)" if args.view_only else ""))
    if app.get("modules"):
        print(f"  Modules:                     {', '.join(app['modules'])}")
    web.run_app(app, host=bind_host, port=args.port, print=None)


if __name__ == "__main__":
    main()
