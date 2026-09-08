"""探索：快速交易模式下单后右下角"委托已成功提交"横幅的截获（feat/entrust-no）

用户观察: 快速交易模式下点击下单按钮后，软件右下角快速闪过一条文本横幅
（约 1-2 秒），内容形如「您的买入委托已成功提交，合同编号：90809889238」。

本脚本在模拟盘真实下单（小额、低价、不成交），提交后立即用三种方式捕获:
1. win32 EnumChildWindows 枚举主窗口子 HWND + GetWindowText（毫秒级/轮）
2. win32 EnumWindows 找 xiadan 进程的顶层窗口 + GetWindowText（毫秒级/轮）
3. UIA 全树 window.descendants() 找含"合同编号"的控件（~0.8s/轮）

命中后记录: 命中方式、耗时、控件 class/control_id/类型、父链路径——
为后续"预测路径"式稳定截获做数据准备。测试完成后撤掉残留委托。
"""
import sys
import os
import re
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import win32gui
import win32process

from src.constants import MAIN_WINDOW_TITLE_KEYWORD
from src.core.trader import Trader
from src.services.window_service import WindowService
from src.services.trading_service import TradingService
from src.utils.uia import safe_control_type, safe_text

BANNER_KEYWORD = "合同编号"
ENTRUST_RE = re.compile(r"合同编号[：:]\s*(\d+)")


def enum_child_texts(root_hwnd):
    """win32 枚举主窗口全部子 HWND 文本（含孙辈，毫秒级）"""
    hits = []

    def cb(h, _):
        try:
            t = win32gui.GetWindowText(h)
            if BANNER_KEYWORD in t:
                hits.append((h, win32gui.GetClassName(h),
                             win32gui.GetDlgCtrlID(h), t))
        except Exception:
            pass
        return True

    try:
        win32gui.EnumChildWindows(root_hwnd, cb, None)
    except Exception:
        pass
    return hits


def enum_toplevel_texts(pid):
    """win32 枚举 xiadan 进程的顶层窗口文本（横幅可能是独立窗）"""
    hits = []

    def cb(h, _):
        try:
            _, wpid = win32process.GetWindowThreadProcessId(h)
            if wpid == pid:
                t = win32gui.GetWindowText(h)
                if BANNER_KEYWORD in t:
                    hits.append((h, win32gui.GetClassName(h),
                                 win32gui.GetDlgCtrlID(h), t))
        except Exception:
            pass
        return True

    try:
        win32gui.EnumWindows(cb, None)
    except Exception:
        pass
    return hits


def uia_full_scan(window):
    """UIA 全树扫描含关键词的控件，命中则返回 (元素, 父链描述)"""
    try:
        for el in window.descendants():
            t = safe_text(el)
            if BANNER_KEYWORD in t:
                hops = []
                cur = el
                while cur is not None:
                    try:
                        hops.append(f"{safe_control_type(cur)}/{cur.class_name()}"
                                    f"#cid={cur.control_id()}")
                    except Exception:
                        hops.append("?")
                    try:
                        if cur.handle == window.handle:
                            break
                    except Exception:
                        break
                    cur = cur.parent()
                hops.reverse()
                return el, hops
    except Exception as e:
        print(f"    [UIA 扫描异常] {e}")
    return None, None


def capture_banner(ws, window, pid, budget=3.0):
    """提交后立即轮询截获横幅，返回命中信息 dict 或 None"""
    t0 = time.perf_counter()
    uia_scanned = False
    while time.perf_counter() - t0 < budget:
        # 1. win32 子窗口枚举（最快，每轮都试）
        hits = enum_child_texts(window.handle)
        if hits:
            h, cls, cid, text = hits[0]
            return {"method": "win32 子窗口枚举", "elapsed": time.perf_counter() - t0,
                    "hwnd": h, "class": cls, "cid": cid, "text": text, "path": None}
        # 2. 顶层窗口枚举（快，每轮都试）
        hits = enum_toplevel_texts(pid)
        if hits:
            h, cls, cid, text = hits[0]
            return {"method": "win32 顶层窗口", "elapsed": time.perf_counter() - t0,
                    "hwnd": h, "class": cls, "cid": cid, "text": text, "path": None}
        # 3. UIA 全树（慢，每轮最多一次）
        if not uia_scanned:
            uia_scanned = True
            el, path = uia_full_scan(window)
            if el is not None:
                try:
                    cls, cid = el.class_name(), el.control_id()
                except Exception:
                    cls, cid = "?", "?"
                return {"method": "UIA 全树扫描", "elapsed": time.perf_counter() - t0,
                        "hwnd": el.handle, "class": cls, "cid": cid,
                        "text": safe_text(el), "path": " → ".join(path)}
        time.sleep(0.03)
    return None


def main():
    ws = WindowService()
    trader = Trader(ws)
    window = ws.get_trading_window()
    if window is None:
        print("[FAIL] 未找到交易窗口")
        return
    _, pid = win32process.GetWindowThreadProcessId(window.handle)
    print(f"[OK] 交易窗口 hwnd={window.handle:#x} pid={pid}")

    findings = []
    for i in range(2):
        print(f"\n===== 第 {i + 1} 笔测试单（模拟盘 买入 601991 @5.10 ×100，不会成交）=====")
        t0 = time.perf_counter()
        try:
            result = trader.place_order(code="601991", status="1", amount="100",
                                        price="5.10", price_type="limit",
                                        confirm=False)
        except Exception as e:
            print(f"  [FAIL] 下单失败: {e}")
            continue
        submit_return = time.perf_counter() - t0
        print(f"  place_order 返回: {result}（耗时 {submit_return:.2f}s）")

        window = ws.get_trading_window()
        info = capture_banner(ws, window, pid)
        if info:
            print(f"  [命中] 方式={info['method']} 距 place_order 返回 {info['elapsed']:.2f}s")
            print(f"         class={info['class']} cid={info['cid']} hwnd={info['hwnd']:#x}")
            print(f"         文本: {info['text']}")
            m = ENTRUST_RE.search(info["text"])
            if m:
                print(f"         >>> 委托号: {m.group(1)}")
            if info["path"]:
                print(f"         父链: {info['path']}")
            findings.append(info)
        else:
            print("  [未命中] 横幅未捕获（可能已消失或不可访问）")
        if i == 0:
            time.sleep(4)  # 等横幅消失，避免误抓上一笔的

    print("\n===== 清理：撤销测试委托 =====")
    try:
        ts = TradingService(ws)
        r = ts.cancel_all_orders("A")
        print(f"  撤单结果: {r}")
    except Exception as e:
        print(f"  [WARN] 撤单失败（请手动检查）: {e}")

    print("\n===== 探索结论 =====")
    if findings:
        print(f"命中 {len(findings)}/2：")
        for f in findings:
            print(f"  - {f['method']} @{f['elapsed']:.2f}s class={f['class']} cid={f['cid']}")
    else:
        print("两种 win32 枚举与 UIA 全树均未命中——横幅可能是自绘无文本控件，"
              "需换 OCR/截图区域方案")


if __name__ == "__main__":
    main()
