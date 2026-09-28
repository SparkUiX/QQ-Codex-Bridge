"""Register the shared QQ/Desktop runtime for hidden Windows logon startup."""

import argparse
import os
from pathlib import Path
import sys
import winreg

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "CodexQQBridge"


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--remove", action="store_true")
    args = parser.parse_args()
    if os.name != "nt":
        raise SystemExit("仅支持 Windows")
    with winreg.CreateKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
        if args.remove:
            try:
                winreg.DeleteValue(key, VALUE_NAME)
            except FileNotFoundError:
                pass
            print("已移除登录自启。")
            return
        pythonw = Path(sys.executable).with_name("pythonw.exe")
        if not pythonw.is_file():
            raise SystemExit(f"找不到无窗口解释器：{pythonw}")
        launcher = Path(__file__).resolve().with_name("start_shared.py")
        command = f'"{pythonw}" "{launcher}"'
        winreg.SetValueEx(key, VALUE_NAME, 0, winreg.REG_SZ, command)
        print(f"已配置当前用户登录时静默启动：{command}")


if __name__ == "__main__":
    main()
