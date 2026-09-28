"""补充探测：1036 名称控件在 清空/无效代码 场景下的行为

场景矩阵（决定校验条件设计）:
  S1 清空代码框后           → 1036 应被券商清空（否则连续下单会残留旧名称误判）
  S2 输入无效代码 '999999'  → 1036 应保持空（否则不完整输入误判通过）
  S3 再输入有效代码 '601991' → 1036 恢复 '大唐发电'
只读实验：不点击下单按钮。
"""
import sys
import os
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import win32con
import win32gui

from src.models.config import AppConfig
from src.services.window_service import WindowService

NAME_CID = 1036
CODE_CID = 1032


def clear_box(el):
    try:
        el.type_keys("{HOME}+{END}{BACKSPACE}")
        time.sleep(0.1)
    except Exception:
        pass
    try:
        win32gui.SendMessage(el.handle, win32con.WM_SETTEXT, 0, "")
    except Exception:
        pass
    time.sleep(0.15)


def read_name(ws, window):
    el = ws.find_element_in_window(window, NAME_CID)
    try:
        return (el.window_text() or "").strip() if el else "<控件未找到>"
    except Exception as e:
        return f"<异常 {e}>"


def main():
    ws = WindowService()
    window = ws.get_trading_window()
    if window is None:
        print("[FATAL] 交易窗口未找到")
        return
    ws.activate_window(AppConfig().get_trading_app_paths())
    time.sleep(0.3)
    ws.send_key("F1", background=True)
    time.sleep(0.3)
    window = ws.get_trading_window()
    code_el = ws.find_element_in_window(window, CODE_CID)
    if code_el is None:
        print("[FATAL] 代码输入框未找到")
        return

    print(f"S0 初始 1036: {read_name(ws, window)!r}")

    clear_box(code_el)
    time.sleep(0.5)
    print(f"S1 清空代码框后 1036: {read_name(ws, window)!r}")

    clear_box(code_el)
    code_el.type_keys("999999")
    time.sleep(1.0)
    print(f"S2 输入无效代码后 1036: {read_name(ws, window)!r}")

    clear_box(code_el)
    code_el.type_keys("601991")
    time.sleep(1.0)
    print(f"S3 输入有效代码后 1036: {read_name(ws, window)!r}")

    # 连续场景：直接再输一遍同代码（模拟连续同向下单，不清空直接覆盖输入）
    code_el.type_keys("{HOME}+{END}{BACKSPACE}")
    time.sleep(0.1)
    code_el.type_keys("601991")
    time.sleep(1.0)
    print(f"S4 二次输入同代码后 1036: {read_name(ws, window)!r}")

    print("\n设计判定：")
    print("  S1/S2 为空 → 校验条件 = 轮询 1036 非空（名称自动填充=代码被接受）")
    print("  S1/S2 非空 → 需额外对比名称值变化或改用其他信号")


if __name__ == "__main__":
    main()
