"""横幅区域像素探测（诊断工具，跑在交互会话）

复刻 entrust_capture._grab_banner_digits 的裁剪几何（窗口相对
55% × 底部 32px），左/上外扩上下文，以 ~80ms 周期抓帧：
- 每帧写元数据 JSONL（窗口矩形、裁剪框、屏幕/虚拟屏尺寸、DPI 感知、
  黄色掩码计数）
- 黄色掩码命中（≥300px，与截获器同阈值）时保存整框 PNG

用于离线分析 OCR 黏连污染源（2026-10-09: RDP→console 重挂后横幅
捕获黏连前置「8119」）。只读屏幕像素，不碰窗口焦点、不碰 UIA。
"""
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import numpy as np
import win32api
import win32gui
from PIL import ImageGrab

from src.constants import TRADING_WINDOW_TITLE

DURATION_S = float(os.environ.get("PROBE_SECONDS", "90"))
OUT_DIR = os.environ.get("PROBE_OUT", "logs/probe_banner")

# 黄色掩码阈值（与 entrust_capture 一致：黄底红字）
_YELLOW_R, _YELLOW_G, _YELLOW_B = 200, 180, 120


def metrics():
    import ctypes
    sm = win32api.GetSystemMetrics
    return {
        "screen": [sm(0), sm(1)],
        "virtual": [sm(76), sm(77), sm(78), sm(79)],
        "dpi_aware": bool(ctypes.windll.user32.IsProcessDPIAware()),
    }


def main():
    os.makedirs(OUT_DIR, exist_ok=True)
    hwnd = win32gui.FindWindow(None, TRADING_WINDOW_TITLE)
    if not hwnd:
        print(f"window not found: {TRADING_WINDOW_TITLE}", flush=True)
        return 1
    meta_path = os.path.join(OUT_DIR, "frames.jsonl")
    saved = 0
    n = 0
    t0 = time.perf_counter()
    with open(meta_path, "w", encoding="utf-8") as w:
        while time.perf_counter() - t0 < DURATION_S:
            n += 1
            try:
                l, t, r, b = win32gui.GetWindowRect(hwnd)
                # 裁剪几何与 entrust_capture 一致，左扩 260 / 上扩 24 作上下文
                x0 = l + int((r - l) * 0.55)
                box = (max(l, x0 - 260), max(t, b - 34 - 24), r - 4, b - 2)
                img = ImageGrab.grab(bbox=box)
                arr = np.asarray(img)
                mask = ((arr[:, :, 0] > _YELLOW_R)
                        & (arr[:, :, 1] > _YELLOW_G)
                        & (arr[:, :, 2] < _YELLOW_B))
                ys = int(mask.sum())
                rec = {"i": n, "t": round(time.perf_counter() - t0, 3),
                       "rect": [l, t, r, b], "box": list(box),
                       "yellow": ys, **metrics()}
                if ys >= 300 and saved < 80:
                    p = os.path.join(
                        OUT_DIR, f"frame_{n:04d}_t{int((time.perf_counter()-t0)*1000):05d}.png")
                    img.save(p)
                    rec["png"] = os.path.basename(p)
                    saved += 1
                elif n == 1:
                    p = os.path.join(OUT_DIR, "baseline.png")
                    img.save(p)
                    rec["png"] = "baseline.png"
                w.write(json.dumps(rec, ensure_ascii=False) + "\n")
                w.flush()
            except Exception as e:
                w.write(json.dumps({"i": n, "err": str(e)}) + "\n")
                w.flush()
            time.sleep(0.08)
    print(f"probe done: {n} frames, {saved} yellow frames -> {OUT_DIR}",
          flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
