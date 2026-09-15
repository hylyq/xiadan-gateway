"""探索买入窗口结构，找到正确的关闭方式"""
import time
import urllib.request
import json

BASE = "http://127.0.0.1:5000"

def api(path, method="GET", body=None, timeout=15):
    url = BASE + path
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    return json.loads(urllib.request.urlopen(req, timeout=timeout).read())

# 1. 诊断当前状态
print("=== 1. 当前状态 ===")
r = api("/diagnostic/snapshot")
ui = r["data"].get("ui_text", "")
has_buy = "ZhengQuanDaiMa" in ui or "证券代码" in ui
print(f"买入窗口已开: {has_buy}")

# 2. 如果买入窗口没开，打开它
if not has_buy:
    print("\n=== 2. 打开买入窗口 ===")
    r = api("/actions/send-key", "POST", {"key": "F1"})
    print(f"F1: {r['status']} ({r['duration_ms']}ms)")
    time.sleep(2)
    r = api("/diagnostic/snapshot")
    ui = r["data"].get("ui_text", "")
    print(f"买入窗口已开: {'证券代码' in ui}")

# 3. 探索窗口控件结构
print("\n=== 3. 窗口控件探索 ===")
from pywinauto import Desktop, Application
desktop = Desktop(backend="uia")

# 3a. 找主窗口
dialogs = desktop.windows(title="网上股票交易系统5.0")
if not dialogs:
    print("未找到主窗口")
    exit()

main_w = dialogs[0]
print(f"主窗口 title=\"{main_w.window_text()}\"")

# 3b. 找所有子Pane/Window控件（包括买入子窗口）
print("\n--- 所有Pane/Window子控件 ---")
seen = set()
for ctrl in main_w.descendants():
    try:
        ctrl_type = ctrl.element_info.control_type
        text = ctrl.window_text() or ""
        cid = ctrl.control_id()
        rect = ctrl.rectangle() if hasattr(ctrl, 'rectangle') else None
        auto_id = ""
        try:
            auto_id = ctrl.element_info.automation_id or ""
        except:
            pass
        if ctrl_type in ("Pane", "Window", "TitleBar"):
            key = f"{ctrl_type}_{cid}"
            if key not in seen and (text or rect):
                seen.add(key)
                r_str = f" rect=({rect.left},{rect.top})-({rect.right},{rect.bottom})" if rect else ""
                print(f"  {ctrl_type:10s} cid={str(cid):5s} text=\"{text[:30]:30s}\" auto_id=\"{auto_id}\"{r_str}")
    except:
        pass

# 3c. 找所有Button控件（找关闭按钮）
print("\n--- 所有Button控件 ---")
seen_btn = set()
for ctrl in main_w.descendants():
    try:
        if ctrl.element_info.control_type == "Button":
            text = ctrl.window_text() or ""
            cid = ctrl.control_id()
            rect = ctrl.rectangle() if hasattr(ctrl, 'rectangle') else None
            key = f"{cid}_{text}"
            if key not in seen_btn:
                seen_btn.add(key)
                r_str = f" rect=({rect.left},{rect.top})-({rect.right},{rect.bottom})" if rect else ""
                print(f"  cid={str(cid):5s} text=\"{text:20s}\" {r_str}")
    except:
        pass

# 3d. 尝试找TitleBar的关闭按钮
print("\n--- 尝试找关闭按钮 ---")
try:
    titlebar = main_w.child_window(control_type="TitleBar")
    if titlebar.exists():
        print("  TitleBar存在")
        for btn in titlebar.descendants():
            try:
                text = btn.window_text() or ""
                cid = btn.control_id()
                print(f"    子控件 cid={cid} text=\"{text}\" type={btn.element_info.control_type}")
            except:
                pass
except Exception as e:
    print(f"  TitleBar探索失败: {e}")

# 3e. 直接找标题栏关闭按钮（系统按钮）
print("\n--- 系统关闭按钮 ---")
try:
    # 找标题栏上的关闭按钮
    close_btn = main_w.child_window(title="关闭", control_type="Button")
    if close_btn.exists():
        print(f"  找到关闭按钮: cid={close_btn.control_id()}")
        rect = close_btn.rectangle()
        print(f"  位置: ({rect.left},{rect.top})-({rect.right},{rect.bottom})")
except Exception as e:
    print(f"  未找到: {e}")

# 4. 总结
print("\n=== 4. 结论 ===")
print("建议关闭方式:")
print("  a) 使用 SendMessage WM_CLOSE 到买入子窗口")
print("  b) 找到关闭按钮并 click()")
print("  c) 使用 keybd_event ALT+F4 并确认只影响子窗口")
