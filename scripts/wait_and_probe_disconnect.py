"""断开态复现监测：防火墙阻断 xiadan 出站后，轮询断开迹象并自动取证

配合防火墙实验（blk-xiadan-test-1010 出站阻断）使用：
每 10s 轮询 ①可见顶层 #32770 弹窗 ②UIA 全树「断开」字样；
任一出现 → 自动取证（桌面截图 + 命中元素清单 + 弹窗 UIA 树）后退出。
超时 300s 未观察到断开迹象 → 报 TIMEOUT 退出（规则可能未生效）。

零输入：不点击/不发键/不激活；仅 Enum/UIA 文本枚举/截图。

用法:
    .venv/Scripts/python.exe scripts/run_in_session.py --cwd <根> \
        --stdout <日志> -- .venv/Scripts/python.exe \
        scripts/wait_and_probe_disconnect.py [--out <存档目录>] [--timeout 300]
"""
import argparse
import json
import os
import sys
import time
from datetime import datetime

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

import psutil
import pyautogui
import win32gui
import win32process
from pywinauto import Desktop

from src.utils.uia import safe_text

KEYWORDS = ("断开", "mncg", "连接", "电信", "联通", "移动", "错误")
TRIGGER_TEXT = "断开"
DIALOG_CLASS = "#32770"
MAIN_TITLE_KEYWORD = "网上股票交易系统"


def find_xiadan():
    for proc in psutil.process_iter(["name", "pid"]):
        if (proc.info["name"] or "").lower() == "xiadan.exe":
            return proc.pid
    raise RuntimeError("xiadan.exe 未找到")


def find_main_hwnd(pid):
    out = []

    def cb(h, _):
        _, wpid = win32process.GetWindowThreadProcessId(h)
        if wpid == pid and win32gui.IsWindowVisible(h) \
                and MAIN_TITLE_KEYWORD in (win32gui.GetWindowText(h) or ""):
            out.append(h)
        return True

    win32gui.EnumWindows(cb, None)
    if not out:
        raise RuntimeError("主窗口未找到")
    return out[0]


def enum_popups(pid):
    out = []

    def cb(h, _):
        try:
            _, wpid = win32process.GetWindowThreadProcessId(h)
            if wpid == pid and win32gui.IsWindowVisible(h) \
                    and win32gui.GetClassName(h) == DIALOG_CLASS:
                out.append({
                    "hwnd": h,
                    "title": win32gui.GetWindowText(h),
                    "rect": list(win32gui.GetWindowRect(h)),
                })
        except Exception:
            pass
        return True

    win32gui.EnumWindows(cb, None)
    return out


def scan_tree_keywords(win):
    """全树扫描，返回命中 KEYWORDS 的元素摘要"""
    hits = []
    try:
        desc = win.descendants()
    except Exception as e:
        return [{"error": str(e)}]
    for d in desc:
        try:
            name = safe_text(d) or ""
        except Exception:
            continue
        if any(k in name for k in KEYWORDS):
            try:
                cls = d.class_name()
                ctype = d.element_info.control_type
                r = d.rectangle()
                rect = [r.left, r.top, r.right, r.bottom]
            except Exception:
                cls = ctype = rect = None
            hits.append({"name": name, "cls": cls,
                         "control_type": ctype, "rect": rect})
    return hits


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=None,
                    help="存档目录（默认 logs/incident_<时间戳>_disconnect）")
    ap.add_argument("--timeout", type=int, default=300)
    ap.add_argument("--poll", type=int, default=10)
    args = ap.parse_args()
    if args.out is None:
        args.out = f"logs/incident_{datetime.now():%Y%m%d_%H%M%S}_disconnect"
    out = args.out if os.path.isabs(args.out) else os.path.join(
        PROJECT_ROOT, args.out)
    os.makedirs(out, exist_ok=True)

    pid = find_xiadan()
    main_hwnd = find_main_hwnd(pid)
    print(f"[wait] pid={pid} main={main_hwnd:#x} 超时={args.timeout}s "
          f"轮询={args.poll}s", flush=True)

    win = Desktop(backend="uia").window(handle=main_hwnd)
    deadline = time.time() + args.timeout
    round_no = 0
    while time.time() < deadline:
        round_no += 1
        popups = enum_popups(pid)
        hits = scan_tree_keywords(win)
        names = [h.get("name", "") for h in hits]
        print(f"[wait] #{round_no} {datetime.now():%H:%M:%S} "
              f"弹窗={len(popups)} 命中={names}", flush=True)

        disconnected = any(TRIGGER_TEXT in n for n in names)
        if popups or disconnected:
            print(f"[wait] >>> 触发取证（弹窗={len(popups)} "
                  f"断开字样={disconnected}）", flush=True)
            # 桌面截图
            try:
                p = os.path.join(out, "desktop_disconnected.png")
                pyautogui.screenshot(p)
                print(f"[wait] 桌面截图: {p}", flush=True)
            except Exception as e:
                print(f"[wait] 截图失败: {e}", flush=True)
            # 命中元素清单 + 弹窗 UIA 树
            with open(os.path.join(out, "disconnect_hits.json"), "w",
                      encoding="utf-8") as f:
                json.dump({"time": datetime.now().isoformat(),
                           "popups": popups, "keyword_hits": hits},
                          f, ensure_ascii=False, indent=2, default=str)
            for i, p_ in enumerate(popups):
                try:
                    dlg = Desktop(backend="uia").window(handle=p_["hwnd"])
                    lines = []
                    for d in dlg.descendants():
                        try:
                            lines.append(
                                f"- {d.element_info.control_type} "
                                f"cls={d.class_name()} "
                                f"name={safe_text(d)!r} "
                                f"auto_id={d.element_info.automation_id}")
                        except Exception:
                            pass
                    with open(os.path.join(
                            out, f"popup{i}_uia.txt"), "w",
                            encoding="utf-8") as f:
                        f.write("\n".join(lines))
                    print(f"[wait] 弹窗{i} UIA 已存档", flush=True)
                except Exception as e:
                    print(f"[wait] 弹窗{i} dump 失败: {e}", flush=True)
            print("[wait] DONE-TRIGGERED", flush=True)
            return 0
        time.sleep(args.poll)

    print("[wait] TIMEOUT: 300s 内未观察到断开迹象——"
          "防火墙规则可能未匹配目标程序", flush=True)
    return 1


if __name__ == "__main__":
    sys.exit(main())
