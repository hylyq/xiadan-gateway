"""检查状态并重启服务"""
import subprocess
import sys
import psutil
import os

VENV_PYTHON = r"c:\Users\Marvin\xiadan-gateway\.venv\Scripts\python.exe"
MAIN_PY = r"c:\Users\Marvin\xiadan-gateway\main.py"

# 1. 检查现有的 main.py 进程
print("=== 检查现有进程 ===")
existing = []
for p in psutil.process_iter(["name", "pid", "cmdline"]):
    try:
        if "python" in (p.info["name"] or "").lower():
            cmd = " ".join(p.info["cmdline"] or [])
            if "main.py" in cmd:
                existing.append(p.info["pid"])
                print(f"  main.py 进程 PID={p.info['pid']}")
    except:
        pass

if existing:
    print(f"  共 {len(existing)} 个 main.py 进程运行中")
else:
    print("  main.py 进程不存在")

# 2. 检查 xiadan.exe
print("\n=== 检查 xiadan.exe ===")
xiadan = None
for p in psutil.process_iter(["name"]):
    try:
        if p.info["name"] and p.info["name"].lower() == "xiadan.exe":
            xiadan = p.info["name"]
            print(f"  xiadan.exe 运行中 PID={p.pid}")
    except:
        pass

if not xiadan:
    print("  xiadan.exe 未运行")
    config_path = r"c:\Users\Marvin\xiadan-gateway\config\app_config.json"
    import json
    with open(config_path) as f:
        cfg = json.load(f)
    exe_path = cfg.get("trading", {}).get("app_path", "")
    if exe_path and os.path.exists(exe_path):
        print(f"  尝试启动: {exe_path}")
        subprocess.Popen([exe_path])
    else:
        print(f"  未找到 xiadan.exe 路径，请手动启动")

# 3. 重启 main.py
print("\n=== 重启服务 ===")
if not existing:
    subprocess.Popen(
        [VENV_PYTHON, MAIN_PY],
        cwd=r"c:\Users\Marvin\xiadan-gateway",
        creationflags=subprocess.CREATE_NEW_CONSOLE
    )
    print("  main.py 已在新窗口启动")
else:
    print("  main.py 已在运行")
