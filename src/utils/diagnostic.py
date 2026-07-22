"""诊断工具

在系统遇到未知弹窗、卡住、超时等情况时：
1. 截图保存当前窗口状态（全窗口和/或全屏）
2. 提取 UI 控件文本（通过 pywinauto 枚举所有控件文本，比 OCR 更可靠）
3. OCR 识别截图中所有文字（作为补充）
4. 返回结构化诊断信息，帮助定位问题

使用方式:
    diagnostic = DiagnosticUtil()
    info = diagnostic.snapshot("place_order")
    # info = {"screenshot": "path.png", "ocr_text": "...", "ui_text": "...", ...}
    # 日志中会记录 info 供后续排查
"""
import os
from datetime import datetime
from typing import Optional

from src.constants import TRADING_WINDOW_TITLE
from src.utils.logger import Logger
from src.utils.screenshot import ScreenshotUtil


class DiagnosticUtil:
    """诊断工具（截图 + UI 控件文本 + OCR 全文本识别）"""

    def __init__(self, screenshot_dir: str = "logs/screenshots"):
        self.logger = Logger.get_instance()
        self.screenshot_dir = screenshot_dir
        os.makedirs(self.screenshot_dir, exist_ok=True)

    def snapshot(self, prefix: str = "diagnostic", window=None) -> dict:
        """截取当前状态并提取 UI 文本 + OCR 诊断

        流程:
        1. 全窗口截图（优先交易窗口，降级全屏）
        2. 提取 UI 控件文本（pywinauto 枚举，比 OCR 更可靠）
        3. OCR 全文本识别截图（作为补充）
        4. 返回结构化信息 + 日志记录

        Args:
            prefix: 文件名前缀，如 "dialog_unknown", "timeout_place_order"
            window: 可选的窗口对象，避免重复 Desktop().windows() 查找

        Returns:
            {
                "screenshot": "截图文件路径" or None,
                "ui_text": "UI 控件文本（pywinauto 枚举）" or "",
                "ocr_text": "OCR 识别出的全部文字" or "",
                "ocr_failed": bool  # OCR 是否失败
            }
        """
        result = {
            "screenshot": None,
            "ui_text": "",
            "ocr_text": "",
            "ocr_failed": False,
        }

        main_window = window

        # 1. 截图
        try:
            util = ScreenshotUtil(self.screenshot_dir)
            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            filename = f"{prefix}_{timestamp}.png"
            filepath = os.path.join(self.screenshot_dir, filename)

            if main_window is not None:
                main_window.capture_as_image().save(filepath)
            else:
                from pywinauto import Desktop
                dialogs = Desktop(backend="uia").windows(title=TRADING_WINDOW_TITLE)
                if dialogs:
                    dialogs[0].capture_as_image().save(filepath)
                else:
                    import pyautogui
                    pyautogui.screenshot(filepath)
            result["screenshot"] = filepath
            self.logger.info(f"诊断截图保存: {filepath}")
        except Exception as e:
            self.logger.warning(f"诊断截图失败: {e}")

        # 2. 提取 UI 控件文本（比 OCR 更可靠）
        try:
            ui_text_lines = []
            if main_window is None:
                from pywinauto import Desktop
                dialogs = Desktop(backend="uia").windows(title=TRADING_WINDOW_TITLE)
                if dialogs:
                    main_window = dialogs[0]

            if main_window is not None:
                # 枚举所有子孙控件，提取文本
                seen_texts = set()
                for ctrl in main_window.descendants():
                    try:
                        text = ctrl.window_text()
                        if text and text.strip() and text.strip() not in seen_texts:
                            seen_texts.add(text.strip())
                            ui_text_lines.append(text.strip())
                    except Exception:
                        continue
                # 也获取窗口标题
                try:
                    title = main_window.window_text()
                    if title and title.strip():
                        ui_text_lines.insert(0, f"[窗口标题] {title.strip()}")
                except Exception:
                    pass
                # 获取状态栏文本
                try:
                    for ctrl in main_window.descendants():
                        try:
                            if ctrl.element_info.control_type == "StatusBar":
                                status = ctrl.window_text()
                                if status and status.strip():
                                    ui_text_lines.append(f"[状态栏] {status.strip()}")
                        except Exception:
                            continue
                except Exception:
                    pass

            if ui_text_lines:
                result["ui_text"] = "\n".join(ui_text_lines)
                self.logger.info(
                    f"诊断 UI 控件文本 ({len(ui_text_lines)} 项):\n{result['ui_text']}"
                )
            else:
                self.logger.info("诊断 UI 控件文本为空（可能窗口未就绪或无文本控件）")
        except Exception as e:
            self.logger.warning(f"UI 控件文本提取失败: {e}")

        # 3. OCR 全文本识别（截图降级补充）
        if result["screenshot"] and os.path.exists(result["screenshot"]):
            try:
                from src.core.ocr import OcrService
                ocr = OcrService.get_instance()
                text = ocr.recognize_text(result["screenshot"])
                if text:
                    result["ocr_text"] = text
                    self.logger.info(f"诊断 OCR 识别结果 ({len(text)} 字符):\n{text}")
                else:
                    result["ocr_failed"] = True
                    self.logger.warning("诊断 OCR 识别为空（可能图片无文字或模型未加载）")
            except Exception as e:
                result["ocr_failed"] = True
                self.logger.error(f"诊断 OCR 识别异常: {e}")
        else:
            result["ocr_failed"] = True

        return result
