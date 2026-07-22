"""快速测试：Win32 Menu API 能否找到并触发"系统 -> 系统设置"菜单项"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

import time
import win32gui
import win32con
from pywinauto import Desktop

from src.constants import TRADING_WINDOW_TITLE


def main():
    # 1. 获取交易窗口
    dialogs = Desktop(backend="uia").windows(title=TRADING_WINDOW_TITLE)
    if not dialogs:
        print("[FAIL] 未找到交易窗口")
        return
    window = dialogs[0]
    hwnd = window.handle
    print(f"[OK] 窗口句柄: {hwnd:#x}")

    # 2. 尝试 GetMenu
    menu = win32gui.GetMenu(hwnd)
    if menu == 0:
        print("[FAIL] GetMenu 返回 0！窗口没有标准 Win32 菜单")
        return
    print(f"[OK] GetMenu 返回: {menu:#x}")

    # 3. 遍历顶级菜单项
    print("\n顶级菜单项:")
    top_count = win32gui.GetMenuItemCount(menu)
    for i in range(top_count):
        try:
            text = win32gui.GetMenuString(menu, i, win32con.MF_BYPOSITION)
            submenu = win32gui.GetSubMenu(menu, i)
            item_id = win32gui.GetMenuItemID(menu, i)
            print(f"  [{i}] text='{text}' submenu={submenu:#x} item_id={item_id}")
        except Exception as e:
            print(f"  [{i}] 读取失败: {e}")

    # 4. 查找"系统"子菜单
    print("\n查找'系统'子菜单...")
    system_submenu = None
    for i in range(top_count):
        try:
            text = win32gui.GetMenuString(menu, i, win32con.MF_BYPOSITION)
            if "系统" in text:
                system_submenu = win32gui.GetSubMenu(menu, i)
                print(f"[OK] 找到'系统'在位置 [{i}], submenu={system_submenu:#x}")
                break
        except Exception:
            continue

    if system_submenu is None or system_submenu == 0:
        print("[FAIL] 未找到'系统'子菜单")
        return

    # 5. 遍历"系统"子菜单项
    print("\n'系统'子菜单项:")
    sub_count = win32gui.GetMenuItemCount(system_submenu)
    for j in range(sub_count):
        try:
            text = win32gui.GetMenuString(system_submenu, j, win32con.MF_BYPOSITION)
            item_id = win32gui.GetMenuItemID(system_submenu, j)
            has_sub = win32gui.GetSubMenu(system_submenu, j)
            print(f"  [{j}] text='{text}' item_id={item_id} has_submenu={has_sub != 0}")
        except Exception as e:
            print(f"  [{j}] 读取失败: {e}")

    # 6. 查找"系统设置"
    print("\n查找'系统设置'...")
    for j in range(sub_count):
        try:
            text = win32gui.GetMenuString(system_submenu, j, win32con.MF_BYPOSITION)
            if "系统设置" in text:
                item_id = win32gui.GetMenuItemID(system_submenu, j)
                print(f"[OK] 找到'系统设置'在位置 [{j}], item_id={item_id}")

                if item_id == -1:
                    print("[FAIL] item_id=-1，'系统设置'是子菜单而非命令项")
                    return

                # 7. 发送 WM_COMMAND
                print(f"\n发送 WM_COMMAND({item_id})...")
                window.click_input()  # 先聚焦窗口
                time.sleep(0.2)
                win32gui.PostMessage(hwnd, win32con.WM_COMMAND, item_id, 0)
                print("[OK] WM_COMMAND 已发送")
                time.sleep(1.5)

                # 8. 查找系统设置弹窗
                print("\n查找系统设置弹窗...")
                all_windows = Desktop(backend="uia").windows(visible_only=True)
                found = None
                for w in all_windows:
                    try:
                        title = w.window_text() or ""
                        name = w.element_info.name or ""
                        if "系统设置" in title or "系统设置" in name or "设置" in title:
                            found = w
                            print(f"[OK] 找到: title='{title[:80]}' name='{name[:80]}'")
                    except Exception:
                        continue

                if found:
                    print("\n[SUCCESS] Win32 Menu API 方式有效！弹窗已出现。")
                    # 列出弹窗的关键控件
                    tab_count = 0
                    combo_count = 0
                    for ctrl in found.descendants():
                        try:
                            ctype = ctrl.element_info.control_type
                            text = ctrl.window_text() or ""
                            name = ctrl.element_info.name or ""
                            if ctype == "TabItem":
                                tab_count += 1
                                print(f"  Tab: text='{text[:40]}' name='{name[:40]}'")
                            if ctype == "ComboBox":
                                combo_count += 1
                                print(f"  ComboBox: text='{text[:40]}' name='{name[:40]}'")
                        except Exception:
                            continue
                    print(f"  共 {tab_count} 个 TabItem, {combo_count} 个 ComboBox")

                    # 关闭弹窗
                    try:
                        found.type_keys("{ESC}")
                    except Exception:
                        pass
                else:
                    print("[FAIL] 未找到系统设置弹窗！")
                    print("列出所有可见窗口标题:")
                    for w in all_windows:
                        try:
                            title = w.window_text() or ""
                            if title.strip():
                                print(f"  [{w.element_info.control_type}] '{title[:80]}'")
                        except Exception:
                            continue
                return
        except Exception as e:
            print(f"  [{j}] 处理失败: {e}")

    print("[FAIL] 未在子菜单中找到'系统设置'")


if __name__ == "__main__":
    main()
