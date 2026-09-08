"""实验：WMCopy 消息级复制表格 vs 现有 Ctrl+C 剪贴板法（perf/wmcopy 分支实验）

背景: easytrader 的 grid_strategies.WMCopy 通过向 CVirtualGridCtrl 投递
WM_COMMAND(0xE122) 消息触发客户端内置"复制"命令，无需前台激活、无需真实键盘，
理论上可绕开现有路径的三大成本：窗口激活、输入法拦截、GetAsyncKeyState 延迟。

本脚本在模拟盘上验证四件事:
1. 消息级复制(0xE122 / WM_COPY 0x0301)能否取到查询表格数据
2. 是否触发验证码弹窗（对照: 现有 Ctrl+C 路径必然触发）
3. 窗口最小化(后台)时消息级复制是否仍有效
4. 耗时对比: 消息级复制 vs 现有 Ctrl+C+OCR 全流程

只读实验: 仅查询页面切换与表格复制，不下单不撤单。
"""
import sys
import os
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import win32clipboard
import win32con
import win32gui

from src.core.ocr import OcrService
from src.services.position_service import PositionService
from src.services.window_service import WindowService

WM_COMMAND = 0x0111
GRID_COPY_COMMAND = 0xE122  # easytrader WMCopy 使用的客户端内置"复制"命令 ID
WM_COPY = 0x0301            # 标准 Windows 复制消息（对照实验）

results = []


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
    if not data or "\t" not in data.splitlines()[0]:
        return False
    return True


def post_copy(grid_hwnd: int, message: int, wparam: int) -> None:
    win32gui.PostMessage(grid_hwnd, message, wparam, 0)


def message_copy_attempt(ps, ws, grid_hwnd, label, wparam, message=WM_COMMAND,
                         solve_captcha=True, timeout=4.0):
    """单次消息级复制尝试: 清剪贴板 → post → 轮询数据/验证码 → 返回 (数据, 耗时, 是否触发验证码)"""
    clear_clipboard()
    t0 = time.perf_counter()
    post_copy(grid_hwnd, message, wparam)
    deadline = t0 + timeout
    captcha_flag = False
    while time.perf_counter() < deadline:
        data = read_clipboard_once()
        if is_valid_table(data):
            return data, time.perf_counter() - t0, captcha_flag
        try:
            window = ws.get_trading_window_fast()
            if window is not None and ps._detect_captcha(window):
                if not captcha_flag:
                    captcha_flag = True
                    print(f"  [{label}] 触发验证码弹窗")
                if solve_captcha:
                    try:
                        ps._solve_captcha(ps._captcha_window or window)
                    except Exception as e:
                        print(f"  [{label}] 验证码处理失败: {e}")
                        break
        except Exception:
            pass
        time.sleep(0.05)
    return read_clipboard_once(), time.perf_counter() - t0, captcha_flag


def dismiss_captcha(ps, ws):
    try:
        window = ws.get_trading_window_fast()
        if window is not None and ps._detect_captcha(window):
            win = ps._captcha_window or window
            btn = ws.find_element_in_window(win, 2)  # CAPTCHA_CANCEL_BUTTON_ID
            if btn is not None:
                btn.click()
                time.sleep(0.2)
    except Exception:
        pass


def main():
    ws = WindowService()  # Singleton.__new__ 返回全局单例
    ocr = OcrService.get_instance()
    ocr.configure(ddddocr_enabled=False)
    print("OCR 引擎预热...", flush=True)
    if not ocr.warmup():
        print("[WARN] OCR 预热失败，验证码场景可能无法自动处理")
    ps = PositionService(ws, ocr)

    window = ws.get_trading_window()
    if window is None:
        print("[FAIL] 未找到交易窗口 '网上股票交易系统5.0'")
        return
    main_hwnd = window.handle
    print(f"[OK] 交易窗口: hwnd={main_hwnd:#x}")

    # 导航到资金股票页（与 get_position 相同的前置）
    ps._prepare_query_panel()
    window = ws.get_trading_window()
    ps._navigate_to_query_page(window, "资金股票")
    time.sleep(0.3)

    # 定位表格控件
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
    print(f"[OK] 表格控件: hwnd={grid_hwnd:#x} control_id={grid.control_id()}")

    # ---------- 实验1: 基线（现有 Ctrl+C + OCR 全流程） ----------
    print("\n[实验1] 基线: 现有 Ctrl+C 剪贴板法（含验证码 OCR）")
    try:
        t0 = time.perf_counter()
        baseline_rows = ps._copy_table_via_clipboard()
        baseline_dt = time.perf_counter() - t0
        print(f"  耗时 {baseline_dt:.2f}s，得到 {len(baseline_rows)} 行数据")
        results.append(("基线: Ctrl+C+OCR（copy 阶段）", baseline_dt, "-", f"{len(baseline_rows)} 行"))
    except Exception as e:
        print(f"  [FAIL] 基线失败: {e}")
        baseline_rows = None
        dismiss_captcha(ps, ws)

    # ---------- 实验2: WMCopy 0xE122 前台 ×3 ----------
    print("\n[实验2] WMCopy (WM_COMMAND 0xE122) 前台 ×3")
    wm_rows = None
    for i in range(3):
        data, dt, captcha = message_copy_attempt(
            ps, ws, grid_hwnd, f"第{i + 1}次", GRID_COPY_COMMAND, solve_captcha=True)
        ok = is_valid_table(data)
        print(f"  第{i + 1}次: {'成功' if ok else '失败'} 耗时 {dt:.2f}s 触发验证码={captcha}")
        if ok:
            wm_rows = ps._format_table_data(data)
            results.append((f"WMCopy 0xE122 前台 第{i + 1}次", dt,
                            "触发" if captcha else "未触发", f"{len(wm_rows)} 行"))
        else:
            results.append((f"WMCopy 0xE122 前台 第{i + 1}次", dt,
                            "触发" if captcha else "未触发", "无数据"))
            dismiss_captcha(ps, ws)

    # ---------- 实验3: WMCopy 后台（窗口最小化） ----------
    print("\n[实验3] WMCopy 0xE122 后台（最小化交易窗口）")
    win32gui.ShowWindow(main_hwnd, win32con.SW_MINIMIZE)
    time.sleep(0.3)
    data, dt, captcha = message_copy_attempt(
        ps, ws, grid_hwnd, "后台", GRID_COPY_COMMAND, solve_captcha=False, timeout=2.5)
    ok = is_valid_table(data)
    print(f"  后台: {'成功' if ok else '失败'} 耗时 {dt:.2f}s 触发验证码={captcha}")
    results.append(("WMCopy 0xE122 后台(最小化)", dt,
                    "触发" if captcha else "未触发", f"{'有效' if ok else '无数据'}"))
    if captcha:
        dismiss_captcha(ps, ws)
    win32gui.ShowWindow(main_hwnd, win32con.SW_RESTORE)
    time.sleep(0.5)

    # ---------- 实验4: WM_COPY 0x0301 直接投递（对照） ----------
    print("\n[实验4] WM_COPY (0x0301) 直接投递到表格控件")
    data, dt, captcha = message_copy_attempt(
        ps, ws, grid_hwnd, "WM_COPY", 0, message=WM_COPY, solve_captcha=False, timeout=2.0)
    ok = is_valid_table(data)
    print(f"  结果: {'有数据' if ok else '无数据'} 耗时 {dt:.2f}s 触发验证码={captcha}")
    results.append(("WM_COPY 0x0301 直接投递", dt,
                    "触发" if captcha else "未触发", f"{'有效' if ok else '无数据'}"))
    if captcha:
        dismiss_captcha(ps, ws)

    # ---------- 一致性对比 ----------
    print("\n[一致性] WMCopy 数据 vs 基线数据")
    if baseline_rows is not None and wm_rows is not None:
        if baseline_rows == wm_rows:
            print(f"  [OK] 完全一致（{len(baseline_rows)} 行，字段与数值相同）")
        else:
            print(f"  [DIFF] 不一致: 基线 {len(baseline_rows)} 行 vs WMCopy {len(wm_rows)} 行")
            keys_b = set(baseline_rows[0].keys()) if baseline_rows else set()
            keys_w = set(wm_rows[0].keys()) if wm_rows else set()
            if keys_b != keys_w:
                print(f"    表头差异: 基线={sorted(keys_b)} WMCopy={sorted(keys_w)}")
            elif baseline_rows and wm_rows:
                for rb, rw in zip(baseline_rows, wm_rows):
                    if rb != rw:
                        print(f"    基线行: {rb}")
                        print(f"    WMCopy行: {rw}")
    else:
        print("  [SKIP] 任一方法无数据，跳过对比")

    # ---------- 汇总 ----------
    print("\n========== 耗时汇总 ==========")
    print(f"{'方法':<32}{'耗时(s)':>8}{'验证码':>8}  结果")
    for name, dt, captcha, note in results:
        print(f"{name:<32}{dt:>8.2f}{captcha:>8}  {note}")


if __name__ == "__main__":
    main()
