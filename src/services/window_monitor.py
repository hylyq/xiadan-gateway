"""窗口监控服务

后台线程定期检查 xiadan.exe 是否最小化，自动恢复到前台
"""
import threading
import time
from typing import Optional

import psutil
import win32con
import win32gui
import win32process

from src.utils.logger import Logger


class WindowMonitor:
    """窗口监控器

    监控目标窗口是否最小化，如果最小化则自动恢复到前台。
    防止交易窗口被最小化导致快捷键失效。
    """

    def __init__(self, check_interval: float = 5):
        self.logger = Logger.get_instance()
        self.check_interval = check_interval
        self._running = False
        self._monitor_thread: Optional[threading.Thread] = None
        self._target_app_path: Optional[str] = None
        self._target_hwnd: Optional[int] = None
        self._lock = threading.Lock()

    def start(self, app_path: str) -> bool:
        """启动监控"""
        with self._lock:
            if self._running:
                self.logger.warning("窗口监控已在运行中")
                return False

            self._target_app_path = app_path
            self._running = True

            self._monitor_thread = threading.Thread(
                target=self._monitor_loop,
                daemon=True,
                name="Window-Monitor"
            )
            self._monitor_thread.start()
            self.logger.info(f"窗口监控已启动，目标程序: {app_path}")
            return True

    def stop(self) -> None:
        """停止监控"""
        with self._lock:
            if not self._running:
                return
            self._running = False
            self._target_hwnd = None
            self.logger.info("窗口监控已停止")

    def is_running(self) -> bool:
        return self._running

    def _find_target_window(self) -> Optional[int]:
        """根据 exe 路径查找窗口句柄"""
        hwnd_found = None
        target_path = self._target_app_path.lower() if self._target_app_path else None

        def callback(hwnd, extra):
            nonlocal hwnd_found
            if win32gui.IsWindowVisible(hwnd):
                try:
                    _, pid = win32process.GetWindowThreadProcessId(hwnd)
                    proc = psutil.Process(pid)
                    if proc.exe().lower() == target_path:
                        hwnd_found = hwnd
                        return False
                except (psutil.NoSuchProcess, psutil.AccessDenied):
                    pass
            return True

        if target_path:
            win32gui.EnumWindows(callback, None)
        return hwnd_found

    def _restore_window(self, hwnd: int) -> bool:
        """恢复最小化的窗口"""
        try:
            if not win32gui.IsWindow(hwnd):
                self.logger.warning("窗口句柄无效，尝试重新查找")
                return False

            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
            time.sleep(0.1)

            try:
                win32gui.SetForegroundWindow(hwnd)
            except Exception:
                self._force_foreground(hwnd)

            self.logger.info("已自动恢复最小化窗口")
            return True
        except Exception as e:
            self.logger.error(f"恢复窗口失败: {str(e)}")
            return False

    def _force_foreground(self, hwnd: int) -> None:
        """强制将窗口置于前台（备用方案）"""
        try:
            win32gui.ShowWindow(hwnd, win32con.SW_MINIMIZE)
            time.sleep(0.05)
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        except Exception as e:
            self.logger.error(f"强制前台失败: {str(e)}")

    def _monitor_loop(self) -> None:
        self.logger.info("窗口监控线程已启动")
        consecutive_failures = 0

        while self._running:
            try:
                hwnd = self._find_target_window()
                if hwnd is None:
                    consecutive_failures += 1
                    if consecutive_failures >= 5:
                        self.logger.warning("连续5次未找到目标窗口，请检查 xiadan.exe 是否已启动")
                        consecutive_failures = 0
                else:
                    consecutive_failures = 0
                    self._target_hwnd = hwnd
                    if win32gui.IsIconic(hwnd):
                        self.logger.info("检测到目标窗口已最小化，正在恢复...")
                        if not self._restore_window(hwnd):
                            self._target_hwnd = None
            except Exception as e:
                self.logger.error(f"监控循环异常: {str(e)}")

            time.sleep(self.check_interval)

        self.logger.info("窗口监控线程已退出")

    def get_status(self) -> dict:
        return {
            "running": self._running,
            "target_app": self._target_app_path,
            "target_hwnd": self._target_hwnd,
            "check_interval": self.check_interval
        }
