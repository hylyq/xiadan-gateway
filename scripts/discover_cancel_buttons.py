"""发现：F1/F2/F3 三页的 全撤/撤买/撤卖/撤最后 按钮控件 ID

用户观察: 三个页面都有相同按键。本脚本逐页 dump 目标按钮的
control_id/class/text/rect，确认跨页一致性与"撤最后"的 cid。
"""
import sys
import os
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from src.services.window_service import WindowService

TARGET_TEXTS = ("全撤", "撤买", "撤卖", "撤最后")


def dump_page_buttons(ws, window, page_label):
    window.set_focus()
    time.sleep(0.2)
    print(f"\n===== {page_label} =====")
    found = {}
    for el in window.descendants():
        try:
            if el.element_info.control_type != "Button":
                continue
            cid = el.control_id()
            text = (el.window_text() or "").strip()
            if cid >= 30000 or any(k in text for k in TARGET_TEXTS):
                rect = el.rectangle()
                found[text] = cid
                print(f"  cid={cid:6d} class={el.class_name():18s} "
                      f"text={text!r:14} rect=({rect.left},{rect.top},"
                      f"{rect.right},{rect.bottom})")
        except Exception:
            continue
    if not found:
        print("  （未发现目标按钮）")
    return found


def main():
    ws = WindowService()
    from src.models.config import AppConfig
    ws.activate_window(AppConfig().get_trading_app_paths())
    window = ws.get_trading_window()
    if window is None:
        print("[FAIL] 未找到交易窗口")
        return
    ws.activate_window(AppConfig().get_trading_app_paths())

    results = {}
    for key, label in (("F1", "F1 买入页"), ("F2", "F2 卖出页"), ("F3", "F3 撤单页")):
        ws.send_key(key, background=True)
        time.sleep(0.4)
        window = ws.get_trading_window()
        results[key] = dump_page_buttons(ws, window, label)

    print("\n===== 跨页一致性 =====")
    for text in TARGET_TEXTS:
        ids = {results[k].get(text) for k in results if results[k].get(text) is not None}
        pages = [k for k in results if text in results[k]]
        print(f"  {text}: cid={ids if ids else '未发现'} 出现于 {pages}")


if __name__ == "__main__":
    main()
