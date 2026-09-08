"""横幅数字模板匹配：样本收集 + 真值全覆盖验证（feat/entrust-no）

流程：逐笔下单价递增的测试委托（5.16→5.21，互异价格用于真值匹配）→
提交后即时截获横幅条带（保存 PNG）→ BannerDigitOCR 模板读数 →
当日委托表按价格取真值逐位校验 → 数字覆盖齐 10 个即提前收工。

样本0（crop_full_04.79.png，真值 6246860043 已验证）一并复验。
"""
import sys
import os
import time
import threading

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import numpy as np
import win32gui
from PIL import Image, ImageGrab

from src.core.banner_ocr import BannerDigitOCR
from src.core.trader import Trader
from src.core.ocr import OcrService
from src.models.config import AppConfig
from src.services.position_service import PositionService
from src.services.trading_service import TradingService
from src.services.window_service import WindowService

SAMPLE_DIR = os.path.join("logs", "banner_samples")
PRICES = ["5.16", "5.17", "5.18", "5.19", "5.20", "5.21"]


def grab_band(window):
    """窗口右下角截条带，黄色掩码定位，返回 band PIL Image 或 None"""
    l, t, r, b = win32gui.GetWindowRect(window.handle)
    box = (l + int((r - l) * 0.55), b - 34, r - 4, b - 2)
    img = ImageGrab.grab(bbox=box)
    arr = np.asarray(img)
    m = (arr[:, :, 0] > 200) & (arr[:, :, 1] > 180) & (arr[:, :, 2] < 120)
    if int(m.sum()) < 300:
        return None
    rows = np.where(m.sum(axis=1) > 30)[0]
    cols = np.where(m.sum(axis=0) > 5)[0]
    return img.crop((int(cols.min()), int(rows.min()),
                     int(cols.max()) + 1, int(rows.max()) + 1))


def capture_sample_async(ws, bo, price, budget=6.0):
    """后台截获线程：下单前启动（横幅在点击后 ~1-1.5s 内出现并消失）"""
    result = {}

    def worker():
        window = ws.get_trading_window()
        t0 = time.perf_counter()
        while time.perf_counter() - t0 < budget:
            band = grab_band(window)
            if band is not None:
                digits, conf = bo.read_digits(np.asarray(band))
                if len(digits) >= 8:
                    path = os.path.join(SAMPLE_DIR, f"band_{price}.png")
                    band.save(path)
                    result.update(digits=digits, conf=conf, path=path)
                    return
            time.sleep(0.08)
        result.update(digits=None)

    th = threading.Thread(target=worker, daemon=True)
    th.start()
    return th, result


def ground_truth(ps, price):
    """当日委托表按价格唯一匹配委托号"""
    for o in ps.get_today_orders():
        try:
            if abs(float(o.get("委托价格", 0)) - float(price)) < 1e-6 \
                    and o.get("证券代码") == "601991":
                return o.get("委托编号")
        except (TypeError, ValueError):
            continue
    return None


def main():
    cfg = AppConfig()
    cfg._config["query"] = {"copy_method": "message"}  # 内存切换：真值查询提速
    ws = WindowService()
    ocr = OcrService.get_instance()
    ocr.configure(ddddocr_enabled=False)
    ocr.warmup()
    ps = PositionService(ws, ocr)
    trader = Trader(ws)
    bo = BannerDigitOCR()

    os.makedirs(SAMPLE_DIR, exist_ok=True)
    for f in os.listdir(SAMPLE_DIR):
        os.remove(os.path.join(SAMPLE_DIR, f))

    verified = set()
    samples = []  # (price, read, truth)

    # 样本0：已有真值截图复验
    p0 = "logs/screenshots/banner_probe/crop_full_04.79.png"
    if os.path.exists(p0):
        band = Image.open(p0).convert("RGB")
        arr = np.array(band)
        m = (arr[:, :, 0] > 200) & (arr[:, :, 1] > 180) & (arr[:, :, 2] < 120)
        rows = np.where(m.sum(axis=1) > 30)[0]
        cols = np.where(m.sum(axis=0) > 5)[0]
        digits, conf = bo.read_digits(
            np.array(band.crop((cols.min(), rows.min(),
                                cols.max() + 1, rows.max() + 1))))
        truth = "6246860043"
        ok = digits == truth
        print(f"[样本0] 模板读数={digits} 真值={truth} "
              f"{'一致' if ok else '不一致!'} conf={conf:.2f}")
        if ok:
            verified |= set(digits)
            samples.append(("sample0", digits, truth))

    for price in PRICES:
        missing = sorted(set("0123456789") - verified)
        if not missing:
            print(f"\n[完成] 10 个数字已全覆盖，停止下单")
            break
        print(f"\n===== 第 {price} 笔测试单（缺少数字: {' '.join(missing)}）=====")
        print(f"  启动后台截获线程后下单…")
        th, result = capture_sample_async(ws, bo, price)
        try:
            r = trader.place_order(code="601991", status="1", amount="100",
                                   price=price, price_type="limit",
                                   confirm=False)
            print(f"  下单返回: confirmed={r.get('confirmed')}")
        except Exception as e:
            print(f"  [FAIL] 下单失败: {e}")
            th.join(timeout=7)
            continue
        th.join(timeout=8)
        digits, conf, path = result.get("digits"), result.get("conf", 0.0), result.get("path")
        if digits is None:
            print("  [未捕获] 横幅超时")
            continue
        truth = ground_truth(ps, price)
        if truth is None:
            print(f"  [WARN] 当日委托表未找到价格 {price} 的真值")
            continue
        ok = digits == truth
        print(f"  模板读数={digits} 真值={truth} "
              f"{'一致' if ok else '不一致!'} conf={conf:.2f} 已存 {path}")
        if ok:
            verified |= set(digits)
            samples.append((price, digits, truth))
        else:
            print("  [诊断] 读数与真值不符——保留样本供排查")

    print("\n===== 覆盖与结论 =====")
    print(f"已验证数字: {''.join(sorted(verified))}")
    missing = sorted(set("0123456789") - verified)
    print(f"未覆盖: {' '.join(missing) if missing else '无，全覆盖完成'}")
    ok_n = sum(1 for _, a, b in samples if a == b)
    print(f"样本一致率: {ok_n}/{len(samples)}")

    print("\n===== 清理：撤销测试委托 =====")
    try:
        r = TradingService(ws).cancel_all_orders("A")
        print(f"  撤单结果: {r}")
    except Exception as e:
        print(f"  [WARN] 撤单失败（请手动检查）: {e}")


if __name__ == "__main__":
    main()
