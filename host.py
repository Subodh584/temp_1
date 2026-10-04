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
LOG_FILE      = Path.home() / ".liteview.log"

# ---------------------------------------------------------------- DWM hook reader
# When liteview_dwm_hook.dll is injected into dwm.exe it writes every composited
# frame to a named shared-memory mapping BEFORE the GPU driver applies the
# WDA_EXCLUDEFROMCAPTURE black-out.  We read those frames here.

_SHM_NAME       = "Local\\LiteViewFrame"
_SHM_TOTAL_SIZE = 64 + 3840 * 2160 * 4   # header (64 B) + worst-case 4K BGRA
_PIXFMT_BGRA8   = 0
_PIXFMT_RGBA8   = 1
_PIXFMT_RGB10A2 = 2

class _DWMReader:
    """Reads frames from the shared memory written by liteview_dwm_hook.dll."""
    def __init__(self):
        import ctypes, ctypes.wintypes
        k32 = ctypes.windll.kernel32
        self._k32 = k32
        FILE_MAP_READ = 0x0004
        self._hMap = k32.OpenFileMappingW(FILE_MAP_READ, False, _SHM_NAME)
        if not self._hMap:
            raise OSError("DWM hook shared memory not found")
        self._ptr = k32.MapViewOfFile(self._hMap, FILE_MAP_READ, 0, 0, _SHM_TOTAL_SIZE)
        if not self._ptr:
            k32.CloseHandle(self._hMap)
            raise OSError("Failed to map DWM shared memory")
        self._last_frame = 0

    def grab(self):
        """Return (width, height, format_code, bytes) or None if no new frame."""
        import ctypes
        base = self._ptr
        # Read header fields (offsets match FrameHeader in shared.h)
        magic    = ctypes.c_uint32.from_address(base +  0).value
        width    = ctypes.c_uint32.from_address(base +  4).value
        height   = ctypes.c_uint32.from_address(base +  8).value
        fmt      = ctypes.c_uint32.from_address(base + 12).value
        frameNum = ctypes.c_uint64.from_address(base + 16).value
        ready    = ctypes.c_uint32.from_address(base + 24).value

        if magic != 0x4C564448 or not ready or frameNum == self._last_frame:
            return None
        if width == 0 or height == 0:
            return None
        pixel_bytes = width * height * 4
        data = (ctypes.c_uint8 * pixel_bytes).from_address(base + 64)
        result = (width, height, fmt, bytes(data))
        self._last_frame = frameNum
        return result

    def close(self):
        if self._ptr:
            self._k32.UnmapViewOfFile(self._ptr)
            self._ptr = 0
        if self._hMap:
            self._k32.CloseHandle(self._hMap)
            self._hMap = None


_dwm_reader: "_DWMReader | None" = None


def _enable_debug_privilege() -> bool:
    """Enable SeDebugPrivilege so OpenProcess can open system processes like dwm.exe.
    The privilege is present in admin tokens but disabled by default."""
    try:
        import ctypes, ctypes.wintypes
        advapi32 = ctypes.windll.advapi32
        k32 = ctypes.windll.kernel32
        TOKEN_ADJUST_PRIVILEGES = 0x0020
        TOKEN_QUERY = 0x0008
        SE_PRIVILEGE_ENABLED = 0x00000002

        class _LUID(ctypes.Structure):
            _fields_ = [("LowPart", ctypes.wintypes.DWORD), ("HighPart", ctypes.c_long)]

        class _LUID_ATTR(ctypes.Structure):
            _fields_ = [("Luid", _LUID), ("Attributes", ctypes.wintypes.DWORD)]

        class _TOKEN_PRIVS(ctypes.Structure):
            _fields_ = [("PrivilegeCount", ctypes.wintypes.DWORD),
                        ("Privileges", _LUID_ATTR * 1)]

        hTok = ctypes.wintypes.HANDLE()
        if not advapi32.OpenProcessToken(k32.GetCurrentProcess(),
                                         TOKEN_ADJUST_PRIVILEGES | TOKEN_QUERY,
                                         ctypes.byref(hTok)):
            return False
        luid = _LUID()
        advapi32.LookupPrivilegeValueW(None, "SeDebugPrivilege", ctypes.byref(luid))
        tp = _TOKEN_PRIVS()
        tp.PrivilegeCount = 1
        tp.Privileges[0].Luid = luid
        tp.Privileges[0].Attributes = SE_PRIVILEGE_ENABLED
        advapi32.AdjustTokenPrivileges(hTok, False, ctypes.byref(tp),
                                       ctypes.sizeof(tp), None, None)
        k32.CloseHandle(hTok)
        return True
    except Exception:
        return False


def _init_dwm_hook(dll_path: Path) -> bool:
    """Inject liteview_dwm_hook.dll into dwm.exe and open the shared memory."""
    global _dwm_reader
    if not dll_path.exists():
        print(f"[!] DWM hook: DLL not found at {dll_path}", flush=True)
        return False

    # Enable SeDebugPrivilege — needed to OpenProcess on dwm.exe even as admin.
    _enable_debug_privilege()

    # Find dwm.exe PID.
    try:
        out = subprocess.check_output(
            ["tasklist", "/FI", "IMAGENAME eq dwm.exe", "/FO", "CSV", "/NH"],
            text=True, timeout=5, stderr=subprocess.DEVNULL,
        )
    except Exception as exc:
        print(f"[!] DWM hook: tasklist failed: {exc}", flush=True)
        return False
    dwm_pid = None
    for line in out.splitlines():
        parts = line.split(",")
        if len(parts) >= 2 and "dwm" in parts[0].lower():
            try:
                dwm_pid = int(parts[1].strip('"'))
                break
            except ValueError:
                pass
    if not dwm_pid:
        print("[!] DWM hook: dwm.exe not found in tasklist", flush=True)
        return False

    print(f"[*] DWM hook: injecting into dwm.exe (PID {dwm_pid}) ...", flush=True)
    if not _inject_dll(dwm_pid, dll_path):
        print("[!] DWM hook: DLL refused by dwm.exe — likely blocked by Code Integrity policy "
              "(BlockNonMicrosoftBinaries). Run 'Get-ProcessMitigation -Name dwm.exe' to confirm.",
              flush=True)
        return False

    # Give the DLL's hook thread time to install the hook and create the mapping.
    # 2 s is generous; the thread only needs ~300 ms but dwm.exe can be slow to load.
    print("[*] DWM hook: waiting for hook to initialise ...", flush=True)
    time.sleep(2)
    try:
        _dwm_reader = _DWMReader()
        print("[+] DWM hook: active — all windows capturable (WDA bypassed at compositor level)", flush=True)
        return True
    except OSError as exc:
        print(f"[!] DWM hook: shared memory not ready: {exc}", flush=True)
        return False

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


def _inject_dll(pid: int, dll_path: Path) -> bool:
    """Inject dll_path into process pid via LoadLibrary remote thread."""
    try:
        import ctypes
        k32 = ctypes.windll.kernel32
        dll_bytes = str(dll_path).encode("mbcs") + b"\x00"
        h = k32.OpenProcess(0x0002 | 0x0008 | 0x0020, False, pid)  # CREATE_THREAD|VM_OP|VM_WRITE
        if not h:
            return False
        try:
            addr = k32.VirtualAllocEx(h, None, len(dll_bytes), 0x3000, 0x04)  # COMMIT|RESERVE, RW
            if not addr:
                return False
            import ctypes.wintypes
            written = ctypes.c_size_t(0)
            k32.WriteProcessMemory(h, addr, dll_bytes, len(dll_bytes), ctypes.byref(written))
            hk = k32.GetModuleHandleA(b"kernel32.dll")
            load_lib = k32.GetProcAddress(hk, b"LoadLibraryA")
            ht = k32.CreateRemoteThread(h, None, 0, load_lib, addr, 0, None)
            if not ht:
                k32.VirtualFreeEx(h, addr, 0, 0x8000)
                return False
            k32.WaitForSingleObject(ht, 5000)
            # Exit code = LoadLibraryA return value (HMODULE).
            # 0 means the DLL failed to load (blocked by code integrity policy,
            # missing dependency, or DllMain returned FALSE).
            exit_code = ctypes.c_ulong(0)
            k32.GetExitCodeThread(ht, ctypes.byref(exit_code))
            k32.CloseHandle(ht)
            k32.VirtualFreeEx(h, addr, 0, 0x8000)
            return exit_code.value != 0
        finally:
            k32.CloseHandle(h)
    except Exception:
        return False


def _get_process_name(pid: int) -> str:
    """Return the lowercase exe name for a PID, or '' on failure."""
    try:
        import ctypes
        k32 = ctypes.windll.kernel32
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        h = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not h:
            return ''
        buf = ctypes.create_unicode_buffer(260)
        size = ctypes.c_ulong(260)
        k32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size))
        k32.CloseHandle(h)
        return buf.value.lower()
    except Exception:
        return ''


# Process names (substrings, lowercase) that self-terminate on DLL injection.
# We skip these entirely so they don't close themselves when LiteView starts.
_INJECTION_RESISTANT = ("hackerrank", "proctorio", "respondus", "examity", "honorlock")


def _capture_bypass_monitor(cb_dir: Path):
    """Background thread: watch for WDA-protected windows and inject into them.
    Skips processes that actively resist injection (e.g. HackerRank desktop app)."""
    dll = cb_dir / "payload_dll_persistent.dll"
    if not dll.exists():
        print(f"[!] capture-bypass: {dll} not found — run the install script to download it", flush=True)
        return

    print("[*] capture-bypass: monitor started (watching for protected windows)", flush=True)
    injected: set[int] = set()
    skip: set[int] = set()
    while True:
        for pid in _get_protected_pids():
            if pid not in injected and pid not in skip:
                name = _get_process_name(pid)
                if any(r in name for r in _INJECTION_RESISTANT):
                    skip.add(pid)  # known anti-injection app; never touch it
                    continue
                if _inject_dll(pid, dll):
                    print(f"[+] capture-bypass: cleared WDA on PID {pid}", flush=True)
                    injected.add(pid)
                else:
                    skip.add(pid)  # injection-resistant process; don't retry
        time.sleep(2)


def _grab_print_window(hwnd: int, max_width: int, quality: int):
    """Capture hwnd using PrintWindow(PW_RENDERFULLCONTENT) — no injection needed.
    Works on some WDA-protected apps (e.g. Electron apps with anti-injection).
    Returns JPEG bytes or None if the window can't be captured this way."""
    try:
        import ctypes
        import ctypes.wintypes
        user32  = ctypes.windll.user32
        gdi32   = ctypes.windll.gdi32
        PW_RENDERFULLCONTENT = 0x00000002

        rect = ctypes.wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        w = rect.right  - rect.left
        h = rect.bottom - rect.top
        if w <= 0 or h <= 0:
            return None

        hdc_screen = user32.GetDC(None)
        hdc_mem    = gdi32.CreateCompatibleDC(hdc_screen)
        hbmp       = gdi32.CreateCompatibleBitmap(hdc_screen, w, h)
        gdi32.SelectObject(hdc_mem, hbmp)

        ok = user32.PrintWindow(hwnd, hdc_mem, PW_RENDERFULLCONTENT)

        if ok:
            # Convert HBITMAP → PIL Image via BITMAPINFOHEADER
            class BITMAPINFOHEADER(ctypes.Structure):
                _fields_ = [
                    ("biSize",          ctypes.wintypes.DWORD),
                    ("biWidth",         ctypes.wintypes.LONG),
                    ("biHeight",        ctypes.wintypes.LONG),
                    ("biPlanes",        ctypes.wintypes.WORD),
                    ("biBitCount",      ctypes.wintypes.WORD),
                    ("biCompression",   ctypes.wintypes.DWORD),
                    ("biSizeImage",     ctypes.wintypes.DWORD),
                    ("biXPelsPerMeter", ctypes.wintypes.LONG),
                    ("biYPelsPerMeter", ctypes.wintypes.LONG),
                    ("biClrUsed",       ctypes.wintypes.DWORD),
                    ("biClrImportant",  ctypes.wintypes.DWORD),
                ]
            bih = BITMAPINFOHEADER()
            bih.biSize      = ctypes.sizeof(BITMAPINFOHEADER)
            bih.biWidth     = w
            bih.biHeight    = -h  # top-down
            bih.biPlanes    = 1
            bih.biBitCount  = 32
            bih.biCompression = 0  # BI_RGB
            buf = (ctypes.c_char * (w * h * 4))()
            gdi32.GetDIBits(hdc_mem, hbmp, 0, h, buf, ctypes.byref(bih), 0)
            img = Image.frombuffer("RGBA", (w, h), bytes(buf), "raw", "BGRA", 0, 1)
            img = img.convert("RGB")
            if img.width > max_width:
                img = img.resize(
                    (max_width, round(img.height * max_width / img.width)),
                    Image.BILINEAR,
                )
            out = io.BytesIO()
            img.save(out, "JPEG", quality=quality)
            result = out.getvalue()
        else:
            result = None

        gdi32.DeleteObject(hbmp)
        gdi32.DeleteDC(hdc_mem)
        user32.ReleaseDC(None, hdc_screen)
        return result
    except Exception:
        return None


def _find_protected_hwnds():
    """Return list of HWNDs that have WDA protection set."""
    try:
        import ctypes
        import ctypes.wintypes
        user32 = ctypes.windll.user32
    except Exception:
        return []

    hwnds = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.wintypes.HWND, ctypes.wintypes.LPARAM)
    def _cb(hwnd, _):
        affinity = ctypes.c_uint(0)
        user32.GetWindowDisplayAffinity(hwnd, ctypes.byref(affinity))
        if affinity.value != 0:
            hwnds.append(hwnd)
        return True

    user32.EnumWindows(_cb, 0)
    return hwnds


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

    # Tier 1: DWM hook — reads DWM's compositor back buffer via shared memory,
    # captured BEFORE the GPU driver applies the WDA black-out.  Works on every
    # app including HackerRank desktop without touching the target process at all.
    if _dwm_reader is not None:
        try:
            frame = _dwm_reader.grab()
            if frame is not None:
                w, h, fmt, raw = frame
                if fmt == _PIXFMT_BGRA8:
                    import numpy as np
                    arr = np.frombuffer(raw, dtype=np.uint8).reshape(h, w, 4)
                    img = Image.fromarray(arr[:, :, [2, 1, 0]], "RGB")  # BGRA→RGB
                elif fmt == _PIXFMT_RGBA8:
                    import numpy as np
                    arr = np.frombuffer(raw, dtype=np.uint8).reshape(h, w, 4)
                    img = Image.fromarray(arr[:, :, :3], "RGB")
                else:
                    img = None
                if img is not None:
                    if img.width > max_width:
                        img = img.resize(
                            (max_width, round(img.height * max_width / img.width)),
                            Image.BILINEAR,
                        )
                    buf = io.BytesIO()
                    img.save(buf, "JPEG", quality=quality)
                    jpeg = buf.getvalue()
        except Exception as exc:
            print(f"[DWM hook] read error: {exc}", flush=True)

    # Tier 2: dxcam (DXGI) — captures GPU-composited frames including hardware video.
    # Works when WDA has been cleared by the capture-bypass monitor thread.
    if jpeg is None and _dxcam_camera is not None:
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

    # Tier 2: PrintWindow(PW_RENDERFULLCONTENT) on any still-protected window.
    # No injection — asks the window to render itself via DWM. Works on apps like
    # HackerRank desktop that resist DLL injection but don't block PrintWindow.
    if jpeg is None:
        for hwnd in _find_protected_hwnds():
            pw_jpeg = _grab_print_window(hwnd, max_width, quality)
            if pw_jpeg:
                jpeg = pw_jpeg
                break

    # Tier 3: thirdeye BitBlt with per-frame WDA bypass — universal fallback.
    if jpeg is None:
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

    # DWM hook: inject liteview_dwm_hook.dll into dwm.exe so every frame is
    # captured at the compositor level, bypassing WDA for all apps at once.
    # Falls back silently if the DLL hasn't been built yet.
    # DLL can be in build\ (cl.exe direct build) or build\Release\ (cmake build).
    _dwm_dll = HERE / "module_4" / "dwm-hook" / "build" / "liteview_dwm_hook.dll"
    if not _dwm_dll.exists():
        _dwm_dll = HERE / "module_4" / "dwm-hook" / "build" / "Release" / "liteview_dwm_hook.dll"
    _init_dwm_hook(_dwm_dll)

    # Fallback background thread for per-process WDA injection (browsers, etc.)
    # when the DWM hook is not available.
    if _dwm_reader is None:
        threading.Thread(
            target=_capture_bypass_monitor,
            args=(HERE / "capture-bypass",),
            daemon=True,
        ).start()

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
