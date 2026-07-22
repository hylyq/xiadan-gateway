"""窗口监控服务

后台线程定期检查 xiadan.exe 是否最小化，自动恢复到前台
支持多个可能的 xiadan.exe 路径（免费版/远航版等）
"""
import threading
import time
from typing import Optional, Union, List

import psutil
import win32con
import win32gui
import win32process

from src.utils.logger import Logger
from src.constants import TRADING_WINDOW_TITLE


class WindowMonitor:
    """窗口监控器

    监控目标窗口是否最小化，如果最小化则自动恢复到前台。
    防止交易窗口被最小化导致快捷键失效。
    """

    def __init__(self, check_interval: float = 2.0):
        self.logger = Logger.get_instance()
        self.check_interval = check_interval
        self._running = False
        self._monitor_thread: Optional[threading.Thread] = None
        self._target_app_paths: List[str] = []
        self._target_hwnd: Optional[int] = None
        self._lock = threading.Lock()

    def start(self, app_paths: Union[str, List[str]]) -> bool:
        """启动监控

        Args:
            app_paths: xiadan.exe 完整路径，支持单个 str 或 List[str]
        """
        with self._lock:
            if self._running:
                self.logger.warning("窗口监控已在运行中")
                return False

            if isinstance(app_paths, str):
                self._target_app_paths = [app_paths]
            else:
                self._target_app_paths = list(app_paths)

            if not self._target_app_paths:
                self.logger.warning("未配置 xiadan.exe 路径，窗口监控未启动")
                return False

            self._running = True

            self._monitor_thread = threading.Thread(
                target=self._monitor_loop,
                daemon=True,
                name="Window-Monitor"
            )
            self._monitor_thread.start()
            self.logger.info(f"窗口监控已启动，目标程序: {self._target_app_paths}")
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
        """根据 exe 路径列表查找窗口句柄（按配置顺序优先）

        不检查 IsWindowVisible — 窗口可能被隐藏到系统托盘。
        优先匹配主窗口标题"网上股票交易系统5.0"，子窗口作为 fallback。
        """
        paths_lower = [p.lower() for p in self._target_app_paths]
        found_windows = {}  # exe_lower -> hwnd

        def callback(hwnd, extra):
            try:
                _, pid = win32process.GetWindowThreadProcessId(hwnd)
                proc = psutil.Process(pid)
                exe = proc.exe().lower()
                if exe in paths_lower:
                    title = win32gui.GetWindowText(hwnd)
                    # 主窗口标题优先覆盖（如"网上股票交易系统5.0"）
                    if title == TRADING_WINDOW_TITLE:
                        found_windows[exe] = hwnd
                    elif exe not in found_windows:
                        found_windows[exe] = hwnd
            except (psutil.NoSuchProcess, psutil.AccessDenied):
                pass
            return True

        if paths_lower:
            win32gui.EnumWindows(callback, None)

        # 按配置顺序返回第一个匹配的窗口句柄
        if found_windows:
            for path in paths_lower:
                if path in found_windows:
                    return found_windows[path]
        return None

    def _restore_window(self, hwnd: int) -> bool:
        """恢复最小化或隐藏的窗口"""
        try:
            if not win32gui.IsWindow(hwnd):
                self.logger.warning("窗口句柄无效，尝试重新查找")
                return False

            # 先确保窗口可见（可能隐藏到系统托盘）
            if not win32gui.IsWindowVisible(hwnd):
                self.logger.info("窗口不可见，正在显示...")
                win32gui.ShowWindow(hwnd, win32con.SW_SHOW)
                time.sleep(0.1)

            # 恢复最小化
            if win32gui.IsIconic(hwnd):
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
        startup_skip = True  # 启动初期跳过日志噪音

        while self._running:
            try:
                hwnd = self._find_target_window()
                if hwnd is None:
                    consecutive_failures += 1
                    if consecutive_failures >= 5:
                        if not startup_skip:
                            self.logger.warning("连续5次未找到目标窗口，请检查 xiadan.exe 是否已启动")
                        consecutive_failures = 0
                        startup_skip = False
                else:
                    consecutive_failures = 0
                    startup_skip = False
                    self._target_hwnd = hwnd
                    if win32gui.IsIconic(hwnd):
                        self.logger.info("检测到目标窗口已最小化，正在恢复...")
                        if not self._restore_window(hwnd):
                            self._target_hwnd = None
            except Exception as e:
                consecutive_failures += 1
                if consecutive_failures >= 3:
                    self.logger.warning(f"窗口监控连续失败 {consecutive_failures} 次: {str(e)}")
                if consecutive_failures >= 10:
                    self.logger.error(
                        f"窗口监控已连续失败 {consecutive_failures} 次，可能 xiadan.exe 未启动"
                    )

            time.sleep(self.check_interval)

        self.logger.info("窗口监控线程已退出")

    def get_status(self) -> dict:
        return {
            "running": self._running,
            "target_apps": self._target_app_paths,
            "target_hwnd": self._target_hwnd,
            "check_interval": self.check_interval
        }
