"""探索第三轮：横幅视觉采样——右下角区域连拍（feat/entrust-no）

第二轮结论: 横幅文字不走 GetWindowText/UIA Name（自绘），文本级截获不可行。
本轮: 下单提交后立即对窗口右下角区域以 ~10fps 连拍 3 秒，保存与上一帧
有差异的帧到 logs/screenshots/banner_probe/，用于:
1. 确认横幅出现的精确位置（为固定截图框定坐标）
2. 观察横幅样式（底色/字色/字号），决定 OCR/模板匹配方案
3. 测量横幅实际存活时长
"""
import sys
import os
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import win32gui
from PIL import Image, ImageChops

from src.core.trader import Trader
from src.services.window_service import WindowService
from src.services.trading_service import TradingService

OUT_DIR = os.path.join("logs", "screenshots", "banner_probe")
BUDGET_SECONDS = 3.0
INTERVAL = 0.1


def grab_region_frames(window, out_dir):
    """对窗口右下 45%×35% 区域连拍，保存与上一帧不同的帧"""
    os.makedirs(out_dir, exist_ok=True)
    l, t, r, b = win32gui.GetWindowRect(window.handle)
    # 右下角区域（右 45%、下 35%）
    box = (l + int((r - l) * 0.55), t + int((b - t) * 0.65), r, b)
    saved = 0
    prev = None
    t0 = time.perf_counter()
    while time.perf_counter() - t0 < BUDGET_SECONDS:
        t = time.perf_counter() - t0
        img = ImageGrab_grab(box)
        if img is None:
            continue
        if prev is None or list(img.getdata()) != list(prev.getdata()):
            path = os.path.join(out_dir, f"frame_{t:05.2f}.png")
            img.save(path)
            saved += 1
            prev = img
        else:
            prev = img
        time.sleep(INTERVAL)
    return box, saved, t0


def ImageGrab_grab(box):
    try:
        from PIL import ImageGrab
        return ImageGrab.grab(bbox=box)
    except Exception as e:
        print(f"    [截屏异常] {e}")
        return None


def main():
    ws = WindowService()
    trader = Trader(ws)
    window = ws.get_trading_window()
    if window is None:
        print("[FAIL] 未找到交易窗口")
        return
    rect = win32gui.GetWindowRect(window.handle)
    print(f"[OK] 交易窗口 rect={rect}")

    # 清理旧帧
    os.makedirs(OUT_DIR, exist_ok=True)
    for f in os.listdir(OUT_DIR):
        os.remove(os.path.join(OUT_DIR, f))

    print("\n===== 测试单（买入 601991 @5.12 ×100）=====")
    try:
        result = trader.place_order(code="601991", status="1", amount="100",
                                    price="5.12", price_type="limit",
                                    confirm=False)
        print(f"  place_order: {result}")
    except Exception as e:
        print(f"  [FAIL] 下单失败: {e}")
        return

    window = ws.get_trading_window()
    print("  --- 右下角区域连拍（3s @10fps，仅保存变化帧）---")
    box, saved, t0 = grab_region_frames(window, OUT_DIR)
    print(f"  截图框: {box}，保存 {saved} 帧至 {OUT_DIR}")

    print("\n===== 清理：撤销测试委托 =====")
    try:
        r = TradingService(ws).cancel_all_orders("A")
        print(f"  撤单结果: {r}")
    except Exception as e:
        print(f"  [WARN] 撤单失败（请手动检查）: {e}")


if __name__ == "__main__":
    main()
