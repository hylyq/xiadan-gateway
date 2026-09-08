"""双方法诊断：横幅出现期间，屏幕抓取 vs PrintWindow 谁能看到黄色横幅

背景: occluded 测试中 PrintWindow 版截获超时——怀疑横幅是独立分层
子窗口，主窗口 WM_PRINT 渲染不包含它。

方法: 下单后 6 秒内，每 100ms 同时探测两种方式的黄色像素数:
  A. ImageGrab 抓屏幕条带（要求区域在屏幕内且未被遮挡）
  B. PrintWindow 渲染主窗口后裁条带（不受遮挡/出屏影响，但可能
     不包含分层子窗口覆盖层）
命中时保存两种条带图，最后输出时间线对比。
"""
import sys
import os
import threading
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import numpy as np
import win32gui
from PIL import Image, ImageGrab

from src.core.trader import Trader
from src.models.config import AppConfig
from src.services.trading_service import TradingService
from src.services.window_service import WindowService

OUT = os.path.join("logs", "screenshots", "dual_probe")
PW_RENDERFULLCONTENT = 0x2


def render_window(hwnd, w, h):
    import win32ui
    import ctypes
    hwnd_dc = win32gui.GetWindowDC(hwnd)
    mfc_dc = win32ui.CreateDCFromHandle(hwnd_dc)
    save_dc = mfc_dc.CreateCompatibleDC()
    bmp = win32ui.CreateBitmap()
    bmp.CreateCompatibleBitmap(mfc_dc, w, h)
    save_dc.SelectObject(bmp)
    ctypes.windll.user32.PrintWindow(hwnd, save_dc.GetSafeHdc(),
                                     PW_RENDERFULLCONTENT)
    buf = bmp.GetBitmapBits(True)
    img = Image.frombuffer("RGB", (w, h), buf, "raw", "BGRX", 0, 1)
    win32gui.DeleteObject(bmp.GetHandle())
    save_dc.DeleteDC()
    mfc_dc.DeleteDC()
    win32gui.ReleaseDC(hwnd, hwnd_dc)
    return img


def yellow_count(arr):
    m = (arr[:, :, 0] > 200) & (arr[:, :, 1] > 180) & (arr[:, :, 2] < 120)
    return int(m.sum())


def strip_box(window):
    l, t, r, b = win32gui.GetWindowRect(window.handle)
    w, h = r - l, b - t
    return (l + int(w * 0.55), b - 34, r - 4, b - 2), (int(w * 0.55), h - 34,
                                                       w - 4, h - 2)


def probe_worker(ws, results):
    window = ws.get_trading_window()
    box, rel = strip_box(window)
    hwnd = window.handle
    t0 = time.perf_counter()
    seen_screen = seen_pw = False
    while time.perf_counter() - t0 < 6.0:
        t = time.perf_counter() - t0
        # A: 屏幕抓取
        try:
            sc = np.asarray(ImageGrab.grab(bbox=box))
            yc = yellow_count(sc)
            if yc > 300:
                if not seen_screen:
                    seen_screen = True
                    Image.fromarray(sc).save(os.path.join(OUT, "screen_hit.png"))
                results.append(("screen", t, yc))
        except Exception as e:
            print(f"  screen 异常: {e}")
        # B: PrintWindow
        try:
            pw = np.asarray(render_full(hwnd).crop(rel))
            yc = yellow_count(pw)
            if yc > 300:
                if not seen_pw:
                    seen_pw = True
                    Image.fromarray(pw).save(os.path.join(OUT, "pw_hit.png"))
                results.append(("printwindow", t, yc))
        except Exception as e:
            print(f"  printwindow 异常: {e}")
        time.sleep(0.1)


_full_cache = {}


def render_full(hwnd):
    l, t, r, b = win32gui.GetWindowRect(hwnd)
    return render_window(hwnd, r - l, b - t)


def main():
    os.makedirs(OUT, exist_ok=True)
    for f in os.listdir(OUT):
        os.remove(os.path.join(OUT, f))
    cfg = AppConfig()
    cfg._config["order"] = {"capture_entrust_no": False}
    ws = WindowService()
    trader = Trader(ws)

    results = []
    th = threading.Thread(target=probe_worker, args=(ws, results), daemon=True)
    th.start()

    print("下单…（5.30，模拟盘不成交价）", flush=True)
    try:
        r = trader.place_order(code="601991", status="1", amount="100",
                               price="5.30", price_type="limit", confirm=False)
        print(f"  下单返回: confirmed={r.get('confirmed')}")
    except Exception as e:
        print(f"  [FAIL] {e}")
        return
    th.join(timeout=8)

    print("\n===== 时间线 =====")
    last = {}
    for method, t, yc in results:
        key = (method, round(t, 1))
        if key in last:
            continue
        last[key] = yc
        print(f"  T+{t:5.2f}s {method:12s} 黄色像素={yc}")
    a = [t for m, t, _ in results if m == "screen"]
    p = [t for m, t, _ in results if m == "printwindow"]
    print(f"\n屏幕抓取命中: {'%.2f-%.2fs' % (min(a), max(a)) if a else '从未命中'}")
    print(f"PrintWindow 命中: {'%.2f-%.2fs' % (min(p), max(p)) if p else '从未命中'}")

    print("\n===== 清理 =====")
    try:
        TradingService(ws).cancel_all_orders("A")
        print("已撤单清理")
    except Exception as e:
        print(f"撤单失败: {e}")


if __name__ == "__main__":
    main()
