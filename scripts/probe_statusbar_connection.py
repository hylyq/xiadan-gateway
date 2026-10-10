"""只读探测（第 2 轮）：状态栏连接字样在 UIA 树深处的位置与读取耗时

第 1 轮结论：主窗口直接子控件无状态栏（MDI 框架结构，非标准
msctls_statusbar32）。本轮：
  1. 全树 descendants() 冷扫描，找 StatusBar / 含连接字样的元素
  2. 对命中元素连续 texts() ×5 ——「缓存后定向读取」的周期成本
  3. 顺带打印主窗口全部子窗口类名分布（自绘状态栏的真实类名）

零输入：不点击/不发键/不激活；仅 Enum/WM_GETTEXT 级只读操作。
"""
import os
import sys
import time
from collections import Counter

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

import psutil
import win32gui
import win32process
from pywinauto import Desktop

from src.utils.uia import safe_text

KEYWORDS = ("断开", "mncg", "连接", "电信", "联通", "移动")


def find_main_hwnd():
    for proc in psutil.process_iter(["name", "pid"]):
        if (proc.info["name"] or "").lower() == "xiadan.exe":
            pid = proc.pid
            break
    else:
        raise RuntimeError("xiadan.exe 未找到")
    out = []

    def cb(h, _):
        _, wpid = win32process.GetWindowThreadProcessId(h)
        if wpid == pid and win32gui.IsWindowVisible(h) \
                and "网上股票交易系统" in (win32gui.GetWindowText(h) or ""):
            out.append(h)
        return True

    win32gui.EnumWindows(cb, None)
    return out[0]


def main():
    main_hwnd = find_main_hwnd()
    print(f"[probe2] 主窗口 hwnd={main_hwnd:#x}")

    # ---- 子窗口类名分布（找自绘状态栏的真身）----
    classes = Counter()

    def child_cb(h, _):
        classes[win32gui.GetClassName(h) or "?"] += 1
        return True

    win32gui.EnumChildWindows(main_hwnd, child_cb, None)
    print(f"[probe2] 子窗口类名分布（{sum(classes.values())} 个）:")
    for cls, n in classes.most_common():
        print(f"    {n:4d}  {cls}")

    # ---- UIA 全树冷扫描 ----
    win = Desktop(backend="uia").window(handle=main_hwnd)
    t0 = time.perf_counter()
    desc = win.descendants()
    t_cold = (time.perf_counter() - t0) * 1000
    print(f"[probe2] UIA 全树 descendants() 冷扫描: {t_cold:.0f}ms, "
          f"{len(desc)} 个元素")

    # ---- 找连接字样 / StatusBar ----
    hits = []
    t1 = time.perf_counter()
    for d in desc:
        try:
            cls = d.class_name() or ""
            name = safe_text(d) or ""
        except Exception:
            continue
        ctype = ""
        try:
            ctype = d.element_info.control_type or ""
        except Exception:
            pass
        if ctype == "StatusBar" or "statusbar" in cls.lower() \
                or any(k in name for k in KEYWORDS):
            hits.append((d, ctype, cls, name))
    t_scan = (time.perf_counter() - t1) * 1000
    print(f"[probe2] 含连接字样/StatusBar 的元素 {len(hits)} 个 "
          f"(扫描+读文本 {t_scan:.0f}ms):")
    for d, ctype, cls, name in hits[:15]:
        try:
            r = d.rectangle()
            rect = f"({r.left},{r.top},{r.right},{r.bottom})"
        except Exception:
            rect = "(?)"
        print(f"    - {ctype} cls={cls} name={name!r} rect={rect}")

    # ---- 缓存后定向读取 ×5（模拟 30-60s 监控周期内的读取成本）----
    if hits:
        target = hits[0][0]
        samples = []
        for i in range(5):
            t2 = time.perf_counter()
            try:
                txt = safe_text(target)
            except Exception as e:
                txt = f"ERR: {e}"
            ms = (time.perf_counter() - t2) * 1000
            samples.append(ms)
            if i < 2:
                print(f"[probe2] 缓存元素 texts() #{i}: {ms:.1f}ms "
                      f"-> {txt!r}")
        print(f"[probe2] 定向读取 5 次均值: "
              f"{sum(samples) / len(samples):.2f}ms")


if __name__ == "__main__":
    main()
