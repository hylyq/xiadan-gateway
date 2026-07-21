"""截图工具

用于任务失败/超时时保存 xiadan.exe 当前状态截图
"""
import os
import time
from datetime import datetime
from typing import Optional

import pyautogui
from pywinauto import Desktop

from src.constants import TRADING_WINDOW_TITLE
from src.utils.logger import Logger

# 截图保留策略
MAX_SCREENSHOTS = 200          # 最多保留截图数量
MAX_AGE_DAYS = 7               # 最多保留天数


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
            dialogs = Desktop(backend="uia").windows(title=TRADING_WINDOW_TITLE)
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

    def cleanup_old_screenshots(self) -> int:
        """清理过期截图（保留最近 MAX_SCREENSHOTS 张且不超过 MAX_AGE_DAYS 天）

        Returns:
            删除的文件数量
        """
        try:
            if not os.path.isdir(self.screenshot_dir):
                return 0

            # 获取所有 png 文件及其修改时间
            files = []
            for f in os.listdir(self.screenshot_dir):
                if f.endswith(".png"):
                    filepath = os.path.join(self.screenshot_dir, f)
                    mtime = os.path.getmtime(filepath)
                    files.append((filepath, mtime))

            if not files:
                return 0

            # 按修改时间排序（最新的在前）
            files.sort(key=lambda x: x[1], reverse=True)

            now = time.time()
            max_age_seconds = MAX_AGE_DAYS * 86400
            deleted = 0

            for i, (filepath, mtime) in enumerate(files):
                # 超过数量限制 或 超过时间限制 → 删除
                if i >= MAX_SCREENSHOTS or (now - mtime) > max_age_seconds:
                    os.remove(filepath)
                    deleted += 1

            if deleted > 0:
                self.logger.info(f"截图清理完成: 删除 {deleted} 张过期截图")
            return deleted

        except Exception as e:
            self.logger.warning(f"截图清理失败: {e}")
            return 0
