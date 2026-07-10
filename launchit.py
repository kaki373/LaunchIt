"""LaunchIt - lightweight resident launcher for WebUI-style apps.

Sits in the system tray, pops up with a global hotkey, and can launch /
restart / stop .bat-driven apps (whole process tree), open folders and URLs.
Configuration lives in launchit.json next to this file.
"""

import ctypes
import ctypes.wintypes
import json
import logging
import os
import queue
import re
import socket
import subprocess
import sys
import threading
import time
import urllib.parse
import webbrowser
import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog

APP_TITLE = "LaunchIt"
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "launchit.json")
LOG_PATH = os.path.join(BASE_DIR, "launchit.log")
IPC_PORT = 48123
STILL_ACTIVE = 259
CREATE_NO_WINDOW = 0x08000000
WM_HOTKEY = 0x0312

logging.basicConfig(
    filename=LOG_PATH,
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    encoding="utf-8",
)
log = logging.getLogger("launchit")

WT_EXE = os.path.expandvars(
    r"%LOCALAPPDATA%\Microsoft\WindowsApps\wt.exe"
)

DEFAULT_CONFIG = {
    "hotkey": "ctrl+space",
    "hotkey_passthrough": ["AfterFX.exe"],
    "position": "bottom-right",
    "columns": 2,
    "width": 720,
    "height": 0,
    "items": [
        {
            "name": "My WebUI app",
            "type": "app",
            "path": "C:/path/to/app/start.bat",
            "stop_path": "C:/path/to/app/stop.bat",
            "url": "http://127.0.0.1:8188",
        },
        {"name": "Notepad", "type": "app", "path": "C:/Windows/notepad.exe"},
        {
            "name": "Claude (new session)",
            "type": "app",
            "track": False,
            "path": WT_EXE,
            "args": ["-d", os.path.expanduser("~"), "claude"],
        },
        {"name": "Claude", "type": "wingroup",
         "title_re": "^[✳⠀-⣿]", "color": "#e8926b"},
        {"name": "Codex", "type": "wingroup",
         "title_re": "(?i)codex", "color": "#10a37f"},
        {"name": "Home", "type": "folder", "path": os.path.expanduser("~")},
        {"name": "GitHub", "type": "url", "url": "https://github.com"},
    ],
}


def load_config():
    if not os.path.exists(CONFIG_PATH):
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(DEFAULT_CONFIG, f, ensure_ascii=False, indent=2)
        log.info("default config created: %s", CONFIG_PATH)
        return json.loads(json.dumps(DEFAULT_CONFIG))
    with open(CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)


# ---------------------------------------------------------------- hotkey ----

MOD_KEYS = {"alt": 0x0001, "ctrl": 0x0002, "shift": 0x0004, "win": 0x0008}
MOD_NOREPEAT = 0x4000
VK_KEYS = {
    "space": 0x20, "tab": 0x09, "enter": 0x0D, "esc": 0x1B,
    "up": 0x26, "down": 0x28, "left": 0x25, "right": 0x27,
    "home": 0x24, "end": 0x23, "insert": 0x2D, "delete": 0x2E,
}


def parse_hotkey(spec):
    """'ctrl+alt+space' -> (modifier flags, virtual key code)."""
    mods, vk = 0, None
    for part in spec.lower().replace(" ", "").split("+"):
        if part in MOD_KEYS:
            mods |= MOD_KEYS[part]
        elif part in VK_KEYS:
            vk = VK_KEYS[part]
        elif len(part) == 1 and part.isalnum():
            vk = ord(part.upper())
        elif part.startswith("f") and part[1:].isdigit():
            vk = 0x70 + int(part[1:]) - 1
        else:
            raise ValueError(f"unknown key in hotkey: {part}")
    if vk is None:
        raise ValueError(f"hotkey needs a non-modifier key: {spec}")
    return mods | MOD_NOREPEAT, vk


WM_APP_REREGISTER = 0x8001
MOD_VKS = {"shift": 0x10, "ctrl": 0x11, "alt": 0x12, "win": 0x5B}


def _pid_exe(pid):
    """Basename (lowercased) of a process's executable, or ''."""
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(0x1000, False, pid)
    if not handle:
        return ""
    buf = ctypes.create_unicode_buffer(260)
    size = ctypes.wintypes.DWORD(260)
    ok = kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size))
    kernel32.CloseHandle(handle)
    return os.path.basename(buf.value).lower() if ok else ""


def _foreground_exe():
    """Basename (lowercased) of the foreground window's process, or ''."""
    user32 = ctypes.windll.user32
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return ""
    pid = ctypes.wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return _pid_exe(pid.value)


class HotkeyListener(threading.Thread):
    def __init__(self, spec, passthrough, cmd_queue):
        super().__init__(daemon=True)
        self.spec = spec
        self.passthrough = {p.lower() for p in passthrough}
        self.q = cmd_queue
        self.tid = None
        self._combo_vks = []

    def update(self, spec, passthrough):
        """Apply new hotkey / passthrough list without restarting."""
        self.passthrough = {p.lower() for p in passthrough}
        if spec == self.spec:
            return
        self.spec = spec
        if self.tid:
            ctypes.windll.user32.PostThreadMessageW(self.tid, WM_APP_REREGISTER, 0, 0)

    def _register(self, user32, quiet=False):
        try:
            mods, vk = parse_hotkey(self.spec)
        except ValueError as e:
            log.error("hotkey parse failed: %s", e)
            return
        parts = self.spec.lower().replace(" ", "").split("+")
        self._combo_vks = [MOD_VKS[p] for p in parts if p in MOD_VKS] + [vk]
        # retry: right after a self-restart the dying instance may still
        # hold the combo for a moment
        for _ in range(15):
            if user32.RegisterHotKey(None, 1, mods, vk):
                if not quiet:
                    log.info("hotkey registered: %s", self.spec)
                return
            _sleep(0.2)
        log.error("RegisterHotKey failed for %r (already in use?)", self.spec)

    def _reinject(self, user32):
        """Hand the swallowed combo back to the foreground app: unregister,
        synthesize the same keys, re-register."""
        user32.UnregisterHotKey(None, 1)
        for vk in self._combo_vks:
            user32.keybd_event(vk, 0, 0, 0)
        for vk in reversed(self._combo_vks):
            user32.keybd_event(vk, 0, 2, 0)
        # let the foreground app consume the synthetic keys before we
        # re-register, or we would swallow them again ourselves
        _sleep(0.15)
        self._register(user32, quiet=True)

    def run(self):
        user32 = ctypes.windll.user32
        self.tid = ctypes.windll.kernel32.GetCurrentThreadId()
        self._register(user32)
        msg = ctypes.wintypes.MSG()
        while user32.GetMessageW(ctypes.byref(msg), None, 0, 0):
            if msg.message == WM_HOTKEY:
                try:
                    fg = _foreground_exe()
                except Exception:
                    log.exception("foreground check failed")
                    fg = ""
                if self.passthrough and fg in self.passthrough:
                    log.info("hotkey passthrough to %s", fg)
                    self._reinject(user32)
                else:
                    log.info("hotkey fired (foreground=%s)", fg)
                    self.q.put("toggle")
            elif msg.message == WM_APP_REREGISTER:
                user32.UnregisterHotKey(None, 1)
                self._register(user32)


# ------------------------------------------------------------- processes ----

def _check_target(item):
    """(host, port) to health-check for an app item, or None.
    Uses explicit "check_port", else the port of a local "url"."""
    port = item.get("check_port")
    if port:
        return ("127.0.0.1", int(port))
    url = item.get("url")
    if url:
        u = urllib.parse.urlparse(url)
        if u.hostname in ("127.0.0.1", "localhost") and u.port:
            return ("127.0.0.1", u.port)
    return None


def _port_open(target, timeout=0.3):
    try:
        with socket.create_connection(target, timeout=timeout):
            return True
    except OSError:
        return False


class ProcessManager:
    """Tracks launched apps by item name; stop kills the whole process tree."""

    def __init__(self):
        self.procs = {}  # name -> {"popen": Popen|None, "pid": int}

    def pid_of(self, name):
        rec = self.procs.get(name)
        return rec["pid"] if rec else None

    def is_running(self, name):
        rec = self.procs.get(name)
        if not rec:
            return False
        if rec["popen"] is not None:
            return rec["popen"].poll() is None
        return _pid_alive(rec["pid"])

    def launch(self, item):
        name = item["name"]
        if self.is_running(name):
            return False
        path = item["path"]
        ext = os.path.splitext(path)[1].lower()
        if ext in (".bat", ".cmd"):
            args = ["cmd.exe", "/c", path]
        elif ext == ".py":
            args = [sys.executable, path]
        else:
            args = [path]
        args += item.get("args", [])
        cwd = item.get("cwd") or os.path.dirname(path)
        popen = subprocess.Popen(
            args, cwd=cwd, creationflags=subprocess.CREATE_NEW_CONSOLE
        )
        if item.get("track", True):
            self.procs[name] = {"popen": popen, "pid": popen.pid}
        log.info("launched %s (pid %d): %s", name, popen.pid, path)
        return True

    def stop(self, item, timeout=15):
        name = item["name"]
        tracked = self.is_running(name)
        target = _check_target(item)
        if not tracked and not (target and _port_open(target)):
            return False
        stop_path = item.get("stop_path")

        def alive():
            if tracked and self.is_running(name):
                return True
            return bool(target and _port_open(target))

        if stop_path and os.path.exists(stop_path):
            subprocess.Popen(
                ["cmd.exe", "/c", stop_path],
                cwd=os.path.dirname(stop_path),
                creationflags=subprocess.CREATE_NEW_CONSOLE,
            )
            for _ in range(timeout * 10):
                if not alive():
                    log.info("stopped %s via stop script", name)
                    return True
                _sleep(0.1)
            log.warning("%s still alive after stop script", name)
        if tracked:
            pid = self.pid_of(name)
            _kill_tree(pid)
            for _ in range(50):
                if not self.is_running(name):
                    break
                _sleep(0.1)
            log.info("stopped %s (pid %d)", name, pid)
            return True
        return False

    def restart(self, item):
        self.stop(item)
        self.launch(item)

    def adopt_running(self, items):
        """Find already-running instances of our entries and take over their
        pids, so apps started outside LaunchIt (or before a LaunchIt restart)
        can still be stopped/restarted. .bat entries are matched by cmd.exe
        command line, .exe entries by executable path."""
        bat_items, exe_items = [], []
        for i in items:
            if i.get("type", "app") != "app" or not i.get("track", True):
                continue
            ext = os.path.splitext(i.get("path", ""))[1].lower()
            if ext in (".bat", ".cmd"):
                bat_items.append(i)
            elif ext == ".exe":
                exe_items.append(i)
        if not bat_items and not exe_items:
            return 0
        names = {os.path.basename(i["path"]) for i in exe_items}
        if bat_items:
            names.add("cmd.exe")
        flt = " OR ".join(f"Name='{n}'" for n in sorted(names))
        ps = (f"Get-CimInstance Win32_Process -Filter \"{flt}\" | "
              "Select-Object ProcessId,Name,ExecutablePath,CommandLine | "
              "ConvertTo-Json -Compress")
        try:
            out = subprocess.run(
                ["powershell", "-NoProfile", "-Command", ps],
                capture_output=True, text=True, timeout=30,
                creationflags=CREATE_NO_WINDOW,
            )
            data = json.loads(out.stdout or "[]")
        except Exception as e:
            log.error("adopt scan failed: %s", e)
            return 0
        if isinstance(data, dict):
            data = [data]
        adopted = 0

        def adopt(item, proc):
            self.procs[item["name"]] = {"popen": None, "pid": proc["ProcessId"]}
            log.info("adopted %s (pid %s)", item["name"], proc["ProcessId"])

        for item in bat_items:
            if self.is_running(item["name"]):
                continue
            needle = os.path.normpath(item["path"]).lower()
            for proc in data:
                cmdline = (proc.get("CommandLine") or "").lower().replace("/", "\\")
                if (proc.get("Name", "").lower() == "cmd.exe"
                        and needle in cmdline):
                    adopt(item, proc)
                    adopted += 1
                    break
        for item in exe_items:
            if self.is_running(item["name"]):
                continue
            needle = os.path.normpath(item["path"]).lower()
            for proc in data:
                exe = os.path.normpath(proc.get("ExecutablePath") or "").lower()
                if exe == needle:
                    adopt(item, proc)
                    adopted += 1
                    break
        return adopted


def _sleep(sec):
    threading.Event().wait(sec)


def _pid_alive(pid):
    kernel32 = ctypes.windll.kernel32
    handle = kernel32.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION
    if not handle:
        return False
    code = ctypes.c_ulong()
    ok = kernel32.GetExitCodeProcess(handle, ctypes.byref(code))
    kernel32.CloseHandle(handle)
    return bool(ok) and code.value == STILL_ACTIVE


def _kill_tree(pid):
    subprocess.run(
        ["taskkill", "/PID", str(pid), "/T", "/F"],
        capture_output=True, creationflags=CREATE_NO_WINDOW,
    )


# ------------------------------------------------------------------- IPC ----

IPC_HANDLERS = {}  # commands answered inline instead of queued (e.g. status)


def start_ipc_server(cmd_queue):
    """Bind the single-instance port; return the server socket.
    Raises OSError if another instance already owns the port."""
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.bind(("127.0.0.1", IPC_PORT))
    srv.listen(2)

    def serve():
        while True:
            try:
                conn, _ = srv.accept()
            except OSError:
                return
            try:
                cmd = conn.recv(64).decode("utf-8", "ignore").strip().lower()
                if cmd == "ping":
                    conn.sendall(b"pong")
                elif cmd in IPC_HANDLERS:
                    try:
                        conn.sendall(IPC_HANDLERS[cmd]())
                    except Exception:
                        log.exception("ipc handler failed: %s", cmd)
                elif cmd:
                    cmd_queue.put(cmd)
            finally:
                conn.close()

    threading.Thread(target=serve, daemon=True).start()
    return srv


def send_ipc(cmd):
    with socket.create_connection(("127.0.0.1", IPC_PORT), timeout=3) as s:
        s.sendall(cmd.encode())


# ------------------------------------------------------------------ tray ----

def start_tray(cmd_queue):
    import pystray
    from PIL import Image, ImageDraw

    img = None
    ico = os.path.join(BASE_DIR, "launchit.ico")
    if os.path.exists(ico):
        try:
            img = Image.open(ico)
            img.size = (64, 64)  # pick the 64px frame
            img = img.convert("RGBA")
        except Exception:
            log.exception("failed to load launchit.ico, using fallback icon")
            img = None
    if img is None:
        img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        d.rounded_rectangle([4, 4, 60, 60], radius=14, fill=(52, 78, 204, 255))
        d.polygon([(24, 18), (24, 46), (48, 32)], fill=(255, 255, 255, 255))

    menu = pystray.Menu(
        pystray.MenuItem("表示", lambda: cmd_queue.put("show"), default=True),
        pystray.MenuItem("実行中アプリを再スキャン", lambda: cmd_queue.put("rescan")),
        pystray.MenuItem("設定を再読み込み", lambda: cmd_queue.put("reload")),
        pystray.MenuItem("項目を編集", lambda: cmd_queue.put("edititems")),
        pystray.MenuItem("設定ファイルを編集", lambda: cmd_queue.put("editcfg")),
        pystray.MenuItem("LaunchIt を再起動", lambda: cmd_queue.put("selfrestart")),
        pystray.MenuItem("終了", lambda: cmd_queue.put("quit")),
    )
    icon = pystray.Icon(APP_TITLE, img, APP_TITLE, menu)
    threading.Thread(target=icon.run, daemon=True).start()
    return icon


# -------------------------------------------------------------------- UI ----

class _MONITORINFO(ctypes.Structure):
    _fields_ = [("cbSize", ctypes.wintypes.DWORD),
                ("rcMonitor", ctypes.wintypes.RECT),
                ("rcWork", ctypes.wintypes.RECT),
                ("dwFlags", ctypes.wintypes.DWORD)]


def _cursor_work_area():
    """Work area rect of the monitor the mouse cursor is on."""
    user32 = ctypes.windll.user32
    pt = ctypes.wintypes.POINT()
    user32.GetCursorPos(ctypes.byref(pt))
    user32.MonitorFromPoint.argtypes = [ctypes.wintypes.POINT, ctypes.wintypes.DWORD]
    user32.MonitorFromPoint.restype = ctypes.c_void_p
    hmon = user32.MonitorFromPoint(pt, 2)  # MONITOR_DEFAULTTONEAREST
    mi = _MONITORINFO()
    mi.cbSize = ctypes.sizeof(_MONITORINFO)
    if hmon and user32.GetMonitorInfoW(ctypes.c_void_p(hmon), ctypes.byref(mi)):
        return mi.rcWork
    rect = ctypes.wintypes.RECT()
    user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(rect), 0)
    return rect


class _PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [("dwSize", ctypes.wintypes.DWORD),
                ("cntUsage", ctypes.wintypes.DWORD),
                ("th32ProcessID", ctypes.wintypes.DWORD),
                ("th32DefaultHeapID", ctypes.c_size_t),
                ("th32ModuleID", ctypes.wintypes.DWORD),
                ("cntThreads", ctypes.wintypes.DWORD),
                ("th32ParentProcessID", ctypes.wintypes.DWORD),
                ("pcPriClassBase", ctypes.c_long),
                ("dwFlags", ctypes.wintypes.DWORD),
                ("szExeFile", ctypes.c_wchar * 260)]


def _descendant_pids(root_pid):
    """root_pid and all its (grand)children, via a Toolhelp snapshot."""
    k32 = ctypes.windll.kernel32
    snap = k32.CreateToolhelp32Snapshot(2, 0)  # TH32CS_SNAPPROCESS
    entries = []
    entry = _PROCESSENTRY32W()
    entry.dwSize = ctypes.sizeof(_PROCESSENTRY32W)
    if k32.Process32FirstW(snap, ctypes.byref(entry)):
        while True:
            entries.append((entry.th32ProcessID, entry.th32ParentProcessID))
            if not k32.Process32NextW(snap, ctypes.byref(entry)):
                break
    k32.CloseHandle(snap)
    pids = {root_pid}
    changed = True
    while changed:
        changed = False
        for pid, ppid in entries:
            if ppid in pids and pid not in pids:
                pids.add(pid)
                changed = True
    return pids


def _visible_windows():
    """(hwnd, pid, title, area) for every visible, non-cloaked, titled
    top-level window."""
    user32 = ctypes.windll.user32
    dwmapi = ctypes.windll.dwmapi
    wins = []

    @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.wintypes.HWND, ctypes.wintypes.LPARAM)
    def cb(hwnd, _l):
        if not user32.IsWindowVisible(hwnd):
            return True
        cloaked = ctypes.wintypes.DWORD()
        dwmapi.DwmGetWindowAttribute(hwnd, 14, ctypes.byref(cloaked), 4)
        if cloaked.value:  # UWP ghost windows report visible while cloaked
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if not length:
            return True
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        pid = ctypes.wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        r = ctypes.wintypes.RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(r))
        wins.append((hwnd, pid.value, buf.value,
                     max(0, r.right - r.left) * max(0, r.bottom - r.top)))
        return True

    user32.EnumWindows(cb, 0)
    return wins


def _win_class(hwnd):
    buf = ctypes.create_unicode_buffer(64)
    ctypes.windll.user32.GetClassNameW(hwnd, buf, 64)
    return buf.value


TERMINAL_CLASSES = {"CASCADIA_HOSTING_WINDOW_CLASS", "ConsoleWindowClass"}


def _wingroup_items(group, wins):
    """Expand a "wingroup" config entry into one synthetic item per matching
    terminal window (e.g. every running Claude Code / Codex session)."""
    try:
        pat = re.compile(group.get("title_re", ""))
    except re.error:
        log.error("bad title_re in wingroup %s", group.get("name"))
        return []
    out = []
    for hwnd, _pid, title, _area in wins:
        if _win_class(hwnd) in TERMINAL_CLASSES and pat.search(title):
            # drop Claude Code's leading status marker (✳ idle / ⠐ busy)
            clean = re.sub(r"^[✳⠀-⣿]\s*", "", title)
            gname = group.get("name", "win")
            if clean.lower().startswith(gname.lower()):
                clean = clean[len(gname):].lstrip(" -–:") or clean
            out.append({"type": "_win", "hwnd": hwnd, "group": gname,
                        "color": group.get("color"),
                        "name": f"{gname}: {clean[:38]}"})
    return out


def _focus_window(hwnd):
    user32 = ctypes.windll.user32
    user32.ShowWindow(hwnd, 9 if user32.IsIconic(hwnd) else 5)  # RESTORE/SHOW
    user32.SetForegroundWindow(hwnd)


# ------------------------------------------ recent folders (shell COM) ----

class _GUID(ctypes.Structure):
    _fields_ = [("d1", ctypes.c_ulong), ("d2", ctypes.c_ushort),
                ("d3", ctypes.c_ushort), ("d4", ctypes.c_ubyte * 8)]

    def __init__(self, s):
        super().__init__()
        ctypes.windll.ole32.CLSIDFromString(s, ctypes.byref(self))


class _VARIANT(ctypes.Structure):
    _fields_ = [("vt", ctypes.c_ushort), ("r1", ctypes.c_ushort),
                ("r2", ctypes.c_ushort), ("r3", ctypes.c_ushort),
                ("val", ctypes.c_longlong), ("pad", ctypes.c_longlong)]


_CLSID_ShellLink = _GUID("{00021401-0000-0000-C000-000000000046}")
_IID_IShellLinkW = _GUID("{000214F9-0000-0000-C000-000000000046}")
_IID_IPersistFile = _GUID("{0000010B-0000-0000-C000-000000000046}")
_CLSID_ShellWindows = _GUID("{9BA05972-F6A8-11CF-A442-00A0C90A8F39}")
_IID_IWebBrowser2 = _GUID("{D30C1661-CDAF-11D0-8A3E-00C04FC9E26E}")
_IID_IShellWindows = _GUID("{85CB6900-4D95-11CF-960C-0080C7F4EE85}")


def _com_method(ptr, index, *argtypes):
    vtbl = ctypes.cast(ptr, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))[0]
    proto = ctypes.WINFUNCTYPE(ctypes.c_long, ctypes.c_void_p, *argtypes)
    return proto(vtbl[index])


def _com_release(ptr):
    _com_method(ptr, 2)(ptr)


def _recent_folders(limit=40):
    """Folder targets of the shell Recent .lnk files, newest first.
    Call off the Tk thread: os.path.isdir on a dead network share blocks."""
    recent = os.path.join(os.environ["APPDATA"], r"Microsoft\Windows\Recent")
    entries = []
    try:
        for f in os.listdir(recent):
            if f.lower().endswith(".lnk"):
                p = os.path.join(recent, f)
                try:
                    entries.append((os.path.getmtime(p), p))
                except OSError:
                    pass
    except OSError:
        return []
    entries.sort(reverse=True)
    ole32 = ctypes.windll.ole32
    ole32.CoInitialize(None)
    link = ctypes.c_void_p()
    if ole32.CoCreateInstance(
            ctypes.byref(_CLSID_ShellLink), None, 1,
            ctypes.byref(_IID_IShellLinkW), ctypes.byref(link)) != 0:
        return []
    out, seen = [], set()
    try:
        pf = ctypes.c_void_p()
        qi = _com_method(link, 0, ctypes.POINTER(_GUID),
                         ctypes.POINTER(ctypes.c_void_p))
        if qi(link, ctypes.byref(_IID_IPersistFile), ctypes.byref(pf)) != 0:
            return []
        try:
            load = _com_method(pf, 5, ctypes.c_wchar_p, ctypes.c_ulong)
            getpath = _com_method(link, 3, ctypes.c_wchar_p, ctypes.c_int,
                                  ctypes.c_void_p, ctypes.c_ulong)
            buf = ctypes.create_unicode_buffer(1024)
            fd = ctypes.create_string_buffer(1024)  # WIN32_FIND_DATAW scratch
            for _mt, lnk in entries:
                if load(pf, lnk, 0) != 0:
                    continue
                if (getpath(link, buf, 1024,
                            ctypes.cast(fd, ctypes.c_void_p), 0) != 0
                        or not buf.value):
                    continue  # S_FALSE: lnk without a file-system target
                tgt = buf.value
                key = tgt.lower().rstrip("\\")
                if key in seen or not os.path.isdir(tgt):
                    continue
                seen.add(key)
                out.append(tgt)
                if len(out) >= limit:
                    break
        finally:
            _com_release(pf)
    finally:
        _com_release(link)
    return out


def _url_to_path(url):
    if not url.lower().startswith("file:"):
        return None
    u = urllib.parse.urlparse(url)
    p = urllib.parse.unquote(u.path)
    if u.netloc:  # UNC share
        return "\\\\" + u.netloc + p.replace("/", "\\")
    return os.path.normpath(p.lstrip("/"))


def _explorer_windows():
    """(hwnd, normalized folder path) for every open Explorer window.
    Win11 tabs share one hwnd, so several paths may map to one window."""
    ole32 = ctypes.windll.ole32
    ole32.CoInitialize(None)
    sw = ctypes.c_void_p()
    if ole32.CoCreateInstance(
            ctypes.byref(_CLSID_ShellWindows), None, 0x15,
            ctypes.byref(_IID_IShellWindows), ctypes.byref(sw)) != 0:
        return []
    out = []
    try:
        count = ctypes.c_long()
        _com_method(sw, 7, ctypes.POINTER(ctypes.c_long))(
            sw, ctypes.byref(count))                     # get_Count
        item = _com_method(sw, 8, _VARIANT, ctypes.POINTER(ctypes.c_void_p))
        for i in range(count.value):
            disp = ctypes.c_void_p()
            if (item(sw, _VARIANT(vt=3, val=i), ctypes.byref(disp)) != 0
                    or not disp):
                continue
            try:
                wb = ctypes.c_void_p()
                qi = _com_method(disp, 0, ctypes.POINTER(_GUID),
                                 ctypes.POINTER(ctypes.c_void_p))
                if (qi(disp, ctypes.byref(_IID_IWebBrowser2),
                       ctypes.byref(wb)) != 0 or not wb):
                    continue
                try:
                    hwnd = ctypes.c_longlong()
                    _com_method(wb, 37, ctypes.POINTER(ctypes.c_longlong))(
                        wb, ctypes.byref(hwnd))          # get_HWND
                    bstr = ctypes.c_void_p()
                    _com_method(wb, 30, ctypes.POINTER(ctypes.c_void_p))(
                        wb, ctypes.byref(bstr))          # get_LocationURL
                    url = ctypes.wstring_at(bstr.value) if bstr.value else ""
                    if bstr.value:
                        ctypes.windll.oleaut32.SysFreeString(bstr)
                    path = _url_to_path(url)
                    if path and _win_class(hwnd.value) == "CabinetWClass":
                        out.append((hwnd.value,
                                    os.path.normpath(path).lower().rstrip("\\")))
                finally:
                    _com_release(wb)
            finally:
                _com_release(disp)
    finally:
        _com_release(sw)
    return out


def _short_path(path):
    """Compact display form: drive + parent + folder ('D:\\…\\parent\\name')."""
    drive, rest = os.path.splitdrive(os.path.normpath(path))
    parts = [p for p in rest.split("\\") if p]
    if not parts:
        return drive + "\\"
    if len(parts) <= 2:
        return drive + "\\" + "\\".join(parts)
    return drive + "\\…\\" + "\\".join(parts[-2:])


COL_BG = "#1e1e28"
COL_FG = "#e8e8f0"
COL_DIM = "#8a8fa8"
COL_SEL = "#334ecc"
COL_RUN = "#5ad46e"
COL_BORDER = "#444a66"
FONT = "Yu Gothic UI"
HINT_MAIN = ("Enter:起動/開く   Space:最近のフォルダ   Ctrl+R:再起動   "
             "Ctrl+D:停止   右クリック:メニュー   Esc:閉じる")
HINT_RECENT = ("最近使ったフォルダ   Enter:開く(開いていれば前面化)   "
               "右クリック:ロック/解除   ドラッグ:並び替え   Space/Esc:戻る")


class LaunchItApp:
    def __init__(self, root, cfg, pm, cmd_queue, tray_icon, hotkey):
        self.root = root
        self.cfg = cfg
        self.pm = pm
        self.q = cmd_queue
        self.tray = tray_icon
        self.hotkey = hotkey
        self.cfg_mtime = os.path.getmtime(CONFIG_PATH)
        self.visible = False
        self.filtered = []
        self._menu_open = False
        self.port_state = {}
        self._port_kick = threading.Event()
        self._last_scan = 0.0
        self._win_cache = {}  # hwnd -> (item, last_matched_ts)
        self.view = "main"    # "main" | "recent" (recent-folders page)
        self._recent = []
        self._recent_ts = 0.0
        self._drag_src = None   # (index, x_root, y_root) of the pressed cell
        self._drag_on = False
        self._drag_tgt = None
        self._build_popup()
        threading.Thread(target=self._port_watcher, daemon=True).start()
        self.root.after(80, self._poll_queue)
        self.root.after(1000, self._tick)

    # ---- window ----

    def _build_popup(self):
        win = tk.Toplevel(self.root)
        win.withdraw()
        win.title(APP_TITLE)
        win.overrideredirect(True)
        win.attributes("-topmost", True)
        win.configure(bg=COL_BORDER)

        outer = tk.Frame(win, bg=COL_BG)
        outer.pack(fill="both", expand=True, padx=1, pady=1)
        self.outer = outer

        top = tk.Frame(outer, bg=COL_BG)
        top.pack(fill="x", padx=10, pady=(10, 6))
        self.query = tk.StringVar()
        self.query.trace_add("write", lambda *_: self._refresh_list())
        entry = tk.Entry(
            top, textvariable=self.query, font=(FONT, 14),
            bg="#282836", fg=COL_FG, insertbackground=COL_FG,
            relief="flat", highlightthickness=0,
        )
        entry.pack(side="left", fill="x", expand=True, ipady=6)
        add_btn = tk.Button(
            top, text="+", font=(FONT, 13, "bold"), width=3,
            bg="#282836", fg=COL_FG, activebackground=COL_SEL,
            activeforeground="#ffffff", relief="flat", takefocus=0,
            command=self._add_menu,
        )
        add_btn.pack(side="left", padx=(6, 0), fill="y")
        self.add_btn = add_btn

        self.grid_frame = tk.Frame(outer, bg=COL_BG)
        self.grid_frame.pack(fill="both", expand=True, padx=10, pady=(2, 0))
        self.cells = []
        self.sel = 0

        self.status = tk.StringVar()
        tk.Label(
            outer, textvariable=self.status, font=(FONT, 10),
            bg=COL_BG, fg=COL_RUN, anchor="w",
        ).pack(fill="x", padx=12)
        self.hint = tk.StringVar(value=HINT_MAIN)
        tk.Label(
            outer, textvariable=self.hint,
            font=(FONT, 9), bg=COL_BG, fg=COL_DIM, anchor="w",
        ).pack(fill="x", padx=12, pady=(0, 8))

        def bindkey(seq, fn):
            # "break" stops class bindings (e.g. Entry's default Control-d)
            entry.bind(seq, lambda e, f=fn: f() or "break")

        bindkey("<Escape>", self._on_escape)
        bindkey("<Return>", self._activate)
        bindkey("<Control-Return>", self._open_dir)
        bindkey("<Control-r>", self._restart)
        bindkey("<Control-d>", self._stop)
        entry.bind("<space>", self._on_space)
        entry.bind("<FocusOut>", self._on_focus_out)
        entry.bind("<Down>", lambda e: self._move_sel(1))
        entry.bind("<Up>", lambda e: self._move_sel(-1))

        # map once invisibly with the list rendered so fonts (incl. emoji
        # fallback) load at startup instead of on the first hotkey press
        self.win, self.entry = win, entry
        self._refresh_list()
        win.attributes("-alpha", 0.0)
        win.geometry("+0+0")
        win.deiconify()
        win.update_idletasks()
        win.withdraw()
        win.attributes("-alpha", 1.0)

    def show(self):
        self._maybe_reload()
        self._port_kick.set()
        if time.time() - self._last_scan > 60:  # pick up externally started apps
            self._last_scan = time.time()
            self.q.put("rescan")
        self.view = "main"
        self.hint.set(HINT_MAIN)
        self._kick_recent()  # warm the cache for a Space press later
        self.query.set("")
        self._refresh_list()
        self._apply_geometry()
        self.win.deiconify()
        self.win.lift()
        self.visible = True
        self.status.set("")
        # nudge Windows' foreground lock so focus_force actually works
        user32 = ctypes.windll.user32
        user32.keybd_event(0x12, 0, 0, 0)
        user32.keybd_event(0x12, 0, 2, 0)
        self.win.focus_force()
        self.entry.focus_set()
        self.entry.select_range(0, "end")
        log.debug("show: state=%s viewable=%s geo=%s",
                  self.win.state(), self.win.winfo_viewable(),
                  self.win.winfo_geometry())

    def _apply_geometry(self):
        w = int(self.cfg.get("width", 720))
        wa = _cursor_work_area()  # monitor the cursor is on
        self.win.update_idletasks()  # settle the grid so reqheight is exact
        h = int(self.cfg.get("height", 0) or 0)
        if h <= 0 or self.view == "recent":  # auto: exactly fit the items
            h = self.outer.winfo_reqheight() + 2
        h = min(h, wa.bottom - wa.top - 60)
        if self.cfg.get("position", "bottom-right") == "center":
            x = wa.left + (wa.right - wa.left - w) // 2
            y = wa.top + int((wa.bottom - wa.top) * 0.18)
        else:  # bottom-right, just above the taskbar
            x, y = wa.right - w - 12, wa.bottom - h - 12
        self.win.geometry(f"{w}x{h}+{x}+{y}")

    def _preview_popup(self):
        """Show the popup as a live layout preview without taking focus."""
        self._refresh_list()
        self._apply_geometry()
        self.win.deiconify()
        self.win.lift()
        self.visible = True

    def hide(self):
        self.win.withdraw()
        self.visible = False

    def toggle(self):
        """Hotkey cycle: hidden -> main list -> recent folders -> hidden,
        so holding the modifier and tapping Space twice lands on folders."""
        if not self.visible:
            self.show()
        elif self.view == "main":
            self._toggle_view()
        else:
            self.hide()

    # ---- recent-folders view ----

    def _kick_recent(self):
        """Refresh the recent-folder cache in the background (throttled)."""
        if time.time() - self._recent_ts < 10:
            return
        self._recent_ts = time.time()

        def work():
            try:
                self._recent = _recent_folders()
            except Exception:
                log.exception("recent folder scan failed")

        threading.Thread(target=work, daemon=True).start()

    def _on_space(self, _event):
        if self.query.get().strip():
            return None  # typing a query: let the space through
        self._toggle_view()
        return "break"

    def _on_escape(self):
        if self.view == "recent":
            self._toggle_view()
        else:
            self.hide()

    def _toggle_view(self):
        self.view = "recent" if self.view == "main" else "main"
        if self.view == "recent":
            self._kick_recent()
            log.info("recent view: %d folders cached", len(self._recent))
        self.hint.set(HINT_RECENT if self.view == "recent" else HINT_MAIN)
        self.query.set("")  # fires _refresh_list via the write trace
        self._apply_geometry()

    def _press(self, event, i):
        self._select(i)
        self._drag_src = (i, event.x_root, event.y_root)
        self._drag_on = False

    def _drag_motion(self, event):
        if self.view != "recent" or self._drag_src is None:
            return
        si, px, py = self._drag_src
        if not self._drag_on:
            if abs(event.x_root - px) + abs(event.y_root - py) < 10:
                return  # sloppy click, not a drag yet
            self._drag_on = True
            self.win.configure(cursor="hand2")
        w = self.win.winfo_containing(event.x_root, event.y_root)
        t = self.cells.index(w) if w in self.cells else None
        if t is not None and (t >= len(self.filtered) or t == si):
            t = None
        if t == self._drag_tgt:
            return
        prev = self._drag_tgt
        if prev is not None and prev < len(self.cells):
            self.cells[prev].config(
                bg=COL_SEL if prev == self.sel else COL_BG)
        if t is not None:
            self.cells[t].config(bg="#46507a")
        self._drag_tgt = t

    def _drag_drop(self, _event):
        dragged, tgt = self._drag_on, self._drag_tgt
        src = self._drag_src
        self._drag_src, self._drag_on, self._drag_tgt = None, False, None
        if not dragged:
            return
        self.win.configure(cursor="")
        if self.view == "recent" and src is not None and tgt is not None:
            self._reorder_recent(src[0], tgt)
        else:
            self._refresh_list(rebuild_only=True)  # clear the highlight

    def _reorder_recent(self, si, ti):
        """Drop item si onto slot ti: lock it there (into recent_pinned)."""
        if not (0 <= si < len(self.filtered) and 0 <= ti < len(self.filtered)):
            return
        path = self.filtered[si]["path"]
        key = path.lower().rstrip("\\")
        tkey = self.filtered[ti]["path"].lower().rstrip("\\")
        cfg = load_config()  # re-read so manual edits aren't clobbered
        pinned = [p for p in cfg.setdefault("recent_pinned", [])
                  if p.lower().rstrip("\\") != key]
        idx = next((j for j, p in enumerate(pinned)
                    if p.lower().rstrip("\\") == tkey), None)
        if idx is None:
            pinned.append(path)  # dropped into the recency zone: lock at end
        else:
            pinned.insert(idx + 1 if si < ti else idx, path)
        cfg["recent_pinned"] = pinned
        self._save_config(cfg)
        self.status.set("📌 ロックして並び替えました(右クリックで解除)")

    def _pin_toggle(self):
        item = self._current_item()
        if not item or not item.get("_recent"):
            return
        key = item["path"].lower().rstrip("\\")
        cfg = load_config()  # re-read so manual edits aren't clobbered
        pinned = cfg.setdefault("recent_pinned", [])
        kept = [p for p in pinned if p.lower().rstrip("\\") != key]
        if len(kept) == len(pinned):
            kept.append(item["path"])
            self.status.set("📌 ロックしました(右クリックで解除)")
        else:
            self.status.set("ロックを解除しました")
        cfg["recent_pinned"] = kept
        self._save_config(cfg)

    def _on_focus_out(self, _event):
        def check():
            try:
                if (self.visible and not self._menu_open
                        and self.win.focus_displayof() is None):
                    self.hide()
            except (KeyError, tk.TclError):
                pass
        self.win.after(80, check)

    # ---- list ----

    def _expand_wingroups(self, raw):
        """Synthetic items for all wingroup entries, with stickiness: a
        session window stays listed for 2 minutes after its title stops
        matching (transient title flaps while the agent runs commands),
        as long as the window still exists."""
        groups = [it for it in raw if it.get("type") == "wingroup"]
        if not groups:
            self._win_cache = {}
            return []
        wins = _visible_windows()
        now = time.time()
        cache = {}
        for g in groups:
            for item in _wingroup_items(g, wins):
                cache[item["hwnd"]] = (item, now)
        user32 = ctypes.windll.user32
        for hwnd, (item, ts) in self._win_cache.items():
            if (hwnd not in cache and now - ts < 120
                    and user32.IsWindow(hwnd)):
                cache[hwnd] = (item, ts)
        self._win_cache = cache
        return [item for item, _ts in cache.values()]

    def _refresh_list(self, rebuild_only=False, preserve_sel=False):
        q = self.query.get().strip().lower()
        if not rebuild_only:
            keep = None
            if preserve_sel and 0 <= self.sel < len(self.filtered):
                keep = self.filtered[self.sel].get("name")
            raw = self.cfg.get("items", [])
            expanded = self._expand_wingroups(raw)  # keeps _win_cache fresh
            if self.view == "recent":
                limit = max(1, int(self.cfg.get("columns", 2))) * 7
                pinned = list(self.cfg.get("recent_pinned", []))
                pset = {p.lower().rstrip("\\") for p in pinned}
                paths = pinned + [p for p in self._recent
                                  if p.lower().rstrip("\\") not in pset]
                items = [{"name": _short_path(p), "type": "folder", "path": p,
                          "_recent": True, "_pinned": i < len(pinned)}
                         for i, p in enumerate(paths[:limit])]
            else:
                items = []
                for it in raw:
                    if it.get("type") == "wingroup":
                        sub = [x for x in expanded
                               if x.get("group") == it.get("name")]
                        sub.sort(key=lambda x: x["name"])
                        items.extend(sub)
                    else:
                        items.append(it)
            if q:
                subs = [i for i in items if q in i["name"].lower()]
                rest = [i for i in items if i not in subs and _subseq(q, i["name"].lower())]
                self.filtered = subs + rest
            else:
                self.filtered = list(items)
            self.sel = 0
            if keep:
                self.sel = next((i for i, it in enumerate(self.filtered)
                                 if it.get("name") == keep), 0)
        n = len(self.filtered)
        self.sel = min(max(self.sel, 0), max(n - 1, 0))
        self._ensure_cells(n)
        ncols = max(1, int(self.cfg.get("columns", 2)))
        nrows = max(1, -(-n // ncols))
        for c in range(ncols):
            self.grid_frame.grid_columnconfigure(c, weight=1, uniform="col")
        # clear weights left over from a higher column count, or the empty
        # columns keep their share of width and everything drifts left
        for c in range(ncols, getattr(self, "_max_cols", 0)):
            self.grid_frame.grid_columnconfigure(c, weight=0, uniform="")
        self._max_cols = max(getattr(self, "_max_cols", 0), ncols)
        # smaller font in the recent view so path tails don't get clipped
        fnt = (FONT, 10 if self.view == "recent" else 12)
        for i, (item, lbl) in enumerate(zip(self.filtered, self.cells)):
            col, row = divmod(i, nrows)  # column-major: fills down, then right
            lbl.grid(row=row, column=col, sticky="ew")
            text, fg = self._item_label(item)
            selected = (i == self.sel)
            lbl.config(text=text, font=fnt,
                       fg="#ffffff" if selected else fg,
                       bg=COL_SEL if selected else COL_BG)

    def _item_label(self, item):
        typ = item.get("type", "app")
        custom = item.get("color")
        if typ == "_win":
            return f"⌨ {item['name']}", custom or COL_RUN
        if typ == "folder":
            if item.get("_pinned"):
                return f"📌 {item['name']}", custom or COL_RUN
            return f"📁 {item['name']}", custom or COL_FG
        if typ == "url":
            return f"🌐 {item['name']}", custom or COL_FG
        if not item.get("track", True):
            return f"▶  {item['name']}", custom or COL_FG
        if self._is_active(item):
            return f"●  {item['name']}", COL_RUN  # running state wins
        return f"○  {item['name']}", custom or COL_FG

    def _ensure_cells(self, n):
        while len(self.cells) < n:
            lbl = tk.Label(self.grid_frame, font=(FONT, 12), bg=COL_BG,
                           fg=COL_FG, anchor="w", padx=8, pady=3)
            i = len(self.cells)
            lbl.bind("<Button-1>", lambda e, i=i: self._press(e, i))
            lbl.bind("<B1-Motion>", self._drag_motion)
            lbl.bind("<ButtonRelease-1>", self._drag_drop)
            lbl.bind("<Double-Button-1>",
                     lambda e, i=i: (self._select(i), self._activate()))
            lbl.bind("<Button-3>",
                     lambda e, i=i: (self._select(i), self._context_menu(e)))
            self.cells.append(lbl)
        while len(self.cells) > n:
            self.cells.pop().destroy()

    def _select(self, i):
        if 0 <= i < len(self.filtered):
            self.sel = i
            self._refresh_list(rebuild_only=True)

    def _move_sel(self, delta):
        if not self.filtered:
            return "break"
        self._select(max(0, min(self.sel + delta, len(self.filtered) - 1)))
        return "break"

    def _current_item(self):
        if 0 <= self.sel < len(self.filtered):
            return self.filtered[self.sel]
        return None

    def _tick(self):
        if self.visible:
            # full refresh so running sessions appear/disappear live
            self._refresh_list(preserve_sel=True)
        self.root.after(1000, self._tick)

    def _is_active(self, item):
        """Running as a tracked process, or its WebUI port answers."""
        return (self.pm.is_running(item["name"])
                or self.port_state.get(item["name"], False))

    def _port_watcher(self):
        """Poll WebUI ports of app items (5s visible / 20s hidden) so apps
        whose launcher .bat exits right away (spawning the real server in a
        separate window) still show a correct running state."""
        while True:
            state = {}
            for item in list(self.cfg.get("items", [])):
                if item.get("type", "app") != "app":
                    continue
                target = _check_target(item)
                if target:
                    state[item["name"]] = _port_open(target)
            self.port_state = state
            self._port_kick.clear()
            self._port_kick.wait(5 if self.visible else 20)

    def status_json(self):
        """Payload for the IPC 'status' command (runs on the IPC thread)."""
        out = {}
        for item in list(self.cfg.get("items", [])):
            if item.get("type", "app") == "app":
                out[item["name"]] = {
                    "tracked": self.pm.is_running(item["name"]),
                    "port": self.port_state.get(item["name"]),
                }
        out["_sessions"] = sorted(
            it["name"] for it, _ts in self._win_cache.values())
        return json.dumps(out, ensure_ascii=False).encode("utf-8")

    # ---- actions ----

    def _activate(self):
        item = self._current_item()
        if not item:
            return
        typ = item.get("type", "app")
        try:
            if typ == "_win":
                if ctypes.windll.user32.IsWindow(item["hwnd"]):
                    _focus_window(item["hwnd"])
                    self.hide()
                else:
                    self.status.set("そのウィンドウは閉じられています")
                    self._refresh_list()
            elif typ == "folder":
                self._open_folder(item["path"])
                self.hide()
            elif typ == "url":
                webbrowser.open(item["url"])
                self.hide()
            elif self._is_active(item):
                if self._focus_item(item):
                    self.hide()
                elif item.get("url"):
                    webbrowser.open(item["url"])
                    self.hide()
                else:
                    self.status.set(f"{item['name']} は実行中 (Ctrl+R 再起動 / Ctrl+D 停止)")
            else:
                self.pm.launch(item)
                self.status.set(f"起動しました: {item['name']}")
                self._refresh_list(rebuild_only=True)
                self.win.after(700, self.hide)
        except Exception as e:
            log.exception("activate failed: %s", item.get("name"))
            self.status.set(f"エラー: {e}")

    def _context_menu(self, event):
        item = self._current_item()
        if not item:
            return "break"
        typ = item.get("type", "app")
        m = tk.Menu(self.win, tearoff=0, bg=COL_BG, fg=COL_FG,
                    activebackground=COL_SEL, activeforeground="#ffffff")
        if typ == "app":
            if self._is_active(item):
                m.add_command(label="手前に表示", command=self._focus_current)
            m.add_command(label="起動 / 開く", command=self._activate)
            if item.get("track", True):
                m.add_command(label="再起動", command=self._restart)
                m.add_command(label="停止", command=self._stop)
            m.add_separator()
            m.add_command(label="フォルダを開く", command=self._open_dir)
        elif typ == "_win":
            m.add_command(label="手前に表示", command=self._activate)
        elif typ == "folder":
            m.add_command(label="開く", command=self._activate)
            if item.get("_recent"):
                m.add_command(
                    label="ロック解除" if item.get("_pinned")
                    else "ロック(リストに固定)",
                    command=self._pin_toggle)
        else:
            m.add_command(label="ブラウザで開く", command=self._activate)
        self._menu_open = True
        try:
            m.tk_popup(event.x_root, event.y_root)
        finally:
            m.grab_release()
            self.win.after(200, lambda: setattr(self, "_menu_open", False))
        return "break"

    # ---- add items ----

    def _add_menu(self):
        m = tk.Menu(self.win, tearoff=0, bg=COL_BG, fg=COL_FG,
                    activebackground=COL_SEL, activeforeground="#ffffff")
        m.add_command(label="フォルダを追加...", command=self._add_folder)
        m.add_command(label="アプリを追加 (.bat/.exe/.py)...", command=self._add_app)
        m.add_command(label="URL を追加...", command=self._add_url)
        m.add_separator()
        m.add_command(label="項目の編集(並び替え・削除)...", command=self._open_editor)
        self._menu_open = True
        try:
            m.tk_popup(self.add_btn.winfo_rootx(),
                       self.add_btn.winfo_rooty() + self.add_btn.winfo_height())
        finally:
            m.grab_release()
            self.win.after(200, lambda: setattr(self, "_menu_open", False))

    def _dialog(self, func, *args, **kwargs):
        """Run a modal dialog without the focus-out auto-hide kicking in."""
        self._menu_open = True
        try:
            return func(*args, **kwargs)
        finally:
            self.win.after(200, lambda: setattr(self, "_menu_open", False))
            if self.visible:
                self.win.focus_force()
                self.entry.focus_set()

    def _add_folder(self):
        path = self._dialog(filedialog.askdirectory,
                            parent=self.win, title="追加するフォルダを選択")
        if not path:
            return
        name = os.path.basename(path.rstrip("/\\")) or path
        self._append_item({"name": name, "type": "folder", "path": path})

    def _add_app(self):
        path = self._dialog(
            filedialog.askopenfilename, parent=self.win,
            title="追加するアプリを選択",
            filetypes=[("アプリ", "*.bat;*.cmd;*.exe;*.py"), ("すべて", "*.*")],
        )
        if not path:
            return
        name = os.path.splitext(os.path.basename(path))[0]
        self._append_item({"name": name, "type": "app", "path": path})

    def _add_url(self):
        url = self._dialog(simpledialog.askstring, "URL を追加",
                           "URL:", parent=self.win)
        if not url:
            return
        if "://" not in url:
            url = "https://" + url
        default = urllib.parse.urlparse(url).netloc or url
        name = self._dialog(simpledialog.askstring, "URL を追加",
                            "表示名:", parent=self.win,
                            initialvalue=default) or default
        self._append_item({"name": name, "type": "url", "url": url})

    def _append_item(self, item):
        cfg = load_config()  # re-read so manual edits aren't clobbered
        names = {i.get("name") for i in cfg.get("items", [])}
        base, n = item["name"], 2
        while item["name"] in names:
            item["name"] = f"{base} ({n})"
            n += 1
        cfg.setdefault("items", []).append(item)
        self._save_config(cfg)
        self.status.set(f"追加しました: {item['name']}")
        log.info("item added: %s", item)

    def _save_config(self, cfg):
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, ensure_ascii=False, indent=2)
        self.cfg = cfg
        self.cfg_mtime = os.path.getmtime(CONFIG_PATH)
        self._refresh_list()
        cb = getattr(self, "_editor_refresh", None)
        if cb:
            try:
                cb()
            except tk.TclError:
                pass

    # ---- item editor ----

    def _open_editor(self):
        if getattr(self, "_editor", None) and self._editor.winfo_exists():
            self._editor.deiconify()
            self._editor.lift()
            self._editor.focus_force()
            return
        ed = tk.Toplevel(self.root)
        self._editor = ed
        ed.title("LaunchIt - 項目の編集")
        ed.configure(bg=COL_BG)
        ed.attributes("-topmost", True)
        wa = _cursor_work_area()
        ed.geometry(f"+{wa.left + 150}+{wa.top + 120}")

        top = tk.Frame(ed, bg=COL_BG)
        top.pack(side="top", fill="x", padx=14, pady=(12, 4))

        def mkspin(label, key, lo, hi, inc, default):
            tk.Label(top, text=label, font=(FONT, 10), bg=COL_BG,
                     fg=COL_DIM).pack(side="left")
            var = tk.IntVar(value=int(self.cfg.get(key, default) or default))

            def apply(*_a):
                try:
                    val = int(var.get())
                except tk.TclError:
                    return
                cfg = load_config()
                if cfg.get(key) != val:
                    cfg[key] = val
                    self._save_config(cfg)
                    self._preview_popup()

            sp = tk.Spinbox(top, from_=lo, to=hi, increment=inc,
                            textvariable=var, width=5, font=(FONT, 11),
                            bg="#282836", fg=COL_FG, relief="flat",
                            buttonbackground="#282836",
                            insertbackground=COL_FG, command=apply)
            sp.bind("<Return>", apply)
            sp.bind("<FocusOut>", apply)
            sp.pack(side="left", padx=(4, 14))

        mkspin("列数", "columns", 1, 5, 1, 2)
        mkspin("横幅", "width", 480, 1800, 30, 720)
        mkspin("高さ(0=自動)", "height", 0, 1400, 30, 0)

        tk.Label(top, text="配置", font=(FONT, 10), bg=COL_BG,
                 fg=COL_DIM).pack(side="left")
        pos_var = tk.StringVar(value=self.cfg.get("position", "bottom-right"))

        def apply_pos(_v):
            cfg = load_config()
            cfg["position"] = pos_var.get()
            self._save_config(cfg)
            self._preview_popup()

        om = tk.OptionMenu(top, pos_var, "bottom-right", "center",
                           command=apply_pos)
        om.config(font=(FONT, 9), bg="#282836", fg=COL_FG, relief="flat",
                  highlightthickness=0, activebackground=COL_SEL,
                  activeforeground="#ffffff")
        om["menu"].config(bg=COL_BG, fg=COL_FG, activebackground=COL_SEL)
        om.pack(side="left", padx=(4, 0))

        lb = tk.Listbox(ed, font=(FONT, 12), width=38, height=18,
                        activestyle="none", bg=COL_BG, fg=COL_FG,
                        selectbackground=COL_SEL, selectforeground="#ffffff",
                        relief="flat", highlightthickness=0)
        lb.pack(side="left", fill="both", expand=True, padx=(12, 8), pady=12)
        btns = tk.Frame(ed, bg=COL_BG)
        btns.pack(side="left", fill="y", padx=(0, 12), pady=12)

        def mkbtn(label, cmd):
            tk.Button(btns, text=label, font=(FONT, 10), width=18,
                      bg="#282836", fg=COL_FG, activebackground=COL_SEL,
                      activeforeground="#ffffff", relief="flat",
                      command=cmd).pack(fill="x", pady=2)

        def refresh(keep=None):
            lb.delete(0, "end")
            for it in self.cfg.get("items", []):
                typ = it.get("type", "app")
                mark = {"folder": "📁", "url": "🌐", "wingroup": "⌨"}.get(typ, "▶")
                lb.insert("end", f" {mark} {it.get('name', '?')}")
            if keep is not None and 0 <= keep < lb.size():
                lb.selection_set(keep)
                lb.see(keep)

        def sel_i():
            s = lb.curselection()
            return s[0] if s else -1

        def move(delta):
            i = sel_i()
            j = i + delta
            cfg = load_config()
            items = cfg.get("items", [])
            if i < 0 or i >= len(items) or not (0 <= j < len(items)):
                return
            items[i], items[j] = items[j], items[i]
            self._save_config(cfg)
            refresh(j)

        def rename():
            i = sel_i()
            cfg = load_config()
            items = cfg.get("items", [])
            if not (0 <= i < len(items)):
                return
            new = simpledialog.askstring("名前を変更", "表示名:", parent=ed,
                                         initialvalue=items[i].get("name", ""))
            if not new:
                return
            items[i]["name"] = new
            self._save_config(cfg)
            refresh(i)

        def delete():
            i = sel_i()
            cfg = load_config()
            items = cfg.get("items", [])
            if not (0 <= i < len(items)):
                return
            if not messagebox.askyesno(
                    "削除", f"「{items[i].get('name')}」を削除しますか?",
                    parent=ed):
                return
            del items[i]
            self._save_config(cfg)
            refresh(min(i, lb.size() - 1))

        mkbtn("↑ 上へ", lambda: move(-1))
        mkbtn("↓ 下へ", lambda: move(1))
        mkbtn("名前を変更...", rename)
        mkbtn("削除", delete)
        tk.Frame(btns, bg=COL_BG, height=12).pack()
        mkbtn("フォルダを追加...", self._add_folder)
        mkbtn("アプリを追加...", self._add_app)
        mkbtn("URL を追加...", self._add_url)
        tk.Frame(btns, bg=COL_BG, height=12).pack()
        mkbtn("閉じる", ed.destroy)

        self._editor_refresh = refresh

        def on_destroy(e):
            if e.widget is ed:
                self._editor_refresh = None
                self.hide()  # end the layout preview

        ed.bind("<Destroy>", on_destroy)
        refresh()
        self._preview_popup()  # live preview while editing layout

    def _focus_current(self):
        item = self._current_item()
        if item and self._focus_item(item):
            self.hide()
        else:
            self.status.set("ウィンドウが見つかりませんでした")

    BROWSER_EXES = {"firefox.exe", "chrome.exe", "msedge.exe", "brave.exe",
                    "opera.exe", "vivaldi.exe", "floorp.exe", "waterfox.exe",
                    "librewolf.exe"}
    # window classes are more reliable than exe names (sandboxed Firefox
    # reports a mangled image name): Gecko family / Chromium family
    BROWSER_CLASSES = ("MozillaWindowClass", "Chrome_WidgetWin")

    def _focus_item(self, item):
        """Bring an already-running item's window to the front.
        url items: a BROWSER window whose title shows the item name (i.e. the
        tab is active somewhere) — never other apps that merely mention the
        name (e.g. an image viewer showing ComfyUI_xxx.png).
        Other items: windows of the tracked process tree, then windows whose
        exe or title matches the launch file name."""
        try:
            wins = _visible_windows()
        except Exception:
            log.exception("window enumeration failed")
            return False
        exe_cache = {}

        def wexe(pid):
            if pid not in exe_cache:
                exe_cache[pid] = _pid_exe(pid)
            return exe_cache[pid]

        def is_browser(w):
            return (_win_class(w[0]).startswith(self.BROWSER_CLASSES)
                    or wexe(w[1]) in self.BROWSER_EXES)

        name = item["name"].lower()
        if item.get("url"):
            cands = [w for w in wins if name in w[2].lower() and is_browser(w)]
        else:
            cands = []
            pid = self.pm.pid_of(item["name"])
            if pid and self.pm.is_running(item["name"]):
                tree = _descendant_pids(pid)
                cands = [w for w in wins if w[1] in tree]
            if not cands and item.get("path"):
                base = os.path.basename(item["path"]).lower()
                cands = [w for w in wins
                         if wexe(w[1]) == base or base in w[2].lower()]
        if not cands:
            return False
        best = max(cands, key=lambda w: w[3])  # biggest window wins
        _focus_window(best[0])
        log.info("focused window %r for %s", best[2], item["name"])
        return True

    def _open_dir(self):
        item = self._current_item()
        if not item:
            return
        path = item.get("path") or ""
        target = path if os.path.isdir(path) else os.path.dirname(path)
        if target and os.path.exists(target):
            self._open_folder(target)
            self.hide()

    def _open_folder(self, path):
        """Open a folder; if an Explorer window already shows it, focus that
        window instead of opening a duplicate."""
        want = os.path.normpath(path).lower().rstrip("\\")
        try:
            wins = _explorer_windows()
        except Exception:
            log.exception("explorer window enumeration failed")
            wins = []
        for hwnd, p in wins:
            if p == want and ctypes.windll.user32.IsWindow(hwnd):
                _focus_window(hwnd)
                log.info("focused explorer window for %s", path)
                return
        os.startfile(path)

    def _stop(self):
        self._proc_action("stop", "停止")

    def _restart(self):
        self._proc_action("restart", "再起動")

    def _proc_action(self, action, label):
        item = self._current_item()
        if (not item or item.get("type", "app") != "app"
                or not item.get("track", True)):
            return "break"
        if not self._is_active(item) and action == "stop":
            self.status.set(f"{item['name']} は実行されていません")
            return
        self.status.set(f"{label}中: {item['name']} ...")

        def work():
            try:
                getattr(self.pm, action)(item)
            except Exception:
                log.exception("%s failed: %s", action, item["name"])
            self._port_kick.set()
            self.q.put("action_done")

        threading.Thread(target=work, daemon=True).start()

    # ---- queue / lifecycle ----

    def _maybe_reload(self):
        try:
            mtime = os.path.getmtime(CONFIG_PATH)
            if mtime != self.cfg_mtime:
                self.cfg = load_config()
                self.cfg_mtime = mtime
                if self.hotkey:
                    self.hotkey.update(
                        self.cfg.get("hotkey", "shift+space"),
                        self.cfg.get("hotkey_passthrough", []),
                    )
                log.info("config reloaded")
        except Exception:
            log.exception("config reload failed")

    def _poll_queue(self):
        try:
            while True:
                cmd = self.q.get_nowait()
                log.debug("cmd: %s", cmd)
                try:
                    if cmd == "show":
                        self.show()
                    elif cmd == "hide":
                        self.hide()
                    elif cmd == "toggle":
                        self.toggle()
                    elif cmd == "refresh":
                        self._refresh_list(rebuild_only=True)
                    elif cmd == "action_done":
                        self._refresh_list(rebuild_only=True)
                        self.status.set("完了")
                    elif cmd == "reload":
                        self.cfg_mtime = 0
                        self._maybe_reload()
                    elif cmd == "editcfg":
                        os.startfile(CONFIG_PATH)
                    elif cmd == "edititems":
                        self._open_editor()
                    elif cmd == "rescan":
                        threading.Thread(
                            target=lambda: (self.pm.adopt_running(self.cfg.get("items", [])),
                                            self.q.put("refresh")),
                            daemon=True,
                        ).start()
                    elif cmd == "selfrestart":
                        self._self_restart()
                        return
                    elif cmd == "quit":
                        self.quit()
                        return
                except Exception:
                    # one bad command must never kill the queue loop
                    log.exception("command failed: %s", cmd)
        except queue.Empty:
            pass
        self.root.after(80, self._poll_queue)

    def _self_restart(self):
        exe = os.path.join(os.path.dirname(sys.executable), "pythonw.exe")
        if not os.path.exists(exe):
            exe = sys.executable
        script = os.path.join(BASE_DIR, "launchit.py")
        # detached relaunch; --wait-port makes it wait for this instance's exit
        flags = 0x00000008 | 0x00000200  # DETACHED_PROCESS | NEW_PROCESS_GROUP
        args = [exe, script, "--wait-port"]
        try:
            subprocess.Popen(args, cwd=BASE_DIR,
                             creationflags=flags | 0x01000000)  # BREAKAWAY_FROM_JOB
        except OSError:
            subprocess.Popen(args, cwd=BASE_DIR, creationflags=flags)
        log.info("self-restarting")
        self.quit()

    def quit(self):
        log.info("shutting down (launched apps keep running)")
        try:
            if self.tray:
                self.tray.stop()
        except Exception:
            pass
        self.root.after(50, self.root.destroy)


def _subseq(needle, haystack):
    it = iter(haystack)
    return all(ch in it for ch in needle)


# ------------------------------------------------------------------ main ----

def main():
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass

    cmd_queue = queue.Queue()
    ipc = None
    attempts = 40 if "--wait-port" in sys.argv[1:] else 1
    for _ in range(attempts):
        try:
            ipc = start_ipc_server(cmd_queue)
            break
        except OSError:
            if attempts > 1:
                _sleep(0.5)
    if ipc is None:
        # another instance is running: just tell it to show itself
        try:
            send_ipc("show")
        except OSError:
            pass
        return

    cfg = load_config()
    pm = ProcessManager()
    threading.Thread(
        target=lambda: pm.adopt_running(cfg.get("items", [])), daemon=True
    ).start()

    hotkey = HotkeyListener(cfg.get("hotkey", "shift+space"),
                            cfg.get("hotkey_passthrough", []), cmd_queue)
    hotkey.start()

    try:
        tray = start_tray(cmd_queue)
    except Exception:
        log.exception("tray init failed, continuing without tray")
        tray = None

    root = tk.Tk()
    root.withdraw()
    try:
        root.iconbitmap(default=os.path.join(BASE_DIR, "launchit.ico"))
    except tk.TclError:
        pass  # missing/broken ico: editor windows just keep the Tk default
    app = LaunchItApp(root, cfg, pm, cmd_queue, tray, hotkey)
    IPC_HANDLERS["status"] = app.status_json
    log.info("LaunchIt started")
    root.mainloop()
    ipc.close()


if __name__ == "__main__":
    main()
