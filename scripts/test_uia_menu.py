"""测试 UIA ExpandCollapse/Invoke 模式操作自定义菜单栏"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

import time
import win32gui
from pywinauto import Desktop
from pywinauto.uia_element_info import UIAElementInfo
from pywinauto.controls.uiawrapper import UIAWrapper

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

    # 激活窗口
    window.click_input()
    time.sleep(0.3)

    # 2. 查找 MenuBar "系统" 及其 MenuItem 子项
    print("\n=== 查找 MenuBar/MenuItem ===")
    menubar = None
    system_menuitem = None
    for ctrl in window.descendants():
        try:
            ctype = ctrl.element_info.control_type
            text = ctrl.window_text() or ""
            name = ctrl.element_info.name or ""

            if ctype == "MenuBar" and "系统" in (text + name):
                menubar = ctrl
                print(f"[OK] 找到 MenuBar: text='{text}' name='{name}'")
                # 尝试获取其 children
                try:
                    children = ctrl.children()
                    print(f"  MenuBar 有 {len(children)} 个直接子控件:")
                    for ch in children:
                        try:
                            ch_type = ch.element_info.control_type
                            ch_text = ch.window_text() or ""
                            ch_name = ch.element_info.name or ""
                            ch_rect = ch.rectangle()
                            print(f"    [{ch_type}] text='{ch_text[:40]}' name='{ch_name[:40]}' "
                                  f"rect=({ch_rect.left},{ch_rect.top},{ch_rect.right},{ch_rect.bottom})")
                        except Exception as e:
                            print(f"    子控件读取失败: {e}")
                except Exception as e:
                    print(f"  获取 children 失败: {e}")

            if ctype == "MenuItem" and "系统" in (text + name):
                system_menuitem = ctrl
                print(f"[OK] 找到 MenuItem: text='{text}' name='{name}'")
                # 尝试获取其 children（展开后的下拉菜单项）
                try:
                    children = ctrl.children()
                    print(f"  MenuItem 有 {len(children)} 个子控件:")
                    for ch in children:
                        try:
                            ch_type = ch.element_info.control_type
                            ch_text = ch.window_text() or ""
                            ch_name = ch.element_info.name or ""
                            print(f"    [{ch_type}] text='{ch_text[:40]}' name='{ch_name[:40]}'")
                        except Exception as e:
                            print(f"    子控件读取失败: {e}")
                except Exception as e:
                    print(f"  获取 children 失败: {e}")
        except Exception:
            continue

    if system_menuitem is None:
        print("[FAIL] 未找到'系统'MenuItem")
        return

    # 3. 尝试 UIA ExpandCollapsePattern
    print("\n=== 尝试 ExpandCollapsePattern ===")
    try:
        ei = system_menuitem.element_info
        # 尝试用 IUIAutomation 直接调用 Expand
        import comtypes.client
        uia = comtypes.client.CreateObject("CUIAutomation8")
        # 或者用 pywinauto 的方式
        from pywinauto.controls.uia_controls import MenuItemWrapper
        wrapper = MenuItemWrapper(system_menuitem.element_info)

        # 检查是否支持 ExpandCollapse
        if hasattr(wrapper, 'expand'):
            print("支持 expand()")
            wrapper.expand()
            time.sleep(0.5)
            print("[OK] expand() 已调用")
        elif hasattr(wrapper, 'expand_collapse_state'):
            print(f"expand_collapse_state: {wrapper.expand_collapse_state()}")
        else:
            print("不支持 expand() 方法")

        # 检查是否支持 LegacyIAccessiblePattern
        if hasattr(wrapper, 'legacyiaccessible_pattern'):
            print("支持 LegacyIAccessiblePattern")
            try:
                lia = wrapper.legacyiaccessible_pattern()
                print(f"  DefaultAction: {lia.DefaultAction}")
                lia.DoDefaultAction()
                print("[OK] DoDefaultAction() 已调用")
            except Exception as e:
                print(f"  LegacyIAccessible 失败: {e}")
    except Exception as e:
        print(f"ExpandCollapsePattern 失败: {e}")

    # 4. 改用 click_input() 点击 MenuItem "系统"，然后立即搜索
    print("\n=== 点击 MenuItem '系统' → 搜索下拉菜单 ===")
    system_menuitem.click_input()
    print("已点击，等待...")
    time.sleep(0.8)

    # 搜索所有可见窗口中的菜单相关窗口
    print("\n搜索新出现的窗口（可能是下拉菜单）:")
    all_windows = Desktop(backend="uia").windows(visible_only=True)
    for w in all_windows:
        try:
            title = w.window_text() or ""
            name = w.element_info.name or ""
            ctype = w.element_info.control_type
            class_name = ""
            try:
                class_name = w.element_info.class_name or ""
            except Exception:
                pass
            if class_name and ("menu" in class_name.lower() or "popup" in class_name.lower()):
                print(f"  >>> [{ctype}] class='{class_name}' title='{title[:60]}' name='{name[:60]}'")
                # 列出菜单项
                for item in w.descendants():
                    try:
                        item_type = item.element_info.control_type
                        item_text = item.window_text() or ""
                        item_name = item.element_info.name or ""
                        if item_text.strip() or item_name.strip():
                            print(f"      [{item_type}] text='{item_text[:50]}' name='{item_name[:50]}'")
                    except Exception:
                        continue
        except Exception:
            continue

    # 5. 搜索主窗口中新出现的 MenuItem（可能是下拉菜单内嵌在主窗口中）
    print("\n搜索主窗口中的新 MenuItem:")
    dialogs = Desktop(backend="uia").windows(title=TRADING_WINDOW_TITLE)
    if dialogs:
        window2 = dialogs[0]
        menu_items = []
        for ctrl in window2.descendants():
            try:
                ctype = ctrl.element_info.control_type
                text = ctrl.window_text() or ""
                name = ctrl.element_info.name or ""
                if ctype == "MenuItem":
                    menu_items.append((text, name))
                    if text.strip() or name.strip():
                        print(f"  [MenuItem] text='{text[:50]}' name='{name[:50]}'")
            except Exception:
                continue
        print(f"  共找到 {len(menu_items)} 个 MenuItem")

    # 6. 尝试搜索整个窗口树中所有包含"系统设置"的控件
    print("\n=== 全量搜索 '系统设置' 文本 ===")
    for w in all_windows:
        try:
            for ctrl in w.descendants():
                try:
                    text = ctrl.window_text() or ""
                    name = ctrl.element_info.name or ""
                    combined = f"{text} {name}"
                    if "系统设置" in combined:
                        ctype = ctrl.element_info.control_type
                        print(f"  [FOUND!] [{ctype}] text='{text[:60]}' name='{name[:60]}'")
                        print(f"    所属窗口: {w.window_text() or w.element_info.name}")
                except Exception:
                    continue
        except Exception:
            continue

    # 关闭可能打开的菜单
    print("\n清理：按 ESC...")
    try:
        window.type_keys("{ESC}")
    except Exception:
        pass


if __name__ == "__main__":
    main()
