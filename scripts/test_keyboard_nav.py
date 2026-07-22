"""测试键盘导航方式操作自定义菜单

点击 MenuBar "系统" → 键盘方向键导航 → 找到"系统设置" → 回车
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

import time
import win32api
import win32con
import win32gui
from pywinauto import Desktop

from src.constants import TRADING_WINDOW_TITLE


def send_key_combo(vk_mod, vk_key, delay=0.05):
    """发送组合键"""
    win32api.keybd_event(vk_mod, 0, 0, 0)
    time.sleep(delay)
    win32api.keybd_event(vk_key, 0, 0, 0)
    time.sleep(delay)
    win32api.keybd_event(vk_key, 0, win32con.KEYEVENTF_KEYUP, 0)
    win32api.keybd_event(vk_mod, 0, win32con.KEYEVENTF_KEYUP, 0)


def send_key(vk, delay=0.05):
    """发送单个按键"""
    win32api.keybd_event(vk, 0, 0, 0)
    time.sleep(delay)
    win32api.keybd_event(vk, 0, win32con.KEYEVENTF_KEYUP, 0)
    time.sleep(delay)


def main():
    print("=== 键盘导航菜单测试 ===")

    # 获取窗口
    dialogs = Desktop(backend="uia").windows(title=TRADING_WINDOW_TITLE)
    if not dialogs:
        print("[FAIL] 未找到交易窗口")
        return
    window = dialogs[0]
    hwnd = window.handle
    print(f"[OK] 窗口句柄: {hwnd:#x}")

    # 确保窗口在前台
    window.click_input()
    time.sleep(0.3)
    if win32gui.GetForegroundWindow() != hwnd:
        print(f"[WARN] 窗口不在前台! FG={win32gui.GetForegroundWindow():#x}")
        # 尝试强置前台
        win32gui.SetForegroundWindow(hwnd)
        time.sleep(0.3)
    print(f"[OK] 前台窗口: {win32gui.GetForegroundWindow():#x}")

    # 方案A: 点击 MenuBar "系统" 然后用 Down 导航
    print("\n--- 方案A: click_input + 方向键 ---")

    # 找到"系统"MenuItem并点击
    system_item = None
    for ctrl in window.descendants():
        try:
            if ctrl.element_info.control_type == "MenuItem" and (
                "系统" in (ctrl.window_text() or "") or "系统" in (ctrl.element_info.name or "")
            ):
                system_item = ctrl
                break
        except Exception:
            continue

    if system_item:
        print(f"找到 MenuItem '系统'，点击...")
        system_item.click_input()
        time.sleep(0.3)

        # 发送 Down 箭头 + Enter（从最少开始，避免误选"退出系统"）
        for down_presses in [2, 3, 4]:
            print(f"\n尝试 Down×{down_presses} + Enter...")

            # 先按 ESC 关闭可能已打开的菜单（从上次尝试）
            send_key(win32con.VK_ESCAPE, 0.1)
            time.sleep(0.1)

            # 重新打开菜单
            system_item.click_input()
            time.sleep(0.2)

            # Down 若干次
            for _ in range(down_presses):
                send_key(win32con.VK_DOWN, 0.1)
                time.sleep(0.08)

            # Enter
            send_key(win32con.VK_RETURN, 0.1)
            time.sleep(1.0)

            # 检查对话框是否出现
            all_windows = Desktop(backend="uia").windows(visible_only=True)
            found = False
            for w in all_windows:
                try:
                    title = w.window_text() or ""
                    name = w.element_info.name or ""
                    if "系统设置" in title or "系统设置" in name:
                        print(f"  [SUCCESS!] Down×{down_presses} 成功打开了系统设置弹窗!")
                        print(f"  title='{title}' name='{name}'")
                        found = True
                        # 关闭弹窗
                        try:
                            w.type_keys("{ESC}")
                        except Exception:
                            pass
                        break
                except Exception:
                    continue

            if not found:
                print(f"  Down×{down_presses} 未打开系统设置弹窗")

    # 方案B: Alt 键激活菜单栏
    print("\n--- 方案B: Alt 键 + 方向键 ---")
    window.click_input()
    time.sleep(0.2)

    # Alt 键激活菜单栏
    send_key(win32con.VK_MENU, 0.1)  # VK_MENU = Alt
    time.sleep(0.3)

    # down arrow 打开第一个菜单(系统)
    send_key(win32con.VK_DOWN, 0.1)
    time.sleep(0.2)

    # 导航到系统设置（尝试 3/4/5 次 Down）
    for down_presses in [2, 3, 4]:
        print(f"\n尝试 Alt→Down→Down×{down_presses}→Enter...")

        # 先 ESC 重置
        send_key(win32con.VK_ESCAPE, 0.1)
        time.sleep(0.1)
        send_key(win32con.VK_ESCAPE, 0.1)
        time.sleep(0.1)

        # 重新激活菜单
        send_key(win32con.VK_MENU, 0.1)
        time.sleep(0.2)
        send_key(win32con.VK_DOWN, 0.1)
        time.sleep(0.15)

        for _ in range(down_presses):
            send_key(win32con.VK_DOWN, 0.1)
            time.sleep(0.08)

        send_key(win32con.VK_RETURN, 0.1)
        time.sleep(1.0)

        # 检查
        all_windows = Desktop(backend="uia").windows(visible_only=True)
        for w in all_windows:
            try:
                title = w.window_text() or ""
                name = w.element_info.name or ""
                if "系统设置" in title or "系统设置" in name:
                    print(f"  [SUCCESS!] 方案B Down×{down_presses} 成功!")
                    try:
                        w.type_keys("{ESC}")
                    except Exception:
                        pass
                    break
            except Exception:
                continue
        else:
            print(f"  方案B Down×{down_presses} 未成功")

    print("\n=== 测试完成 ===")


if __name__ == "__main__":
    main()
