"""诊断脚本：检查券商软件"系统设置 → 快速交易"的 UI 结构

独立运行，不依赖主程序。用于定位 configure_quick_trade_settings() 失败的原因。
"""
import time
import sys
import os

# 强制 UTF-8 输出，避免 GBK 编码报错
sys.stdout.reconfigure(encoding='utf-8', errors='replace')

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pywinauto import Application, Desktop

from src.constants import TRADING_WINDOW_TITLE


def safe_str(s):
    """安全编码字符串"""
    if s is None:
        return ""
    return str(s)


def dump_ui_tree(element, prefix="", max_depth=4, current_depth=0):
    """递归输出 UI 控件树"""
    if current_depth > max_depth:
        return
    try:
        children = element.children()
    except Exception:
        return
    for child in children:
        try:
            cid = child.control_id()
        except Exception:
            cid = "?"
        try:
            ctype = child.element_info.control_type
        except Exception:
            ctype = "?"
        try:
            text = child.window_text() or ""
        except Exception:
            text = ""
        try:
            name = child.element_info.name or ""
        except Exception:
            name = ""
        try:
            rect = child.rectangle()
            rect_str = f"({rect.left},{rect.top},{rect.right},{rect.bottom})"
        except Exception:
            rect_str = "?"

        # 标记可能是目标控件的行
        markers = []
        if "系统" in text or "系统" in name:
            markers.append(">>> '系统'按钮 <<<")
        if "设置" in text or "设置" in name:
            markers.append(">> '设置' <<")
        if "快速交易" in text or "快速交易" in name:
            markers.append(">>> '快速交易'标签 <<<")
        if "确认" in text or "确认" in name:
            markers.append("* '确认' *")
        if "买入" in text or "买入" in name:
            markers.append("* '买入' *")
        if "卖出" in text or "卖出" in name:
            markers.append("* '卖出' *")
        if ctype == "ComboBox":
            markers.append("[ComboBox]")
        if ctype == "TabControl" or ctype == "TabItem":
            markers.append("[Tab]")

        marker_str = " ".join(markers) if markers else ""
        print(f"{prefix}{'  ' * current_depth}[{ctype}] cid={cid} "
              f"text='{text[:80]}' name='{name[:80]}' rect={rect_str} {marker_str}")

        dump_ui_tree(child, prefix, max_depth, current_depth + 1)


def main():
    print("=" * 80)
    print("券商软件系统设置 -> 快速交易 UI 诊断")
    print("=" * 80)

    # 1. 查找交易窗口
    print("\n[1] 查找交易窗口（网上股票交易系统5.0）...")
    dialogs = Desktop(backend="uia").windows(title=TRADING_WINDOW_TITLE)
    if not dialogs:
        print("[FAIL] 未找到交易窗口！请确认券商软件已打开。")
        return
    window = dialogs[0]
    print(f"[OK] 找到窗口: {window.window_text()}")
    print(f"   句柄: {window.handle:#x}")

    # 激活窗口
    window.click_input()
    time.sleep(0.3)

    # 2. 输出主窗口 UI 结构（聚焦右上角区域，只输出浅层）
    print("\n[2] 主窗口 UI 结构（浅层扫描）...")
    print("-" * 80)
    dump_ui_tree(window, "", max_depth=2)

    # 3. 专门搜索"系统/设置"相关控件
    print("\n" + "=" * 80)
    print("[3] 搜索 '系统' / '设置' 相关控件（深层扫描）...")
    print("-" * 80)
    found_system = []
    for ctrl in window.descendants():
        try:
            text = ctrl.window_text() or ""
            name = ctrl.element_info.name or ""
            combined = f"{text} {name}"
            if "系统" in combined or "设置" in combined:
                cid = ctrl.control_id()
                ctype = ctrl.element_info.control_type
                try:
                    rect = ctrl.rectangle()
                    rect_str = f"({rect.left},{rect.top},{rect.right},{rect.bottom})"
                except Exception:
                    rect_str = "?"
                print(f"  [{ctype}] cid={cid} text='{text[:80]}' name='{name[:80]}' "
                      f"rect={rect_str}")
                found_system.append(ctrl)
        except Exception:
            continue

    if not found_system:
        print("  [FAIL] 未找到任何包含'系统'或'设置'的控件！")
        print("  尝试列出所有 ToolBar/MenuBar/Button 控件...")
        for ctrl in window.descendants():
            try:
                ctype = ctrl.element_info.control_type
                if ctype in ("ToolBar", "Button", "MenuItem", "MenuBar"):
                    text = ctrl.window_text() or ""
                    name = ctrl.element_info.name or ""
                    cid = ctrl.control_id()
                    try:
                        rect = ctrl.rectangle()
                        rect_str = f"({rect.left},{rect.top},{rect.right},{rect.bottom})"
                    except Exception:
                        rect_str = "?"
                    if text.strip() or name.strip():
                        print(f"  [{ctype}] cid={cid} text='{text[:60]}' name='{name[:60]}' "
                              f"rect={rect_str}")
            except Exception:
                continue
    else:
        print(f"\n  找到 {len(found_system)} 个相关控件。尝试点击第一个...")
        btn = found_system[0]
        try:
            btn.click_input()
            print("  已点击，等待 0.5s...")
        except Exception as e:
            print(f"  [FAIL] click_input 失败: {e}")
            # 尝试用 click() 替代
            try:
                btn.click()
                print("  改用 click() 成功")
            except Exception as e2:
                print(f"  [FAIL] click() 也失败: {e2}")
        time.sleep(0.8)

        # 4. 查找弹出菜单
        print("\n[4] 查找弹出菜单/下拉列表...")
        print("-" * 80)
        popups = Desktop(backend="uia").windows(visible_only=True)
        menu_found = False
        popup_count = 0
        for popup in popups:
            try:
                title = popup.window_text() or ""
                name = popup.element_info.name or ""
                ctype = popup.element_info.control_type
                popup_count += 1
                print(f"\n  弹出窗口 #{popup_count}: type={ctype} title='{title[:60]}' name='{name[:60]}'")
                # 列出子项
                for item in popup.descendants():
                    try:
                        item_text = item.window_text() or ""
                        item_name = item.element_info.name or ""
                        item_type = item.element_info.control_type
                        if item_text.strip() or item_name.strip():
                            marker = ""
                            if "系统设置" in item_text or "系统设置" in item_name:
                                menu_found = True
                                marker = " <<< 找到了！"
                            print(f"    [{item_type}] text='{item_text[:60]}' name='{item_name[:60]}'{marker}")
                    except Exception:
                        continue
            except Exception:
                continue

        if popup_count == 0:
            print("  (没有找到任何可见弹出窗口)")

        if not menu_found:
            print("\n  [WARN] 未在弹出窗口中找到'系统设置'菜单项")
            print("  尝试在主窗口中搜索（菜单可能是内嵌子控件）...")
            # 重新获取窗口
            dialogs = Desktop(backend="uia").windows(title=TRADING_WINDOW_TITLE)
            if dialogs:
                window2 = dialogs[0]
                for ctrl in window2.descendants():
                    try:
                        text = ctrl.window_text() or ""
                        name = ctrl.element_info.name or ""
                        if "系统设置" in text or "系统设置" in name:
                            ctype = ctrl.element_info.control_type
                            print(f"  [OK] 在主窗口中找到: [{ctype}] text='{text[:60]}' name='{name[:60]}'")
                            menu_found = True
                    except Exception:
                        continue

        # 5. 如果有"系统设置"菜单项，点击它
        if menu_found:
            print("\n[5] 点击'系统设置'并检查弹窗...")
            # 先尝试在弹出窗口中点击
            clicked = False
            popups = Desktop(backend="uia").windows(visible_only=True)
            for popup in popups:
                try:
                    for item in popup.descendants():
                        try:
                            item_text = item.window_text() or ""
                            item_name = item.element_info.name or ""
                            if "系统设置" in item_text or "系统设置" in item_name:
                                item.click_input()
                                print("  [OK] 已点击'系统设置'（弹出窗口方式）")
                                clicked = True
                                break
                        except Exception:
                            continue
                    if clicked:
                        break
                except Exception:
                    continue

            if not clicked:
                # 在主窗口中找到并点击
                dialogs = Desktop(backend="uia").windows(title=TRADING_WINDOW_TITLE)
                if dialogs:
                    for ctrl in dialogs[0].descendants():
                        try:
                            text = ctrl.window_text() or ""
                            name = ctrl.element_info.name or ""
                            if "系统设置" in text or "系统设置" in name:
                                ctrl.click_input()
                                print("  [OK] 已点击'系统设置'（主窗口方式）")
                                clicked = True
                                break
                        except Exception:
                            continue

            time.sleep(1.5)

            # 6. 查找系统设置弹窗 — 列出所有可见窗口
            print("\n[6] 查找系统设置弹窗（列出所有可见顶层窗口）...")
            print("-" * 80)
            all_dialogs = Desktop(backend="uia").windows(visible_only=True)
            settings_dialog = None
            dlg_count = 0
            for dlg in all_dialogs:
                try:
                    title = dlg.window_text() or ""
                    name = dlg.element_info.name or ""
                    dlg_count += 1
                    ctype = dlg.element_info.control_type
                    marker = ""
                    if "系统设置" in title or "系统设置" in name or "设置" in title:
                        settings_dialog = dlg
                        marker = " <<< 可能是系统设置弹窗！"
                    print(f"  #{dlg_count} [{ctype}] title='{title[:80]}' name='{name[:80]}'{marker}")
                except Exception:
                    continue

            if settings_dialog:
                print(f"\n[7] 系统设置弹窗 UI 结构（深度扫描, max_depth=3）...")
                print("-" * 80)
                dump_ui_tree(settings_dialog, "", max_depth=3)

                # 专门搜索关键控件
                print("\n[8] 关键控件搜索...")
                print("-" * 80)

                # 搜索"快速交易"标签
                print("\n  [Tab 控件搜索]:")
                for ctrl in settings_dialog.descendants():
                    try:
                        text = ctrl.window_text() or ""
                        name = ctrl.element_info.name or ""
                        ctype = ctrl.element_info.control_type
                        combined = f"{text} {name}"
                        if "快速交易" in combined or "快速" in combined or ctype in ("TabItem", "TabControl"):
                            print(f"  [{ctype}] text='{text[:60]}' name='{name[:60]}'")
                    except Exception:
                        continue

                # 搜索所有 ComboBox
                print("\n  [ComboBox 控件搜索]:")
                combo_count = 0
                for ctrl in settings_dialog.descendants():
                    try:
                        if ctrl.element_info.control_type == "ComboBox":
                            combo_count += 1
                            text = ctrl.window_text() or ""
                            name = ctrl.element_info.name or ""
                            try:
                                rect = ctrl.rectangle()
                                rect_str = f"({rect.left},{rect.top},{rect.right},{rect.bottom})"
                            except Exception:
                                rect_str = "?"
                            # 搜索附近文本标签
                            try:
                                parent = ctrl.parent()
                                nearby_texts = []
                                if parent:
                                    for sib in parent.descendants():
                                        try:
                                            st = sib.window_text() or ""
                                            if st.strip() and st != text:
                                                nearby_texts.append(st.strip()[:50])
                                        except Exception:
                                            pass
                                nearby = " | ".join(nearby_texts[:5])
                            except Exception:
                                nearby = ""
                            print(f"  [ComboBox #{combo_count}] text='{text[:40]}' name='{name[:40]}' "
                                  f"rect={rect_str}")
                            print(f"    附近文本: {nearby[:200]}")
                    except Exception:
                        continue
                if combo_count == 0:
                    print("  (未找到 ComboBox 控件)")

                # 搜索包含"确认"的控件
                print("\n  [包含'确认'的控件]:")
                for ctrl in settings_dialog.descendants():
                    try:
                        text = ctrl.window_text() or ""
                        name = ctrl.element_info.name or ""
                        ctype = ctrl.element_info.control_type
                        combined = f"{text} {name}"
                        if "确认" in combined:
                            print(f"  [{ctype}] text='{text[:80]}' name='{name[:80]}'")
                    except Exception:
                        continue

                # 搜索"确定"/"关闭"/"取消"按钮
                print("\n  [对话框按钮]:")
                for ctrl in settings_dialog.descendants():
                    try:
                        text = ctrl.window_text() or ""
                        name = ctrl.element_info.name or ""
                        ctype = ctrl.element_info.control_type
                        combined = f"{text} {name}"
                        if any(kw in combined for kw in ["确定", "取消", "关闭", "应用"]):
                            try:
                                rect = ctrl.rectangle()
                                rect_str = f"({rect.left},{rect.top},{rect.right},{rect.bottom})"
                            except Exception:
                                rect_str = "?"
                            print(f"  [{ctype}] cid={ctrl.control_id()} text='{text[:60]}' name='{name[:60]}' rect={rect_str}")
                    except Exception:
                        continue

                # 列出所有 Text 控件（可能包含标签文本）
                print("\n  [所有 Text 控件（前30个）]:")
                text_count = 0
                for ctrl in settings_dialog.descendants():
                    try:
                        if ctrl.element_info.control_type == "Text":
                            text = ctrl.window_text() or ""
                            name = ctrl.element_info.name or ""
                            if text.strip() or name.strip():
                                text_count += 1
                                if text_count <= 30:
                                    print(f"  [Text] cid={ctrl.control_id()} text='{text[:80]}' name='{name[:80]}'")
                    except Exception:
                        continue
                print(f"  (共 {text_count} 个 Text 控件)")

            else:
                print("\n  [FAIL] 未找到系统设置弹窗！")
                print("  可能原因：")
                print("  1. 点击'系统设置'没有触发弹窗打开")
                print("  2. 弹窗的 title/name 不包含'系统设置'或'设置'")
                print("  3. 弹窗作为主窗口的子控件而非独立窗口")
                print("\n  尝试搜索主窗口中是否出现了'快速交易'相关内容...")
                dialogs = Desktop(backend="uia").windows(title=TRADING_WINDOW_TITLE)
                if dialogs:
                    main_win = dialogs[0]
                    for ctrl in main_win.descendants():
                        try:
                            text = ctrl.window_text() or ""
                            name = ctrl.element_info.name or ""
                            combined = f"{text} {name}"
                            if "快速交易" in combined or "系统设置" in combined:
                                ctype = ctrl.element_info.control_type
                                print(f"  找到: [{ctype}] text='{text[:60]}' name='{name[:60]}'")
                        except Exception:
                            continue

    # 关闭可能打开的菜单
    print("\n\n清理：按 ESC 关闭可能打开的菜单...")
    try:
        window.type_keys("{ESC}")
    except Exception:
        pass
    time.sleep(0.2)
    try:
        window.type_keys("{ESC}")
    except Exception:
        pass

    print("\n" + "=" * 80)
    print("诊断完成！请将以上输出发给我，我来分析失败原因。")
    print("=" * 80)


if __name__ == "__main__":
    main()
