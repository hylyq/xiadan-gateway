"""实验2：WMCopy 连续复制 + 完整验证码检测（perf/wmcopy 分支）

实验1 结论: WMCopy(0xE122) 首次 0.50s 成功且无验证码、数据与基线完全一致，
但连续尝试无数据且用户观察到验证码弹窗——实验脚本只用了 _detect_captcha
（仅扫顶层窗口），未用 _detect_captcha_full（含主窗口子树扫描），
验证码弹窗疑似主窗口内子对话框，被脚本漏检后一直卡住复制。

本脚本验证:
1. 用完整检测 + OCR 自动解题后，连续 WMCopy 能否稳定出数据
2. 验证码触发规律（每次都触发？还是频率限流：N 次内触发一次？）
3. 含验证码求解的单次总耗时 vs 基线 5.5s

注意: 运行期间会弹出验证码，脚本自动 OCR 作答，无需人工干预。
"""
import sys
import os
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import win32clipboard
import win32con
import win32gui

from pywinauto import Desktop

from src.core.ocr import OcrService
from src.services.position_service import PositionService
from src.services.window_service import WindowService

WM_COMMAND = 0x0111
GRID_COPY_COMMAND = 0xE122

ATTEMPTS = 5        # 连续尝试次数
PACE_SECONDS = 3.0  # 尝试间隔（降低触发频率风控的概率）
POLL_TIMEOUT = 8.0  # 单次等待数据上限（需容纳验证码 OCR 求解时间）


def read_clipboard_once() -> str:
    for _ in range(5):
        try:
            win32clipboard.OpenClipboard()
            try:
                if win32clipboard.IsClipboardFormatAvailable(win32con.CF_UNICODETEXT):
                    return win32clipboard.GetClipboardData(win32con.CF_UNICODETEXT) or ""
                return ""
            finally:
                win32clipboard.CloseClipboard()
        except Exception:
            time.sleep(0.03)
    return ""


def clear_clipboard() -> None:
    for _ in range(5):
        try:
            win32clipboard.OpenClipboard()
            win32clipboard.EmptyClipboard()
            win32clipboard.CloseClipboard()
            return
        except Exception:
            time.sleep(0.03)


def is_valid_table(data: str) -> bool:
    return bool(data) and "\t" in data.splitlines()[0]


def dump_top_windows():
    """诊断：列出当前所有可见顶层窗口（定位验证码弹窗的真实形态）"""
    try:
        for w in Desktop(backend="uia").windows(visible_only=True):
            print(f"    top-window: title={w.window_text()!r} class={w.class_name()}")
    except Exception as e:
        print(f"    top-window 枚举失败: {e}")


def solve_captcha_safe(ps, window) -> bool:
    """OCR 解验证码，返回是否成功（不抛异常）"""
    try:
        ps._solve_captcha(ps._captcha_window or window)
        return True
    except Exception as e:
        print(f"    验证码 OCR 求解失败: {e}")
        return False


def main():
    ws = WindowService()
    ocr = OcrService.get_instance()
    ocr.configure(ddddocr_enabled=False)
    print("OCR 引擎预热...", flush=True)
    ocr.warmup()
    ps = PositionService(ws, ocr)

    window = ws.get_trading_window()
    if window is None:
        print("[FAIL] 未找到交易窗口")
        return

    ps._prepare_query_panel()
    window = ws.get_trading_window()
    ps._navigate_to_query_page(window, "资金股票")
    time.sleep(0.3)

    grid = None
    for el in window.descendants():
        try:
            if el.class_name() == "CVirtualGridCtrl":
                grid = el
                break
        except Exception:
            continue
    if grid is None:
        print("[FAIL] 未找到 CVirtualGridCtrl 表格控件")
        return
    grid_hwnd = grid.handle
    print(f"[OK] 表格控件 hwnd={grid_hwnd:#x}，开始 {ATTEMPTS} 次连续 WMCopy（间隔 {PACE_SECONDS}s）\n")

    stats = []
    dumped = False
    for i in range(ATTEMPTS):
        label = f"#{i + 1}"
        clear_clipboard()
        t0 = time.perf_counter()
        win32gui.PostMessage(grid_hwnd, WM_COMMAND, GRID_COPY_COMMAND, 0)

        got_data = False
        captcha_seen = False
        solve_ok = None
        deadline = t0 + POLL_TIMEOUT
        while time.perf_counter() < deadline:
            data = read_clipboard_once()
            if is_valid_table(data):
                got_data = True
                break
            try:
                win = ws.get_trading_window_fast()
                if win is not None and ps._detect_captcha_full(win):
                    if not captcha_seen:
                        captcha_seen = True
                        print(f"  {label} 出现验证码（完整检测命中，等待 OCR 求解）")
                    if solve_ok is None:
                        solve_ok = solve_captcha_safe(ps, win)
                        if not solve_ok:
                            break
            except Exception:
                pass
            time.sleep(0.2)

        dt = time.perf_counter() - t0
        data = read_clipboard_once() if got_data else ""
        rows = ps._format_table_data(data) if got_data else None
        state = f"{len(rows)}行" if rows is not None else "无数据"
        cap_desc = "无" if not captcha_seen else (f"有/解题{'成功' if solve_ok else '失败'}")
        print(f"  {label}: {state} | 总耗时 {dt:.2f}s | 验证码: {cap_desc}")
        stats.append((label, state, dt, captcha_seen, bool(solve_ok)))

        if not got_data and not dumped:
            dumped = True
            print("  [诊断] 无数据且未检出验证码，当前顶层窗口清单:")
            dump_top_windows()

        if i < ATTEMPTS - 1:
            time.sleep(PACE_SECONDS)

    # 汇总
    print("\n========== 汇总 ==========")
    n_ok = sum(1 for s in stats if s[1] != "无数据")
    n_cap = sum(1 for s in stats if s[3])
    times = [s[2] for s in stats]
    print(f"成功 {n_ok}/{ATTEMPTS}，触发验证码 {n_cap}/{ATTEMPTS}")
    print(f"耗时: " + ", ".join(f"{s[0]}={s[2]:.2f}s" for s in stats))
    if n_ok:
        best = min(times)
        print(f"最快单次: {best:.2f}s（对照基线 copy 阶段 5.54s）")


if __name__ == "__main__":
    main()
