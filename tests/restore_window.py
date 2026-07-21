"""恢复 xiadan.exe 窗口（从系统托盘/最小化状态恢复）"""
import win32gui
import win32con
import psutil
import time

# 1. 找到 xiadan.exe 的所有窗口
print("=== 查找 xiadan.exe 窗口 ===")
windows = []
def enum_callback(hwnd, param):
    if win32gui.IsWindowVisible(hwnd):
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        try:
            proc = psutil.Process(pid)
            if proc.name().lower() == "xiadan.exe":
                title = win32gui.GetWindowText(hwnd)
                rect = win32gui.GetWindowRect(hwnd)
                is_iconic = win32gui.IsIconic(hwnd)
                is_visible = win32gui.IsWindowVisible(hwnd)
                windows.append({
                    "hwnd": hwnd,
                    "title": title,
                    "rect": rect,
                    "is_iconic": is_iconic,
                    "is_visible": is_visible,
                })
                print(f"  hwnd={hwnd} title=\"{title}\" iconic={is_iconic} visible={is_visible} rect={rect}")
        except:
            pass
    return True

import win32process
win32gui.EnumWindows(enum_callback, None)

if not windows:
    print("未找到 xiadan.exe 的可见窗口！（可能完全隐藏到托盘）")
    print("尝试找所有 xiadan.exe 窗口（包括不可见）...")
    def enum_all(hwnd, param):
        _, pid = win32process.GetWindowThreadProcessId(hwnd)
        try:
            proc = psutil.Process(pid)
            if proc.name().lower() == "xiadan.exe":
                title = win32gui.GetWindowText(hw