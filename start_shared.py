"""Start the Python shared runtime, QQ bridge, and Codex Desktop on Windows."""

import argparse
import ctypes
import json
import msvcrt
import os
from pathlib import Path
import subprocess
import sys
import time
import winreg
from urllib.request import urlopen

from qq_state import DATA_DIR
from shared_runtime import HOST, PUBLIC_PORT

ROOT = Path(__file__).resolve().parent
URL = f"ws://{HOST}:{PUBLIC_PORT}"


def health():
    try:
        with urlopen(f"http://{HOST}:{PUBLIC_PORT}/healthz", timeout=2) as response:
            return json.load(response)
    except (OSError, ValueError):
        return None


def package_family():
    command = ["powershell.exe", "-NoProfile", "-Command",
               "(Get-AppxPackage OpenAI.Codex | Sort-Object Version -Descending | Select-Object -First 1).PackageFamilyName"]
    family = subprocess.check_output(command, text=True, timeout=15).strip()
    if not family:
        raise RuntimeError("找不到 Codex Desktop 应用包")
    return family


def desktop_running():
    command = ["powershell.exe", "-NoProfile", "-Command",
               "@(Get-CimInstance Win32_Process -Filter \"name='ChatGPT.exe'\" | Where-Object { $_.ExecutablePath -like '*\\WindowsApps\\OpenAI.Codex_*\\app\\ChatGPT.exe' -and $_.CommandLine -notmatch '--type=' }).Count"]
    try:
        return int(subprocess.check_output(command, text=True, timeout=15).strip()) > 0
    except (OSError, ValueError, subprocess.SubprocessError):
        raise RuntimeError("无法确认 Desktop 是否已退出")


def bridge_running():
    lock_path = DATA_DIR / "bridge.lock"
    with lock_path.open("a+b") as lock:
        if lock.seek(0, 2) == 0:
            lock.write(b"0")
            lock.flush()
        lock.seek(0)
        try:
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError:
            return True
        msvcrt.locking(lock.fileno(), msvcrt.LK_UNLCK, 1)
        return False


def broadcast_environment():
    result = ctypes.c_ulong()
    ctypes.windll.user32.SendMessageTimeoutW(0xffff, 0x1a, 0, "Environment", 2, 2000, ctypes.byref(result))


def activate_desktop():
    # Modern MSIX builds require package activation. Running app/ChatGPT.exe
    # directly loses package identity and fails during Desktop bootstrap.
    family = package_family()
    names = {"CODEX_APP_SERVER_WS_URL": URL, "CODEX_APP_SERVER_FORCE_CLI": "0"}
    previous = {}
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
        for name, value in names.items():
            try:
                previous[name] = winreg.QueryValueEx(key, name)
            except FileNotFoundError:
                previous[name] = None
            winreg.SetValueEx(key, name, 0, winreg.REG_SZ, value)
    broadcast_environment()
    try:
        subprocess.Popen(["explorer.exe", f"shell:AppsFolder\\{family}!App"], stdin=subprocess.DEVNULL)
        for _ in range(30):
            runtime = health()
            if runtime and any("Desktop" in title for title in runtime.get("clients", [])):
                return
            time.sleep(1)
        raise RuntimeError("Desktop 未连接共享代理；检查应用启动和共享运行时日志")
    finally:
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, "Environment") as key:
            for name, old in previous.items():
                if old is None:
                    winreg.DeleteValue(key, name)
                else:
                    winreg.SetValueEx(key, name, 0, old[1], old[0])
        broadcast_environment()


def launch():
    parser = argparse.ArgumentParser()
    parser.add_argument("--no-desktop", action="store_true", help="只启动共享运行时和 QQ 桥接")
    parser.add_argument("--no-bridge", action="store_true", help="只启动共享运行时和 Desktop")
    args = parser.parse_args()
    if os.name != "nt":
        raise SystemExit("此启动器仅支持 Windows")
    if not args.no_desktop and desktop_running():
        raise SystemExit("Codex Desktop 已在运行；请完整退出后再启动共享运行时")
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not args.no_bridge and bridge_running():
        raise SystemExit("QQ 桥接已在运行；请先停止旧进程再启动共享运行时")
    runtime = health()
    if runtime and not runtime.get("upstream"):
        raise SystemExit("共享代理端口已占用，但 App Server 未就绪")
    if not runtime:
        log = (DATA_DIR / "shared_runtime.log").open("a", encoding="utf-8")
        subprocess.Popen([sys.executable, str(ROOT / "shared_runtime.py")], cwd=ROOT,
                         stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                         creationflags=subprocess.CREATE_NO_WINDOW)
        for _ in range(100):
            runtime = health()
            if runtime and runtime.get("upstream"):
                break
            time.sleep(.2)
        else:
            raise SystemExit(f"共享运行时启动失败；查看 {DATA_DIR / 'shared_runtime.log'}")
    env = os.environ.copy()
    env["CODEXQQ_APP_SERVER_URL"] = URL
    env["CODEX_APP_SERVER_WS_URL"] = URL
    env["CODEX_APP_SERVER_FORCE_CLI"] = "0"
    if not args.no_bridge:
        pythonw = Path(sys.executable).with_name("pythonw.exe")
        log = (DATA_DIR / "bridge_startup.log").open("a", encoding="utf-8")
        subprocess.Popen([str(pythonw if pythonw.is_file() else sys.executable), str(ROOT / "qq_bridge.py")],
                         cwd=ROOT, env=env, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                         creationflags=subprocess.CREATE_NO_WINDOW)
    if not args.no_desktop:
        activate_desktop()
    expected = set()
    if not args.no_bridge:
        expected.add("Codex QQ Bridge")
    if not args.no_desktop:
        expected.add("Codex Desktop")
    for _ in range(40):
        runtime = health()
        if runtime and runtime.get("upstream") and expected.issubset(set(runtime.get("clients") or [])):
            break
        time.sleep(.5)
    else:
        raise RuntimeError(f"共享客户端未全部接入：预期 {sorted(expected)}，实际 {runtime}")
    print(f"共享 App Server：{URL}")


if __name__ == "__main__":
    launch()
