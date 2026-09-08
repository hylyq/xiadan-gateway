"""探索第二轮：横幅文本时间线 + 全量候选 dump（feat/entrust-no）

第一轮发现: 提交后 0.01s 内 win32 枚举即命中 hwnd=0x20a12 (Static cid=65535)，
但文本只有"合同编号"四字——是横幅组件之一还是常驻标签待确认；
且 win32 命中短路了 UIA 扫描。

本轮:
1. 提交后连续 4s、每 100ms 全量枚举含关键词（委托/合同编号/成功提交）的
   子窗口控件，记录 (t, hwnd, class, text, 屏幕坐标) 的变化时间线
2. 补跑 UIA 全树扫描交叉验证（找"委托已成功提交"/"合同编号"）
3. 横幅在右下角——坐标可确认哪个候选是真横幅
"""
import sys
import os
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import win32gui
import win32process

from src.core.trader import Trader
from src.services.window_service import WindowService
from src.services.trading_service import TradingService
from src.utils.uia import safe_control_type, safe_text

KEYWORDS = ("委托", "合同编号", "成功提交")


def enum_children_with_rect(root_hwnd):
    """枚举主窗口全部子 HWND: (hwnd, class, cid, text, rect)"""
    out = []

    def cb(h, _):
        try:
            t = win32gui.GetWindowText(h)
            if any(k in t for k in KEYWORDS):
                out.append((h, win32gui.GetClassName(h),
                            win32gui.GetDlgCtrlID(h), t,
                            win32gui.GetWindowRect(h)))
        except Exception:
            pass
        return True

    try:
        win32gui.EnumChildWindows(root_hwnd, cb, None)
    except Exception:
        pass
    return out


def uia_scan(window):
    """UIA 全树找含关键词的控件"""
    hits = []
    try:
        for el in window.descendants():
            t = safe_text(el)
            if any(k in t for k in KEYWORDS) and len(t) > len("合同编号"):
                hits.append((safe_control_type(el), el.class_name(),
                             el.control_id(), t[:80]))
    except Exception as e:
        print(f"    [UIA 异常] {e}")
    return hits


def timeline_scan(ws, window, seconds=4.0):
    """连续采样控件文本变化，打印时间线"""
    t0 = time.perf_counter()
    seen = {}  # hwnd -> text（记录变化）
    last_print = {}
    while time.perf_counter() - t0 < seconds:
        t = time.perf_counter() - t0
        for h, cls, cid, text, rect in enum_children_with_rect(window.handle):
            prev = seen.get(h)
            if prev != text:
                seen[h] = text
                # 同一 hwnd 短时间多次变化只打印状态切换
                if last_print.get(h) != text:
                    print(f"    T+{t:5.2f}s hwnd={h:#x} class={cls} cid={cid} "
                          f"rect={rect}")
                    print(f"           text={text!r}")
                    last_print[h] = text
        time.sleep(0.1)


def main():
    ws = WindowService()
    trader = Trader(ws)
    window = ws.get_trading_window()
    if window is None:
        print("[FAIL] 未找到交易窗口")
        return
    _, pid = win32process.GetWindowThreadProcessId(window.handle)
    print(f"[OK] 交易窗口 hwnd={window.handle:#x} pid={pid}")

    print("\n===== 测试单（买入 601991 @5.10 ×100）=====")
    try:
        result = trader.place_order(code="601991", status="1", amount="100",
                                    price="5.10", price_type="limit",
                                    confirm=False)
        print(f"  place_order: {result}")
    except Exception as e:
        print(f"  [FAIL] 下单失败: {e}")
        return

    window = ws.get_trading_window()
    print("  --- 横幅文本时间线（4s，每 100ms 采样）---")
    timeline_scan(ws, window, 4.0)

    print("  --- UIA 全树交叉验证（横幅若还在应能扫到）---")
    window = ws.get_trading_window()
    for ct, cls, cid, text in uia_scan(window):
        print(f"    {ct} class={cls} cid={cid} text={text!r}")

    print("\n===== 第二笔（复现时间线）=====")
    time.sleep(2)
    try:
        trader.place_order(code="601991", status="1", amount="100",
                           price="5.11", price_type="limit", confirm=False)
        print("  place_order: 已提交")
    except Exception as e:
        print(f"  [FAIL] 下单失败: {e}")
        return
    window = ws.get_trading_window()
    timeline_scan(ws, window, 4.0)

    print("\n===== 清理：撤销测试委托 =====")
    try:
        r = TradingService(ws).cancel_all_orders("A")
        print(f"  撤单结果: {r}")
    except Exception as e:
        print(f"  [WARN] 撤单失败（请手动检查）: {e}")


if __name__ == "__main__":
    main()
