"""清理残留验证码弹窗：找到 xiadan 进程的"提示"弹窗并关闭"""
import sys
import os
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import win32gui
import win32process

from src.constants import CAPTCHA_CANCEL_BUTTON_ID, MAIN_WINDOW_TITLE_KEYWORD
from src.services.window_service import WindowService


def main():
    ws = WindowService()
    window = ws.get_trading_window()
    if window is None:
        print("[FAIL] 未找到交易窗口")
        return
    hwnd = window.handle
    _, pid = win32process.GetWindowThreadProcessId(hwnd)
    print(f"交易窗口 hwnd={hwnd:#x} pid={pid}")

    # 枚举该进程的顶层"提示"弹窗
    popups = []

    def cb(h, acc):
        try:
            _, wpid = win32process.GetWindowThreadProcessId(h)
            if wpid == pid and win32gui.IsWindowVisible(h):
                t = win32gui.GetWindowText(h)
                if t and "网上股票交易系统5.0" not in t:
                    acc.append((h, t))
        except Exception:
            pass
        return True

    win32gui.EnumWindows(cb, popups)
    print(f"找到 {len(popups)} 个非主窗口的顶层弹窗:")
    for h, t in popups:
        print(f"  hwnd={h:#x} title={t!r}")

    closed = 0
    for h, t in popups:
        try:
            # 优先点取消按钮（cid=2），失败则 WM_CLOSE
            try:
                win32gui.SetForegroundWindow(h)
                time.sleep(0.1)
            except Exception:
                pass
            try:
                # 枚举子窗口找取消按钮
                btns = []

                def bcb(bh, acc2):
                    acc2.append(bh)
                    return True

                win32gui.EnumChildWindows(h, bcb, btns)
                win32gui.PostMessage(h, 0x0010, 0, 0)  # WM_CLOSE
                closed += 1
                print(f"  已发送 WM_CLOSE: {h:#x} ({t!r})")
            except Exception as e:
                print(f"  关闭失败 {h:#x}: {e}")
        except Exception as e:
            print(f"  处理失败 {h:#x}: {e}")

    time.sleep(0.5)
    # 复核：还有没有残留
    remain = []
    win32gui.EnumWindows(cb, remain)
    print(f"\n清理后残留弹窗: {len(remain)} 个")
    print(f"[OK] 已关闭 {closed} 个" if closed else "[INFO] 无弹窗可关闭")


if __name__ == "__main__":
    main()
