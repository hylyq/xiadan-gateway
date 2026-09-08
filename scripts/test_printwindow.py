"""验证 PrintWindow(PW_RENDERFULLCONTENT) 能否渲染 xiadan 窗口内容

背景: EntrustNoCapture 当前用 ImageGrab 抓屏幕坐标——要求横幅区域可见
且未被遮挡。若 PrintWindow 可行（窗口自我渲染到位图），被其他窗口
遮挡时也能截获，可消除"窗口必须在前台可见"的约束。

验证内容:
1. PrintWindow 渲染整窗 → 保存整窗位图
2. 裁右下角条带区域（与生产截获同一相对坐标）
3. 与 ImageGrab 同区域截图对比相似度（窗口可见时两者应基本一致）
"""
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import ctypes

import numpy as np
import win32gui
import win32ui
from PIL import Image

from src.services.window_service import WindowService

OUT = os.path.join("logs", "screenshots", "printwindow_probe")
PW_RENDERFULLCONTENT = 0x00000002


def printwindow_capture(hwnd, w, h) -> Image.Image:
    hwnd_dc = win32gui.GetWindowDC(hwnd)
    mfc_dc = win32ui.CreateDCFromHandle(hwnd_dc)
    save_dc = mfc_dc.CreateCompatibleDC()
    bmp = win32ui.CreateBitmap()
    bmp.CreateCompatibleBitmap(mfc_dc, w, h)
    save_dc.SelectObject(bmp)
    try:
        res = ctypes.windll.user32.PrintWindow(
            hwnd, save_dc.GetSafeHdc(), PW_RENDERFULLCONTENT)
        info = ctypes.windll.user32.GetWindowRect
        buf = bmp.GetBitmapBits(True)
        img = Image.frombuffer("RGB", (w, h), buf, "raw", "BGRX", 0, 1)
        return img, res
    finally:
        win32gui.DeleteObject(bmp.GetHandle())
        save_dc.DeleteDC()
        mfc_dc.DeleteDC()
        win32gui.ReleaseDC(hwnd, hwnd_dc)


def main():
    ws = WindowService()
    window = ws.get_trading_window()
    if window is None:
        print("[FAIL] 未找到交易窗口")
        return
    hwnd = window.handle
    l, t, r, b = win32gui.GetWindowRect(hwnd)
    w, h = r - l, b - t
    print(f"[OK] 窗口 rect=({l},{t},{r},{b}) size={w}x{h}")

    os.makedirs(OUT, exist_ok=True)
    img, res = printwindow_capture(hwnd, w, h)
    print(f"PrintWindow 返回: {res}（非 0 = 成功）")
    full_path = os.path.join(OUT, "printwindow_full.png")
    img.save(full_path)

    # 裁横幅条带（与生产 EntrustNoCapture 相同的相对坐标）
    strip = img.crop((int(w * 0.55), h - 34, w - 4, h - 2))
    strip_path = os.path.join(OUT, "printwindow_strip.png")
    strip.save(strip_path)

    # 屏幕抓取同区域对比
    screen = ImageGrab.grab(bbox=(l, t, r, b))
    screen_strip = screen.crop((int(w * 0.55), h - 34, w - 4, h - 2))
    a = np.asarray(strip.convert("L"), dtype=np.int16)
    s = np.asarray(screen_strip.convert("L"), dtype=np.int16)
    diff = np.abs(a - s)
    print(f"条带尺寸: {strip.size}，与屏幕截图平均像素差: {diff.mean():.1f}/255"
          f"（<10 视为一致）")
    print(f"整窗图: {full_path}")
    print(f"条带图: {strip_path}")


if __name__ == "__main__":
    from PIL import ImageGrab
    main()
