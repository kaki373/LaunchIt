"""Safely stop AI-Toolkit only.

The AI-Toolkit UI runs under `concurrently --restart-tries -1`, which
resurrects the server one second after it is killed. The only reliable stop
is to kill the whole tree from the topmost supervisor (the `cmd /k npm run
build_and_start` window). This script finds the process listening on the
AI-Toolkit port, climbs up through its node.exe/cmd.exe ancestors, and
taskkills that tree — nothing else on the machine is touched (unlike the
bundled stop bat, which kills EVERY node.exe and python.exe).
"""
import ctypes
import ctypes.wintypes
import subprocess
import sys
import time

PORT = 8675
CLIMBABLE = {"node.exe", "cmd.exe"}


class PROCESSENTRY32W(ctypes.Structure):
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


def process_snapshot():
    """pid -> (ppid, exe name lowercased)"""
    k32 = ctypes.windll.kernel32
    snap = k32.CreateToolhelp32Snapshot(2, 0)
    procs = {}
    entry = PROCESSENTRY32W()
    entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
    if k32.Process32FirstW(snap, ctypes.byref(entry)):
        while True:
            procs[entry.th32ProcessID] = (entry.th32ParentProcessID,
                                          entry.szExeFile.lower())
            if not k32.Process32NextW(snap, ctypes.byref(entry)):
                break
    k32.CloseHandle(snap)
    return procs


def listener_pid(port):
    out = subprocess.run(["netstat", "-ano", "-p", "tcp"],
                         capture_output=True, text=True).stdout
    for line in out.splitlines():
        parts = line.split()
        if (len(parts) >= 5 and parts[0] == "TCP"
                and parts[1].endswith(f":{port}") and parts[3] == "LISTENING"):
            return int(parts[4])
    return None


def main():
    pid = listener_pid(PORT)
    if not pid:
        print(f"AI-Toolkit is not running (nothing listens on :{PORT}).")
        return 0
    procs = process_snapshot()
    top = cur = pid
    for _ in range(12):
        ppid = procs.get(cur, (0, ""))[0]
        if not ppid or ppid not in procs:
            break
        if procs[ppid][1] in CLIMBABLE:
            top = cur = ppid
        else:
            break
    print(f"killing AI-Toolkit tree from pid {top} "
          f"({procs.get(top, (0, '?'))[1]})")
    subprocess.run(["taskkill", "/PID", str(top), "/T", "/F"],
                   capture_output=True)
    # verify it stays down (concurrently would revive within ~1s)
    for _ in range(20):
        time.sleep(0.4)
        if not listener_pid(PORT):
            break
    leftover = listener_pid(PORT)
    if leftover:
        print(f"port still busy, killing listener {leftover} directly")
        subprocess.run(["taskkill", "/PID", str(leftover), "/T", "/F"],
                       capture_output=True)
    time.sleep(2.0)
    if listener_pid(PORT):
        print("WARNING: AI-Toolkit is still running.")
        return 1
    print("AI-Toolkit stopped.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
