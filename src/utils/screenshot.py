"""截图工具

用于任务失败/超时时保存 xiadan.exe 当前状态截图
"""
import os
from datetime import datetime
from typing import Optional

import pyautogui
from pywinauto import Desktop

from src.utils.logger import Logger


class ScreenshotUtil:
    """截图工具类"""

    def __init__(self, screenshot_dir: str = "logs/screenshots"):
        self.screenshot_dir = screenshot_dir
        self.logger = Logger.get_instance()
        os.makedirs(self.screenshot_dir, exist_ok=True)

    def capture_trading_window(self, prefix: str = "screenshot") -> Optional[str]:
        """截取交易窗口当前状态

        Args:
            prefix: 文件名前缀（如 timeout_place_order）

        Returns:
            截图文件路径，失败返回 None
        """
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"{prefix}_{timestamp}.png"
        filepath = os.path.join(self.screenshot_dir, filename)

        try:
            # 优先截取交易窗口
            dialogs = Desktop(backend="uia").windows(title="网上股票交易系统5.0")
            if dialogs:
                dialogs[0].capture_as_image().save(filepath)
                self.logger.info(f"截图保存: {filepath}")
                return filepath
        except Exception as e:
            self.logger.warning(f"截取交易窗口失败: {str(e)}，改为全屏截图")

        try:
            # 降级为全屏截图
            pyautogui.screenshot(filepath)
            self.logger.info(f"全屏截图保存: {filepath}")
            return filepath
        except Exception as e:
            self.logger.error(f"全屏截图也失败: {str(e)}")
            return None

    def capture_full_screen(self, prefix: str = "fullscreen") -> Optional[str]:
        """全屏截图"""
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"{prefix}_{timestamp}.png"
        filepath = os.path.join(self.screenshot_dir, filename)

        try:
            pyautogui.screenshot(filepath)
            return filepath
        except Exception as e:
            self.logger.error(f"全屏截图失败: {str(e)}")
            return None
