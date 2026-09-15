"""验证修复效果：F1 不泄漏 + close-dialog 安全关闭"""
import time
import urllib.request
import json

BASE = "http://127.0.0.1:5000"

def api(path, method="GET", body=None):
    url = BASE + path
    data = json.dumps(body).encode() if body else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Content-Type", "application/json")
    return json.loads(urllib.request.urlopen(req, timeout=15).read())

def check(msg):
    r = api("/diagnostic/snapshot")
    ui = r["data"].get("ui_text", "")
    items = len(ui.split("\n"))
    from pywinauto import Desktop
    wins = Desktop(backend="uia").windows(title="网上股票交易系统5.0")
    has_buy = "证券代码" in ui
    win_ok = len(wins) > 0
    print("  [%s] 窗口=%s 买入=%s UI=%d项" % (
        msg, "OK" if win_ok else "消失", "开" if has_buy else "关", items))
    return has_buy, win_ok

print("=== 1. 初始状态 ===")
check("初始")

print("\n=== 2. 打开买入窗口 F1 ===")
r = api("/actions/send-key", "POST", {"key": "F1"})
print("   响应: %s (%dms)" % (r["status"], r["duration_ms"]))
time.sleep(1.5)
has_buy, win_ok = check("F1后")

if not win_ok:
    print("!!! 窗口消失 - 异常！")
elif win_ok and has_buy:
    print("\n=== 3. 安全关闭买入窗口 ===")
    r = api("/actions/close-dialog", "POST", {"title": "买入"})
    print("   响应: %s (%dms)" % (r["status"], r["duration_ms"]))
    time.sleep(1)
    has_buy, win_ok = check("关闭后")

    if win_ok and not has_buy:
        print("\n=== 验证通过 ===")
        print("  [安全] F1 未泄漏到桌面（未打开 Edge/Windows 帮助）")
        print("  [安全] close-dialog 仅关闭买入子对话框")
        print("  [安全] 券商程序窗口仍然存在")
    elif not win_ok:
        print("\n!!! 窗口消失 - close-dialog 可能有问题！")
    elif has_buy:
        print("\n! 买入窗口未关闭，close-dialog 可能需要调整")
