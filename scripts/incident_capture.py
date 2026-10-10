"""弹窗现场只读取证：桌面/交易窗口/弹窗截图 + 顶层窗口清单 + UIA 树 dump

用途：券商客户端出现罕见弹窗（如断联提示、风控弹窗）时的现场存档，
供事后分析弹窗类型/成因/按钮语义。

【只读保证】全程零输入：不点击、不发键、不激活、不动鼠标、不改窗口
Z 序——截图走 PrintWindow/GDI，文本走 UIA 枚举（WM_GETTEXT 级），
窗口清单走 EnumWindows。现场保持原样，后续分析可复现。

用法（Session 0 经 run_in_session 启动到交互会话）:
    .venv/Scripts/python.exe scripts/run_in_session.py \
        --cwd <项目根> --stdout <out>/capture.log -- \
        .venv/Scripts/python.exe scripts/incident_capture.py --out <out>
"""
import argparse
import json
import os
import sys
from datetime import datetime

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

import psutil
import pyautogui
import win32gui
import win32process
from pywinauto import Desktop

from src.constants import MAIN_WINDOW_TITLE_KEYWORD
from src.utils.uia import safe_text

DIALOG_CLASS = "#32770"


def enum_process_toplevel_windows(pid: int) -> list:
    """枚举进程所有可见顶层窗口（不限类名——弹窗可能是任意窗口类）"""
    out = []

    def cb(h, _):
        try:
            _, wpid = win32process.GetWindowThreadProcessId(h)
            if wpid != pid or not win32gui.IsWindowVisible(h):
                return True
            out.append({
                "hwnd": h,
                "class": win32gui.GetClassName(h),
                "title": win32gui.GetWindowText(h),
                "rect": list(win32gui.GetWindowRect(h)),
            })
        except Exception:
            pass
        return True

    win32gui.EnumWindows(cb, None)
    return out


def enum_child_dialogs(main_hwnd: int) -> list:
    """枚举主窗口直接子级的 #32770（复制触发类弹窗，顶层枚举不可见）"""
    out = []

    def cb(h, _):
        try:
            if win32gui.GetParent(h) == main_hwnd and win32gui.IsWindowVisible(h) \
                    and win32gui.GetClassName(h) == DIALOG_CLASS:
                out.append({
                    "hwnd": h,
                    "class": win32gui.GetClassName(h),
                    "title": win32gui.GetWindowText(h),
                    "rect": list(win32gui.GetWindowRect(h)),
                })
        except Exception:
            pass
        return True

    win32gui.EnumWindows(cb, None)
    return out


def dump_uia_tree(el, max_depth: int = 10) -> str:
    """递归 dump UIA 控件树（只读文本枚举）"""
    lines = []

    def walk(e, depth):
        if depth > max_depth:
            lines.append(f"{'  ' * depth}- ...（超深截断）")
            return
        try:
            r = e.rectangle()
            rect = f"({r.left},{r.top}) {r.width()}x{r.height()}"
        except Exception:
            rect = "(?)"
        try:
            ctype = e.element_info.control_type or ""
        except Exception:
            ctype = ""
        try:
            cls = e.class_name() or ""
        except Exception:
            cls = ""
        try:
            name = safe_text(e) or ""
        except Exception:
            name = ""
        try:
            auto_id = e.element_info.automation_id or ""
        except Exception:
            auto_id = ""
        lines.append(f"{'  ' * depth}- {ctype} cls={cls} "
                     f"name={name!r} auto_id={auto_id} rect={rect}")
        try:
            for ch in e.children():
                walk(ch, depth + 1)
        except Exception:
            pass

    walk(el, 0)
    return "\n".join(lines)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True, help="存档目录")
    args = ap.parse_args()
    out = args.out if os.path.isabs(args.out) else os.path.join(
        PROJECT_ROOT, args.out)
    os.makedirs(out, exist_ok=True)
    print(f"[capture] 存档目录: {out}")

    # ---- 定位 xiadan.exe 与主窗口 ----
    xiadan = None
    for proc in psutil.process_iter(["name", "pid", "create_time"]):
        if (proc.info["name"] or "").lower() == "xiadan.exe":
            xiadan = proc
            break
    if xiadan is None:
        print("[capture] FATAL: 未找到 xiadan.exe")
        return 1
    print(f"[capture] xiadan.exe pid={xiadan.pid} "
          f"启动于 {datetime.fromtimestamp(xiadan.create_time())}")

    # ---- 1. 桌面全域截图（最外层现场：任务栏/窗口 Z 序/弹窗位置）----
    try:
        p = os.path.join(out, "desktop_full.png")
        pyautogui.screenshot(p)
        print(f"[capture] 桌面全域截图: {p}")
    except Exception as e:
        print(f"[capture] 桌面截图失败: {e}")

    # ---- 2. 顶层窗口清单（先枚举后截图，清单本身零副作用）----
    tops = enum_process_toplevel_windows(xiadan.pid)
    main_hwnd = None
    popups = []
    for w in tops:
        if MAIN_WINDOW_TITLE_KEYWORD in (w["title"] or ""):
            main_hwnd = w["hwnd"]
        else:
            popups.append(w)
    print(f"[capture] 顶层可见窗口 {len(tops)} 个: "
          + "; ".join(f"hwnd={w['hwnd']:#x} cls={w['class']} "
                      f"title={w['title']!r} rect={w['rect']}" for w in tops))
    print(f"[capture] 主窗口 hwnd={main_hwnd:#x}" if main_hwnd
          else "[capture] WARN: 未找到主窗口")

    child_dialogs = enum_child_dialogs(main_hwnd) if main_hwnd else []
    if child_dialogs:
        print(f"[capture] 主窗口子级 #32770: "
              + "; ".join(f"hwnd={w['hwnd']:#x} title={w['title']!r}"
                          for w in child_dialogs))
        popups += child_dialogs

    # ---- 3. 交易主窗口 PrintWindow 截图（含被弹窗遮挡区域）----
    if main_hwnd:
        try:
            win = Desktop(backend="uia").window(handle=main_hwnd)
            p = os.path.join(out, "trading_window.png")
            win.capture_as_image().save(p)
            print(f"[capture] 交易窗口截图: {p}")
        except Exception as e:
            print(f"[capture] 主窗口截图失败: {e}")

    # ---- 4. 每个候选弹窗 PrintWindow 截图 + UIA 树 dump ----
    uia_dump_path = os.path.join(out, "popup_uia_dump.txt")
    with open(uia_dump_path, "w", encoding="utf-8") as f:
        f.write(f"# 弹窗 UIA 控件树 dump @ {datetime.now()}\n")
        for i, w in enumerate(popups):
            tag = f"popup{i}_hwnd{w['hwnd']:#x}"
            try:
                dlg = Desktop(backend="uia").window(handle=w["hwnd"])
                img_path = os.path.join(out, f"{tag}.png")
                dlg.capture_as_image().save(img_path)
                print(f"[capture] 弹窗截图: {img_path}")
            except Exception as e:
                print(f"[capture] 弹窗截图失败 {tag}: {e}")
            f.write(f"\n===== {tag} cls={w['class']} "
                    f"title={w['title']!r} rect={w['rect']} =====\n")
            try:
                f.write(dump_uia_tree(dlg) + "\n")
            except Exception as e:
                f.write(f"(UIA dump 失败: {e})\n")
            print(f"[capture] UIA 树已写入 {uia_dump_path}（{tag}）")

    # ---- 5. 主窗口直接子窗口清单（一层，防弹窗挂在主窗口下）----
    main_children = []
    if main_hwnd:
        try:
            for ch in Desktop(backend="uia").window(handle=main_hwnd).children():
                try:
                    r = ch.rectangle()
                    main_children.append({
                        "class": ch.class_name(),
                        "title": safe_text(ch),
                        "rect": [r.left, r.top, r.width(), r.height()],
                    })
                except Exception:
                    pass
        except Exception as e:
            print(f"[capture] 主窗口子窗口枚举失败: {e}")

    # ---- 6. meta.json（现场上下文快照）----
    meta = {
        "captured_at": datetime.now().isoformat(),
        "xiadan_pid": xiadan.pid,
        "xiadan_create_time": datetime.fromtimestamp(
            xiadan.create_time()).isoformat(),
        "main_hwnd": main_hwnd,
        "toplevel_windows": tops,
        "popup_candidates": popups,
        "main_children": main_children,
    }
    meta_path = os.path.join(out, "meta.json")
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2, default=str)
    print(f"[capture] meta 写入: {meta_path}")
    print("[capture] DONE")
    return 0


if __name__ == "__main__":
    sys.exit(main())
