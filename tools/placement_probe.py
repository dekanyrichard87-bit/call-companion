"""Placement probe (for 0.9.0-rc "corner first, step aside when occupied").

Read-only: lists the windows that cover the bottom-right corner of the screen, every 0.25 s, and prints a line only
when something changes. Nothing is installed or changed. Standard library only (ctypes), Windows only.
Diagnostic tool: not bundled in the exe / installer.

Run (PowerShell, in the repo folder):
    cd d:\\callcompanion\\call-companion
    .venv\\Scripts\\python tools\\placement_probe.py            # 90 s
    .venv\\Scripts\\python tools\\placement_probe.py 120        # other duration in seconds

While it runs: 1) let a call ring (Bria's toast), 2) trigger a Windows notification (e.g. Teams / Outlook, or
Settings > System > Notifications test), 3) open and move Bria's main window near the corner.
The result is also written to %LOCALAPPDATA%\\CallCompanion\\placement_probe.txt (send that file).

Privacy: window titles are shortened to 24 characters and every digit is replaced by '#', so no phone number ends
up in the file.
"""
import ctypes
import os
import sys
import time
from ctypes import wintypes
from datetime import datetime

if sys.platform != "win32":
    sys.exit("Windows only")

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
dwmapi = ctypes.WinDLL("dwmapi")
try:
    ctypes.WinDLL("shcore").SetProcessDpiAwareness(2)          # per-monitor aware: real pixel coordinates
except (OSError, AttributeError):
    user32.SetProcessDPIAware()

ZONE_W, ZONE_H = 900, 700        # logical px from the bottom-right corner of the work area that we watch
GWL_STYLE, GWL_EXSTYLE = -16, -20
WS_EX = {"TOPMOST": 0x8, "TOOLWINDOW": 0x80, "LAYERED": 0x80000, "NOACTIVATE": 0x8000000, "TRANSPARENT": 0x20,
         "APPWINDOW": 0x40000}
WS_POPUP, WS_CAPTION = 0x80000000, 0x00C00000
DWMWA_CLOAKED, DWMWA_EXTENDED_FRAME_BOUNDS = 14, 9
SPI_GETWORKAREA = 0x30
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)

user32.GetWindowLongW.restype = ctypes.c_long
user32.GetDpiForWindow.restype = wintypes.UINT


def work_area():
    r = wintypes.RECT()
    user32.SystemParametersInfoW(SPI_GETWORKAREA, 0, ctypes.byref(r), 0)
    return r.left, r.top, r.right, r.bottom


def proc_name(hwnd):
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    h = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
    if not h:
        return f"pid{pid.value}", pid.value
    try:
        buf = ctypes.create_unicode_buffer(512)
        size = wintypes.DWORD(512)
        if kernel32.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)):
            return os.path.basename(buf.value), pid.value
        return f"pid{pid.value}", pid.value
    finally:
        kernel32.CloseHandle(h)


def rect_of(hwnd):
    r = wintypes.RECT()
    if dwmapi.DwmGetWindowAttribute(hwnd, DWMWA_EXTENDED_FRAME_BOUNDS, ctypes.byref(r), ctypes.sizeof(r)) != 0:
        user32.GetWindowRect(hwnd, ctypes.byref(r))
    return r.left, r.top, r.right, r.bottom


def cloaked(hwnd):
    v = ctypes.c_int(0)
    dwmapi.DwmGetWindowAttribute(hwnd, DWMWA_CLOAKED, ctypes.byref(v), ctypes.sizeof(v))
    return v.value != 0


def text(hwnd, fn, n=256):
    buf = ctypes.create_unicode_buffer(n)
    fn(hwnd, buf, n)
    return buf.value


def safe_title(t):
    t = "".join("#" if ch.isdigit() else ch for ch in t)
    return (t[:24] + "…") if len(t) > 24 else t


def snapshot(zone, own_pid):
    out = []

    def cb(hwnd, _):
        if not user32.IsWindowVisible(hwnd) or user32.IsIconic(hwnd):
            return True
        l, t, r, b = rect_of(hwnd)
        if r - l <= 0 or b - t <= 0:
            return True
        zl, zt, zr, zb = zone
        in_zone = not (r <= zl or l >= zr or b <= zt or t >= zb)
        ex = user32.GetWindowLongW(hwnd, GWL_EXSTYLE) & 0xFFFFFFFF
        name, pid = proc_name(hwnd)
        if pid == own_pid:
            return True
        is_bria = "bria" in name.lower()
        small_topmost = bool(ex & WS_EX["TOPMOST"]) and (r - l) < 1400 and (b - t) < 1000
        # v2: outside the corner zone only Bria's windows and small always-on-top windows (any monitor)
        if not (in_zone or is_bria or small_topmost):
            return True
        st = user32.GetWindowLongW(hwnd, GWL_STYLE) & 0xFFFFFFFF
        flags = [k for k, bit in WS_EX.items() if ex & bit]
        if st & WS_POPUP:
            flags.append("POPUP")
        if st & WS_CAPTION == WS_CAPTION:
            flags.append("CAPTION")
        if cloaked(hwnd):
            flags.append("CLOAKED")
        try:
            scale = user32.GetDpiForWindow(hwnd) / 96.0 or 1.0
        except (AttributeError, OSError):
            scale = 1.0
        out.append({
            "hwnd": int(hwnd or 0), "proc": name, "cls": text(hwnd, user32.GetClassNameW),
            "title": safe_title(text(hwnd, user32.GetWindowTextW)), "rect": (l, t, r, b),
            "logical": (round((r - l) / scale), round((b - t) / scale)), "scale": scale,
            "flags": flags + ([] if in_zone else ["OUTSIDE-ZONE"]),
        })
        return True

    user32.EnumWindows(WNDENUMPROC(cb), 0)
    return out


def fmt(w):
    l, t, r, b = w["rect"]
    return (f"{w['proc']:<24} cls={w['cls'][:34]:<34} {w['logical'][0]}x{w['logical'][1]} (px {r - l}x{b - t} "
            f"@{l},{t}) [{' '.join(w['flags'])}] title='{w['title']}'")


def main():
    seconds = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else 90
    wa = work_area()
    scale = user32.GetDpiForSystem() / 96.0 if hasattr(user32, "GetDpiForSystem") else 1.0
    zone = (wa[2] - int(ZONE_W * scale), wa[3] - int(ZONE_H * scale), wa[2], wa[3])
    out_dir = os.path.join(os.environ.get("LOCALAPPDATA", os.path.expanduser("~")), "CallCompanion")
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, "placement_probe.txt")
    lines = []

    def emit(s):
        line = f"{datetime.now():%H:%M:%S.%f}"[:-3] + "  " + s
        print(line, flush=True)
        lines.append(line)

    mons = []
    MONENUM = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HMONITOR, wintypes.HDC, ctypes.POINTER(wintypes.RECT),
                                 wintypes.LPARAM)
    user32.EnumDisplayMonitors(None, None, MONENUM(lambda h, d, r, p: mons.append(
        (r.contents.left, r.contents.top, r.contents.right, r.contents.bottom)) or True), 0)
    emit(f"probe v2 | monitors {mons}")
    emit(f"placement probe, {seconds} s | work area {wa} | system scale {scale:.2f} | corner zone {zone}")
    emit("Most: csörgess meg egy hívást, és figyeld, hol jelenik meg a Bria értesítése (melyik monitoron, hol).")
    own = os.getpid()
    prev = {}
    end = time.monotonic() + seconds
    try:
        while time.monotonic() < end:
            cur = {w["hwnd"]: w for w in snapshot(zone, own)}
            for h, w in cur.items():
                old = prev.get(h)
                if old is None:
                    emit("+ " + fmt(w))
                elif old["rect"] != w["rect"] or old["flags"] != w["flags"]:
                    emit("~ " + fmt(w))
            for h, w in prev.items():
                if h not in cur:
                    emit("- " + fmt(w))
            prev = cur
            time.sleep(0.25)
    except KeyboardInterrupt:
        emit("stopped (Ctrl+C)")
    emit("done")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines) + "\n")
    print(f"\nMentve: {path}")


if __name__ == "__main__":
    main()
