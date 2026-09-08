"""探索第四轮：全屏连拍定位横幅真实位置（feat/entrust-no）

第三轮结论: 窗口右下角区域无横幅——要么横幅在窗口之外（屏幕右下角），
要么它消失得比 place_order 返回更快。

本轮: 后台线程在 place_order 开始前就启动全屏 ~8fps 连拍（持续 5.5s，
覆盖下单点击瞬间），仅保存与上帧有差异的帧。之后逐帧检查横幅位置。
"""
import sys
import os
import time
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

from PIL import Image, ImageChops, ImageGrab

from src.core.trader import Trader
from src.services.window_service import WindowService
from src.services.trading_service import TradingService

OUT_DIR = os.path.join("logs", "screenshots", "banner_probe_full")


def capture_worker(seconds=5.5, fps=8):
    os.makedirs(OUT_DIR, exist_ok=True)
    t0 = time.perf_counter()
    prev = None
    n = 0
    while time.perf_counter() - t0 < seconds:
        t = time.perf_counter() - t0
        try:
            img = ImageGrab.grab()
        except Exception:
            continue
        if prev is None or ImageChops.difference(img, prev).getbbox() is not None:
            img.save(os.path.join(OUT_DIR, f"full_{t:05.2f}.png"))
            n += 1
            prev = img
        time.sleep(1.0 / fps)
    return n


def main():
    ws = WindowService()
    trader = Trader(ws)
    if ws.get_trading_window() is None:
        print("[FAIL] 未找到交易窗口")
        return

    os.makedirs(OUT_DIR, exist_ok=True)
    for f in os.listdir(OUT_DIR):
        os.remove(os.path.join(OUT_DIR, f))

    # 后台连拍先启动，覆盖下单全过程
    t = threading.Thread(target=capture_worker, daemon=True)
    t.start()

    print("===== 测试单（买入 601991 @5.13 ×100）=====")
    try:
        result = trader.place_order(code="601991", status="1", amount="100",
                                    price="5.13", price_type="limit",
                                    confirm=False)
        print(f"  place_order: {result}")
    except Exception as e:
        print(f"  [FAIL] 下单失败: {e}")
        return
    t.join()
    frames = sorted(os.listdir(OUT_DIR))
    print(f"  保存 {len(frames)} 帧:")
    for f in frames:
        print(f"    {f}")

    print("\n===== 清理：撤销测试委托 =====")
    try:
        r = TradingService(ws).cancel_all_orders("A")
        print(f"  撤单结果: {r}")
    except Exception as e:
        print(f"  [WARN] 撤单失败（请手动检查）: {e}")


if __name__ == "__main__":
    main()
